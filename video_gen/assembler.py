"""用 ffmpeg 将各镜头片段拼接为最终成片。"""

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
        self._log = log

    def check_ffmpeg(self) -> bool:
        return shutil.which(self._ffmpeg) is not None or Path(self._ffmpeg).exists()

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

    def _run(self, args: list[str]) -> None:
        cmd = [self._ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *args]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            creationflags=_CREATIONFLAGS,
        )
        if result.returncode != 0:
            self._log(f"ffmpeg 出错: {result.stderr.strip()[:500]}")
            raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
