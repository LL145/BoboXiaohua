"""LLM 担任编剧 + 导演:把一句话描述扩写成完整的分镜脚本。

通过 OpenRouter(OpenAI 兼容接口)调用,可在 config.yaml 中切换任意模型;
默认使用 anthropic/claude-fable-5(reasoning effort: high)。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass

import requests

from .config import Config

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 524}


@dataclass
class Shot:
    index: int
    title: str          # 镜头中文名,用于界面展示
    prompt: str         # 交给 Kling 的英文提示词
    negative_prompt: str
    duration: int       # 秒


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

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "logline": self.logline,
            "reference_prompt": self.reference_prompt,
            "bgm_file": self.bgm_file,
            "shots": [asdict(s) for s in self.shots],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Storyboard":
        return cls(
            title=data["title"],
            logline=data["logline"],
            reference_prompt=data.get("reference_prompt", ""),
            bgm_file=data.get("bgm_file", ""),
            shots=[Shot(**raw) for raw in data["shots"]],
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
                "color palette, lighting scheme, film stock / rendering style"
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
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "镜头的简短中文名称"},
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Complete English text-to-video prompt following the "
                            "structure: setting, subject, sequential action, "
                            "camera movement + lens, lighting, style keywords"
                        ),
                    },
                    "negative_prompt": {
                        "type": "string",
                        "description": "English negative prompt (artifacts to avoid)",
                    },
                },
                "required": ["title", "prompt", "negative_prompt"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "logline", "style_anchor", "reference_prompt", "shots"],
    "additionalProperties": False,
}

# 按 Kling 文生视频提示词最佳实践设计:
# 场景 → 主体 → 顺序动作 → 镜头运动/镜别 → 光线 → 风格词,单镜头单一动作。
_SYSTEM_PROMPT = """\
你是一位资深短视频编剧兼导演,擅长把一两句话的创意扩写成适合 AI 文生视频模型(Kling)\
逐镜头生成的分镜脚本。用户只提供简短创意,其余全部由你专业决定,不要留下待用户选择的空间。

## 产出要求
严格产出 {num_shots} 个镜头。每个镜头将被独立生成为 {clip_duration} 秒的片段,\
最后按顺序拼接为约 {total} 秒的成片,因此镜头顺序即叙事顺序,要有清晰的起承转合。

## 风格一致性(最重要)
1. 先确定 style_anchor:一段英文风格签名,固定描述色调、光线方案、胶片/渲染风格\
(例如 "muted teal-and-amber palette, soft diffused golden-hour light, shallow depth \
of field, shot on 35mm film, cinematic color grading")。
2. 每个镜头的 prompt 都必须原封不动地包含这段 style_anchor。
3. 出现相同角色/场景时,为其写一段固定的英文外观描述,并在涉及的每个镜头 prompt 中\
逐字重复(如 "a ginger tabby cat with white paws and a red collar")。绝不使用 \
"the same cat as before" 这类跨镜头指代——各镜头相互独立生成,看不到彼此。

## 主角参考图(角色一致性)
判断创意中是否存在贯穿多个镜头的核心主体(人物、动物、物品等):
- 存在:在 reference_prompt 字段写一段英文文生图提示词,用于生成该主体的参考图——\
正面、完整主体、姿态自然、背景干净的纯色或简单环境、外观细节完整清晰,并包含 \
style_anchor 的风格词。该参考图会随每个镜头一起送入视频模型以锁定主角外观。\
同时,凡镜头 prompt 中提到该主体,必须写成 "@Image1 (完整的固定外观描述)" 的形式\
——@Image1 是参考图占位符,括号内是逐字重复的外观描述。
- 不存在(纯风景、抽象影像、每个镜头主体各不相同等):reference_prompt 置为空字符串,\
所有 prompt 中都不得出现 @Image1。

## 每个镜头 prompt 的结构(英文,按顺序)
1. Setting:环境、时间、天气、氛围;
2. Subject:主体及关键外观细节(复用固定描述);
3. Action:一个简单连贯的动作,可用 "First ... then ..." 描述顺序,\
   但幅度必须能在 {clip_duration} 秒内自然完成,禁止复杂多事件序列;
4. Camera:一种明确的镜头运动 + 镜别(如 "slow dolly-in, medium close-up" / \
   "aerial tracking shot, wide angle"),每镜头只用一种镜头运动;
5. Lighting 与 style_anchor 风格词。

## 其他
- 画面中不得出现文字、字幕、logo、水印。
- 以视觉叙事为主,不要设计对白/台词场景(模型会自动生成环境音效)。
- negative_prompt 用英文,列出需规避项(如 blur, distortion, warping, text, \
watermark, extra limbs, deformed hands, flickering)。
- title 与 logline 用中文;title 简短(不超过 10 个字),将用作文件名。

## 输出格式
只输出一个 JSON 对象(不要 Markdown 代码块、不要任何解释文字),字段为:
title(string)、logline(string)、style_anchor(string)、reference_prompt(string)、\
shots(数组,每项含 title、prompt、negative_prompt);\
若用户消息中提供了背景音乐列表,则额外包含 bgm_file(string)。
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
    ) -> Storyboard:
        """根据用户一句话描述生成分镜脚本(含瞬时错误重试)。"""
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return self._write_once(description, bgm_options, aspect_ratio)
            except (requests.ConnectionError, requests.Timeout,
                    json.JSONDecodeError, KeyError, _RetryableHTTPError) as exc:
                last_error = exc
                time.sleep(2 * (attempt + 1))
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
    ) -> Storyboard:
        kling = self._config["kling"]
        clip_duration = int(kling["clip_duration"])
        target = int(self._config["video"]["target_duration"])
        num_shots = max(1, round(target / clip_duration))

        system = _SYSTEM_PROMPT.format(
            num_shots=num_shots,
            clip_duration=clip_duration,
            total=num_shots * clip_duration,
        )

        user_message = f"请为以下创意撰写分镜脚本:\n\n{description}"
        note = self._ASPECT_NOTES.get(str(aspect_ratio).strip())
        if note:
            user_message += f"\n\n{note}"
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

        llm = self._config["llm"]
        body: dict = {
            "model": llm["model"],
            "max_tokens": int(llm["max_tokens"]),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
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
        return self._build_storyboard(parsed, num_shots, clip_duration)

    # ---------------- 结果组装 ----------------

    @staticmethod
    def _build_storyboard(data: dict, num_shots: int, clip_duration: int) -> Storyboard:
        raw_shots = data["shots"][:num_shots]
        if not raw_shots:
            raise KeyError("shots 为空")
        shots = [
            Shot(
                index=i + 1,
                title=str(raw["title"]),
                prompt=str(raw["prompt"]),
                negative_prompt=str(raw.get("negative_prompt", "")),
                duration=clip_duration,
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
