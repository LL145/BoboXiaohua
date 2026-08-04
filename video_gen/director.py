"""LLM 担任编剧 + 导演:把一句话描述扩写成完整的分镜脚本。

通过 OpenRouter(OpenAI 兼容接口)调用,可在 config.yaml 中切换任意模型;
默认使用 anthropic/claude-fable-5(reasoning effort: high)。
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

from .config import Config

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 524}

# Kling 3 的时长约束(秒):一个镜头组一次生成,总长 3~15;
# 组内单个分镜(multi_prompt 元素)最短可到 1 秒
_MIN_GROUP_SECONDS = 3
_MAX_GROUP_SECONDS = 15
_MIN_CUT_SECONDS = 1
_MAX_CUTS_PER_GROUP = 6  # Kling multi_prompt 上限
# Kling 对 multi_prompt 单条分镜提示词有 512 字符硬上限(超长直接 422 拒绝),
# 要求模型控制在 450 以内留出余量;kling.py 提交前还会做最终钳制兜底
_MAX_PROMPT_CHARS = 450

# 用户上传主角参考图时,随创意一起发给导演模型的图片格式
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
}


def _encode_image(path: Path) -> str | None:
    """把本地图片编码为 data URL,作为多模态消息发给导演模型;失败返回 None。"""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    mime = _IMAGE_MIME.get(Path(path).suffix.lower(), "image/png")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _clamp_duration(value, fallback: int, minimum: int = _MIN_GROUP_SECONDS) -> int:
    """把模型给出的时长钳制到 Kling 支持的范围;缺失/非法时用回退值。"""
    try:
        return max(minimum, min(_MAX_GROUP_SECONDS, int(value)))
    except (TypeError, ValueError):
        return fallback


@dataclass
class Cut:
    """镜头组内的一个分镜(Kling multi_prompt 的一个元素)。"""
    prompt: str         # 交给 Kling 的英文提示词
    duration: int       # 秒,1~15


@dataclass
class Shot:
    """一个镜头组:一次 Kling 调用连续生成组内全部分镜,组内衔接由模型原生保证。"""
    index: int
    title: str          # 镜头组中文名,用于界面展示
    negative_prompt: str
    cuts: list[Cut]
    narration: str = "" # 该组的中文旁白;空串表示无旁白

    @property
    def duration(self) -> int:
        return sum(cut.duration for cut in self.cuts)

    @property
    def combined_prompt(self) -> str:
        """全部分镜 prompt 拼接,用于检测 @Element1/@Image1 等占位符。"""
        return " ".join(cut.prompt for cut in self.cuts)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "title": self.title,
            "negative_prompt": self.negative_prompt,
            "narration": self.narration,
            "cuts": [asdict(c) for c in self.cuts],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Shot":
        # 兼容旧 manifest:旧格式为平铺的 prompt/duration 单镜头
        if "cuts" in data:
            cuts = [
                Cut(prompt=str(c["prompt"]), duration=int(c.get("duration", 5)))
                for c in data["cuts"]
            ]
        else:
            cuts = [Cut(prompt=str(data["prompt"]), duration=int(data.get("duration", 5)))]
        return cls(
            index=int(data["index"]),
            title=str(data["title"]),
            negative_prompt=str(data.get("negative_prompt", "")),
            narration=str(data.get("narration", "")),
            cuts=cuts,
        )


@dataclass
class Storyboard:
    title: str
    logline: str
    shots: list[Shot]
    reference_prompt: str = ""  # 主角参考图的文生图提示词,空串表示无固定主角
    bgm_file: str = ""          # 导演按情绪挑选的背景音乐文件名,空串表示未选

    @property
    def total_duration(self) -> int:
        return sum(shot.duration for shot in self.shots)

    @property
    def has_narration(self) -> bool:
        return any(shot.narration.strip() for shot in self.shots)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "logline": self.logline,
            "reference_prompt": self.reference_prompt,
            "bgm_file": self.bgm_file,
            "shots": [s.to_dict() for s in self.shots],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Storyboard":
        return cls(
            title=data["title"],
            logline=data["logline"],
            reference_prompt=data.get("reference_prompt", ""),
            bgm_file=data.get("bgm_file", ""),
            shots=[Shot.from_dict(raw) for raw in data["shots"]],
        )


_STORYBOARD_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "短视频的中文标题"},
        "logline": {"type": "string", "description": "一句话中文剧情概要"},
        "style_anchor": {
            "type": "string",
            "description": (
                "English style signature reused verbatim in every shot prompt: "
                "color palette, lighting scheme, film stock / rendering style. "
                "Keep it compact (under 120 characters) — it is repeated in "
                "every cut prompt, which has a strict length budget"
            ),
        },
        "reference_prompt": {
            "type": "string",
            "description": (
                "If the story has one recurring main subject: an English "
                "text-to-image prompt for its reference portrait (frontal, "
                "full body, clean neutral background, includes style_anchor). "
                "Empty string if there is no recurring subject."
            ),
        },
        "shots": {
            "type": "array",
            "description": (
                "Shot groups, played in order. Each group is generated by Kling "
                "in ONE continuous pass (native continuity inside the group); "
                "cross-dissolve transitions are applied between groups."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "镜头组的简短中文名称"},
                    "negative_prompt": {
                        "type": "string",
                        "description": "English negative prompt (artifacts to avoid)",
                    },
                    "narration": {
                        "type": "string",
                        "description": (
                            "该镜头组的中文旁白解说词(口语自然,约每秒 4 个字,"
                            "字数不超过组时长×4)。整部影片不需要旁白时置空字符串"
                        ),
                    },
                    "cuts": {
                        "type": "array",
                        "description": (
                            "Cuts inside this group, 1-6 items; sum of durations "
                            "must be 3-15 seconds"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "prompt": {
                                    "type": "string",
                                    "description": (
                                        "Complete self-contained English "
                                        "text-to-video prompt for this cut: "
                                        "setting, subject, action, camera "
                                        "movement + lens, lighting, style "
                                        "keywords; may embed dialogue lines. "
                                        "HARD LIMIT: at most 450 characters "
                                        "including spaces — longer prompts "
                                        "are rejected by the video model"
                                    ),
                                },
                                "duration": {
                                    "type": "integer",
                                    "description": (
                                        "Cut duration in seconds (integer "
                                        "1-15), chosen for narrative pacing"
                                    ),
                                },
                            },
                            "required": ["prompt", "duration"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["title", "negative_prompt", "narration", "cuts"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "logline", "style_anchor", "reference_prompt", "shots"],
    "additionalProperties": False,
}

# 按 Kling 3 多分镜(multi_prompt)最佳实践设计:
# 影片 = 镜头组序列;每组一次连续生成(组内衔接由模型原生保证),组间交叉溶解。
_SYSTEM_PROMPT = """\
你是一位资深短视频编剧兼导演,擅长把一两句话的创意扩写成适合 AI 文生视频模型(Kling 3)\
生成的分镜脚本。用户只提供简短创意,其余全部由你专业决定,不要留下待用户选择的空间。

