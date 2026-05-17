# 自動サイトマップ生成ツール

指定したURLからWebサイトを巡回（クロール）し、視覚的なツリー構造のMarkdownファイルと、URL一覧のCSVファイルを自動生成するツールセットです。

## 特徴

- **並列クロール**: `Playwright` と `asyncio` を活用し、複数のページを同時に高速取得します。
- **ツリー構造の可視化**: ディレクトリ階層を `├──` や `└──` を用いたテキスト形式で出力し、サイト構成を直感的に把握できます。
- **URL抽出機能**: 生成されたMarkdownからURLのみを抽出し、CSV形式で保存できます。
- **柔軟なカスタマイズ**: `config.json` を編集することで、探索の深さや並列数、クロール対象のフィルタリングを自由に変更可能です。

## 必要条件

- [Playwright](https://playwright.dev/python/)が動作するOSとPython(2026/5/17時点)
- Python 3.8以上
- Windows 11+, Windows Server 2019+ or Windows Subsystem for Linux (WSL).
- macOS 14 Sonoma, or later.
- Debian 12, Debian 13, Ubuntu 22.04, Ubuntu 24.04, on x86-64 and arm64 architecture.


## セットアップ

本プロジェクトでは `uv` を使用した環境構築を推奨します。

1. **リポジトリの準備**
   ```bash
   git clone <repository-url>
   cd sitemap
   ```

2. **仮想環境の作成と依存ライブラリのインストール**
   ```bash
   uv venv
   # Windowsの場合
   .venv\Scripts\activate
   # macOS/Linuxの場合
   source .venv/bin/activate

   uv pip install playwright beautifulsoup4
   uv run playwright install chromium
   ```

## 使い方

### 1. 設定ファイルの作成
`default.config.json` をコピーして `config.json` を作成し、対象サイトの情報を入力してください。

```json
[
    {
        "target_urls": ["https://example.com/"],
        "base_urls": ["https://example.com/"],
        "max_depth": 5,
        "max_concurrent": 5
    }
]
```
- `target_urls`: クロールを開始する起点のURL（複数指定可）。
- `base_urls`: この文字列で始まるURLのみを収集対象とします（外部サイトへの遷移を防止します）。
- `max_depth`: 探索する階層の深さ。
- `max_concurrent`: 同時並列リクエスト数。

### 2. クロールの実行 (`sitemap.py`)
Webサイトをクロールし、`save/` ディレクトリ内にMarkdown形式のサイトマップを出力します。

```bash
python sitemap.py
```

### 3. CSVへの書き出し (`extract_sitemap_urls.py`)
生成されたMarkdownファイルを解析し、URLの一覧をCSVファイルとして保存します。

```bash
python extract_sitemap_urls.py
```

## 出力ファイル

実行完了後、`save/` ディレクトリに以下のファイルが生成されます。

- `sitemap_domain_name.md`: ツリー構造のサイトマップ。
- `sitemap_domain_name.csv`: 抽出されたURL一覧（重複排除済み）。

## プロジェクト構成

```
.
├── sitemap.py                # クローラー本体
├── extract_sitemap_urls.py    # URL抽出・CSV変換スクリプト
├── config.json               # 実行設定ファイル（ユーザー作成）
├── default.config.json       # 設定ファイルのサンプル
├── save/                     # 生成されたファイルの保存先
└── README.md                 # 本ドキュメント
```

## 注意事項

- 本ツールは短時間に多数のリクエストを送信する可能性があります。`max_concurrent` の値を大きくしすぎないよう、対象サーバーの負荷に配慮して実行してください。
- JavaScriptによる動的なコンテンツ描画に対応するため、ブラウザ（Chromium）をバックグラウンドで起動します。
