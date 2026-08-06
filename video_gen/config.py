"""读取程序目录下的 config.yaml。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import yaml


def app_dir() -> Path:
    """程序所在目录(兼容 PyInstaller 打包后的程序)。"""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        # macOS .app 包内可执行文件位于 xx.app/Contents/MacOS,
        # 配置与输出放到 .app 旁边,便于用户找到
        if sys.platform == "darwin" and exe_dir.parts[-2:] == ("Contents", "MacOS"):
            return exe_dir.parent.parent.parent
        return exe_dir
    return Path(__file__).resolve().parent.parent


def _bundle_dir() -> Path | None:
    """PyInstaller 解包目录(打包时通过 --add-data/--add-binary 放入的资源)。"""
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else None


def bundled_ffmpeg() -> str | None:
    """查找随程序分发的 ffmpeg:打包资源目录或程序目录下的 ffmpeg/ 子目录。"""
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    roots = [_bundle_dir(), app_dir()]
    for root in roots:
        if root is None:
            continue
        candidate = root / "ffmpeg" / exe
        if candidate.exists():
            return str(candidate)
    return None


def bundled_font_dir() -> Path | None:
    """查找随程序分发的字幕字体目录(fonts/,内含 Noto Sans SC)。"""
    for root in (_bundle_dir(), app_dir()):
        if root is None:
            continue
        candidate = root / "fonts"
        if candidate.is_dir() and any(
            p.suffix.lower() in (".ttf", ".otf") for p in candidate.iterdir()
        ):
            return candidate
    return None


CONFIG_PATH = app_dir() / "config.yaml"

_DEFAULTS: dict[str, Any] = {
    "openrouter_api_key": "",
    "fal_api_key": "",
    "ark_api_key": "",
    "jimeng_access_key": "",
    "jimeng_secret_key": "",
    "llm": {
        "model": "qwen/qwen3.8-max",
        "reasoning_effort": "high",
        "max_tokens": 32000,
    },
    "seedance": {
        "text_endpoint": "bytedance/seedance-2.0/text-to-video",
        "reference_endpoint": "bytedance/seedance-2.0/reference-to-video",
        "resolution": "720p",
        "price_per_second": 0.3034,
    },
    "seedance25": {
        "model": "doubao-seedance-2-5-260628",
        "api_base": "https://ark.cn-beijing.volces.com/api/v3",
        "resolution": "720p",
        "seed": -1,
        "price_per_second": 0.21,
    },
    "kling": {
        "text_endpoint": "fal-ai/kling-video/v3/pro/text-to-video",
        "reference_endpoint": "fal-ai/kling-video/o3/pro/reference-to-video",
        "price_per_second": 0.168,
    },
    "jimeng": {
        "req_key": "jimeng_ti2v_v30_pro",
        "host": "visual.volcengineapi.com",
        "region": "cn-north-1",
        "seed": -1,
        "price_per_second": 0.04,
    },
    "image": {"endpoint": "fal-ai/nano-banana-2"},
    "narration": {
        "enabled": True,
        "voice": "zh-CN-XiaoxiaoNeural",
        "volume": 1.0,
        "subtitles": True,
    },
    "video": {
        "engine": "seedance25",
        "aspect_ratio": "16:9",
        "generate_audio": True,
        "clip_duration": 10,
        "max_retries": 2,
        "concurrency": 3,
        "shot_timeout": 1500,
        "target_duration": 60,
        "output_dir": "output",
        "transition": 0.5,
        "bgm_volume": 0.22,
    },
    "ffmpeg": {"path": "ffmpeg"},
}

# 旧版配置把这些引擎无关参数放在 kling 节;读取时迁移到 video 节保持兼容
_LEGACY_KLING_KEYS = (
    "aspect_ratio", "generate_audio", "clip_duration",
    "max_retries", "concurrency", "shot_timeout",
)


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
    def ark_api_key(self) -> str:
        """火山方舟(Volcengine Ark)API KEY,Seedance 2.5 引擎使用。"""
        return (self._data.get("ark_api_key") or "").strip()

    @property
    def jimeng_access_key(self) -> str:
        """火山引擎 Access Key ID,即梦引擎的 AK/SK 签名鉴权使用。"""
        return (self._data.get("jimeng_access_key") or "").strip()

    @property
    def jimeng_secret_key(self) -> str:
        """火山引擎 Secret Access Key,即梦引擎的 AK/SK 签名鉴权使用。"""
        return (self._data.get("jimeng_secret_key") or "").strip()

    @property
    def engine(self) -> str:
        """视频生成引擎:seedance25(默认)、seedance、kling 或 jimeng。"""
        return str(self._data["video"].get("engine") or "seedance25").strip().lower()

    @property
    def engine_name(self) -> str:
        """引擎的展示名(日志用)。"""
        return {
            "seedance": "Seedance 2.0",
            "seedance25": "Seedance 2.5",
            "jimeng": "即梦 3.0 Pro",
        }.get(self.engine, "Kling")

    @property
    def engine_section(self) -> dict[str, Any]:
        """当前引擎的专属配置节(端点、单价等)。"""
        section = (
            self.engine if self.engine in ("seedance", "seedance25", "jimeng")
            else "kling"
        )
        return self._data[section]

    @property
    def ffmpeg_path(self) -> str:
        """ffmpeg 可执行文件:用户显式配置优先,否则优先随程序分发的版本。"""
        configured = str(self._data["ffmpeg"]["path"]).strip()
        if configured in ("", "ffmpeg", "ffmpeg.exe"):
            bundled = bundled_ffmpeg()
            if bundled:
                return bundled
        return configured or "ffmpeg"

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
        if self.engine == "seedance25":
            # Seedance 2.5 经火山方舟官方 API 生成,需要方舟 KEY;
            # fal KEY 仅用于自动文生主角参考图,缺失时自动降级,不拦截
            if not self.ark_api_key:
                problems.append(
                    "config.yaml 中缺少 ark_api_key(默认引擎 Seedance 2.5 需要"
                    "火山方舟 API KEY;若想改用 fal.ai,把 video.engine 设为"
                    " seedance 或 kling)"
                )
        elif self.engine == "jimeng":
            # 即梦经火山引擎视觉智能 API 生成,用 AK/SK 签名鉴权;
            # 其余 KEY(fal/方舟)均不需要
            if not self.jimeng_access_key or not self.jimeng_secret_key:
                problems.append(
                    "config.yaml 中缺少 jimeng_access_key / jimeng_secret_key"
                    "(即梦引擎需要火山引擎的 AK/SK,在火山引擎控制台"
                    "「访问控制-密钥管理」中获取)"
                )
        elif not self.fal_api_key:
            problems.append("config.yaml 中缺少 fal_api_key")
        if self.engine not in ("seedance", "seedance25", "kling", "jimeng"):
            problems.append(
                "video.engine 需为 seedance / seedance25 / kling / jimeng 之一"
            )
        if not 3 <= int(self._data["video"]["clip_duration"]) <= 15:
            problems.append("video.clip_duration 需在 3~15 秒之间")
        if str(self._data["video"]["aspect_ratio"]) not in (
            "16:9", "9:16", "1:1", "3:4", "4:3"
        ):
            problems.append("video.aspect_ratio 需为 16:9 / 9:16 / 1:1 / 3:4 / 4:3 之一")
        if str(self._data["seedance"]["resolution"]) not in (
            "480p", "720p", "1080p", "4k"
        ):
            problems.append("seedance.resolution 需为 480p / 720p / 1080p / 4k 之一")
        if str(self._data["seedance25"]["resolution"]) not in (
            "480p", "720p", "1080p", "2k", "4k"
        ):
            problems.append(
                "seedance25.resolution 需为 480p / 720p / 1080p / 2k / 4k 之一"
            )
        try:
            int(self._data["seedance25"].get("seed", -1))
        except (TypeError, ValueError):
            problems.append("seedance25.seed 需为整数(-1 表示每次随机)")
        try:
            int(self._data["jimeng"].get("seed", -1))
        except (TypeError, ValueError):
            problems.append("jimeng.seed 需为整数(-1 表示每次随机)")
        if not 10 <= int(self._data["video"]["target_duration"]) <= 600:
            problems.append("video.target_duration 需在 10~600 秒之间")
        if float(self._data["video"]["transition"]) < 0:
            problems.append("video.transition 不能为负数")
        if not 0 <= float(self._data["narration"]["volume"]) <= 2:
            problems.append("narration.volume 需在 0~2 之间")
        for section in ("seedance", "seedance25", "kling", "jimeng"):
            if float(self._data[section]["price_per_second"]) < 0:
                problems.append(
                    f"{section}.price_per_second 不能为负数(设 0 可关闭费用预估)"
                )
        return problems


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        # 打包版首次运行:从内置模板生成 config.yaml,用户只需填 KEY
        bundle = _bundle_dir()
        template = bundle / "config.yaml" if bundle else None
        if template is not None and template.exists():
            shutil.copyfile(template, CONFIG_PATH)
        else:
            raise FileNotFoundError(
                f"未找到配置文件: {CONFIG_PATH}\n请在程序目录下创建 config.yaml 并填入 API KEY。"
            )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        user_data = yaml.safe_load(f) or {}
    # 兼容旧版配置:kling 节里的引擎无关参数迁移到 video 节
    legacy = user_data.get("kling") or {}
    if isinstance(legacy, dict):
        for key in _LEGACY_KLING_KEYS:
            if key in legacy:
                video_section = user_data.setdefault("video", {}) or {}
                user_data["video"] = video_section
                video_section.setdefault(key, legacy.pop(key))
    return Config(_merge(_DEFAULTS, user_data))