## 结构:镜头组(shots)与分镜(cuts)
成片目标总时长约 {target} 秒。影片由若干"镜头组"顺序拼接而成:
- 每个镜头组由 1~6 个"分镜"(cuts)构成,一组会被模型一次性连续生成,\
组内分镜之间的画面衔接、运动连续性由模型原生保证——因此同一场景内的连续动作、\
景别推进、正反打应放进同一组;切换场景、时间跳跃、叙事段落转折时才另起一组;
- 组间拼接使用交叉溶解转场,适合承担章节感的切换;
- 每个分镜 duration 为 1~15 的整数(秒);一个组内全部分镜时长之和必须为 3~15 秒;
- 全部镜头组时长之和必须落在 {total_min}~{total_max} 秒之间,尽量接近 {target} 秒;
- 节奏由你掌控:动作、冲突、快切用 1~3 秒的短分镜(放在同一组内连续快切),\
常规叙事用 4~8 秒,氛围铺陈、情绪高潮、收尾用 9~15 秒的单分镜组;不要所有分镜等长。

## 风格一致性(最重要)
1. 先确定 style_anchor:一段英文风格签名,固定描述色调、光线方案、胶片/渲染风格\
(例如 "muted teal-and-amber palette, soft diffused golden-hour light, shallow depth \
of field, shot on 35mm film, cinematic color grading")。
2. 每个分镜的 prompt 都必须原封不动地包含这段 style_anchor。
3. 出现相同角色/场景时,为其写一段固定的英文外观描述,并在涉及的每个分镜 prompt 中\
逐字重复(如 "a ginger tabby cat with white paws and a red collar")。绝不使用 \
"the same cat as before" 这类指代——每个分镜 prompt 必须自包含、可独立理解。

