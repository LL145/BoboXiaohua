"""用 ffmpeg 将各镜头片段拼接为最终成片。

拼接优先带交叉溶解转场 + 首尾淡入淡出(需要 ffprobe 与重编码);
任一环节不可用时自动回退为普通拼接,保证一定能出片。
背景音乐(如有)在拼接后混入,与片段原生音效共存。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from .config import Config, bundled_font_dir

LogFn = Callable[[str], None]

# Windows 下隐藏 ffmpeg 的控制台窗口
_CREATIONFLAGS = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


class Assembler:
    def __init__(self, config: Config, log: LogFn):
        self._ffmpeg = config.ffmpeg_path
        self._ffprobe = self._derive_ffprobe(self._ffmpeg)
        self._bgm_volume = float(config["video"]["bgm_volume"])
        self._transition = max(0.0, float(config["video"]["transition"]))
        self._log = log

    @staticmethod
    def _derive_ffprobe(ffmpeg: str) -> str:
        """ffprobe 与 ffmpeg 同目录同后缀,由配置的 ffmpeg 路径推导。"""
        path = Path(ffmpeg)
        if "ffmpeg" not in path.name:
            return "ffprobe"
        probe_name = path.name.replace("ffmpeg", "ffprobe")
        return probe_name if str(path) == path.name else str(path.with_name(probe_name))

    def check_ffmpeg(self) -> bool:
        return shutil.which(self._ffmpeg) is not None or Path(self._ffmpeg).exists()

    # ---------------- 探测 ----------------

    def probe_duration(self, path: Path) -> float | None:
        out = self._probe(
            ["-show_entries", "format=duration", "-of", "csv=p=0", str(path)]
        )
        try:
            return float(out.strip()) if out else None
        except ValueError:
            return None

    def _has_audio(self, path: Path) -> bool | None:
        out = self._probe(
            ["-select_streams", "a", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(path)]
        )
        return None if out is None else bool(out.strip())

    def _probe(self, args: list[str]) -> str | None:
        try:
            result = subprocess.run(
                [self._ffprobe, "-v", "error", *args],
                capture_output=True, text=True, creationflags=_CREATIONFLAGS,
            )
        except OSError:
            return None
        return result.stdout if result.returncode == 0 else None

    # ---------------- 拼接 ----------------

    def concat(
        self,
        clips: list[Path],
        out_path: Path,
        fallback_durations: list[float] | None = None,
    ) -> tuple[Path, list[float]]:
        """拼接片段:优先交叉溶解转场,失败回退普通拼接。

        返回 (成片路径, 各片段在成片时间轴上的起点秒数)——旁白与字幕
        依赖这些偏移定位到对应镜头组。
        """
        probed = [self.probe_duration(c) for c in clips]
        durations: list[float | None] = list(probed)
        if fallback_durations:
            durations = [
                d if d is not None else fallback_durations[i]
                for i, d in enumerate(probed)
            ]
        safe_durations = [d if d is not None else 5.0 for d in durations]

        if len(clips) == 1:
            shutil.copyfile(clips[0], out_path)
            return out_path, [0.0]

        if self._transition > 0:
            if all(p is None for p in probed):
                self._log(
                    "⚠ 未找到 ffprobe,转场与旁白位置只能按分镜计划时长估算"
                    "(建议使用自带 ffprobe 的完整 ffmpeg 发行包)…"
                )
            try:
                offsets = self._concat_with_transitions(clips, durations, out_path)
                if offsets is not None:
                    return out_path, offsets
                self._log("片段时长过短或无法探测,跳过交叉溶解转场,改用直接拼接 …")
            except subprocess.CalledProcessError:
                self._log("转场拼接失败,回退为直接拼接 …")

        list_file = out_path.with_suffix(".txt")
        # concat demuxer 的文件列表,单引号包裹并转义
        lines = [f"file '{c.resolve().as_posix()}'" for c in clips]
        list_file.write_text("\n".join(lines), encoding="utf-8")

        try:
            self._log("拼接片段(无损模式)…")
            self._run(
                ["-f", "concat", "-safe", "0", "-i", str(list_file),
                 "-c", "copy", str(out_path)]
            )
        except subprocess.CalledProcessError:
            self._log("无损拼接失败,改用重编码拼接 …")
            self._run(
                ["-f", "concat", "-safe", "0", "-i", str(list_file),
                 "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", str(out_path)]
            )
        finally:
            list_file.unlink(missing_ok=True)
        offsets = [sum(safe_durations[:i]) for i in range(len(clips))]
        return out_path, offsets

    def _concat_with_transitions(
        self, clips: list[Path], durations: list[float | None], out_path: Path
    ) -> list[float] | None:
        """xfade 交叉溶解 + 首尾淡入淡出;音轨齐全时同步 acrossfade。

        返回各片段起点偏移;None 表示前置条件不满足(如 ffprobe 缺失),
        由调用方回退。
        """
        t = self._transition
        if any(d is None or d <= 2 * t for d in durations):
            return None
        with_audio = all(self._has_audio(c) for c in clips)

        inputs: list[str] = []
        for clip in clips:
            inputs += ["-i", str(clip)]

        parts: list[str] = []
        starts: list[float] = [0.0]
        prev, offset = "[0:v]", 0.0
        for i in range(1, len(clips)):
            offset += durations[i - 1] - t
            starts.append(offset)
            parts.append(
                f"{prev}[{i}:v]xfade=transition=fade:duration={t}:offset={offset:.3f}[v{i}]"
            )
            prev = f"[v{i}]"
        total = sum(durations) - t * (len(clips) - 1)
        fade_out = max(0.0, total - 1.0)
        parts.append(f"{prev}fade=t=in:d=0.5,fade=t=out:st={fade_out:.3f}:d=1[v]")

        maps = ["-map", "[v]"]
        if with_audio:
            prev = "[0:a]"
            for i in range(1, len(clips)):
                parts.append(f"{prev}[{i}:a]acrossfade=d={t}[a{i}]")
                prev = f"[a{i}]"
            parts.append(f"{prev}afade=t=out:st={fade_out:.3f}:d=1[a]")
            maps += ["-map", "[a]"]

        self._log("拼接片段(交叉溶解转场" + (",含原生音效" if with_audio else "") + ")…")
        args = [*inputs, "-filter_complex", ";".join(parts), *maps,
                "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-pix_fmt", "yuv420p"]
        if with_audio:
            args += ["-c:a", "aac"]
        args.append(str(out_path))
        self._run(args)
        return starts

    # ---------------- 画幅裁剪 ----------------

    def crop_to_aspect(self, video: Path, aspect: str, out_path: Path) -> bool:
        """把成片居中裁剪到目标画幅(如 3:4 / 4:3),失败时沿用原画幅。

        Kling 端点不原生支持这类画幅,片段按相邻原生画幅生成,
        在拼接后一次性裁剪(需在烧录字幕之前,避免字幕被裁掉)。
        """
        try:
            w, h = (int(v) for v in str(aspect).split(":"))
        except ValueError:
            return False
        # 居中裁剪到 w:h,trunc(x/2)*2 保证宽高为偶数(libx264 要求)
        vf = (
            f"crop=trunc(min(iw\\,ih*{w}/{h})/2)*2"
            f":trunc(min(ih\\,iw*{h}/{w})/2)*2"
        )
        try:
            self._log(f"居中裁剪画幅到 {aspect} …")
            self._run(
                ["-i", str(video), "-vf", vf,
                 "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                 "-pix_fmt", "yuv420p", "-c:a", "copy", str(out_path)]
            )
            return True
        except subprocess.CalledProcessError:
            self._log(f"裁剪到 {aspect} 失败,保留原生成画幅输出(不影响成片)。")
            return False

    # ---------------- 旁白配音 ----------------

    def mix_narration(
        self,
        video: Path,
        segments: list[tuple[float, Path, float]],
        out_path: Path,
        volume: float = 1.0,
    ) -> list[tuple[float, float, float]] | None:
        """把各镜头组的旁白音频按时间轴偏移混入成片。

        segments: [(起点秒, 音频路径, 可用时长上限)]。旁白略长于所在镜头组时
        自动加速(至多 1.4 倍)并截断,避免串到下一组画面。
        返回各段旁白实际的 (起点, 时长, 加速倍率) 供字幕对齐;
        失败返回 None(沿用原片)。
        """
        if not segments:
            return None
        timeline: list[tuple[float, float, float]] = []
        inputs: list[str] = ["-i", str(video)]
        filters: list[str] = []
        labels: list[str] = []
        for i, (start, audio, slot) in enumerate(segments):
            duration = self.probe_duration(audio) or slot
            tempo = min(1.4, max(1.0, duration / slot)) if slot > 0 else 1.0
            effective = min(duration / tempo, slot)
            timeline.append((start, effective, tempo))
            inputs += ["-i", str(audio)]
            chain = [f"[{i + 1}:a]"]
            if tempo > 1.001:
                chain.append(f"atempo={tempo:.3f},")
            delay_ms = int(start * 1000)
            fade_start = max(0.0, effective - 0.2)
            chain.append(
                f"atrim=0:{effective:.3f},afade=t=out:st={fade_start:.3f}:d=0.2,"
                f"volume={volume},adelay={delay_ms}|{delay_ms}[n{i}]"
            )
            filters.append("".join(chain))
            labels.append(f"[n{i}]")

        n = len(segments)
        tail = ["-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
                str(out_path)]
        # 先探测原片有无音轨,直接选对混音方案;探测不可用(ffprobe 缺失)时
        # 才退回"先试混音、失败再按无音轨处理"的两步走
        has_audio = self._has_audio(video)
        try:
            self._log(f"混入旁白配音({n} 段)…")
            if has_audio is not False:
                try:
                    # 与原片音轨(环境音/台词)混音
                    mix = f"[0:a]{''.join(labels)}amix=inputs={n + 1}:duration=first:normalize=0[a]"
                    self._run([*inputs, "-filter_complex", ";".join([*filters, mix]), *tail])
                    return timeline
                except subprocess.CalledProcessError:
                    if has_audio:
                        raise  # 确认有音轨仍失败,是真失败,不再按无音轨重试
            # 原片无音轨:仅旁白,时长对齐画面
            video_dur = self.probe_duration(video) or (
                segments[-1][0] + segments[-1][2]
            )
            mix = (
                f"{''.join(labels)}amix=inputs={n}:duration=longest:normalize=0,"
                f"apad,atrim=0:{video_dur:.3f}[a]"
            ) if n > 1 else (
                f"{labels[0]}apad,atrim=0:{video_dur:.3f}[a]"
            )
            self._run([*inputs, "-filter_complex", ";".join([*filters, mix]), *tail])
            return timeline
        except subprocess.CalledProcessError:
            self._log("旁白混入失败,输出无旁白版本(不影响画面)。")
            return None

    # ---------------- 字幕 ----------------

    @staticmethod
    def _escape_filter_path(path: Path) -> str:
        """转义 ffmpeg 滤镜参数中的路径(Windows 盘符冒号、反斜杠)。"""
        return str(path).replace("\\", "/").replace(":", "\\:")

    @staticmethod
    def _subtitle_font() -> tuple[str, Path | None]:
        """字幕字体:优先随程序分发的 Noto Sans SC(fonts/),
        缺失时按平台回退到系统常见中文字体。"""
        fonts_dir = bundled_font_dir()
        if fonts_dir is not None:
            return "Noto Sans SC Medium", fonts_dir
        if sys.platform == "win32":
            return "Microsoft YaHei", None
        if sys.platform == "darwin":
            return "PingFang SC", None
        return "Noto Sans CJK SC", None

    def embed_subtitles(self, video: Path, srt: Path, out_path: Path) -> bool:
        """字幕:优先烧录进画面,失败退为 mp4 软字幕,再失败则放弃。

        用 srt 所在目录作为工作目录、以相对文件名引用,规避 Windows
        盘符冒号在 ffmpeg 滤镜参数中的转义问题。
        """
        font_name, fonts_dir = self._subtitle_font()
        style = (
            f"FontName={font_name},FontSize=14,PrimaryColour=&HFFFFFF&,"
            "OutlineColour=&H66000000&,Outline=1,Shadow=0,MarginV=24"
        )
        vf = f"subtitles={srt.name}"
        if fonts_dir is not None:
            vf += f":fontsdir='{self._escape_filter_path(fonts_dir)}'"
        vf += f":force_style='{style}'"
        try:
            self._log("烧录旁白字幕 …")
            self._run(
                ["-i", str(video.resolve()), "-vf", vf,
                 "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                 "-pix_fmt", "yuv420p", "-c:a", "copy", str(out_path.resolve())],
                cwd=srt.parent,
            )
            return True
        except subprocess.CalledProcessError:
            self._log("字幕烧录失败(可能缺少 libass/字体),改用软字幕 …")
        try:
            self._run(
                ["-i", str(video), "-i", str(srt),
                 "-map", "0", "-map", "1:0", "-c", "copy", "-c:s", "mov_text",
                 "-metadata:s:s:0", "language=zho", str(out_path)]
            )
            return True
        except subprocess.CalledProcessError:
            self._log("软字幕封装也失败,字幕文件将随成片一并保存。")
            return False

    # ---------------- 背景音乐 ----------------

    def add_bgm(self, video: Path, bgm: Path, duration: float, out_path: Path) -> Path:
        """混入背景音乐:循环补齐时长、压低音量、结尾淡出。

        优先与原片音轨混音(保留 Kling 原生音效);若成片无音轨则仅用
        背景音乐。任一步失败都不影响成片——直接沿用无音乐版本。
        """
        fade_start = max(0, duration - 2)
        bgm_filter = (
            f"[1:a]volume={self._bgm_volume},"
            f"afade=t=in:d=1,afade=t=out:st={fade_start:.3f}:d=2[bg]"
        )
        common = [
            "-i", str(video), "-stream_loop", "-1", "-i", str(bgm),
        ]
        tail = ["-map", "0:v", "-c:v", "copy", "-c:a", "aac",
                "-t", f"{duration:.3f}", str(out_path)]

        # 与 mix_narration 同理:先探测音轨,免去一次注定失败的尝试
        has_audio = self._has_audio(video)
        try:
            self._log(f"混入背景音乐: {bgm.name}")
            if has_audio is not False:
                try:
                    # 与原片音轨混音
                    self._run(
                        [*common,
                         "-filter_complex", f"{bgm_filter};[0:a][bg]amix=inputs=2:duration=first[a]",
                         "-map", "[a]", *tail]
                    )
                    return out_path
                except subprocess.CalledProcessError:
                    if has_audio:
                        raise
            # 原片无音轨,仅用背景音乐
            self._run(
                [*common, "-filter_complex", bgm_filter, "-map", "[bg]", *tail]
            )
            return out_path
        except subprocess.CalledProcessError:
            self._log("背景音乐混入失败,输出无音乐版本。")
            shutil.copyfile(video, out_path)
            return out_path

    # ---------------- 内部 ----------------

    def _run(self, args: list[str], cwd: Path | None = None) -> None:
        cmd = [self._ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *args]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            creationflags=_CREATIONFLAGS,
            cwd=str(cwd) if cwd else None,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
