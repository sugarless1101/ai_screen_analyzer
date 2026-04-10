# Screen Analyzer 開発ガイド

## プロジェクト概要
メインPCの画面をHDMIキャプチャボード(USB3 Video)経由でMacBookに取り込み、
Claude Code CLIで解析するGUIアプリケーション。

## アーキテクチャ
```
メインPC → HDMI → キャプチャボード(YFFSFDC) → USB3.0 → MacBook
                                                      ↓
                                            main.py (tkinter GUI)
                                              ├─ CaptureCard: OpenCVでフレーム取得
                                              ├─ ClaudeSession: claude -p で解析
                                              └─ GUI: 結果表示
```

## ディレクトリ構成
```
screen-analyzer/
├── CLAUDE.md              # ← このファイル (開発用)
├── main.py                # メインアプリケーション (単一ファイル)
├── workspace/
│   └── CLAUDE.md          # 解析実行時にClaude Code CLIが読む指示書
├── screenshots/           # キャプチャ画像保存先
└── results/
    └── analysis_result.md # CLI解析結果の出力先
```

## 重要な設計制約
- **単一ファイル構成**: main.py にすべて集約。分割しない。
- **ユーザー操作はスペースキーの1入力のみ**: 解析中の追加操作は不可。
- **同一セッション維持**: Claude Code CLIはスクリプト実行中 `--resume SESSION_ID` で同一セッションを継続する。毎回新規セッションにしない。
- **非同期処理**: 解析はthreadingで別スレッド。GUIスレッドをブロックしない。tkinterの更新は必ず `root.after()` 経由。

## 技術スタック
- Python 3.10+
- tkinter (GUI)
- opencv-python (キャプチャボードからのフレーム取得)
- Claude Code CLI (`claude -p --output-format json`)

## 主要クラス
- **CaptureCard**: デバイス検出・接続・フレーム取得・解放
- **ClaudeSession**: CLIの呼び出しとセッションID管理
- **ScreenAnalyzerApp**: tkinter GUI、キーバインド、スレッド管理

## キャプチャの注意点
- デバイス名: `USB3 Video`
- `system_profiler SPUSBDataType` で接続確認後、index 0〜9をプローブ
- フレーム取得時に `grab()` x5 でバッファの古いフレームを捨てる
- 解像度は1920x1080を要求

## Claude Code CLI呼び出し仕様
- 実行コマンド: `claude -p --output-format json [--resume SESSION_ID] "プロンプト"`
- cwd: `workspace/` (ここのCLAUDE.mdが解析指示として読まれる)
- タイムアウト: 120秒
- JSON応答から `session_id` を抽出・保持して次回 `--resume` に使う

## コーディング規約
- 型ヒント使用 (Python 3.10+ 記法: `X | None`)
- docstringは日本語
- エラーハンドリングはユーザーに見える形でGUIに表示