## 主角参考图(角色一致性)
判断创意中是否存在贯穿多个镜头组的核心主体(人物、动物、物品等):
- 存在:在 reference_prompt 字段写一段英文文生图提示词,用于生成该主体的参考图——\
正面、完整主体、姿态自然、背景干净的纯色或简单环境、外观细节完整清晰,并包含 \
style_anchor 的风格词。该参考图会作为角色元素随每个镜头组送入视频模型锁定外观。\
同时,凡分镜 prompt 中提到该主体,必须写成 "@Element1 (完整的固定外观描述)" 的形式\
——@Element1 是角色元素占位符,括号内是逐字重复的外观描述。
- 不存在(纯风景、抽象影像、每组主体各不相同等):reference_prompt 置为空字符串,\
所有 prompt 中都不得出现 @Element1。

## 声音设计(重要)
先判断本片的叙事声音形态,二选一:
- 解说型(科普、产品介绍、纪录片、广告、故事旁白等叙述性题材):为每个镜头组的 \
narration 字段撰写中文旁白。要求:口语自然、贴合画面;语速约每秒 4 个字,\
每组字数不超过"组时长 × 4",宁短勿长;所有组的旁白连起来必须是一篇完整流畅的解说词。
- 沉浸型(氛围片、MV、纯剧情等):所有镜头组的 narration 一律置空字符串。
角色台词(两种形态下都可用):需要角色开口说话时,把台词直接写进对应分镜的 prompt,\
格式如 ... the young woman looks up and says in Chinese: "我们出发吧" ...(中文台词\
保留中文原文,模型会原生生成配音与口型)。解说型影片中,带台词的分镜要避免与旁白抢话,\
该组旁白应留白或极简。环境音效由模型自动生成,无需描述。

## 每个分镜 prompt 的结构(英文,按顺序)
1. Setting:环境、时间、天气、氛围;
2. Subject:主体及关键外观细节(复用固定描述);
3. Action:一个简单连贯的动作,可用 "First ... then ..." 描述顺序,\
   但幅度必须能在该分镜的 duration 秒内自然完成,禁止复杂多事件序列;
4. Camera:一种明确的镜头运动 + 镜别(如 "slow dolly-in, medium close-up" / \
   "aerial tracking shot, wide angle"),每分镜只用一种镜头运动;
5. Lighting 与 style_anchor 风格词。

**长度硬约束**:每条分镜 prompt(含 style_anchor、角色外观描述与 @Element1 占位符)\
总长必须不超过 450 个字符(英文字符数,含空格)——视频模型对单条分镜提示词有 512 \
字符硬上限,超长会被直接拒绝。为此 style_anchor 与外观描述都要精炼(各控制在 120 \
字符以内),动作与环境描述抓重点,不堆砌同义词。

## 其他
- 画面中不得出现文字、字幕、logo、水印。
- negative_prompt 按镜头组撰写,用英文,列出需规避项(如 blur, distortion, warping, \
text, watermark, extra limbs, deformed hands, flickering)。
- title 与 logline 用中文;title 简短(不超过 10 个字),将用作文件名。

