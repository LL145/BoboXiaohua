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


class KlingGenerator:
    def __init__(self, config: Config, log: LogFn):
        self._config = config
        self._log = log
        # fal_client 通过 FAL_KEY 环境变量读取凭证
        os.environ["FAL_KEY"] = config.fal_api_key

    def generate_clip(self, shot: Shot, out_path: Path) -> Path:
        """生成单个镜头,下载到 out_path,失败时按配置重试。"""
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
                return out_path
            except Exception as exc:  # noqa: BLE001 - 逐镜头重试,最终仍会抛出
                last_error = exc
                self._log(f"  镜头 {shot.index} 生成失败: {exc}")
                if attempt <= max_retries:
                    time.sleep(3)

        raise RuntimeError(f"镜头 {shot.index} 多次生成失败") from last_error

    def _download(self, url: str, out_path: Path) -> None:
        self._log(f"  下载片段 → {out_path.name}")
        with requests.get(url, stream=True, timeout=300) as resp:
            resp.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
