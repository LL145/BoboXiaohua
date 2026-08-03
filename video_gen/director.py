"""Claude 担任编剧 + 导演:把一句话描述扩写成完整的分镜脚本。"""

from __future__ import annotations

import json
from dataclasses import dataclass

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


_STORYBOARD_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "短视频的中文标题"},
        "logline": {"type": "string", "description": "一句话中文剧情概要"},
        "shots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "镜头的简短中文名称"},
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Detailed English text-to-video prompt for this shot: "
                            "subject, action, camera movement, lighting, mood, style"
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
    "required": ["title", "logline", "shots"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = """\
你是一位资深短视频编剧兼导演,擅长把一句话的创意扩写成适合 AI 文生视频模型(Kling)逐镜头生成的分镜脚本。

要求:
1. 严格产出 {num_shots} 个镜头,每个镜头将被生成为 {clip_duration} 秒的视频片段,总时长约 {total} 秒。
2. 镜头之间要有清晰的叙事推进(起承转合),整体风格、色调、光线保持统一,像一条连贯的成片。
3. 每个镜头的 prompt 用英文撰写,面向文生视频模型:
   - 具体描述主体、动作、场景、镜头运动(如 slow dolly in, aerial tracking shot)、
     光线(如 golden hour, soft rim light)、氛围与画风;
   - 动作幅度要适合 {clip_duration} 秒时长,避免要求复杂的多事件序列;
   - 每个 prompt 自包含:不要用 "the same man as before" 这类跨镜头指代,
     而是在每个镜头里重复关键的角色/场景外观描述,保证各镜头人物场景一致;
   - 避免文字、字幕、logo 出现在画面中。
4. negative_prompt 用英文,列出需要规避的内容(如 blur, distortion, text, watermark, extra limbs)。
5. title 与 logline 用中文。
"""


class Director:
    def __init__(self, config: Config):
        self._config = config
        self._client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    def write_storyboard(self, description: str) -> Storyboard:
        """根据用户一句话描述生成分镜脚本。"""
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

        shots = [
            Shot(
                index=i + 1,
                title=raw["title"],
                prompt=raw["prompt"],
                negative_prompt=raw["negative_prompt"],
                duration=clip_duration,
            )
            for i, raw in enumerate(data["shots"][:num_shots])
        ]
        if not shots:
            raise RuntimeError("Claude 未产出任何镜头,请重试")
        return Storyboard(title=data["title"], logline=data["logline"], shots=shots)
