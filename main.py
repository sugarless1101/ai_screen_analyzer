"""
Screen Analyzer - メインPCの画面をキャプチャしClaude Code CLIで解析するGUIアプリ

構成:
  スペースキー → キャプチャボード(USB3 Video)から画像取得 → Claude Code CLI解析 → 結果表示

キャプチャ: OpenCVでUSB3 Videoデバイスを直接取得
解析: Claude Code CLI (同一セッション維持)
GUI: tkinter
"""

from __future__ import annotations

import tkinter as tk
import math
import subprocess
import threading
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    import cv2
except ImportError:
    print("opencv-python が必要です: pip install opencv-python")
    raise SystemExit(1)


def _ensure_camera_permission() -> bool:
    """macOSのカメラ(TCC)権限を明示的に要求する。

    opencv-pythonの prebuilt wheel は初回アクセス時にTCCダイアログを
    正しく発火できず、無言で 'not authorized' になる既知問題があるため、
    AVFoundation経由で先に権限リクエストを投げてダイアログを出す。
    """
    try:
        import AVFoundation  # type: ignore
        from Foundation import NSRunLoop, NSDate  # type: ignore
    except ImportError:
        return True  # PyObjC未導入環境ではスキップ(非macOSなど)

    media_type = AVFoundation.AVMediaTypeVideo
    status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
        media_type
    )
    # 0=NotDetermined, 1=Restricted, 2=Denied, 3=Authorized
    if status == 3:
        return True
    if status in (1, 2):
        print(
            "カメラへのアクセスが拒否されています。\n"
            "システム設定 > プライバシーとセキュリティ > カメラ で\n"
            "使用中のターミナルアプリを許可してください。"
        )
        return False

    # status == 0 (未決定): ダイアログを出して同期的に待つ
    granted = {"v": None}

    def _handler(ok):
        granted["v"] = bool(ok)

    AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
        media_type, _handler
    )

    # completionHandlerはメインRunLoopで呼ばれるので回す
    loop = NSRunLoop.currentRunLoop()
    deadline = time.time() + 30
    while granted["v"] is None and time.time() < deadline:
        loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))

    if not granted["v"]:
        print("カメラ権限が付与されませんでした")
        return False
    return True

# ============================================================
# 設定
# ============================================================
BASE_DIR = Path(__file__).parent.resolve()
SCREENSHOT_DIR = BASE_DIR / "screenshots"
RESULTS_DIR = BASE_DIR / "results"
RESULT_FILE = RESULTS_DIR / "analysis_result.md"
WORKSPACE_DIR = BASE_DIR / "workspace"  # 解析用CLAUDE.mdの配置先
INSTRUCTIONS_DIR = BASE_DIR / "instructions"  # 起動時に選択する指示書群

CAPTURE_DEVICE_NAME = "USB3 Video"

SCREENSHOT_DIR.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)
WORKSPACE_DIR.mkdir(exist_ok=True)
INSTRUCTIONS_DIR.mkdir(exist_ok=True)


# ============================================================
# 指示書ローダー
# ============================================================
@dataclass
class Instruction:
    name: str          # ファイル名(拡張子なし)
    title: str         # フロントマターの title
    description: str   # フロントマターの description
    model: str | None  # フロントマターの model (claude CLI --model 引数用)
    content: str       # フロントマター除去後の本文


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """簡易フロントマターパーサ (key: value の行を解釈)"""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    block = text[4:end]
    body = text[end + 5:]
    meta: dict[str, str] = {}
    for line in block.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, body


