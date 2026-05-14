# OrchisPathReplacer

Orchisランチャーの `.ocs` 設定ファイル内に含まれるパスを一括置換する、**非公式の補助ツール**です。  
本ツールはOrchis本体・作者様・配布元とは無関係です。

## できること

- `.ocs` ファイルを読み込み
- 置換元・置換先パスを指定して一括置換
- `ws:` 文字列項目の置換
- `ItemID=bn:` をWindows Shell APIで再生成（任意）
- 置換前プレビュー
- 更新前バックアップの自動作成

## 動作環境

- Windows（`ItemID=bn:` の解析・再生成はWindows専用）
- Python 3.10 以降を推奨

## 使い方

1. `OrchisPathReplacer.pyw` を起動
2. 対象の `.ocs` ファイルを選択
3. 置換元・置換先を入力（例: `G:\Develop` → `D:\Develop`）
4. 「プレビュー」で変更候補を確認
5. 問題なければ「バックアップして置換実行」を実行

## 重要な仕様（文字コード・BOM維持）

このツールは、読み込んだ `.ocs` ファイルの**文字コードとBOM有無を維持して保存**します。

- UTF-8 with BOM の場合: BOM付きUTF-8として保存
- UTF-16 の場合: UTF-16として保存
- それ以外: 主に `cp932` / `utf-8` を判定して保存

Orchis設定ファイルでは、BOMの有無が挙動に影響するケースがあるため、保存時に不必要なBOM付与を行わない実装になっています。

## 注意事項

- `ItemID=bn:` はWindows ShellのPIDL/ITEMIDLIST情報を含むため、単純な文字列置換は危険です。
- 置換後パスが存在しない場合、Windows APIで `ItemID` を生成できないことがあります。
- 実行前に必ずプレビューを確認してください。

## 実行ファイル化（EXE化）

Python未導入環境向けに、PyInstallerで単一実行ファイル化できます。

```bash
py -m pip install pyinstaller
py -m PyInstaller --noconfirm --onefile --windowed OrchisPathReplacer.pyw
```

生成物は通常 `dist/OrchisPathReplacer.exe` に出力されます。  
配布時は「非公式ツール」であることをREADMEや配布ページに明記してください。

## 免責

本ツールの使用によって生じたいかなる損害についても、作者は責任を負いません。  
必ずバックアップを確認したうえで利用してください。

## License

MIT License
