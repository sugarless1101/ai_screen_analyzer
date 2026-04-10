# Screen Analyzer

メインPCの画面を HDMI キャプチャボード経由で MacBook に取り込み、
Claude Code CLI で解析する tkinter GUI アプリ。

起動時に用途別の指示書を CLI 上で選択 → キャプチャを複数枚キューに積んでまとめて解析 → 結果を Markdown プレビューで表示する、キーボード完結型のツール。

使用例：スクショが使えないが映像信号は出ている時のAIエラー解析など

## 特徴

- **指示書選択**: 起動時にターミナル上で解析用途 (競プロ / 適性検査 / エラー解析 / 汎用) を選択
- **モデル自動切替**: 指示書ごとに使用モデル (`opus` / `sonnet` / `haiku`) を設定
- **マルチキャプチャ**: Space で複数枚ためて Enter で一括解析
- **セッション継続**: Claude Code CLI の `--resume` で同一セッションを維持
- **Markdown プレビュー**: 応答をスタイル付きで表示
- **ページナビゲーション**: 矢印キーでページ単位にジャンプ (スクロール不要)
- **キャンセル可能**: Esc で実行中の解析を中断

## 前提条件

- macOS (AVFoundation 依存)
- Python 3.10+ (macOS 同梱 3.9 の tkinter は macOS 26 でクラッシュするため NG)
- Homebrew
- Claude Code CLI (`claude` コマンドが `PATH` に通っていること)
- HDMI キャプチャボード (USB3 Video 系、OBS で動作するもの)

## セットアップ

```bash
# 1. Tk付き Python 3.12 を導入
brew install python-tk@3.12

# 2. プロジェクトを clone
git clone <このリポジトリ>
cd ai_screen_analyzer

# 3. venv 作成 & 依存インストール
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

初回起動時、macOS がカメラアクセス権限を要求してきます。使用中のターミナルアプリ (Terminal.app / iTerm2 / VS Code 等) を **システム設定 → プライバシーとセキュリティ → カメラ** で許可してください。

## 起動

```bash
.venv/bin/python main.py
```

1. ターミナルに指示書の選択肢が出る → 番号入力で選択 (Enter のみでデフォルト選択)
2. GUI が起動
3. Space で画像を追加 → Enter で解析 → 結果がプレビュー表示

## キー操作

### 解析フロー
| キー | 動作 |
|---|---|
| `Space` | 現在のフレームをキャプチャしてキューに追加 |
| `Enter` | キュー内の全画像をまとめて解析開始 |
| `BackSpace` | キューの末尾 (直近追加分) を 1 枚削除 |
| `Esc` | 実行中の解析プロセスを中断 |

### ページナビゲーション
| キー | 動作 |
|---|---|
| `→` / `↓` / `PageDown` | 次ページ |
| `←` / `↑` / `PageUp` | 前ページ |
| `Home` | 先頭ページ |
| `End` | 末尾ページ |

### その他
| キー | 動作 |
|---|---|
| `Ctrl+R` | デバイス再接続 + セッション・キュー全リセット |
| `Ctrl+Q` / `Cmd+Q` | 終了 |

## ディレクトリ構成

```
ai_screen_analyzer/
├── CLAUDE.md                    # 開発用ガイド (このプロジェクトをClaude Codeで開発する人向け)
├── README.md
├── main.py                      # 単一ファイルアプリ
├── requirements.txt
├── .venv/                       # (git管理外)
├── instructions/                # 起動時に選択する指示書群 (マスター)
│   ├── competitive_programming.md
│   ├── aptitude_test.md
│   ├── error_analysis.md
│   └── general.md
├── workspace/
│   └── CLAUDE.md                # 起動時に選択された指示書の内容で自動生成 (git管理外)
├── screenshots/                 # キャプチャ画像 (git管理外)
└── results/
    └── analysis_result.md       # 最新の解析結果 (git管理外)
```

## 指示書のカスタマイズ

`instructions/*.md` を編集することで解析の挙動をカスタマイズできます。
各ファイルは YAML 風フロントマター + 本文の構成です。

```markdown
---
title: 表示名
model: opus          # opus / sonnet / haiku のいずれか (claude CLI の --model に渡る)
description: 説明文
---

# 指示書本文
...
```

新しい指示書を追加したい場合は `instructions/` に新しい `.md` を置くだけで、次回起動時の選択肢に現れます。

### 同梱指示書

| ファイル | 用途 | モデル |
|---|---|---|
| `competitive_programming.md` | 競技プログラミング練習問題の解答・解説 | `opus` |
| `aptitude_test.md` | SPI/玉手箱の練習補助 (コンテキスト最適化あり) | `haiku` |
| `error_analysis.md` | BSOD・例外・エラーログの分析 | `sonnet` |
| `general.md` | 汎用画面分析・シーン推測・アドバイス | `sonnet` |

## トラブルシューティング

### デバイスが見つからない
- OBS で `USB3 Video` が動作することを確認 (OBS が見えれば Python 側でも見える設計)
- 起動時コンソールの `検出された映像デバイス (N件): [...]` を確認し、一覧に自分のキャプチャボードがなければ USB 接続を点検
- 名前が `USB3 Video` 以外なら `main.py` の `CAPTURE_DEVICE_NAME` を書き換える (部分一致なので一部でOK)

### カメラ権限エラー
- `not authorized to capture video` が出る場合、システム設定 → プライバシーとセキュリティ → カメラ で使用中のターミナルを許可
- 許可後はターミナルを **完全に終了** (Cmd+Q) して再起動

### Python が `Tcl_Panic` でクラッシュ
- macOS 同梱の `python3` (Xcode 付属 3.9) を使っているはず。`brew install python-tk@3.12` で入れた `python3.12` + venv を使うこと

### 解析結果が書き込まれない
- `claude` コマンドが PATH にあるか確認 (`which claude`)
- 初回は認証が必要かもしれないので、一度ターミナルで単独実行して確認

## ライセンス

プライベート利用を想定したサンプル実装。