def load_instructions() -> list[Instruction]:
    """instructions/ 配下の *.md を読み込んで Instruction のリストを返す"""
    items: list[Instruction] = []
    for path in sorted(INSTRUCTIONS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(text)
        items.append(Instruction(
            name=path.stem,
            title=meta.get("title", path.stem),
            description=meta.get("description", ""),
            model=meta.get("model") or None,
            content=body.lstrip(),
        ))
    return items


def select_instruction_cli(instructions: list[Instruction]) -> Instruction | None:
    """ターミナル上で指示書を選択させる (番号入力)"""
    print()
    print("=" * 60)
    print("  Screen Analyzer — 指示書を選択してください")
    print("=" * 60)
    for i, ins in enumerate(instructions, start=1):
        model_text = ins.model or "default"
        print(f"  [{i}] {ins.title}   ({model_text})")
        if ins.description:
            print(f"      {ins.description}")
    print("=" * 60)
    default_idx = 1
    while True:
        try:
            raw = input(
                f"番号を入力 [1-{len(instructions)}] "
                f"(Enterで{default_idx}, qで終了): "
            ).strip()
        except EOFError:
            return None
        if raw.lower() in ("q", "quit", "exit"):
            return None
        if raw == "":
            return instructions[default_idx - 1]
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(instructions):
                return instructions[n - 1]
        print(f"  ! 1〜{len(instructions)} の番号を入力してください")


# ============================================================
# キャプチャボード
# ============================================================
class CaptureCard:
    """USB3 Videoキャプチャボードの管理"""

    def __init__(self, device_name: str = CAPTURE_DEVICE_NAME):
        self.device_name = device_name
        self.cap: cv2.VideoCapture | None = None
        self.device_index: int | None = None

    def open(self) -> bool:
        """デバイスを検出して開く"""
        self.device_index = self._find_device_index()
        if self.device_index is None:
            print(f"デバイス '{self.device_name}' が見つかりません")
            return False

        self.cap = cv2.VideoCapture(self.device_index)
        if not self.cap.isOpened():
            print(f"デバイス index={self.device_index} を開けません")
            return False

        # 最大解像度を要求
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)

        w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"キャプチャデバイス開始: index={self.device_index} ({w}x{h})")
        return True

    def grab_frame(self, save_path: Path) -> Path | None:
        """1フレームを取得して保存"""
        if self.cap is None or not self.cap.isOpened():
            return None

        # バッファに溜まった古いフレームを捨てて最新を取得
        for _ in range(5):
            self.cap.grab()

        ret, frame = self.cap.read()
        if not ret or frame is None:
            print("フレーム取得失敗")
            return None

        cv2.imwrite(str(save_path), frame)
        return save_path

    def close(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def is_open(self) -> bool:
        return self.cap is not None and self.cap.isOpened()

    def _find_device_index(self) -> int | None:
        """AVFoundationでビデオデバイスを列挙し名前一致でインデックスを特定"""
        try:
            import AVFoundation  # type: ignore
        except ImportError:
            print("pyobjc-framework-AVFoundation 未導入のためデバイス列挙不可")
            return None

        devices = AVFoundation.AVCaptureDevice.devicesWithMediaType_(
            AVFoundation.AVMediaTypeVideo
        )
        names = [str(d.localizedName()) for d in devices]
        print(f"検出された映像デバイス ({len(names)}件): {names}")

        if not names:
            print("映像デバイスが1件も検出できません (権限/接続を確認)")
            return None

        # 部分一致(大文字小文字無視)
        target = self.device_name.lower()
        for i, name in enumerate(names):
            if target in name.lower():
                print(f"マッチ: index={i}, name='{name}'")
                return i

        print(f"'{self.device_name}' に一致するデバイスなし")
        return None


# ============================================================
# Claude Code CLI セッション管理
# ============================================================
class ClaudeSession:
    """Claude Code CLIとの同一セッションを管理"""

    def __init__(self, working_dir: Path, model: str | None = None):
        self.working_dir = working_dir
        self.model = model
        self.session_id: str | None = None
        self._current_proc: subprocess.Popen | None = None
        self._cancelled = False

    def analyze(
        self,
        image_paths: list[Path],
        output_path: Path,
        extra_dirs: list[Path],
        timeout: int = 300,
    ) -> str:
        """複数の画像パスをClaude Code CLIに渡して解析結果を返す"""
        self._cancelled = False

        paths_listing = "\n".join(f"- {p}" for p in image_paths)
        multi_note = (
            "複数枚のキャプチャがある場合は、全てを通して一つの対象として解析してください。\n"
            if len(image_paths) > 1 else ""
        )
        prompt = (
            f"以下の画像を解析してください。\n"
            f"画像パス(絶対パス):\n{paths_listing}\n\n"
            f"{multi_note}"
            f"Readツールで画像を読み込み、CLAUDE.mdの解析ルールに従って解析してください。\n"
            f"解析結果は次の絶対パスにWriteツールで書き込んでください: {output_path}\n"
            f"CLAUDE.md内の相対パスは無視し、必ず上記の絶対パスを使ってください。"
        )

        # NOTE: --add-dir は可変長引数なので、後続に別フラグを置いて終端する必要がある
        cmd = ["claude", "-p", "--output-format", "json"]
        for d in extra_dirs:
            cmd.extend(["--add-dir", str(d)])
        cmd.extend(["--permission-mode", "acceptEdits"])

        if self.model:
            cmd.extend(["--model", self.model])

        if self.session_id:
            cmd.extend(["--resume", self.session_id])

        cmd.append(prompt)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self.working_dir),
            )
            self._current_proc = proc
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                return f"[タイムアウト] Claude Code CLIが{timeout}秒以内に応答しませんでした"
            finally:
                self._current_proc = None

            if self._cancelled:
                return "[キャンセル] 解析がユーザーによって中断されました"

            if proc.returncode != 0:
                error_msg = (stderr or "").strip() or "不明なエラー"
                return f"[CLIエラー] returncode={proc.returncode}\n{error_msg}"

            return self._parse_response(stdout)

        except FileNotFoundError:
            return "[エラー] claudeコマンドが見つかりません。Claude Codeがインストールされているか確認してください"
        except Exception as e:
            return f"[例外] {type(e).__name__}: {e}"

    def cancel(self) -> bool:
        """実行中の解析プロセスを停止する"""
        proc = self._current_proc
        if proc is None:
            return False
        self._cancelled = True
        try:
            proc.terminate()
        except Exception:
            pass
        return True

    def _parse_response(self, stdout: str) -> str:
        """CLI出力をパースしセッションIDを保持"""
        try:
            data = json.loads(stdout)
            if "session_id" in data:
                self.session_id = data["session_id"]

            result_text = data.get("result", "")
            if not result_text:
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        result_text += block.get("text", "")

            return result_text if result_text else stdout

        except json.JSONDecodeError:
            return stdout

    def reset(self):
        self.session_id = None


