"""AI 短视频生成器入口。

用法:
    python main.py            启动桌面界面
    python main.py "描述"     命令行模式,直接生成
"""

from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) > 1:
        # 命令行模式:python main.py "一句话描述"
        from video_gen.config import load_config
        from video_gen.pipeline import Pipeline

        config = load_config()
        problems = config.validate()
        if problems:
            print("配置错误:\n" + "\n".join(problems))
            sys.exit(1)
        Pipeline(config, print).run(" ".join(sys.argv[1:]))
    else:
        from video_gen.gui import main as gui_main

        gui_main()


if __name__ == "__main__":
    main()
