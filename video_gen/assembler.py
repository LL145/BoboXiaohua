"""用 ffmpeg 将各镜头片段拼接为最终成片,并自动混入背景音乐(如有)。"""

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
        self._ffmpeg = str(config["ffmpeg"]["path"])
        self._bgm_volume = float(config["video"]["bgm_volume"])
        self._log = log

    def check_ffmpeg(self) -> bool:
        return shutil.which(self._ffmpeg) is not None or Path(self._ffmpeg).exists()

    # ---------------- 拼接 ----------------

    def concat(self, clips: list[Path], out_path: Path) -> Path:
        """拼接片段。优先无损 copy,失败则回退到重编码。"""
        if len(clips) == 1:
            shutil.copyfile(clips[0], out_path)
            return out_path

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

    # ---------------- 背景音乐 ----------------

    def add_bgm(self, video: Path, bgm: Path, duration: int, out_path: Path) -> Path:
        """混入背景音乐:循环补齐时长、压低音量、结尾淡出。

        优先与原片音轨混音;若片段本身无音轨(Kling 文生视频通常无声),
        回退为仅背景音乐。任一步失败都不影响成片——直接沿用无音乐版本。
        """
        fade_start = max(0, duration - 2)
        bgm_filter = (
            f"[1:a]volume={self._bgm_volume},"
            f"afade=t=in:d=1,afade=t=out:st={fade_start}:d=2[bg]"
        )
        common = [
            "-i", str(video), "-stream_loop", "-1", "-i", str(bgm),
        ]
        tail = ["-map", "0:v", "-c:v", "copy", "-c:a", "aac",
                "-t", str(duration), str(out_path)]

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
