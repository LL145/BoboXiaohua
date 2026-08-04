"""旁白配音:用 Edge TTS(微软免费语音)把导演写的中文旁白合成音频。

只有导演判断影片需要解说(narration 字段非空)时才会用到本模块。
稳健性:edge-tts 未安装、网络不可用、单句合成失败,都只是放弃对应旁白,
绝不影响画面成片(与全片"绝不因局部失败毁掉整次任务"的原则一致)。
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Callable

from .director import Storyboard

LogFn = Callable[[str], None]

# 小于该体积的音频视为无效(空文件/截断)
_MIN_AUDIO_BYTES = 1024


def narration_is_valid(path: Path) -> bool:
    return path.exists() and path.stat().st_size >= _MIN_AUDIO_BYTES


def narration_path(run_dir: Path, shot_index: int) -> Path:
    return run_dir / f"narration_{shot_index:02d}.mp3"


def synthesize_all(
    storyboard: Storyboard, run_dir: Path, voice: str, log: LogFn
) -> dict[int, Path]:
    """逐镜头组合成旁白,返回 {镜头组序号: 音频路径};失败的组被跳过。

    已存在的有效音频直接复用(断点续传)。
    """
    pending = [s for s in storyboard.shots if s.narration.strip()]
    if not pending:
        return {}
    try:
        import edge_tts  # noqa: F401 - 仅探测可用性
    except ImportError:
        log("  未安装 edge-tts,跳过旁白配音(安装:pip install edge-tts)。")
        return {}

    results: dict[int, Path] = {}
    for shot in pending:
        out_path = narration_path(run_dir, shot.index)
        if narration_is_valid(out_path):
            results[shot.index] = out_path
            continue
        if _synthesize_once(shot.narration.strip(), voice, out_path, log, shot.index):
            results[shot.index] = out_path
    return results


def _synthesize_once(
    text: str, voice: str, out_path: Path, log: LogFn, index: int
) -> bool:
    import edge_tts

    tmp_path = out_path.with_suffix(".part")
    for attempt in (1, 2):
        try:
            communicate = edge_tts.Communicate(text, voice)
            asyncio.run(communicate.save(str(tmp_path)))
            if tmp_path.exists() and tmp_path.stat().st_size >= _MIN_AUDIO_BYTES:
                tmp_path.replace(out_path)
                return True
            raise RuntimeError("合成结果为空")
        except Exception as exc:  # noqa: BLE001 - 单组失败不致命
            log(f"  镜头组 {index} 旁白第 {attempt} 次合成失败: {exc}")
            time.sleep(2)
    tmp_path.unlink(missing_ok=True)
    log(f"  镜头组 {index} 旁白合成失败,该组将没有解说(不影响画面)。")
    return False


# ---------------- 字幕(SRT) ----------------

_SENTENCE_SPLIT = re.compile(r"(?<=[。!?!?;;…])\s*|\n+")


def split_sentences(text: str) -> list[str]:
    """按中文句读切分旁白,用于把长旁白拆成多条字幕。"""
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text) if p and p.strip()]
    return parts or [text.strip()]


def _srt_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(entries: list[tuple[float, float, str]]) -> str:
    """entries: [(开始秒, 结束秒, 文本)] → SRT 文件内容。"""
    lines: list[str] = []
    for i, (start, end, text) in enumerate(entries, start=1):
        lines += [str(i), f"{_srt_timestamp(start)} --> {_srt_timestamp(end)}", text, ""]
    return "\n".join(lines)


def narration_srt_entries(
    narration: str, start: float, audio_duration: float
) -> list[tuple[float, float, str]]:
    """把一段旁白按句子长度比例分配到音频时长上,生成字幕条目。"""
    sentences = split_sentences(narration)
    total_chars = sum(len(s) for s in sentences) or 1
    entries: list[tuple[float, float, str]] = []
    cursor = start
    for sentence in sentences:
        span = audio_duration * len(sentence) / total_chars
        entries.append((cursor, cursor + span, sentence))
        cursor += span
    return entries
