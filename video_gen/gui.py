"""Tkinter 桌面界面。"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

from .config import (
    CONFIG_PATH,
    ENGINE_RESOLUTIONS,
    ENGINES,
    LLM_MODEL_PRESETS,
    app_dir,
    load_config,
    save_settings,
)
from .pipeline import GenerationCancelled, Pipeline

_PLACEHOLDER = "例如:一只橘猫在雨后的东京街头漫步,霓虹灯倒映在水洼里,电影感画面"
_REF_HINT_EMPTY = "未选择(有固定主角时将由 AI 自动生成形象)"
_NO_RESOLUTION = "(引擎默认)"  # 该引擎端点不接受分辨率参数时的占位文案
_ENGINE_LABELS = {  # 引擎下拉框:展示名 → 配置值(首项为默认引擎)
    f"{name}(默认)" if i == 0 else name: engine
    for i, (engine, name) in enumerate(ENGINES.items())
}

# 画幅选项:显示文案 → 配置值(Seedance 原生支持全部画幅;
# Kling 引擎下 3:4 / 4:3、Gemini 引擎下 1:1 / 3:4 / 4:3 由相邻画幅生成后
# 自动居中裁剪)
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
        self.root.geometry("960x680")
        self.root.minsize(860, 560)

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

        self._build_settings()

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

        self.status_var = tk.StringVar(
            value="就绪。首次使用请在上方「设置」填入两个 API KEY(点「生成」时自动保存)。"
        )
        ttk.Label(self.root, textvariable=self.status_var, anchor="w").pack(fill="x", padx=12, pady=(0, 8))

    def _build_settings(self) -> None:
        """顶部「设置」区:API KEY、编剧模型、视频引擎与分辨率,保存进 config.yaml。"""
        box = ttk.LabelFrame(self.root, text="设置(保存在程序目录的 config.yaml,只需填一次)")
        box.pack(fill="x", padx=12, pady=(8, 0))
        for col in (1, 3):
            box.columnconfigure(col, weight=1)
        settings = self._load_settings()

        # 第一行:两个 API KEY
        ttk.Label(box, text="OpenRouter KEY:").grid(row=0, column=0, sticky="e", padx=(8, 4), pady=4)
        self.openrouter_var = tk.StringVar(value=settings["openrouter_api_key"])
        self.openrouter_entry = ttk.Entry(box, textvariable=self.openrouter_var, show="•")
        self.openrouter_entry.grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(box, text="fal.ai KEY:").grid(row=0, column=2, sticky="e", padx=(12, 4), pady=4)
        self.fal_var = tk.StringVar(value=settings["fal_api_key"])
        self.fal_entry = ttk.Entry(box, textvariable=self.fal_var, show="•")
        self.fal_entry.grid(row=0, column=3, sticky="ew", pady=4)
        self.show_keys_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            box, text="显示", variable=self.show_keys_var, command=self._toggle_key_visibility
        ).grid(row=0, column=4, padx=(6, 8), pady=4)

        # 第二行:编剧模型(可选可填)、视频引擎、分辨率、保存
        ttk.Label(box, text="编剧模型:").grid(row=1, column=0, sticky="e", padx=(8, 4), pady=(0, 6))
        self.llm_model_var = tk.StringVar(value=settings["llm_model"])
        ttk.Combobox(
            box, textvariable=self.llm_model_var, values=list(LLM_MODEL_PRESETS)
        ).grid(row=1, column=1, sticky="ew", pady=(0, 6))
        engine_row = ttk.Frame(box)
        engine_row.grid(row=1, column=2, columnspan=3, sticky="ew", padx=(12, 8), pady=(0, 6))
        ttk.Label(engine_row, text="视频引擎:").pack(side="left", padx=(0, 4))
        self.engine_var = tk.StringVar(
            value=next(
                (label for label, v in _ENGINE_LABELS.items() if v == settings["engine"]),
                next(iter(_ENGINE_LABELS)),
            )
        )
        engine_box = ttk.Combobox(
            engine_row, textvariable=self.engine_var, state="readonly",
            values=list(_ENGINE_LABELS), width=22,
        )
        engine_box.pack(side="left")
        engine_box.bind("<<ComboboxSelected>>", self._on_engine_change)
        ttk.Label(engine_row, text="分辨率:").pack(side="left", padx=(10, 4))
        self.resolution_var = tk.StringVar()
        self.resolution_box = ttk.Combobox(
            engine_row, textvariable=self.resolution_var, state="readonly", width=10
        )
        self.resolution_box.pack(side="left")
        self._resolutions = settings["resolutions"]
        self._refresh_resolution_choices()
        ttk.Button(engine_row, text="💾 保存设置", command=self._save_settings).pack(
            side="right"
        )

    # ---------------- 设置 ----------------

    @staticmethod
    def _load_settings() -> dict:
        """从 config.yaml 读取界面「设置」区的当前值(没有配置文件时用默认值)。"""
        engine = next(iter(ENGINES))
        values = {
            "openrouter_api_key": "",
            "fal_api_key": "",
            "llm_model": LLM_MODEL_PRESETS[0],
            "engine": engine,
            # 各引擎各自记住分辨率,切换引擎时不互相覆盖
            "resolutions": {
                e: (choices[1] if len(choices) > 1 else choices[0]) if choices else ""
                for e, choices in ENGINE_RESOLUTIONS.items()
            },
        }
        try:
            config = load_config()
        except Exception:  # noqa: BLE001 - 首次启动可能还没有配置文件
            return values
        values["openrouter_api_key"] = config.openrouter_api_key
        values["fal_api_key"] = config.fal_api_key
        values["llm_model"] = str(config["llm"]["model"] or LLM_MODEL_PRESETS[0])
        if config.engine in ENGINES:
            values["engine"] = config.engine
        for e, choices in ENGINE_RESOLUTIONS.items():
            current = str(config[e].get("resolution") or "")
            if current in choices:
                values["resolutions"][e] = current
        return values

    def _selected_engine(self) -> str:
        return _ENGINE_LABELS.get(self.engine_var.get(), next(iter(ENGINES)))

    def _refresh_resolution_choices(self) -> None:
        """分辨率下拉框随引擎变化:Kling 端点无分辨率参数时禁用。"""
        engine = self._selected_engine()
        choices = ENGINE_RESOLUTIONS.get(engine, ())
        if choices:
            self.resolution_box.config(values=list(choices), state="readonly")
            current = self._resolutions.get(engine) or choices[0]
            self.resolution_var.set(current if current in choices else choices[0])
        else:
            self.resolution_box.config(values=[_NO_RESOLUTION], state="disabled")
            self.resolution_var.set(_NO_RESOLUTION)

    def _on_engine_change(self, _event: object = None) -> None:
        self._refresh_resolution_choices()

    def _toggle_key_visibility(self) -> None:
        show = "" if self.show_keys_var.get() else "•"
        self.openrouter_entry.config(show=show)
        self.fal_entry.config(show=show)

    def _collect_settings(self) -> dict:
        """界面「设置」区的值 → 写入 config.yaml 的键值(键为「节.键」)。"""
        engine = self._selected_engine()
        updates = {
            "openrouter_api_key": self.openrouter_var.get().strip(),
            "fal_api_key": self.fal_var.get().strip(),
            "llm.model": self.llm_model_var.get().strip() or LLM_MODEL_PRESETS[0],
            "video.engine": engine,
        }
        choices = ENGINE_RESOLUTIONS.get(engine, ())
        if choices:
            resolution = self.resolution_var.get()
            if resolution not in choices:
                resolution = choices[0]
            self._resolutions[engine] = resolution
            updates[f"{engine}.resolution"] = resolution
        return updates

    def _save_settings(self, silent: bool = False) -> bool:
        """把界面设置写回 config.yaml;失败时弹窗提示(silent 为 True 只写日志)。"""
        try:
            save_settings(self._collect_settings())
        except Exception as exc:  # noqa: BLE001 - 写文件失败不应让程序崩溃
            message = f"设置保存失败: {exc}"
            if silent:
                self._log(f"⚠ {message}(本次生成仍按界面上的设置进行)")
            else:
                messagebox.showerror("保存失败", message)
            return False
        if not silent:
            self.status_var.set(f"设置已保存到 {CONFIG_PATH.name}。")
        return True

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
                f"参考图 {len(picked)} 张(用途已标注;Seedance / Gemini 引擎支持多图,"
                "Kling 仅作同一主角的多角度参考)"
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

        # 界面「设置」先落盘再读取,保证 config.yaml 与界面一致;
        # 万一写盘失败,仍把界面上的值覆盖进本次使用的配置
        self._save_settings(silent=True)
        try:
            config = load_config()
        except FileNotFoundError as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        for dotted, value in self._collect_settings().items():
            section, _, key = dotted.partition(".")
            if key:
                config[section][key] = value
            else:
                config[section] = value
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
