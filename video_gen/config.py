"""读取程序目录下的 config.yaml,并把界面上的设置写回该文件。"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
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

ALL_ASPECTS = ("16:9", "9:16", "1:1", "3:4", "4:3")


@dataclass(frozen=True)
class EngineSpec:
    """一个视频引擎的能力描述:director/pipeline/gui/generator 各处按此查表,
    引擎之间的差异只登记在这里,不散落成 if engine == … 分支。"""

    name: str                        # 展示名(界面下拉框与日志)
    resolutions: tuple[str, ...]     # 可选分辨率;空元组表示端点不接受分辨率参数
    group_seconds: tuple[int, int]   # 单个镜头组一次生成的时长下限/上限(秒)
    prompt_language: str             # 分镜 prompt 语言:"中文" / "英文"
    native_aspects: tuple[str, ...]  # 原生画幅;其余画幅按相邻画幅生成后成片居中裁剪
    max_reference_images: int        # 参考图张数上限
    reference_notes: bool = True     # 各图的用途说明是否生效(Kling 仅作多角度参考)
    audio_always_on: bool = False    # 原生音频始终开启、不受 video.generate_audio 控制(Gemini)


# 视频引擎(全部经 fal.ai):配置值 → 能力表;顺序即界面下拉框顺序,首项为默认。
# 字节系 Seedance 对中文提示词有官方一等支持,Kling / Gemini 用英文
ENGINES: dict[str, EngineSpec] = {
    "gemini": EngineSpec(
        name="Gemini Omni Flash 1.1",
        resolutions=("360p", "720p", "1080p", "4k"),
        group_seconds=(3, 10),
        prompt_language="英文",
        native_aspects=("16:9", "9:16"),
        max_reference_images=10,
        audio_always_on=True,
    ),
    "seedance25": EngineSpec(
        name="Seedance 2.5",
        resolutions=("480p", "720p", "1080p"),
        group_seconds=(4, 30),
        prompt_language="中文",
        native_aspects=ALL_ASPECTS,
        max_reference_images=30,
    ),
    "seedance": EngineSpec(
        name="Seedance 2.0",
        resolutions=("480p", "720p", "1080p", "4k"),
        group_seconds=(4, 15),
        prompt_language="中文",
        native_aspects=ALL_ASPECTS,
        max_reference_images=9,
    ),
    "kling": EngineSpec(
        name="Kling 3",
        resolutions=(),
        group_seconds=(3, 15),
        prompt_language="英文",
        native_aspects=("16:9", "9:16", "1:1"),
        max_reference_images=4,
        reference_notes=False,
    ),
}
DEFAULT_ENGINE = next(iter(ENGINES))
# 非原生画幅按相邻原生画幅生成,成片时居中裁剪出目标画幅
_GENERATION_ASPECT = {"3:4": "9:16", "4:3": "16:9", "1:1": "16:9"}


def generation_aspect(engine: str, aspect: str) -> str:
    """引擎实际使用的生成画幅;与目标画幅不同时成片阶段会居中裁剪。"""
    aspect = str(aspect).strip()
    spec = ENGINES.get(engine)
    if spec is None or aspect in spec.native_aspects:
        return aspect
    return _GENERATION_ASPECT.get(aspect, "16:9")
# 编剧模型的常用候选(OpenRouter 模型 ID),界面下拉框可直接选、也可手填任意模型
LLM_MODEL_PRESETS: tuple[str, ...] = (
    "qwen/qwen3.8-max",
    "z-ai/glm-5.3",
    "anthropic/claude-fable-5",
    "openai/gpt-5.2",
    "google/gemini-3-pro",
    "deepseek/deepseek-r2",
)
# 角色台词的默认语言(写进导演系统提示词;用户创意明确要求其他语言时除外)
DEFAULT_DIALOGUE_LANGUAGE = "中文普通话"
# 旁白方式:native 视频模型原生配画外音 / tts Edge TTS 合成后混入 / off 不要旁白
NARRATION_MODES: tuple[str, ...] = ("native", "tts", "off")
DEFAULT_NARRATION_MODE = NARRATION_MODES[0]

_DEFAULTS: dict[str, Any] = {
    "openrouter_api_key": "",
    "fal_api_key": "",
    "llm": {
        "model": LLM_MODEL_PRESETS[0],
        "reasoning_effort": "medium",
        "max_tokens": 32000,
    },
    "gemini": {
        "text_endpoint": "google/gemini-omni-flash/v1.1/text-to-video",
        "reference_endpoint": "google/gemini-omni-flash/v1.1/reference-to-video",
        "resolution": "720p",
        # 费用预估单价(美元/秒),按分辨率;fal 实时定价为准
        "price_per_second": {"360p": 0.03, "720p": 0.10, "1080p": 0.15, "4k": 0.30},
    },
    "seedance25": {
        "text_endpoint": "bytedance/seedance-2.5/text-to-video",
        "reference_endpoint": "bytedance/seedance-2.5/reference-to-video",
        "resolution": "720p",
        "seed": -1,
        "price_per_second": {"480p": 0.2205, "720p": 0.473, "1080p": 1.164},
    },
    "seedance": {
        "text_endpoint": "bytedance/seedance-2.0/text-to-video",
        "reference_endpoint": "bytedance/seedance-2.0/reference-to-video",
        "resolution": "720p",
        # 按 token 计费(宽×高×秒×24/1024,$0.014/千 token;4k 为 $0.008/千 token)折算
        "price_per_second": {"480p": 0.135, "720p": 0.3034, "1080p": 0.68, "4k": 1.56},
    },
    "kling": {
        "text_endpoint": "fal-ai/kling-video/v3/pro/text-to-video",
        "reference_endpoint": "fal-ai/kling-video/o3/pro/reference-to-video",
        # 端点无分辨率参数;开/关原生音效两档单价
        "price_per_second": 0.168,
        "price_per_second_no_audio": 0.112,
    },
    "image": {"endpoint": "fal-ai/nano-banana-2"},
    "narration": {
        # 旁白方式:native 由视频模型原生配画外音(默认)/ tts 由 Edge TTS 合成 / off 不要旁白
        "mode": "native",
        # 旧配置兼容:enabled: false 等同 mode: off
        "enabled": True,
        "voice": "zh-CN-XiaoxiaoNeural",
        "volume": 1.0,
        "subtitles": True,
        # Edge TTS 失败时的付费后备(fal.ai,约 $0.10/千字);留空则不启用
        "fallback_endpoint": "fal-ai/minimax/speech-02-hd",
        "fallback_voice": "Chinese (Mandarin)_Warm_Girl",
    },
    "video": {
        "engine": DEFAULT_ENGINE,
        "aspect_ratio": "16:9",
        "dialogue_language": DEFAULT_DIALOGUE_LANGUAGE,
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


def default_resolution(engine: str) -> str:
    """引擎的默认分辨率(端点无分辨率参数时为空串)。"""
    return str(_DEFAULTS.get(engine, {}).get("resolution", ""))


# 旧版配置把这些引擎无关参数放在 kling 节;读取时迁移到 video 节保持兼容
_LEGACY_KLING_KEYS = (
    "aspect_ratio", "generate_audio", "clip_duration",
    "max_retries", "concurrency", "shot_timeout",
)
# 已下线的非 fal.ai 引擎(火山方舟直连 / 即梦):旧配置选了它们时改回默认引擎
_RETIRED_ENGINES = ("ark", "jimeng")


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

    def __setitem__(self, key: str, value: Any) -> None:
        """界面上的选择覆盖配置文件里的值(仅影响本次生成)。"""
        self._data[key] = value

    @property
    def openrouter_api_key(self) -> str:
        return (self._data.get("openrouter_api_key") or "").strip()

    @property
    def fal_api_key(self) -> str:
        return (self._data.get("fal_api_key") or "").strip()

    @property
    def engine(self) -> str:
        """视频生成引擎的配置值(ENGINES 的键),均经 fal.ai;留空取默认引擎。"""
        return str(self._data["video"].get("engine") or DEFAULT_ENGINE).strip().lower()

    @property
    def engine_spec(self) -> EngineSpec:
        """当前引擎的能力表;未知引擎(validate 会报错)按默认引擎处理。"""
        return ENGINES.get(self.engine) or ENGINES[DEFAULT_ENGINE]

    @property
    def engine_name(self) -> str:
        """引擎的展示名(日志与界面用)。"""
        return self.engine_spec.name

    @property
    def engine_section(self) -> dict[str, Any]:
        """当前引擎的专属配置节(端点、分辨率、单价等)。"""
        return self._data.get(self.engine) or self._data[DEFAULT_ENGINE]

    @property
    def price_per_second(self) -> float:
        """当前引擎、当前分辨率(与音效开关)下的费用预估单价(美元/秒);
        配置为 0 或缺失时返回 0(不显示预估)。

        price_per_second 可为单个数字,也可为「分辨率 → 单价」映射(旧配置的
        单个数字仍兼容);Kling 端点无分辨率参数,关音效时取
        price_per_second_no_audio。
        """
        section = self.engine_section
        price = section.get("price_per_second", 0)
        if isinstance(price, dict):
            resolution = str(section.get("resolution") or "")
            price = price.get(resolution)
            if price is None:  # 未登记的分辨率:取最贵一档,宁可高估
                price = max((float(v) for v in section["price_per_second"].values()), default=0)
        elif not bool(self._data["video"].get("generate_audio", True)):
            price = section.get("price_per_second_no_audio", price)
        try:
            return max(0.0, float(price or 0))
        except (TypeError, ValueError):
            return 0.0

    @property
    def narration_mode(self) -> str:
        """配置的旁白方式(native / tts / off);旧配置 enabled: false 视为 off,
        非法值回退默认。是否真能原生配音还取决于引擎音频开关,见 effective_narration_mode。"""
        section = self._data["narration"]
        if not bool(section.get("enabled", True)):
            return "off"
        mode = str(section.get("mode") or "").strip().lower()
        return mode if mode in NARRATION_MODES else DEFAULT_NARRATION_MODE

    @property
    def effective_narration_mode(self) -> str:
        """本次生成实际采用的旁白方式:native 需要引擎原生音频开启
        (Gemini 始终开启;其余引擎关掉 video.generate_audio 时改用 tts)。"""
        mode = self.narration_mode
        if mode == "native" and not self.native_audio_enabled:
            return "tts"
        return mode

    @property
    def native_audio_enabled(self) -> bool:
        """当前引擎本次是否会生成原生音频(音效/台词/画外音)。"""
        return self.engine_spec.audio_always_on or bool(
            self._data["video"].get("generate_audio", True)
        )

    @property
    def dialogue_language(self) -> str:
        """角色台词的默认语言(留空时回退为中文普通话)。"""
        value = str(self._data["video"].get("dialogue_language") or "").strip()
        return value or DEFAULT_DIALOGUE_LANGUAGE

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
            problems.append("缺少 OpenRouter API KEY(在界面「设置」中填入,或编辑 config.yaml)")
        if not self.fal_api_key:
            problems.append("缺少 fal.ai API KEY(在界面「设置」中填入,或编辑 config.yaml)")
        if self.engine not in ENGINES:
            problems.append("video.engine 需为 " + " / ".join(ENGINES) + " 之一")
        else:
            allowed = self.engine_spec.resolutions
            if allowed and str(self.engine_section.get("resolution")) not in allowed:
                problems.append(
                    f"{self.engine}.resolution 需为 {' / '.join(allowed)} 之一"
                )
        if not 3 <= int(self._data["video"]["clip_duration"]) <= 15:
            problems.append("video.clip_duration 需在 3~15 秒之间")
        if str(self._data["video"]["aspect_ratio"]) not in ALL_ASPECTS:
            problems.append("video.aspect_ratio 需为 " + " / ".join(ALL_ASPECTS) + " 之一")
        try:
            int(self._data["seedance25"].get("seed", -1))
        except (TypeError, ValueError):
            problems.append("seedance25.seed 需为整数(-1 表示每次随机)")
        if not 10 <= int(self._data["video"]["target_duration"]) <= 600:
            problems.append("video.target_duration 需在 10~600 秒之间")
        if float(self._data["video"]["transition"]) < 0:
            problems.append("video.transition 不能为负数")
        if not 0 <= float(self._data["narration"]["volume"]) <= 2:
            problems.append("narration.volume 需在 0~2 之间")
        mode = str(self._data["narration"].get("mode") or DEFAULT_NARRATION_MODE).strip().lower()
        if mode not in NARRATION_MODES:
            problems.append("narration.mode 需为 " + " / ".join(NARRATION_MODES) + " 之一")
        for section in ENGINES:
            price = self._data[section].get("price_per_second", 0)
            values = price.values() if isinstance(price, dict) else [price]
            try:
                if any(float(v) < 0 for v in values):
                    raise ValueError
            except (TypeError, ValueError):
                problems.append(
                    f"{section}.price_per_second 需为非负数或「分辨率: 单价」映射"
                    "(设 0 可关闭费用预估)"
                )
        return problems


def _template_text() -> str:
    """打包版内置的 config.yaml 模板内容;没有模板时返回空串。"""
    bundle = _bundle_dir()
    template = bundle / "config.yaml" if bundle else None
    if template is not None and template.exists():
        return template.read_text(encoding="utf-8")
    return ""


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        # 打包版首次运行:从内置模板生成 config.yaml,用户在界面填 KEY 即可
        template = _template_text()
        if template:
            CONFIG_PATH.write_text(template, encoding="utf-8")
        else:
            raise FileNotFoundError(
                f"未找到配置文件: {CONFIG_PATH}\n请在界面「设置」中填入 API KEY 并保存,"
                "程序会自动创建 config.yaml。"
            )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        user_data = yaml.safe_load(f) or {}
    if not isinstance(user_data, dict):
        user_data = {}
    # 兼容旧版配置:kling 节里的引擎无关参数迁移到 video 节
    legacy = user_data.get("kling") or {}
    if isinstance(legacy, dict):
        for key in _LEGACY_KLING_KEYS:
            if key in legacy:
                video_section = user_data.setdefault("video", {}) or {}
                user_data["video"] = video_section
                video_section.setdefault(key, legacy.pop(key))
    # 兼容旧版配置:已下线的方舟直连/即梦引擎改回默认的 fal.ai 引擎
    video_section = user_data.get("video")
    if isinstance(video_section, dict):
        engine = str(video_section.get("engine") or "").strip().lower()
        if engine in _RETIRED_ENGINES:
            video_section["engine"] = DEFAULT_ENGINE
    return Config(_merge(_DEFAULTS, user_data))


# ---------------- 把界面设置写回 config.yaml ----------------

def _yaml_scalar(value: Any) -> str:
    """把 Python 值渲染成单行 YAML 标量(字符串一律加双引号,避免歧义)。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)


