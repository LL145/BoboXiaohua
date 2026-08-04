"""AI 短视频生成器入口。

用法:
    python main.py                     启动桌面界面
    python main.py "描述"              命令行模式,直接生成
    python main.py "描述" 主角图片路径  命令行模式,并用上传的图片锁定主角外观
"""

from __future__ import annotations

import sys
from pathlib import Path

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


def main() -> None:
    if len(sys.argv) > 1:
        # 命令行模式:python main.py "一句话描述" [主角图片路径]
        from video_gen.config import load_config
        from video_gen.pipeline import Pipeline

        config = load_config()
        problems = config.validate()
        if problems:
            print("配置错误:\n" + "\n".join(problems))
            sys.exit(1)
        args = sys.argv[1:]
        reference_image = None
        if len(args) >= 2:
            candidate = Path(args[-1])
            if candidate.suffix.lower() in _IMAGE_EXTS and candidate.is_file():
                reference_image = candidate
                args = args[:-1]
        Pipeline(config, print).run(" ".join(args), reference_image=reference_image)
    else:
        from video_gen.gui import main as gui_main

        gui_main()


if __name__ == "__main__":
    main()
