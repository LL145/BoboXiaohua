"""旁白配音:用 Edge TTS(微软免费语音)把导演写的中文旁白合成音频。

只有导演判断影片需要解说(narration 字段非空)时才会用到本模块。
合成时同步记录逐句的精确时间轴(edge-tts 句边界事件),落盘为
narration_XX.timeline.json,供字幕严格对齐语音;旁白超长时还支持
以更快语速(rate)重新合成,比事后 atempo 变速自然得多。
稳健性:edge-tts 未安装、网络不可用、单句合成失败,都只是放弃对应旁白,
绝不影响画面成片(与全片"绝不因局部失败毁掉整次任务"的原则一致)。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Callable

from .director import Storyboard

LogFn = Callable[[str], None]

# 小于该体积的音频视为无效(空文件/截断)
_MIN_AUDIO_BYTES = 1024
# edge-tts 边界事件的时间单位为 100 纳秒
_TICKS_PER_SECOND = 10_000_000


def narration_is_valid(path: Path) -> bool:
    return path.exists() and path.stat().st_size >= _MIN_AUDIO_BYTES


def narration_path(run_dir: Path, shot_index: int) -> Path:
    return run_dir / f"narration_{shot_index:02d}.mp3"


def timeline_path(audio_path: Path) -> Path:
    """与音频同名的逐句时间轴文件(narration_XX.timeline.json)。"""
    return audio_path.with_name(f"{audio_path.stem}.timeline.json")


def load_timeline(audio_path: Path) -> list[tuple[float, float, str]] | None:
    """读取合成时记录的逐句时间轴 [(开始秒, 结束秒, 句子)];无/损坏返回 None。"""
    path = timeline_path(audio_path)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries = [
            (float(e["start"]), float(e["end"]), str(e["text"])) for e in raw
        ]
        return entries or None
    except Exception:  # noqa: BLE001 - 时间轴损坏只影响字幕精度,回退估算
        return None


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
        if synthesize(shot.narration.strip(), voice, out_path, log, shot.index):
            results[shot.index] = out_path
    return results


def synthesize(
    text: str, voice: str, out_path: Path, log: LogFn, index: int, rate: int = 0
) -> bool:
    """合成一段旁白到 out_path,并写出逐句时间轴;rate 为语速加快百分比(0~N)。"""
    tmp_path = out_path.with_suffix(".part")
    for attempt in (1, 2):
        try:
            sentences = _stream_synthesize(text, voice, rate, tmp_path)
            if tmp_path.exists() and tmp_path.stat().st_size >= _MIN_AUDIO_BYTES:
                tmp_path.replace(out_path)
                _save_timeline(out_path, sentences)
                return True
            raise RuntimeError("合成结果为空")
        except Exception as exc:  # noqa: BLE001 - 单组失败不致命
            log(f"  镜头组 {index} 旁白第 {attempt} 次合成失败: {exc}")
            time.sleep(2)
    tmp_path.unlink(missing_ok=True)
    log(f"  镜头组 {index} 旁白合成失败,该组将没有解说(不影响画面)。")
    return False


def _stream_synthesize(
    text: str, voice: str, rate: int, out_tmp: Path
) -> list[tuple[float, float, str]]:
    """流式合成:边写音频边收集句/词边界事件,返回逐句时间轴。"""
    import edge_tts

    kwargs: dict = {}
    if rate > 0:
        kwargs["rate"] = f"+{int(rate)}%"
    try:
        communicate = edge_tts.Communicate(
            text, voice, boundary="SentenceBoundary", **kwargs
        )
    except TypeError:
        # 旧版 edge-tts 没有 boundary 参数,只会给出 WordBoundary 事件
        communicate = edge_tts.Communicate(text, voice, **kwargs)

    sentences: list[tuple[float, float, str]] = []
    words: list[tuple[float, float, str]] = []

    async def run() -> None:
        with open(out_tmp, "wb") as f:
            async for chunk in communicate.stream():
                ctype = chunk.get("type")
                if ctype == "audio":
                    f.write(chunk.get("data") or b"")
                elif ctype in ("SentenceBoundary", "WordBoundary"):
                    start = float(chunk["offset"]) / _TICKS_PER_SECOND
                    end = start + float(chunk["duration"]) / _TICKS_PER_SECOND
                    target = sentences if ctype == "SentenceBoundary" else words
                    target.append((start, end, str(chunk.get("text") or "")))

    asyncio.run(run())
    return sentences or _words_to_sentences(text, words)


def _save_timeline(audio_path: Path, sentences: list[tuple[float, float, str]]) -> None:
    try:
        if not sentences:
            timeline_path(audio_path).unlink(missing_ok=True)
            return
        payload = [
            {"start": round(s, 3), "end": round(e, 3), "text": t}
            for s, e, t in sentences
        ]
        timeline_path(audio_path).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass  # 时间轴写不出只影响字幕精度,字幕会回退按字数估算


_PUNCT = re.compile(r"[\s。,!?!?,;;、::…·~—\-“”‘’\"'()()《》〈〉【】\[\]]+")


def _content_len(text: str) -> int:
    """去掉标点与空白后的字数,用于把词边界按内容对齐到句子。"""
    return len(_PUNCT.sub("", text))


def _words_to_sentences(
    text: str, words: list[tuple[float, float, str]]
) -> list[tuple[float, float, str]]:
    """旧版 edge-tts 只有词边界:按句子字数把词归组,推出逐句时间轴。"""
    if not words:
        return []
    out: list[tuple[float, float, str]] = []
    i = 0
    for sentence in split_sentences(text):
        need = _content_len(sentence)
        if need <= 0:
            continue
        first: tuple[float, float, str] | None = None
        last: tuple[float, float, str] | None = None
        got = 0
        while i < len(words) and got < need:
            word = words[i]
            if first is None:
                first = word
            last = word
            got += _content_len(word[2]) or len(word[2])
            i += 1
        if first is None or last is None:
            break
        out.append((first[0], last[1], sentence))
    return out


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
    """回退方案:把旁白按句子字数比例分配到音频时长上,生成字幕条目。

    仅在合成时未能记录精确时间轴(narration_XX.timeline.json)时使用。
    """
    sentences = split_sentences(narration)
    total_chars = sum(len(s) for s in sentences) or 1
    entries: list[tuple[float, float, str]] = []
    cursor = start
    for sentence in sentences:
        span = audio_duration * len(sentence) / total_chars
        entries.append((cursor, cursor + span, sentence))
        cursor += span
    return entries
