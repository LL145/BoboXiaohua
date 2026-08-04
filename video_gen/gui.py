"""Tkinter 桌面界面。"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

from .config import CONFIG_PATH, app_dir, load_config
from .pipeline import GenerationCancelled, Pipeline

_PLACEHOLDER = "例如:一只橘猫在雨后的东京街头漫步,霓虹灯倒映在水洼里,电影感画面"


def _open_path(path: Path) -> None:
    """用系统默认程序打开文件(跨平台)。"""
    if sys.platform == "win32":
        import os

        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class App:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("AI 短视频生成器 — LLM × Kling")
        self.root.geometry("840x560")
        self.root.minsize(720, 480)

        self._log_queue: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._final_path: Path | None = None
        self._cancel_event: threading.Event | None = None

        self._build_ui()
        self._poll_log_queue()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- UI ----------------

    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 6}

        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="用一句话描述你想要的视频:").pack(anchor="w")

        self.desc_text = tk.Text(top, height=3, wrap="word", font=("Microsoft YaHei UI", 11))
        self.desc_text.pack(fill="x", pady=(4, 0))
        self.desc_text.insert("1.0", _PLACEHOLDER)
        self.desc_text.bind("<FocusIn>", self._clear_placeholder)

        bar = ttk.Frame(self.root)
        bar.pack(fill="x", **pad)
        self.generate_btn = ttk.Button(bar, text="🎬 生成视频", command=self._on_generate)
        self.generate_btn.pack(side="left")
        self.cancel_btn = ttk.Button(bar, text="⏹ 取消", command=self._on_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))
        self.open_btn = ttk.Button(bar, text="打开成片", command=self._open_result, state="disabled")
        self.open_btn.pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="打开输出文件夹", command=self._open_output_dir).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="打开配置文件", command=self._open_config).pack(side="left", padx=(8, 0))

        self.aspect_var = tk.StringVar(value=self._default_aspect())
        ttk.Radiobutton(
            bar, text="📱 竖屏 9:16", value="9:16", variable=self.aspect_var
        ).pack(side="right")
        ttk.Radiobutton(
            bar, text="🖥 横屏 16:9", value="16:9", variable=self.aspect_var
        ).pack(side="right", padx=(0, 10))
        ttk.Label(bar, text="画幅:").pack(side="right", padx=(0, 4))

        self.progress = ttk.Progressbar(self.root, mode="indeterminate")
        self.progress.pack(fill="x", padx=12)

        log_frame = ttk.LabelFrame(self.root, text="进度日志")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_box = scrolledtext.ScrolledText(
            log_frame, state="disabled", wrap="word", font=("Consolas", 10)
        )
        self.log_box.pack(fill="both", expand=True, padx=6, pady=6)

        self.status_var = tk.StringVar(value="就绪。首次使用请先在 config.yaml 中填入 API KEY。")
        ttk.Label(self.root, textvariable=self.status_var, anchor="w").pack(fill="x", padx=12, pady=(0, 8))

    def _clear_placeholder(self, _event: object) -> None:
        if self.desc_text.get("1.0", "end-1c") == _PLACEHOLDER:
            self.desc_text.delete("1.0", "end")

    @staticmethod
    def _default_aspect() -> str:
        """默认画幅取自 config.yaml,配置缺失或非横/竖屏时用横屏。"""
        try:
            aspect = str(load_config()["kling"]["aspect_ratio"])
        except Exception:  # noqa: BLE001 - 首次启动可能还没有配置文件
            aspect = ""
        return aspect if aspect in ("16:9", "9:16") else "16:9"

    # ---------------- 事件 ----------------

    def _on_generate(self) -> None:
        description = self.desc_text.get("1.0", "end-1c").strip()
        if not description or description == _PLACEHOLDER:
            messagebox.showwarning("提示", "请先输入一句话描述。")
            return

        try:
            config = load_config()
        except FileNotFoundError as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        problems = config.validate()
        if problems:
            messagebox.showerror("配置错误", "\n".join(problems))
            return
        # 界面上选择的画幅优先于 config.yaml
        config["kling"]["aspect_ratio"] = self.aspect_var.get()

        self._final_path = None
        self._cancel_event = threading.Event()
        self.open_btn.config(state="disabled")
        self.generate_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.progress.start(12)
        self.status_var.set("生成中…全程可能需要十几分钟,请勿关闭窗口。")
        self._clear_log()

        cancel_event = self._cancel_event

        def work() -> None:
            try:
                pipeline = Pipeline(
                    config, self._log,
                    progress=self._on_progress, cancel_event=cancel_event,
                )
                final_path = pipeline.run(description)
                self._log_queue.put(f"__DONE__{final_path}")
            except GenerationCancelled:
                self._log("⏹ 已取消。已完成的镜头已保存,不会重复扣费。")
                self._log_queue.put("__CANCEL__")
            except Exception as exc:  # noqa: BLE001 - 汇总展示给用户
                self._log(f"❌ 出错: {exc}")
                self._log_queue.put("__FAIL__")

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _on_cancel(self) -> None:
        if self._cancel_event is None or self._cancel_event.is_set():
            return
        self._cancel_event.set()
        self.cancel_btn.config(state="disabled")
        self.status_var.set("正在取消,等待当前步骤停止(进度已保留)…")
        self._log("⏹ 正在取消 …")

    def _open_output_dir(self) -> None:
        try:
            out_dir = load_config().output_dir
        except Exception:  # noqa: BLE001 - 无配置时也能打开默认输出目录
            out_dir = app_dir() / "output"
            out_dir.mkdir(parents=True, exist_ok=True)
        _open_path(out_dir)

    def _open_result(self) -> None:
        if self._final_path and self._final_path.exists():
            _open_path(self._final_path)

    def _on_close(self) -> None:
        if self._worker and self._worker.is_alive():
            if not messagebox.askokcancel(
                "确认退出",
                "视频仍在生成中,退出后进度会保留:\n"
                "下次输入相同描述再点「生成」会从断点继续,已生成的镜头不会重复扣费。\n\n"
                "确定退出吗?",
            ):
                return
        self.root.destroy()

    def _open_config(self) -> None:
        _open_path(CONFIG_PATH)

    # ---------------- 日志 ----------------

    def _log(self, message: str) -> None:
        self._log_queue.put(message)

    def _on_progress(self, done: int, total: int) -> None:
        """镜头完成进度(由工作线程调用,经队列转到主线程)。"""
        self._log_queue.put(f"__PROG__{done}/{total}")

    def _clear_log(self) -> None:
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.config(state="disabled")

    def _poll_log_queue(self) -> None:
        try:
            while True:
                message = self._log_queue.get_nowait()
                if message.startswith("__DONE__"):
                    self._final_path = Path(message[len("__DONE__"):])
                    self._finish("完成!点击「打开成片」查看视频。")
                    self.open_btn.config(state="normal")
                elif message == "__FAIL__":
                    self._finish("生成失败,详见日志。")
                elif message == "__CANCEL__":
                    self._finish("已取消。再次生成相同描述可从断点继续。")
                elif message.startswith("__PROG__"):
                    done, total = message[len("__PROG__"):].split("/")
                    self.progress.stop()
                    self.progress.config(mode="determinate", maximum=int(total))
                    self.progress["value"] = int(done)
                else:
                    self.log_box.config(state="normal")
                    self.log_box.insert("end", message + "\n")
                    self.log_box.see("end")
                    self.log_box.config(state="disabled")
        except queue.Empty:
            pass
        self.root.after(150, self._poll_log_queue)

    def _finish(self, status: str) -> None:
        self.progress.stop()
        self.progress.config(mode="indeterminate")
        self.progress["value"] = 0
        self.generate_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        self.status_var.set(status)
        self.root.bell()  # 全程耗时较长,提示音告知用户已结束

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    App().run()
