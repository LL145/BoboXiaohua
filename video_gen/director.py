"""Claude 担任编剧 + 导演:把一句话描述扩写成完整的分镜脚本。"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass

import anthropic

from .config import Config


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

    @property
    def total_duration(self) -> int:
        return sum(shot.duration for shot in self.shots)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "logline": self.logline,
            "shots": [asdict(s) for s in self.shots],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Storyboard":
        return cls(
            title=data["title"],
            logline=data["logline"],
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
    "required": ["title", "logline", "style_anchor", "shots"],
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
- negative_prompt 用英文,列出需规避项(如 blur, distortion, warping, text, \
watermark, extra limbs, deformed hands, flickering)。
- title 与 logline 用中文;title 简短(不超过 10 个字),将用作文件名。
"""


class Director:
    def __init__(self, config: Config):
        self._config = config
        self._client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    def write_storyboard(self, description: str) -> Storyboard:
        """根据用户一句话描述生成分镜脚本(含瞬时错误重试)。"""
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                return self._write_once(description)
            except (anthropic.APIConnectionError, json.JSONDecodeError, KeyError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(2)
        raise RuntimeError(f"分镜脚本生成失败: {last_error}") from last_error

    def _write_once(self, description: str) -> Storyboard:
        kling = self._config["kling"]
        clip_duration = int(kling["clip_duration"])
        target = int(self._config["video"]["target_duration"])
        num_shots = max(1, round(target / clip_duration))

        system = _SYSTEM_PROMPT.format(
            num_shots=num_shots,
            clip_duration=clip_duration,
            total=num_shots * clip_duration,
        )

        response = self._client.messages.create(
            model=self._config["claude"]["model"],
            max_tokens=int(self._config["claude"]["max_tokens"]),
            system=system,
            output_config={
                "format": {"type": "json_schema", "schema": _STORYBOARD_SCHEMA}
            },
            messages=[
                {
                    "role": "user",
                    "content": f"请为以下创意撰写分镜脚本:\n\n{description}",
                }
            ],
        )

        if response.stop_reason == "refusal":
            detail = ""
            if response.stop_details and response.stop_details.explanation:
                detail = f"({response.stop_details.explanation})"
            raise RuntimeError(f"Claude 拒绝了该创意,请调整描述后重试 {detail}")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Claude 输出被截断,请提高 claude.max_tokens 后重试")

        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)

        raw_shots = data["shots"][:num_shots]
        if not raw_shots:
            raise KeyError("shots 为空")

        shots = [
            Shot(
                index=i + 1,
                title=raw["title"],
                prompt=raw["prompt"],
                negative_prompt=raw["negative_prompt"],
                duration=clip_duration,
            )
            for i, raw in enumerate(raw_shots)
        ]
        return Storyboard(title=data["title"], logline=data["logline"], shots=shots)
