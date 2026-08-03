"""读取程序目录下的 config.yaml。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml


def app_dir() -> Path:
    """程序所在目录(兼容 PyInstaller 打包后的 exe)。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH = app_dir() / "config.yaml"

_DEFAULTS: dict[str, Any] = {
    "openrouter_api_key": "",
    "fal_api_key": "",
    "llm": {
        "model": "anthropic/claude-fable-5",
        "reasoning_effort": "high",
        "max_tokens": 32000,
    },
    "kling": {
        "text_endpoint": "fal-ai/kling-video/v3/pro/text-to-video",
        "reference_endpoint": "fal-ai/kling-video/o3/pro/reference-to-video",
        "clip_duration": 10,
        "aspect_ratio": "16:9",
        "generate_audio": True,
        "max_retries": 2,
        "concurrency": 3,
        "shot_timeout": 1500,
    },
    "image": {"endpoint": "fal-ai/nano-banana-2"},
    "video": {
        "target_duration": 60,
        "output_dir": "output",
        "transition": 0.5,
        "bgm_volume": 0.22,
    },
    "ffmpeg": {"path": "ffmpeg"},
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            out[key] = _merge(base[key], value)
        elif value is not None:
            out[key] = value
    return out


class Config:
    def __init__(self, data: dict[str, Any]):
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    @property
    def openrouter_api_key(self) -> str:
        return (self._data.get("openrouter_api_key") or "").strip()

    @property
    def fal_api_key(self) -> str:
        return (self._data.get("fal_api_key") or "").strip()

    @property
    def output_dir(self) -> Path:
        path = Path(self._data["video"]["output_dir"])
        if not path.is_absolute():
            path = app_dir() / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    def validate(self) -> list[str]:
        """返回配置问题列表,为空表示可用。"""
        problems = []
        if not self.openrouter_api_key:
            problems.append("config.yaml 中缺少 openrouter_api_key")
        if not self.fal_api_key:
            problems.append("config.yaml 中缺少 fal_api_key")
        if not 3 <= int(self._data["kling"]["clip_duration"]) <= 15:
            problems.append("kling.clip_duration 需在 3~15 秒之间")
        if float(self._data["video"]["transition"]) < 0:
            problems.append("video.transition 不能为负数")
        return problems


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"未找到配置文件: {CONFIG_PATH}\n请在程序目录下创建 config.yaml 并填入 API KEY。"
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        user_data = yaml.safe_load(f) or {}
    return Config(_merge(_DEFAULTS, user_data))