def _split_trailing_comment(rest: str) -> tuple[str, str]:
    """把 "值  # 注释" 拆成 (值, 注释),引号内的 # 不算注释。"""
    rest = rest.rstrip()
    stripped = rest.lstrip()
    if stripped[:1] in ('"', "'"):
        quote = stripped[0]
        i = 1
        while i < len(stripped):
            if stripped[i] == "\\" and quote == '"':
                i += 2
                continue
            if stripped[i] == quote:
                break
            i += 1
        tail = stripped[i + 1:]
        match = re.match(r"\s*(#.*)$", tail)
        return stripped[:i + 1], (match.group(1) if match else "")
    match = re.match(r"^(.*?)(?:\s+(#.*))?$", stripped)
    return (match.group(1) if match else stripped), (match.group(2) or "")


def _set_yaml_value(lines: list[str], path: list[str], value: Any) -> list[str]:
    """在 YAML 文本行中改写 path(顶层键或「节.键」)对应的值,其余行原样保留。"""
    rendered = _yaml_scalar(value)
    if len(path) == 1:
        pattern = re.compile(rf"^({re.escape(path[0])}\s*:)(.*)$")
        for i, line in enumerate(lines):
            match = pattern.match(line)
            if match:
                _, comment = _split_trailing_comment(match.group(2))
                lines[i] = f"{path[0]}: {rendered}" + (f"  {comment}" if comment else "")
                return lines
        lines.append(f"{path[0]}: {rendered}")
        return lines

    section, key = path
    section_re = re.compile(rf"^{re.escape(section)}\s*:\s*(#.*)?$")
    key_re = re.compile(rf"^(\s+{re.escape(key)}\s*:)(.*)$")
    start = next((i for i, line in enumerate(lines) if section_re.match(line)), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([f"{section}:", f"  {key}: {rendered}"])
        return lines
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and not lines[i].startswith((" ", "\t", "#")):
            end = i  # 下一个顶层键
            break
    for i in range(start + 1, end):
        match = key_re.match(lines[i])
        if match:
            indent = match.group(1)[: len(match.group(1)) - len(match.group(1).lstrip())]
            _, comment = _split_trailing_comment(match.group(2))
            lines[i] = f"{indent}{key}: {rendered}" + (f"  {comment}" if comment else "")
            return lines
    # 节内没有该键:插到节内最后一个非空行之后
    insert_at = start + 1
    for i in range(start + 1, end):
        if lines[i].strip():
            insert_at = i + 1
    lines.insert(insert_at, f"  {key}: {rendered}")
    return lines


def save_settings(updates: dict[str, Any]) -> None:
    """把界面上的设置写回 config.yaml。

    updates 的键为 "openrouter_api_key" 或 "节.键"(如 "llm.model")。只改写
    对应的行,文件里的其余内容与注释原样保留;若改写结果无法通过 YAML
    校验,则回退为整体重写(会丢失注释,但保证配置可用)。
    """
    text = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else _template_text()
    lines = text.splitlines()
    for dotted, value in updates.items():
        lines = _set_yaml_value(lines, dotted.split(".", 1), value)
    new_text = "\n".join(lines).rstrip("\n") + "\n"

    def _ok(candidate: str) -> bool:
        try:
            data = yaml.safe_load(candidate) or {}
        except yaml.YAMLError:
            return False
        if not isinstance(data, dict):
            return False
        for dotted, value in updates.items():
            node: Any = data
            for part in dotted.split(".", 1):
                node = node.get(part) if isinstance(node, dict) else None
            if node != value:
                return False
        return True

    if not _ok(new_text):
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        for dotted, value in updates.items():
            parts = dotted.split(".", 1)
            if len(parts) == 1:
                data[parts[0]] = value
            else:
                section = data.get(parts[0])
                if not isinstance(section, dict):
                    section = {}
                    data[parts[0]] = section
                section[parts[1]] = value
        new_text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(CONFIG_PATH)
