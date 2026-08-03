"""生成流水线:一句话描述 → 分镜脚本 → 并行逐镜头生成 → 拼接成片。

稳健性设计:
- 断点续传:同一描述的未完成任务会复用已有分镜脚本和已生成片段,
  失败后再次点击「生成」只补齐缺失的镜头,不重复扣费;
- 并行生成:多个镜头同时提交 Kling,总耗时约等于单个镜头;
- 单镜头独立重试,全部完成后才进入拼接;
- 背景音乐:程序目录 music/ 下有音频文件时自动随机混入,无需任何设置。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from .assembler import Assembler
from .config import Config, app_dir
from .director import Director, Storyboard

LogFn = Callable[[str], None]

_MANIFEST_NAME = "manifest.json"
_BGM_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


def _safe_name(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", title).strip("_")
    return name[:40] or "video"


def _description_key(description: str) -> str:
    return hashlib.sha1(description.strip().encode("utf-8")).hexdigest()[:8]


class Pipeline:
    def __init__(self, config: Config, log: LogFn):
        self._config = config
        self._log = log

    # ---------------- 主流程 ----------------

    def run(self, description: str) -> Path:
        """执行完整流程,返回成片路径。"""
        from .kling import KlingGenerator, clip_is_valid

        config = self._config
        log = self._log

        assembler = Assembler(config, log)
        if not assembler.check_ffmpeg():
            raise RuntimeError(
                f"未找到 ffmpeg(配置路径: {config['ffmpeg']['path']})。"
                "请安装 ffmpeg 并加入 PATH,或在 config.yaml 的 ffmpeg.path 中填写完整路径。"
            )

        # 1. 分镜脚本:优先恢复未完成任务,否则请 LLM 新写
        run_dir, storyboard = self._resume_or_create(description)
        log(f"《{storyboard.title}》—— {storyboard.logline}")
        log(f"共 {len(storyboard.shots)} 个镜头,预计总时长约 {storyboard.total_duration} 秒:")
        for shot in storyboard.shots:
            log(f"  {shot.index}. {shot.title}({shot.duration}s)")

        # 2. Kling 并行生成各镜头
        concurrency = max(1, int(config["kling"]["concurrency"]))
        pending = [
            s for s in storyboard.shots
            if not clip_is_valid(run_dir / f"shot_{s.index:02d}.mp4")
        ]
        done_before = len(storyboard.shots) - len(pending)
        if done_before:
            log(f"② 已有 {done_before} 个镜头可复用,补齐剩余 {len(pending)} 个 …")
        else:
            log(f"② Kling 并行生成 {len(pending)} 个镜头(并发 {concurrency},约需几分钟)…")

        if pending:
            generator = KlingGenerator(config, log)
            errors: list[str] = []
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {
                    pool.submit(
                        generator.generate_clip, shot, run_dir / f"shot_{shot.index:02d}.mp4"
                    ): shot
                    for shot in pending
                }
                finished = done_before
                for future in as_completed(futures):
                    shot = futures[future]
                    try:
                        future.result()
                        finished += 1
                        log(f"  镜头 {shot.index} 完成 ✓({finished}/{len(storyboard.shots)})")
                    except Exception as exc:  # noqa: BLE001
                        errors.append(str(exc))
            if errors:
                raise RuntimeError(
                    f"{len(errors)} 个镜头生成失败:{errors[0]}\n"
                    "已完成的镜头已保存,再次点击「生成」将只补齐失败的镜头。"
                )

        clips = [run_dir / f"shot_{s.index:02d}.mp4" for s in storyboard.shots]

        # 3. ffmpeg 拼接 + 自动背景音乐
        log("③ 正在拼接成片 …")
        final_path = run_dir / f"{_safe_name(storyboard.title)}.mp4"
        bgm = self._pick_bgm()
        if bgm is None:
            assembler.concat(clips, final_path)
        else:
            concat_path = run_dir / "_concat.mp4"
            assembler.concat(clips, concat_path)
            assembler.add_bgm(concat_path, bgm, storyboard.total_duration, final_path)
            concat_path.unlink(missing_ok=True)

        log(f"✅ 完成!成片已保存: {final_path}")
        return final_path

    # ---------------- 断点续传 ----------------

    def _resume_or_create(self, description: str) -> tuple[Path, Storyboard]:
        """同一描述且未产出成片的任务目录 → 恢复;否则新建目录并请 LLM 写分镜。"""
        key = _description_key(description)

        for candidate in sorted(self._config.output_dir.glob(f"*_{key}*"), reverse=True):
            manifest_path = candidate / _MANIFEST_NAME
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("description", "").strip() != description.strip():
                    continue
                storyboard = Storyboard.from_dict(manifest["storyboard"])
            except Exception:  # noqa: BLE001 - 损坏的 manifest 直接忽略
                continue
            final_path = candidate / f"{_safe_name(storyboard.title)}.mp4"
            if final_path.exists():
                continue  # 已完成的任务不复用,重新生成一版
            self._log(f"① 检测到未完成的任务,继续上次进度: {candidate.name}")
            return candidate, storyboard

        self._log("① 导演模型正在撰写分镜脚本 …")
        storyboard = Director(self._config).write_storyboard(description)

        base = f"{time.strftime('%Y%m%d_%H%M%S')}_{_safe_name(storyboard.title)}_{key}"
        run_dir = self._config.output_dir / base
        serial = 2
        while run_dir.exists():  # 避免与旧任务目录同名,导致误复用旧片段
            run_dir = self._config.output_dir / f"{base}_{serial}"
            serial += 1
        run_dir.mkdir(parents=True)
        manifest = {"description": description, "storyboard": storyboard.to_dict()}
        (run_dir / _MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (run_dir / "storyboard.txt").write_text(
            self._format_storyboard(description, storyboard), encoding="utf-8"
        )
        return run_dir, storyboard

    # ---------------- 背景音乐 ----------------

    def _pick_bgm(self) -> Path | None:
        music_dir = app_dir() / "music"
        if not music_dir.is_dir():
            return None
        tracks = [p for p in music_dir.iterdir() if p.suffix.lower() in _BGM_EXTS]
        return random.choice(tracks) if tracks else None

    # ---------------- 其他 ----------------

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
