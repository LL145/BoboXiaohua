"""调用 fal.ai 上的 Kling 模型,把每个镜头生成为视频片段。"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

import requests

from .config import Config
from .director import Shot

LogFn = Callable[[str], None]

# 小于该体积的文件视为无效片段(错误页/截断下载)
_MIN_CLIP_BYTES = 10 * 1024


def clip_is_valid(path: Path) -> bool:
    return path.exists() and path.stat().st_size >= _MIN_CLIP_BYTES


class KlingGenerator:
    def __init__(self, config: Config, log: LogFn):
        self._config = config
        self._log = log
        # fal_client 通过 FAL_KEY 环境变量读取凭证
        os.environ["FAL_KEY"] = config.fal_api_key

    def generate_clip(self, shot: Shot, out_path: Path) -> Path:
        """生成单个镜头并下载到 out_path;已有有效片段时直接复用(断点续传)。"""
        if clip_is_valid(out_path):
            self._log(f"  镜头 {shot.index} 已存在,跳过生成 ↺")
            return out_path

        import fal_client

        kling = self._config["kling"]
        max_retries = int(kling["max_retries"])
        arguments = {
            "prompt": shot.prompt,
            "negative_prompt": shot.negative_prompt,
            "duration": str(shot.duration),
            "aspect_ratio": kling["aspect_ratio"],
        }

        last_error: Exception | None = None
        for attempt in range(1, max_retries + 2):
            try:
                self._log(
                    f"  镜头 {shot.index} 提交 Kling 生成"
                    + (f"(第 {attempt} 次尝试)" if attempt > 1 else "")
                    + " …"
                )
                result = fal_client.subscribe(
                    kling["endpoint"],
                    arguments=arguments,
                    with_logs=False,
                )
                video_url = result["video"]["url"]
                self._download(video_url, out_path)
                if not clip_is_valid(out_path):
                    raise RuntimeError("下载的片段无效(体积过小)")
                return out_path
            except Exception as exc:  # noqa: BLE001 - 逐镜头重试,最终仍会抛出
                last_error = exc
                self._log(f"  镜头 {shot.index} 第 {attempt} 次尝试失败: {exc}")
                if attempt <= max_retries:
                    time.sleep(min(3 * attempt, 15))

        raise RuntimeError(f"镜头 {shot.index} 多次生成失败: {last_error}") from last_error

    def _download(self, url: str, out_path: Path) -> None:
        """先写临时文件再原子改名,避免半截文件被断点续传误认为有效。"""
        tmp_path = out_path.with_suffix(".part")
        with requests.get(url, stream=True, timeout=300) as resp:
            resp.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        tmp_path.replace(out_path)
        self._log(f"  片段已下载 → {out_path.name}")