# ============================================================
# GUI
# ============================================================
class ScreenAnalyzerApp:
    def __init__(self, instruction: Instruction):
        self.selected_instruction = instruction
        # 選択された指示書の本文を workspace/CLAUDE.md に書き込み
        (WORKSPACE_DIR / "CLAUDE.md").write_text(instruction.content, encoding="utf-8")

        self.root = tk.Tk()
        self.root.title(f"Screen Analyzer — {instruction.title}")
        self.root.geometry("900x650")
        self.root.configure(bg="#1e1e1e")

        self.capture = CaptureCard()
        self.claude = ClaudeSession(WORKSPACE_DIR, model=instruction.model)
        self.is_analyzing = False
        self.analysis_count = 0
        self.pending_screenshots: list[Path] = []

        self._build_ui()
        self._bind_keys()
        self._init_capture()

    def _init_capture(self):
        """起動時にカメラ権限を確認しキャプチャデバイスを開く"""
        if not _ensure_camera_permission():
            self._set_status("⚠ カメラ権限なし", "#f44747")
            self._append_result(
                "macOSのカメラ権限が取得できません。\n"
                "システム設定 > プライバシーとセキュリティ > カメラ で\n"
                "このターミナルアプリを許可してから再起動してください。\n\n"
            )
            return
        if self.capture.open():
            self._set_status("● 待機中 — Spaceで追加 / Enterで解析", "#4ec9b0")
            self._append_result(
                "キャプチャデバイス接続完了\n\n"
                f"指示書: **{self.selected_instruction.title}** "
                f"(model: `{self.selected_instruction.model or 'default'}`)\n\n"
            )
        else:
            self._set_status("⚠ デバイス未接続", "#f44747")
            self._append_result(
                f"キャプチャデバイス '{CAPTURE_DEVICE_NAME}' に接続できません。\n"
                "USBケーブルとHDMI接続を確認してください。\n"
                "Ctrl+R でリトライできます。\n\n"
            )

    def _build_ui(self):
        header = tk.Frame(self.root, bg="#1e1e1e")
        header.pack(fill=tk.X, padx=16, pady=(16, 8))

        self.status_label = tk.Label(
            header, text="起動中...",
            font=("Menlo", 13), fg="#dcdcaa", bg="#1e1e1e", anchor="w",
        )
        self.status_label.pack(side=tk.LEFT)

        self.count_label = tk.Label(
            header, text="解析: 0回",
            font=("Menlo", 11), fg="#888888", bg="#1e1e1e", anchor="e",
        )
        self.count_label.pack(side=tk.RIGHT)

        info_frame = tk.Frame(self.root, bg="#1e1e1e")
        info_frame.pack(fill=tk.X, padx=16, pady=(0, 4))

        self.device_label = tk.Label(
            info_frame, text=f"デバイス: {CAPTURE_DEVICE_NAME}",
            font=("Menlo", 10), fg="#666666", bg="#1e1e1e", anchor="w",
        )
        self.device_label.pack(side=tk.LEFT)

        self.session_label = tk.Label(
            info_frame, text="セッション: 未開始",
            font=("Menlo", 10), fg="#666666", bg="#1e1e1e", anchor="e",
        )
        self.session_label.pack(side=tk.RIGHT)

        pending_frame = tk.Frame(self.root, bg="#1e1e1e")
        pending_frame.pack(fill=tk.X, padx=16, pady=(0, 8))
        self.pending_label = tk.Label(
            pending_frame, text="キュー: 0枚",
            font=("Menlo", 11, "bold"), fg="#dcdcaa", bg="#1e1e1e", anchor="w",
        )
        self.pending_label.pack(side=tk.LEFT)

        self.page_label = tk.Label(
            pending_frame, text="📄 1/1",
            font=("Menlo", 11, "bold"), fg="#569cd6", bg="#1e1e1e", anchor="e",
        )
        self.page_label.pack(side=tk.RIGHT)

        # スクロールバーなしの Text。ページ単位のナビゲーションに特化
        self.result_area = tk.Text(
            self.root,
            font=("Menlo", 12), bg="#252526", fg="#d4d4d4",
            insertbackground="#d4d4d4", selectbackground="#264f78",
            wrap=tk.WORD, borderwidth=0, state=tk.DISABLED,
            spacing1=2, spacing3=2, cursor="arrow",
        )
        self.result_area.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 8))
        self._configure_markdown_tags()

        footer = tk.Frame(self.root, bg="#1e1e1e")
        footer.pack(fill=tk.X, padx=16, pady=(0, 16))

        tk.Label(
            footer,
            text=(
                "[Space]追加  [Enter]解析  [BS]削除  [Esc]中止  "
                "[←→↑↓]ページ  [Home/End]先頭/末尾  [Ctrl+R]リセット  [Ctrl+Q]終了"
            ),
            font=("Menlo", 9), fg="#555555", bg="#1e1e1e",
        ).pack(side=tk.LEFT)

    def _bind_keys(self):
        self.root.bind("<space>", self._on_capture_add)
        self.root.bind("<Return>", self._on_enter_analyze)
        self.root.bind("<KP_Enter>", self._on_enter_analyze)
        self.root.bind("<BackSpace>", self._on_backspace_remove)
        self.root.bind("<Escape>", self._on_escape_cancel)
        self.root.bind("<Control-r>", self._on_reset)
        self.root.bind("<Control-q>", self._on_quit)
        self.root.bind("<Command-q>", self._on_quit)
        # ページ単位ナビゲーション
        self.root.bind("<Right>", self._on_page_next)
        self.root.bind("<Down>", self._on_page_next)
        self.root.bind("<Next>", self._on_page_next)   # Page Down
        self.root.bind("<Left>", self._on_page_prev)
        self.root.bind("<Up>", self._on_page_prev)
        self.root.bind("<Prior>", self._on_page_prev)  # Page Up
        self.root.bind("<Home>", self._on_page_home)
        self.root.bind("<End>", self._on_page_end)
        self.root.protocol("WM_DELETE_WINDOW", self._on_quit)

    def _on_page_next(self, event=None):
        self.result_area.yview_scroll(1, "pages")
        self._update_page_indicator()
        return "break"

    def _on_page_prev(self, event=None):
        self.result_area.yview_scroll(-1, "pages")
        self._update_page_indicator()
        return "break"

    def _on_page_home(self, event=None):
        self.result_area.yview_moveto(0.0)
        self._update_page_indicator()
        return "break"

    def _on_page_end(self, event=None):
        self.result_area.yview_moveto(1.0)
        self._update_page_indicator()
        return "break"

    def _update_page_indicator(self):
        """yview() の fraction から現在ページ/総ページを概算"""
        try:
            top, bottom = self.result_area.yview()
        except tk.TclError:
            return
        span = bottom - top
        if span >= 1.0 - 1e-3:
            current = total = 1
        else:
            total = max(1, int(math.ceil(1.0 / span)))
            current = max(1, min(total, int(math.floor(top / span + 1e-6)) + 1))
        self.page_label.config(text=f"📄 {current}/{total}")

    def _on_capture_add(self, event):
        """Space: 現在のフレームをキャプチャしてキューに追加"""
        if self.is_analyzing:
            return
        if not self.capture.is_open():
            self._append_result("デバイス未接続。Ctrl+R で再接続してください。\n")
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        save_path = SCREENSHOT_DIR / f"capture_{timestamp}.png"
        image_path = self.capture.grab_frame(save_path)
        if image_path is None:
            self._append_result(f"[{self._timestamp()}] フレーム取得失敗\n")
            return
        self.pending_screenshots.append(image_path)
        self._update_pending_label()
        self._append_result(
            f"[{self._timestamp()}] ＋追加: {image_path.name} "
            f"(キュー {len(self.pending_screenshots)}枚)\n"
        )

    def _on_enter_analyze(self, event):
        """Enter: キュー内の画像をまとめて解析"""
        if self.is_analyzing:
            return
        if not self.pending_screenshots:
            self._append_result("キューが空です。Space で画像を追加してから Enter。\n")
            return
        self.is_analyzing = True
        self._set_status("解析中...", "#dcdcaa")
        images = list(self.pending_screenshots)
        self.pending_screenshots.clear()
        self._update_pending_label()
        threading.Thread(
            target=self._run_analysis, args=(images,), daemon=True
        ).start()

    def _on_backspace_remove(self, event):
        """BackSpace: キューの末尾(最新)を1つ削除"""
        if self.is_analyzing:
            return
        if not self.pending_screenshots:
            return
        removed = self.pending_screenshots.pop()
        self._update_pending_label()
        # ファイル自体はスクリーンショット履歴として残す
        self._append_result(
            f"[{self._timestamp()}] −削除: {removed.name} "
            f"(キュー {len(self.pending_screenshots)}枚)\n"
        )

    def _on_escape_cancel(self, event):
        """Esc: 実行中の解析プロセスを中断"""
        if not self.is_analyzing:
            return
        if self.claude.cancel():
            self._set_status("解析中断中...", "#f44747")
            self._append_result(f"[{self._timestamp()}] Esc: 解析を中断しました\n")

    def _on_reset(self, event):
        if self.is_analyzing:
            self.claude.cancel()
        self.capture.close()
        self.claude.reset()
        self.analysis_count = 0
        self.pending_screenshots.clear()
        self._update_session_label()
        self._update_count()
        self._update_pending_label()
        self._append_result("--- リセット: デバイス再接続中... ---\n")
        self._init_capture()

    def _on_quit(self, event=None):
        if self.is_analyzing:
            self.claude.cancel()
        self.capture.close()
        self.root.quit()

    def _update_pending_label(self):
        n = len(self.pending_screenshots)
        color = "#4ec9b0" if n > 0 else "#666666"
        self.pending_label.config(text=f"キュー: {n}枚", fg=color)

    def _run_analysis(self, image_paths: list[Path]):
        try:
            self.root.after(
                0, self._set_status,
                f"Claude Code解析中... ({len(image_paths)}枚)",
                "#569cd6",
            )
            result = self.claude.analyze(
                image_paths,
                output_path=RESULT_FILE,
                extra_dirs=[SCREENSHOT_DIR, RESULTS_DIR],
            )

            file_result = ""
            if RESULT_FILE.exists():
                file_result = RESULT_FILE.read_text(encoding="utf-8")

            names = ", ".join(p.name for p in image_paths)
            display = (
                f"\n---\n\n"
                f"## 解析 #{self.analysis_count + 1}  [{self._timestamp()}]\n"
                f"- 画像 ({len(image_paths)}枚): {names}\n"
            )
            if file_result:
                display += f"\n### 📄 出力ファイル\n\n{file_result}\n"
            display += f"\n### 💬 CLI応答\n\n{result}\n\n"

            self.analysis_count += 1
            self.root.after(0, self._append_result, display)
            self.root.after(0, self._update_count)
            self.root.after(0, self._update_session_label)

        except Exception as e:
            self.root.after(
                0, self._append_result,
                f"[{self._timestamp()}] 例外: {e}\n\n"
            )
        finally:
            self.root.after(
                0, self._set_status,
                "● 待機中 — Spaceで追加 / Enterで解析",
                "#4ec9b0",
            )
            self.is_analyzing = False

    def _set_status(self, text: str, color: str = "#4ec9b0"):
        self.status_label.config(text=text, fg=color)

    def _append_result(self, text: str):
        """Markdownとしてレンダリングして末尾に追記、末尾ページに移動"""
        self.result_area.config(state=tk.NORMAL)
        self._render_markdown(text)
        self.result_area.config(state=tk.DISABLED)
        self.result_area.yview_moveto(1.0)
        self._update_page_indicator()

    def _configure_markdown_tags(self):
        ra = self.result_area
        ra.tag_config("h1", font=("Menlo", 18, "bold"),
                      foreground="#4ec9b0", spacing1=10, spacing3=6)
        ra.tag_config("h2", font=("Menlo", 15, "bold"),
                      foreground="#569cd6", spacing1=8, spacing3=4)
        ra.tag_config("h3", font=("Menlo", 13, "bold"),
                      foreground="#9cdcfe", spacing1=6, spacing3=3)
        ra.tag_config("bold", font=("Menlo", 12, "bold"))
        ra.tag_config("italic", font=("Menlo", 12, "italic"),
                      foreground="#c8c8c8")
        ra.tag_config("code_inline", font=("Menlo", 11),
                      background="#2d2d2d", foreground="#ce9178")
        ra.tag_config("code_block", font=("Menlo", 11),
                      background="#1a1a1a", foreground="#ce9178",
                      lmargin1=16, lmargin2=16, spacing1=2, spacing3=2)
        ra.tag_config("bullet", foreground="#dcdcaa")
        ra.tag_config("hr", foreground="#555555", justify="center",
                      spacing1=4, spacing3=4)
        ra.tag_config("quote", foreground="#888888",
                      lmargin1=12, lmargin2=12)

    def _render_markdown(self, md: str):
        """Markdown文字列を解析してタグ付きでresult_areaに書き込む"""
        lines = md.split("\n")
        in_code = False
        for line in lines:
            # コードブロックフェンス
            if line.lstrip().startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                self.result_area.insert(tk.END, line + "\n", "code_block")
                continue

            # 見出し
            if line.startswith("### "):
                self.result_area.insert(tk.END, line[4:] + "\n", "h3")
                continue
            if line.startswith("## "):
                self.result_area.insert(tk.END, line[3:] + "\n", "h2")
                continue
            if line.startswith("# "):
                self.result_area.insert(tk.END, line[2:] + "\n", "h1")
                continue

            # 水平線
            if re.match(r"^\s*([-*_])\1{2,}\s*$", line):
                self.result_area.insert(tk.END, "─" * 50 + "\n", "hr")
                continue

            # 引用
            if line.startswith("> "):
                self._render_inline(line[2:], extra_tag="quote")
                self.result_area.insert(tk.END, "\n", "quote")
                continue

            # 箇条書き
            m = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
            if m:
                indent, content = m.group(1), m.group(2)
                prefix = "  " * (len(indent) // 2) + "• "
                self.result_area.insert(tk.END, prefix, "bullet")
                self._render_inline(content)
                self.result_area.insert(tk.END, "\n")
                continue

            # 番号付きリスト
            m = re.match(r"^(\s*)(\d+)\.\s+(.*)$", line)
            if m:
                indent, num, content = m.group(1), m.group(2), m.group(3)
                prefix = "  " * (len(indent) // 2) + f"{num}. "
                self.result_area.insert(tk.END, prefix, "bullet")
                self._render_inline(content)
                self.result_area.insert(tk.END, "\n")
                continue

            # 通常段落
            self._render_inline(line)
            self.result_area.insert(tk.END, "\n")

    # bold(**x**) / italic(*x*) / inline code(`x`) を抽出する正規表現
    _INLINE_RE = re.compile(r"(\*\*[^*\n]+?\*\*|`[^`\n]+?`|\*[^*\n]+?\*)")

    def _render_inline(self, text: str, extra_tag: str | None = None):
        """段落内のbold/italic/inline codeをタグ付きで書き込む"""
        pos = 0
        for m in self._INLINE_RE.finditer(text):
            if m.start() > pos:
                tags = (extra_tag,) if extra_tag else ()
                self.result_area.insert(tk.END, text[pos:m.start()], tags)
            token = m.group(0)
            if token.startswith("**"):
                tag = "bold"
                content = token[2:-2]
            elif token.startswith("`"):
                tag = "code_inline"
                content = token[1:-1]
            else:
                tag = "italic"
                content = token[1:-1]
            tags = (tag, extra_tag) if extra_tag else (tag,)
            self.result_area.insert(tk.END, content, tags)
            pos = m.end()
        if pos < len(text):
            tags = (extra_tag,) if extra_tag else ()
            self.result_area.insert(tk.END, text[pos:], tags)

    def _update_count(self):
        self.count_label.config(text=f"解析: {self.analysis_count}回")

    def _update_session_label(self):
        sid = self.claude.session_id
        if sid:
            short = sid[:8] if len(sid) > 8 else sid
            self.session_label.config(text=f"セッション: {short}...")
        else:
            self.session_label.config(text="セッション: 未開始")

    @staticmethod
    def _timestamp() -> str:
        return datetime.now().strftime("%H:%M:%S")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    instructions = load_instructions()
    if not instructions:
        print(f"指示書が見つかりません: {INSTRUCTIONS_DIR}")
        print("*.md ファイルを配置してください。")
        raise SystemExit(1)

    selected = select_instruction_cli(instructions)
    if selected is None:
        print("キャンセルされました。")
        raise SystemExit(0)

    print(f"\n→ 指示書: {selected.title} (model: {selected.model or 'default'})")
    print("→ GUI を起動します...\n")

    app = ScreenAnalyzerApp(selected)
    app.run()