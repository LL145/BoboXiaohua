"""Tkinter 桌面界面。"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

from .config import CONFIG_PATH, app_dir, load_config
from .pipeline import GenerationCancelled, Pipeline

_PLACEHOLDER = "例如:一只橘猫在雨后的东京街头漫步,霓虹灯倒映在水洼里,电影感画面"
_REF_HINT_EMPTY = "未选择(有固定主角时将由 AI 自动生成形象)"

# 画幅选项:显示文案 → 配置值(Seedance 原生支持全部画幅;
# Kling 引擎下 3:4 / 4:3 由相邻画幅生成后自动居中裁剪)
_ASPECT_CHOICES = {
    "🖥 横屏 16:9": "16:9",
    "📱 竖屏 9:16": "9:16",
    "方形 1:1": "1:1",
    "横幅 4:3": "4:3",
    "竖幅 3:4": "3:4",
}
# 大约时长选项:显示文案 → 目标秒数(实际成片在目标值 ±15% 内)
_DURATION_CHOICES = {
    "30 秒": 30,
    "1 分钟": 60,
    "2 分钟": 120,
}


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
        self.root.title("AI 短视频生成器")
        self.root.geometry("880x580")
        self.root.minsize(760, 480)

        # 工作线程 → 主线程的消息队列,消息为 (类型, *参数) 元组:
        # ("log", 文本) / ("prog", 百分比, 阶段) / ("done", 成片路径) / ("fail",) / ("cancel",)
        self._log_queue: queue.Queue[tuple] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._final_path: Path | None = None
        self._cancel_event: threading.Event | None = None
        # 用户上传的参考图(可选,可多张):[(路径, 用途说明)]
        self._ref_images: list[tuple[Path, str]] = []

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

        # 可选:上传参考图(可多张,每张可注明用途),锁定主角外观/场景/风格
        ref_bar = ttk.Frame(top)
        ref_bar.pack(fill="x", pady=(4, 0))
        ttk.Button(
            ref_bar, text="🖼 上传参考图(可多选)", command=self._pick_reference
        ).pack(side="left")
        self.ref_clear_btn = ttk.Button(
            ref_bar, text="✕ 清除", command=self._clear_reference, state="disabled"
        )
        self.ref_clear_btn.pack(side="left", padx=(6, 0))
        self.ref_var = tk.StringVar(value=_REF_HINT_EMPTY)
        ttk.Label(ref_bar, textvariable=self.ref_var, foreground="gray").pack(
            side="left", padx=(8, 0)
        )

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

        default_aspect, default_duration, default_subtitles = self._config_defaults()
        # 画幅与时长下拉框以显示文案为值,提交时经映射表转回配置值
        self.aspect_var = tk.StringVar(
            value=next(
                (label for label, v in _ASPECT_CHOICES.items() if v == default_aspect),
                "🖥 横屏 16:9",
            )
        )
        ttk.Combobox(
            bar, textvariable=self.aspect_var, state="readonly",
            values=list(_ASPECT_CHOICES), width=11,
        ).pack(side="right")
        ttk.Label(bar, text="画幅:").pack(side="right", padx=(0, 4))
        self.duration_var = tk.StringVar(
            value=min(
                _DURATION_CHOICES,
                key=lambda label: abs(_DURATION_CHOICES[label] - default_duration),
            )
        )
        ttk.Combobox(
            bar, textvariable=self.duration_var, state="readonly",
            values=list(_DURATION_CHOICES), width=7,
        ).pack(side="right", padx=(0, 10))
        ttk.Label(bar, text="时长:").pack(side="right", padx=(0, 4))
        self.subtitle_var = tk.BooleanVar(value=default_subtitles)
        ttk.Checkbutton(
            bar, text="旁白字幕", variable=self.subtitle_var
        ).pack(side="right", padx=(0, 16))

        prog_frame = ttk.Frame(self.root)
        prog_frame.pack(fill="x", padx=12)
        self.step_var = tk.StringVar(value="")
        ttk.Label(prog_frame, textvariable=self.step_var, anchor="w").pack(fill="x")
        self.progress = ttk.Progressbar(prog_frame, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(2, 0))

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

    def _pick_reference(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择参考图(可多选;多图支持情况随引擎,详见生成日志)",
            filetypes=[
                ("图片文件", "*.png *.jpg *.jpeg *.webp *.bmp"),
                ("所有文件", "*.*"),
            ],
        )
        if not paths:
            return
        # 逐张询问用途:标注用途能显著提升参考图效果(未标注是效果不佳的
        # 最常见原因);多图时 Kling 引擎仅作多角度参考、用途说明不生效
        picked: list[tuple[Path, str]] = []
        for i, raw in enumerate(paths, 1):
            path = Path(raw)
            note = simpledialog.askstring(
                "参考图用途",
                f"第 {i} 张:{path.name}\n\n"
                "请注明这张图的用途(可留空,默认为主角形象参考),例如:\n"
                "主角正面 / 主角侧面 / 场景参考 / 画面风格参考",
                initialvalue="主角正面" if i == 1 and len(paths) > 1 else "",
                parent=self.root,
            )
            picked.append((path, (note or "").strip()))
        self._ref_images = picked
        if len(picked) == 1:
            self.ref_var.set(f"参考图:{picked[0][0].name}")
        else:
            self.ref_var.set(
                f"参考图 {len(picked)} 张(用途已标注;Seedance 引擎支持多图,"
                "Kling 仅作同一主角的多角度参考,即梦引擎不支持参考图)"
            )
        self.ref_clear_btn.config(state="normal")

    def _clear_reference(self) -> None:
        self._ref_images = []
        self.ref_var.set(_REF_HINT_EMPTY)
        self.ref_clear_btn.config(state="disabled")

    @staticmethod
    def _config_defaults() -> tuple[str, int, bool]:
        """界面选项默认值取自 config.yaml:画幅、目标时长(取最接近的档位)与字幕开关。"""
        aspect, duration, subtitles = "", 60, True
        try:
            config = load_config()
            aspect = str(config["video"]["aspect_ratio"])
            duration = int(config["video"]["target_duration"])
            subtitles = bool(config["narration"]["subtitles"])
        except Exception:  # noqa: BLE001 - 首次启动可能还没有配置文件
            pass
        if aspect not in _ASPECT_CHOICES.values():
            aspect = "16:9"
        return aspect, duration, subtitles

    # ---------------- 事件 ----------------

    def _on_generate(self) -> None:
        description = self.desc_text.get("1.0", "end-1c").strip()
        if not description or description == _PLACEHOLDER:
            messagebox.showwarning("提示", "请先输入一句话描述。")
            return
        ref_images = list(self._ref_images)
        missing = [str(p) for p, _ in ref_images if not p.is_file()]
        if missing:
            messagebox.showwarning(
                "提示", "参考图不存在:\n" + "\n".join(missing) + "\n请重新选择或清除。"
            )
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
        # 界面上选择的画幅、时长与字幕开关优先于 config.yaml
        config["video"]["aspect_ratio"] = _ASPECT_CHOICES.get(
            self.aspect_var.get(), "16:9"
        )
        config["video"]["target_duration"] = _DURATION_CHOICES.get(
            self.duration_var.get(), 60
        )
        config["narration"]["subtitles"] = bool(self.subtitle_var.get())

        self._final_path = None
        self._cancel_event = threading.Event()
        self.open_btn.config(state="disabled")
        self.generate_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.progress["value"] = 0
        self.step_var.set("准备开始 …")
        self.status_var.set("生成中…全程可能需要十几分钟,请勿关闭窗口。")
        self._clear_log()

        cancel_event = self._cancel_event

        def work() -> None:
            try:
                pipeline = Pipeline(
                    config, self._log,
                    progress=self._on_progress, cancel_event=cancel_event,
                )
                final_path = pipeline.run(description, reference_images=ref_images)
                self._log_queue.put(("done", final_path))
            except GenerationCancelled:
                self._log("⏹ 已取消。已完成的镜头已保存,不会重复扣费。")
                self._log_queue.put(("cancel",))
            except Exception as exc:  # noqa: BLE001 - 汇总展示给用户
                self._log(f"❌ 出错: {exc}")
                self._log_queue.put(("fail",))

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
        self._log_queue.put(("log", message))

    def _on_progress(self, percent: int, stage: str) -> None:
        """总进度与当前阶段(由工作线程调用,经队列转到主线程)。"""
        self._log_queue.put(("prog", percent, stage))

    def _clear_log(self) -> None:
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.config(state="disabled")

    def _poll_log_queue(self) -> None:
        try:
            while True:
                kind, *payload = self._log_queue.get_nowait()
                if kind == "done":
                    self._final_path = Path(payload[0])
                    self._finish("完成!点击「打开成片」查看视频。", step="✅ 全部完成")
                    self.open_btn.config(state="normal")
                elif kind == "fail":
                    self._finish("生成失败,详见日志。", step="❌ 已中止")
                elif kind == "cancel":
                    self._finish("已取消。再次生成相同描述可从断点继续。", step="⏹ 已取消")
                elif kind == "prog":
                    percent, stage = payload
                    self.progress["value"] = int(percent)
                    self.step_var.set(f"当前步骤:{stage}({percent}%)")
                else:
                    self.log_box.config(state="normal")
                    self.log_box.insert("end", str(payload[0]) + "\n")
                    self.log_box.see("end")
                    self.log_box.config(state="disabled")
        except queue.Empty:
            pass
        self.root.after(150, self._poll_log_queue)

    def _finish(self, status: str, step: str = "") -> None:
        self.generate_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        if step:
            self.step_var.set(step)
        self.status_var.set(status)
        self.root.bell()  # 全程耗时较长,提示音告知用户已结束

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    App().run()
