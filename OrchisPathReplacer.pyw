# -*- coding: utf-8 -*-
"""
OrchisPathReplacer.pyw

Orchis ランチャー設定ファイル（.ocs）内のパス一括置換ツール。

目的:
- フォルダ構成変更時に、Orchis設定ファイル内のパスの一部をまとめて置換する。
- ws: 形式の文字列はデコードして通常文字列として置換し、再度 ws: に戻す。
- ItemID=bn: は Windows Shell の PIDL/ITEMIDLIST として扱い、
  既存 ItemID から実パスを取り出し、置換後のパスから Windows API で ItemID を再生成する。

注意:
- ItemID の再生成は Windows 上でのみ動作します。
- 置換後のパスが存在しない場合、Windows API が PIDL を生成できないことがあります。
- 更新前に .bak_YYYYMMDD_HHMMSS のバックアップを作成します。
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import sys
import traceback
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "Orchis .ocs パス一括置換ツール"


# ----------------------------------------------------------------------
# OCS text helpers
# ----------------------------------------------------------------------

def read_text_auto(path: Path) -> tuple[str, str]:
    """Read text while preserving BOM/no-BOM behavior.

    Orchis appears to check the first line strictly.  If a no-BOM file is
    rewritten as UTF-8 with BOM, the leading BOM can make Orchis fail with
    a misleading "different version" style error.  Therefore we only use
    utf-8-sig when the original file actually has the UTF-8 BOM.
    """
    data = path.read_bytes()

    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16"), "utf-16"

    # .ocs sample files are plain ANSI/ASCII-like text.  Prefer cp932 on
    # Japanese Windows to avoid adding a UTF-8 BOM on save.
    for enc in ("cp932", "utf-8"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            pass
    return data.decode("cp932", errors="replace"), "cp932-replace"


def write_text_with_encoding(path: Path, text: str, encoding: str) -> None:
    enc = encoding.replace("-replace", "")
    data = text.encode(enc, errors="replace")
    # Safety: never add UTF-8 BOM unless the original file had it.
    path.write_bytes(data)


def decode_ws_payload(payload: str) -> str | None:
    """Decode ws: comma-separated Unicode code points."""
    payload = payload.strip()
    if not payload:
        return ""
    try:
        nums = [int(x.strip()) for x in payload.split(",") if x.strip()]
        return "".join(chr(n) for n in nums)
    except Exception:
        return None


def encode_ws_text(s: str) -> str:
    # Encode plain text back into ws: comma-separated code points.
    return ",".join(str(ord(ch)) for ch in s)


def decode_bn_payload(payload: str) -> bytes | None:
    try:
        vals = [int(x.strip()) for x in payload.split(",") if x.strip()]
        if any(v < 0 or v > 255 for v in vals):
            return None
        return bytes(vals)
    except Exception:
        return None


def encode_bn_bytes(data: bytes) -> str:
    # Serialize binary bytes for bn: decimal-list representation.
    return ",".join(str(b) for b in data)


# ----------------------------------------------------------------------
# Windows Shell PIDL helpers
# ----------------------------------------------------------------------

class ShellPIDL:
    """Minimal PIDL helpers using Windows Shell API."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("ItemID の解析・再生成は Windows 上でのみ利用できます。")

        self.shell32 = ctypes.windll.shell32
        self.ole32 = ctypes.windll.ole32

        self.shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
        self.shell32.SHGetPathFromIDListW.restype = wintypes.BOOL

        # PIDLIST_ABSOLUTE ILCreateFromPathW(PCWSTR pszPath)
        self.shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
        self.shell32.ILCreateFromPathW.restype = ctypes.c_void_p

        self.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        self.ole32.CoTaskMemFree.restype = None

    @staticmethod
    def _ensure_terminator(data: bytes) -> bytes:
        # A PIDL ends with SHITEMID cb == 0, i.e. two zero bytes.
        if not data.endswith(b"\x00\x00"):
            return data + b"\x00\x00"
        return data

    def path_from_pidl_bytes(self, data: bytes) -> str | None:
        buf_data = self._ensure_terminator(data)
        buf = ctypes.create_string_buffer(buf_data, len(buf_data))
        out = ctypes.create_unicode_buffer(32768)
        ok = self.shell32.SHGetPathFromIDListW(ctypes.cast(buf, ctypes.c_void_p), out)
        if not ok:
            return None
        return out.value or None

    def bytes_from_path(self, path: str) -> bytes | None:
        pidl = self.shell32.ILCreateFromPathW(path)
        if not pidl:
            return None
        try:
            chunks = []
            offset = 0
            # PIDL = sequence of SHITEMID: USHORT cb; BYTE abID[cb-2]; terminated by cb=0.
            # Read until terminator. Hard limit prevents infinite loop if something is wrong.
            for _ in range(4096):
                cb = ctypes.c_ushort.from_address(pidl + offset).value
                chunks.append(ctypes.string_at(pidl + offset, 2 if cb == 0 else cb))
                if cb == 0:
                    return b"".join(chunks)
                offset += cb
                if offset > 65535 * 64:
                    break
            return None
        finally:
            self.ole32.CoTaskMemFree(pidl)


