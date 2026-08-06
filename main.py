"""AI 短视频生成器入口。

用法:
    python main.py                     启动桌面界面
    python main.py "描述"              命令行模式,直接生成
    python main.py "描述" 图片路径 ...  命令行模式,附一张或多张参考图;
                                       可用 路径=用途 注明每张图的用途,
                                       例如 cat.jpg=主角正面 side.jpg=主角侧面
"""

from __future__ import annotations

import sys
from pathlib import Path

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


def _pop_reference_images(args: list[str]) -> tuple[list[str], list[tuple[Path, str]]]:
    """从参数尾部取出连续的参考图(`路径` 或 `路径=用途`),返回 (剩余参数, 参考图)。"""
    references: list[tuple[Path, str]] = []
    while args:
        raw, _, note = args[-1].partition("=")
        candidate = Path(raw)
        if candidate.suffix.lower() not in _IMAGE_EXTS or not candidate.is_file():
            break
        references.append((candidate, note.strip()))
        args = args[:-1]
    references.reverse()
    return args, references


def main() -> None:
    if len(sys.argv) > 1:
        # 命令行模式:python main.py "一句话描述" [图片路径[=用途]] ...
        from video_gen.config import load_config
        from video_gen.pipeline import Pipeline

        config = load_config()
        problems = config.validate()
        if problems:
            print("配置错误:\n" + "\n".join(problems))
            sys.exit(1)
        args, reference_images = _pop_reference_images(sys.argv[1:])
        Pipeline(config, print).run(" ".join(args), reference_images=reference_images)
    else:
        from video_gen.gui import main as gui_main

        gui_main()


if __name__ == "__main__":
    main()