## 输出格式
只输出一个 JSON 对象(不要 Markdown 代码块、不要任何解释文字),字段为:
title(string)、logline(string)、style_anchor(string)、reference_prompt(string)、\
shots(数组,每项含 title、negative_prompt、narration、cuts;cuts 为数组,\
每项含 prompt、duration);若用户消息中提供了背景音乐列表,则额外包含 bgm_file(string)。
"""


def _extract_json(text: str) -> dict:
    """容错解析:兼容不支持结构化输出、输出裹在代码块/夹带说明文字的模型。"""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 去掉 ```json ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass
    # 截取首个 { 到最后一个 } 之间的内容
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise json.JSONDecodeError("未找到 JSON 对象", text, 0)


class Director:
    def __init__(self, config: Config):
        self._config = config

    def write_storyboard(
        self,
        description: str,
        bgm_options: list[str] | None = None,
        aspect_ratio: str = "",
        reference_image: Path | None = None,
    ) -> Storyboard:
        """根据用户一句话描述生成分镜脚本(含瞬时错误重试)。

        reference_image 为用户上传的主角参考图:随创意一起发给导演模型,
        让它照图写出固定的角色外观描述(@Element1)。
        """
        image_data_url = _encode_image(reference_image) if reference_image else None
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return self._write_once(
                    description, bgm_options, aspect_ratio,
                    image_data_url=image_data_url,
                    has_user_image=reference_image is not None,
                )
            except (requests.ConnectionError, requests.Timeout,
                    json.JSONDecodeError, KeyError, _RetryableHTTPError) as exc:
                last_error = exc
                time.sleep(2 * (attempt + 1))
            except RuntimeError as exc:
                # 携带图片时失败可能是模型不支持图片输入:去掉图片降级重试
                # (文字说明仍会告知模型存在用户参考图)
                if image_data_url is None:
                    raise
                image_data_url = None
                last_error = exc
        raise RuntimeError(f"分镜脚本生成失败: {last_error}") from last_error

    # ---------------- OpenRouter 调用 ----------------

    _ASPECT_NOTES = {
        "9:16": "本片为竖屏 9:16(手机短视频),请按竖屏构图设计画面与镜头运动。",
        "16:9": "本片为横屏 16:9,请按横屏电影感构图设计画面与镜头运动。",
        "1:1": "本片为方形 1:1 画幅,请按居中构图设计画面。",
    }

    def _write_once(
        self,
        description: str,
        bgm_options: list[str] | None = None,
        aspect_ratio: str = "",
        image_data_url: str | None = None,
        has_user_image: bool = False,
    ) -> Storyboard:
        # 镜头组数量与各分镜时长由导演模型按叙事节奏决定,
        # 只约束总时长落在目标值 ±15% 内;clip_duration 仅作缺省回退值。
        fallback_duration = int(self._config["kling"]["clip_duration"])
        target = int(self._config["video"]["target_duration"])
        total_min = max(_MIN_GROUP_SECONDS, round(target * 0.85))
        total_max = round(target * 1.15)
        # 防御模型跑飞:即使全用最短镜头组,也不该超过这个组数
        max_shots = max(1, -(-total_max // _MIN_GROUP_SECONDS))

        system = _SYSTEM_PROMPT.format(
            target=target,
            total_min=total_min,
            total_max=total_max,
        )

        user_message = f"请为以下创意撰写分镜脚本:\n\n{description}"
        note = self._ASPECT_NOTES.get(str(aspect_ratio).strip())
        if note:
            user_message += f"\n\n{note}"
        if has_user_image:
            user_message += (
                "\n\n用户已上传主角参考图(消息附图即为该图,若你看不到图片则依据"
                "创意内容推断主角外观)。该图会作为角色元素随每个镜头组送入视频模型"
                "锁定主角外观,因此:\n"
                "- 将该主角视为贯穿全片的固定主角;reference_prompt 置为空字符串"
                "(参考图已由用户提供,无需再生成);\n"
                "- 所有涉及主角的分镜 prompt 一律写成 \"@Element1 (外观描述)\" 形式,"
                "外观描述需与参考图一致,写成一段固定英文描述并逐字重复。"
            )
        schema = _STORYBOARD_SCHEMA
        if bgm_options:
            schema = json.loads(json.dumps(_STORYBOARD_SCHEMA))
            schema["properties"]["bgm_file"] = {
                "type": "string",
                "description": "从候选列表中选出的最贴合影片情绪的背景音乐文件名(原样照抄)",
            }
            schema["required"].append("bgm_file")
            user_message += (
                "\n\n候选背景音乐文件列表(请根据影片情绪在 bgm_file 字段填入"
                "最合适的一个文件名,原样照抄):\n"
                + "\n".join(f"- {name}" for name in bgm_options)
            )

        # 携带用户参考图时按多模态格式发送;模型不支持图片输入的情况由
        # write_storyboard 捕获后去图重试
        user_content: str | list = user_message
        if image_data_url:
            user_content = [
                {"type": "text", "text": user_message},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ]

        llm = self._config["llm"]
        body: dict = {
            "model": llm["model"],
            "max_tokens": int(llm["max_tokens"]),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            # 支持结构化输出的模型会严格遵守;不支持的模型由 _extract_json 兜底
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "storyboard",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        effort = str(llm.get("reasoning_effort") or "").strip()
        if effort:
            body["reasoning"] = {"effort": effort}

        resp = requests.post(
            _OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {self._config.openrouter_api_key}",
                "Content-Type": "application/json",
                # OpenRouter 推荐的应用标识(可选)
                "X-Title": "AI Short Video Generator",
            },
            json=body,
            timeout=(15, 900),  # 深度思考模型单次请求可能长达数分钟
        )

        if resp.status_code in _RETRYABLE_STATUS:
            raise _RetryableHTTPError(f"HTTP {resp.status_code}: {_error_message(resp)}")
        if resp.status_code == 401:
            raise RuntimeError("OpenRouter API KEY 无效,请检查 config.yaml 中的 openrouter_api_key")
        if resp.status_code == 402:
            raise RuntimeError("OpenRouter 余额不足,请前往 openrouter.ai 充值")
        if resp.status_code == 404:
            raise RuntimeError(
                f"模型不存在: {llm['model']},请检查 config.yaml 中的 llm.model"
            )
        if resp.status_code != 200:
            raise RuntimeError(f"OpenRouter 请求失败 HTTP {resp.status_code}: {_error_message(resp)}")

        data = resp.json()
        if "error" in data:  # OpenRouter 可能以 200 返回上游错误
            raise _RetryableHTTPError(str(data["error"].get("message", data["error"])))

        choice = data["choices"][0]
        finish = choice.get("finish_reason")
        message = choice["message"]
        if message.get("refusal"):
            raise RuntimeError(f"模型拒绝了该创意,请调整描述后重试({message['refusal']})")
        if finish == "length":
            raise RuntimeError("模型输出被截断,请提高 config.yaml 中的 llm.max_tokens 后重试")
        if finish == "content_filter":
            raise RuntimeError("内容被安全策略拦截,请调整描述后重试")

        content = message.get("content") or ""
        parsed = _extract_json(content)
        return self._build_storyboard(parsed, max_shots, fallback_duration)

    # ---------------- 结果组装 ----------------

    @staticmethod
    def _build_cuts(raw: dict, fallback_duration: int) -> list[Cut]:
        """解析并钳制一个镜头组的分镜:单分镜 1~15 秒,组总长 3~15 秒。"""
        raw_cuts = raw.get("cuts")
        if not isinstance(raw_cuts, list) or not raw_cuts:
            # 模型未按新结构输出时,兼容平铺的 prompt/duration
            return [Cut(
                prompt=str(raw.get("prompt", "")),
                duration=_clamp_duration(raw.get("duration"), fallback_duration),
            )]
        cuts = [
            Cut(
                prompt=str(c.get("prompt", "")),
                duration=_clamp_duration(
                    c.get("duration"), fallback_duration, minimum=_MIN_CUT_SECONDS
                ),
            )
            for c in raw_cuts[:_MAX_CUTS_PER_GROUP]
        ]
        if len(cuts) == 1:
            cuts[0].duration = max(_MIN_GROUP_SECONDS, cuts[0].duration)
            return cuts
        # 组总长不足 Kling 下限时,逐秒补齐最短的分镜
        while sum(c.duration for c in cuts) < _MIN_GROUP_SECONDS:
            min(cuts, key=lambda c: c.duration).duration += 1
        # 组总长超过 Kling 上限时按比例压缩,再逐一削减到不超过 15 秒
        total = sum(c.duration for c in cuts)
        if total > _MAX_GROUP_SECONDS:
            scale = _MAX_GROUP_SECONDS / total
            for c in cuts:
                c.duration = max(_MIN_CUT_SECONDS, int(c.duration * scale))
            while sum(c.duration for c in cuts) > _MAX_GROUP_SECONDS:
                longest = max(cuts, key=lambda c: c.duration)
                if longest.duration <= _MIN_CUT_SECONDS:
                    break
                longest.duration -= 1
        return cuts

    @classmethod
    def _build_storyboard(cls, data: dict, max_shots: int, fallback_duration: int) -> Storyboard:
        raw_shots = data["shots"][:max_shots]
        if not raw_shots:
            raise KeyError("shots 为空")
        shots = [
            Shot(
                index=i + 1,
                title=str(raw["title"]),
                negative_prompt=str(raw.get("negative_prompt", "")),
                narration=str(raw.get("narration", "")).strip(),
                cuts=cls._build_cuts(raw, fallback_duration),
            )
            for i, raw in enumerate(raw_shots)
        ]
        return Storyboard(
            title=str(data["title"]),
            logline=str(data["logline"]),
            reference_prompt=str(data.get("reference_prompt", "")).strip(),
            bgm_file=str(data.get("bgm_file", "")).strip(),
            shots=shots,
        )


class _RetryableHTTPError(Exception):
    pass


def _error_message(resp: requests.Response) -> str:
    try:
        return str(resp.json().get("error", {}).get("message", resp.text[:300]))
    except Exception:  # noqa: BLE001
        return resp.text[:300]
