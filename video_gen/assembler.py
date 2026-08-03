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

from .config import Config

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

    def concat(self, clips: list[Path], out_path: Path) -> Path:
        """拼接片段:优先交叉溶解转场,失败回退普通拼接。"""
        if len(clips) == 1:
            shutil.copyfile(clips[0], out_path)
            return out_path

        if self._transition > 0:
            try:
                if self._concat_with_transitions(clips, out_path):
                    return out_path
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
        return out_path

    def _concat_with_transitions(self, clips: list[Path], out_path: Path) -> bool:
        """xfade 交叉溶解 + 首尾淡入淡出;音轨齐全时同步 acrossfade。

        返回 False 表示前置条件不满足(如 ffprobe 缺失),由调用方回退。
        """
        t = self._transition
        durations = [self.probe_duration(c) for c in clips]
        if any(d is None or d <= 2 * t for d in durations):
            return False
        with_audio = all(self._has_audio(c) for c in clips)

        inputs: list[str] = []
        for clip in clips:
            inputs += ["-i", str(clip)]

        parts: list[str] = []
        prev, offset = "[0:v]", 0.0
        for i in range(1, len(clips)):
            offset += durations[i - 1] - t
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
        return True

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

        try:
            self._log(f"混入背景音乐: {bgm.name}")
            try:
                # 与原片音轨混音
                self._run(
                    [*common,
                     "-filter_complex", f"{bgm_filter};[0:a][bg]amix=inputs=2:duration=first[a]",
                     "-map", "[a]", *tail]
                )
            except subprocess.CalledProcessError:
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

    def _run(self, args: list[str]) -> None:
        cmd = [self._ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *args]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            creationflags=_CREATIONFLAGS,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