# ----------------------------------------------------------------------
# Replacement engine
# ----------------------------------------------------------------------

@dataclass
class Change:
    line_no: int
    kind: str
    before: str
    after: str
    detail: str = ""


class OrchisReplacer:
    def __init__(self, text: str) -> None:
        self.text = text
        self.lines = text.splitlines(keepends=True)

    def preview_or_apply(
        self,
        old: str,
        new: str,
        replace_ws: bool = True,
        replace_itemid: bool = True,
        apply: bool = False,
    ) -> tuple[str, list[Change], list[str]]:
        changes: list[Change] = []
        warnings: list[str] = []
        new_lines = list(self.lines)

        shell: ShellPIDL | None = None
        if replace_itemid:
            try:
                shell = ShellPIDL()
            except Exception as e:
                warnings.append(str(e))
                shell = None

        for idx, line in enumerate(self.lines):
            line_body = line.rstrip("\r\n")
            eol = line[len(line_body):]

            # Replace ws: values after '='.
            if replace_ws and "=ws:" in line_body:
                key, payload = line_body.split("=ws:", 1)
                decoded = decode_ws_payload(payload)
                if decoded is not None and old in decoded:
                    replaced = decoded.replace(old, new)
                    new_line_body = f"{key}=ws:{encode_ws_text(replaced)}"
                    new_lines[idx] = new_line_body + eol
                    changes.append(Change(idx + 1, "ws", decoded, replaced, key))
                continue

            # Replace ItemID=bn: by extracting path and regenerating PIDL.
            if replace_itemid and shell is not None and line_body.startswith("ItemID=bn:"):
                payload = line_body.split("ItemID=bn:", 1)[1]
                data = decode_bn_payload(payload)
                if data is None:
                    warnings.append(f"{idx + 1}行目: ItemID の bn: 解析に失敗しました。")
                    continue
                path = shell.path_from_pidl_bytes(data)
                if not path:
                    continue
                if old not in path:
                    continue
                replaced_path = path.replace(old, new)
                new_data = shell.bytes_from_path(replaced_path)
                if new_data is None:
                    warnings.append(
                        f"{idx + 1}行目: 置換後パスの ItemID を生成できませんでした: {replaced_path}"
                    )
                    continue
                new_lines[idx] = "ItemID=bn:" + encode_bn_bytes(new_data) + eol
                changes.append(Change(idx + 1, "ItemID", path, replaced_path, "PIDL再生成"))

        result_text = "".join(new_lines) if apply else self.text
        return result_text, changes, warnings


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("980x680")
        self.minsize(850, 560)

        self.file_path: Path | None = None
        self.encoding: str | None = None
        self.text: str | None = None

        self.var_file = tk.StringVar()
        self.var_old = tk.StringVar()
        self.var_new = tk.StringVar()
        self.var_ws = tk.BooleanVar(value=True)
        self.var_itemid = tk.BooleanVar(value=True)

        self._build_ui()

    def _build_ui(self) -> None:
        # Build the main window controls (file picker, replace options, log).
        pad = {"padx": 8, "pady": 6}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="対象 .ocs:").pack(side="left")
        ent = ttk.Entry(top, textvariable=self.var_file)
        ent.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="選択...", command=self.select_file).pack(side="left")

        frm = ttk.LabelFrame(self, text="置換条件")
        frm.pack(fill="x", **pad)

        row1 = ttk.Frame(frm)
        row1.pack(fill="x", padx=8, pady=4)
        ttk.Label(row1, text="置換元:", width=10).pack(side="left")
        ttk.Entry(row1, textvariable=self.var_old).pack(side="left", fill="x", expand=True)

        row2 = ttk.Frame(frm)
        row2.pack(fill="x", padx=8, pady=4)
        ttk.Label(row2, text="置換先:", width=10).pack(side="left")
        ttk.Entry(row2, textvariable=self.var_new).pack(side="left", fill="x", expand=True)

        row3 = ttk.Frame(frm)
        row3.pack(fill="x", padx=8, pady=4)
        ttk.Checkbutton(row3, text="ws: 文字列項目も置換する", variable=self.var_ws).pack(side="left", padx=(0, 20))
        ttk.Checkbutton(row3, text="ItemID=bn: をパスとして解析し、Windows Shellで再生成する", variable=self.var_itemid).pack(side="left")

        note = (
            "推奨: まず［プレビュー］で対象を確認してください。"
            "ItemIDは単純な文字列置換ではなく、Windows APIでPIDLを作り直します。"
            "置換後のパスが存在しない場合は更新できないことがあります。"
        )
        ttk.Label(frm, text=note, foreground="#555").pack(anchor="w", padx=8, pady=(2, 8))

        btns = ttk.Frame(self)
        btns.pack(fill="x", **pad)
        ttk.Button(btns, text="プレビュー", command=self.preview).pack(side="left")
        ttk.Button(btns, text="バックアップして置換実行", command=self.apply_replace).pack(side="left", padx=8)
        ttk.Button(btns, text="ログをクリア", command=lambda: self.log.delete("1.0", "end")).pack(side="left")

        self.log = tk.Text(self, wrap="none", font=("Consolas", 10))
        self.log.pack(fill="both", expand=True, padx=8, pady=6)

        yscroll = ttk.Scrollbar(self.log, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=yscroll.set)
        yscroll.pack(side="right", fill="y")

        self._write_log(
            "使い方:\n"
            "1) .ocs ファイルを選択\n"
            "2) 置換元・置換先を入力  例: G:\\Develop → D:\\Develop\n"
            "3) プレビューで変更候補を確認\n"
            "4) 問題なければ置換実行\n\n"
        )

    def _write_log(self, s: str) -> None:
        self.log.insert("end", s)
        self.log.see("end")

    def select_file(self) -> None:
        filename = filedialog.askopenfilename(
            title="Orchis設定ファイルを選択",
            filetypes=[("Orchis設定ファイル", "*.ocs"), ("すべてのファイル", "*.*")],
        )
        if not filename:
            return
        self.file_path = Path(filename)
        self.var_file.set(str(self.file_path))
        try:
            self.text, self.encoding = read_text_auto(self.file_path)
            self._write_log(f"読み込み: {self.file_path}\n文字コード推定: {self.encoding}\n\n")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"読み込みに失敗しました。\n{e}")

    def _validate(self) -> bool:
        # Validate required inputs before preview/apply.
        if not self.file_path or not self.text or not self.encoding:
            messagebox.showwarning(APP_TITLE, ".ocs ファイルを選択してください。")
            return False
        if not self.var_old.get():
            messagebox.showwarning(APP_TITLE, "置換元を入力してください。")
            return False
        if self.var_old.get() == self.var_new.get():
            messagebox.showwarning(APP_TITLE, "置換元と置換先が同じです。")
            return False
        if not (self.var_ws.get() or self.var_itemid.get()):
            messagebox.showwarning(APP_TITLE, "置換対象を1つ以上選択してください。")
            return False
        return True

    def preview(self) -> None:
        if not self._validate():
            return
        try:
            rep = OrchisReplacer(self.text or "")
            _, changes, warnings = rep.preview_or_apply(
                self.var_old.get(),
                self.var_new.get(),
                replace_ws=self.var_ws.get(),
                replace_itemid=self.var_itemid.get(),
                apply=False,
            )
            self._show_result("プレビュー", changes, warnings)
        except Exception:
            self._show_exception()

    def apply_replace(self) -> None:
        if not self._validate():
            return
        try:
            rep = OrchisReplacer(self.text or "")
            new_text, changes, warnings = rep.preview_or_apply(
                self.var_old.get(),
                self.var_new.get(),
                replace_ws=self.var_ws.get(),
                replace_itemid=self.var_itemid.get(),
                apply=True,
            )
            self._show_result("置換実行前確認", changes, warnings)
            if not changes:
                messagebox.showinfo(APP_TITLE, "置換対象はありませんでした。")
                return
            msg = f"{len(changes)}件を更新します。よろしいですか？\n\n更新前にバックアップを作成します。"
            if not messagebox.askyesno(APP_TITLE, msg):
                return
            assert self.file_path is not None
            backup = self.file_path.with_suffix(self.file_path.suffix + ".bak_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
            shutil.copy2(self.file_path, backup)
            write_text_with_encoding(self.file_path, new_text, self.encoding or "cp932")
            self.text = new_text
            self._write_log(f"\n更新完了: {self.file_path}\nバックアップ: {backup}\n\n")
            messagebox.showinfo(APP_TITLE, f"更新しました。\n\nバックアップ:\n{backup}")
        except Exception:
            self._show_exception()

    def _show_result(self, title: str, changes: list[Change], warnings: list[str]) -> None:
        self._write_log(f"===== {title} =====\n")
        self._write_log(f"置換対象: {len(changes)}件\n")
        for ch in changes:
            self._write_log(f"[{ch.kind}] line {ch.line_no} {ch.detail}\n")
            self._write_log(f"  BEFORE: {ch.before}\n")
            self._write_log(f"  AFTER : {ch.after}\n")
        if warnings:
            self._write_log("\n警告:\n")
            for w in warnings:
                self._write_log(f"  - {w}\n")
        self._write_log("\n")

    def _show_exception(self) -> None:
        tb = traceback.format_exc()
        self._write_log(tb + "\n")
        messagebox.showerror(APP_TITLE, "処理中にエラーが発生しました。ログを確認してください。")


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
