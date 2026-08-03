"""生成流水线:一句话描述 → 分镜脚本 → 逐镜头生成 → 拼接成片。"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable

from .assembler import Assembler
from .config import Config
from .director import Director, Storyboard

LogFn = Callable[[str], None]


def _safe_name(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", title).strip("_")
    return name[:40] or "video"


class Pipeline:
    def __init__(self, config: Config, log: LogFn):
        self._config = config
        self._log = log

    def run(self, description: str) -> Path:
        """执行完整流程,返回成片路径。"""
        from .kling import KlingGenerator

        config = self._config
        log = self._log

        assembler = Assembler(config, log)
        if not assembler.check_ffmpeg():
            raise RuntimeError(
                f"未找到 ffmpeg(配置路径: {config['ffmpeg']['path']})。"
                "请安装 ffmpeg 并加入 PATH,或在 config.yaml 的 ffmpeg.path 中填写完整路径。"
            )

        # 1. Claude 编剧 + 导演
        log("① Claude 正在撰写分镜脚本 …")
        storyboard: Storyboard = Director(config).write_storyboard(description)
        log(f"《{storyboard.title}》—— {storyboard.logline}")
        log(f"共 {len(storyboard.shots)} 个镜头,预计总时长约 {storyboard.total_duration} 秒:")
        for shot in storyboard.shots:
            log(f"  {shot.index}. {shot.title}({shot.duration}s)")

        # 2. Kling 逐镜头生成
        run_dir = config.output_dir / f"{time.strftime('%Y%m%d_%H%M%S')}_{_safe_name(storyboard.title)}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "storyboard.txt").write_text(
            self._format_storyboard(description, storyboard), encoding="utf-8"
        )

        log("② Kling 正在生成各镜头(每个镜头可能需要几分钟)…")
        generator = KlingGenerator(config, log)
        clips: list[Path] = []
        for shot in storyboard.shots:
            clip_path = run_dir / f"shot_{shot.index:02d}.mp4"
            generator.generate_clip(shot, clip_path)
            clips.append(clip_path)
            log(f"  镜头 {shot.index}/{len(storyboard.shots)} 完成 ✓")

        # 3. ffmpeg 拼接
        log("③ 正在拼接成片 …")
        final_path = run_dir / f"{_safe_name(storyboard.title)}.mp4"
        assembler.concat(clips, final_path)
        log(f"✅ 完成!成片已保存: {final_path}")
        return final_path

    @staticmethod
    def _format_storyboard(description: str, storyboard: Storyboard) -> str:
        lines = [
            f"创意: {description}",
            f"标题: {storyboard.title}",
            f"概要: {storyboard.logline}",
            "",
        ]
        for shot in storyboard.shots:
            lines += [
                f"—— 镜头 {shot.index}: {shot.title}({shot.duration}s)",
                f"prompt: {shot.prompt}",
                f"negative: {shot.negative_prompt}",
                "",
            ]
        return "\n".join(lines)
