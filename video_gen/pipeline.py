"""生成流水线:一句话描述 → 分镜脚本 → 主角参考图 → 并行逐镜头组生成
→ 旁白配音 → 拼接成片(转场/旁白/背景音乐/字幕)。

稳健性设计:
- 生成前预检:先校验 OpenRouter KEY 与磁盘空间,配错即刻提示,不浪费费用;
- 断点续传:同一描述的未完成任务会复用已有分镜脚本、参考图、旁白音频和
  已生成片段,失败后再次点击「生成」只补齐缺失部分,不重复扣费;
- 主角参考图:导演模型判断有固定主角时自动生成参考图并作为角色元素
  (@Element1)送入每个镜头组;参考图任何一步失败都自动降级为纯文生视频;
- 并行生成:多个镜头组同时提交 Kling,总耗时约等于单个镜头组;
- 单镜头组独立重试 + 超时看门狗,KEY 无效等致命错误立即终止,不空耗重试;
- 旁白与字幕:导演判断影片需要解说时,用 Edge TTS 合成旁白并生成字幕;
  TTS 不可用、混音或字幕失败,都只是放弃对应环节,绝不影响画面成片;
- 运行日志同步写入任务目录 log.txt,便于排查问题;
- 背景音乐:程序目录 music/ 下有音频文件时,由导演模型按影片情绪挑选混入。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import requests

from . import tts
from .assembler import Assembler
from .config import Config, app_dir
from .director import Director, Storyboard

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]

_MANIFEST_NAME = "manifest.json"
_REFERENCE_NAME = "reference.png"
_BGM_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


def _safe_name(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", title).strip("_")
    return name[:40] or "video"


def _description_key(description: str) -> str:
    return hashlib.sha1(description.strip().encode("utf-8")).hexdigest()[:8]


class Pipeline:
    def __init__(self, config: Config, log: LogFn, progress: ProgressFn | None = None):
        self._config = config
        self._ui_log = log
        self._progress = progress
        self._logfile: Path | None = None

    # ---------------- 主流程 ----------------

    def run(self, description: str) -> Path:
        """执行完整流程,返回成片路径。"""
        from .kling import FatalGenerationError, KlingGenerator, clip_is_valid

        config = self._config
        log = self._log

        assembler = Assembler(config, log)
        if not assembler.check_ffmpeg():
            raise RuntimeError(
                f"未找到 ffmpeg(查找路径: {config.ffmpeg_path})。"
                "请安装 ffmpeg 并加入 PATH,或在 config.yaml 的 ffmpeg.path 中填写完整路径。"
            )
        self._preflight()

        # 1. 分镜脚本:优先恢复未完成任务,否则请 LLM 新写
        bgm_tracks = self._bgm_tracks()
        run_dir, storyboard = self._resume_or_create(
            description, [t.name for t in bgm_tracks]
        )
        self._logfile = run_dir / "log.txt"
        log(f"《{storyboard.title}》—— {storyboard.logline}")
        log(f"共 {len(storyboard.shots)} 个镜头组,预计总时长约 {storyboard.total_duration} 秒:")
        for shot in storyboard.shots:
            detail = f",{len(shot.cuts)} 个分镜" if len(shot.cuts) > 1 else ""
            if shot.narration.strip():
                detail += ",含旁白"
            log(f"  {shot.index}. {shot.title}({shot.duration}s{detail})")
        log(
            "  声音设计:解说型(旁白 + 字幕)" if storyboard.has_narration
            else "  声音设计:沉浸型(原生音效与台词)"
        )

        pending = [
            s for s in storyboard.shots
            if not clip_is_valid(run_dir / f"shot_{s.index:02d}.mp4")
        ]
        done_before = len(storyboard.shots) - len(pending)
        generator = KlingGenerator(config, log)

        # 2. 主角参考图(导演判断有固定主角时才生成;失败自动降级纯文生)
        reference_url: str | None = None
        if storyboard.reference_prompt and pending:
            reference_url = self._prepare_reference(generator, run_dir, storyboard)

        # 3. Kling 并行生成各镜头
        concurrency = max(1, int(config["kling"]["concurrency"]))
        if done_before:
            log(f"③ 已有 {done_before} 个镜头可复用,补齐剩余 {len(pending)} 个 …")
        else:
            log(f"③ Kling 并行生成 {len(pending)} 个镜头(并发 {concurrency},约需十几分钟)…")
        self._report_progress(done_before, len(storyboard.shots))

        if pending:
            errors: list[str] = []
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {
                    pool.submit(
                        generator.generate_clip,
                        shot,
                        run_dir / f"shot_{shot.index:02d}.mp4",
                        reference_url,
                    ): shot
                    for shot in pending
                }
                finished = done_before
                for future in as_completed(futures):
                    shot = futures[future]
                    try:
                        future.result()
                        finished += 1
                        log(f"  镜头 {shot.index} 完成 ✓({finished}/{len(storyboard.shots)})")
                        self._report_progress(finished, len(storyboard.shots))
                    except FatalGenerationError as exc:
                        # KEY 无效等致命错误:取消尚未开始的镜头,立即终止
                        pool.shutdown(wait=False, cancel_futures=True)
                        raise RuntimeError(str(exc)) from exc
                    except Exception as exc:  # noqa: BLE001
                        errors.append(str(exc))
            if errors:
                raise RuntimeError(
                    f"{len(errors)} 个镜头生成失败:{errors[0]}\n"
                    "已完成的镜头已保存,再次点击「生成」将只补齐失败的镜头。"
                )

        clips = [run_dir / f"shot_{s.index:02d}.mp4" for s in storyboard.shots]

        # 4. 旁白配音(导演判断影片需要解说时;失败只丢旁白,不影响成片)
        narration_cfg = config["narration"]
        narration_audio: dict[int, Path] = {}
        step = 4
        if storyboard.has_narration and bool(narration_cfg["enabled"]):
            log("④ 合成旁白配音(Edge TTS)…")
            narration_audio = tts.synthesize_all(
                storyboard, run_dir, str(narration_cfg["voice"]), log
            )
            step = 5

        # 5. ffmpeg 拼接:转场 → 旁白 → 背景音乐 → 字幕,逐级可降级
        log(f"{'④⑤⑥⑦⑧'[step - 4]} 正在拼接成片 …")
        final_path = run_dir / f"{_safe_name(storyboard.title)}.mp4"
        stage_path = run_dir / "_stage_concat.mp4"
        _, offsets = assembler.concat(
            clips, stage_path,
            fallback_durations=[float(s.duration) for s in storyboard.shots],
        )
        current = stage_path

        srt_path: Path | None = None
        if narration_audio:
            current, srt_path = self._apply_narration(
                assembler, storyboard, narration_audio, offsets, current, run_dir
            )

        bgm = self._pick_bgm(storyboard, bgm_tracks)
        if bgm is not None:
            duration = assembler.probe_duration(current) or float(
                storyboard.total_duration
            )
            bgm_path = run_dir / "_stage_bgm.mp4"
            assembler.add_bgm(current, bgm, duration, bgm_path)
            current = bgm_path

        if srt_path is not None and bool(narration_cfg["subtitles"]):
            subtitled_path = run_dir / "_stage_subtitled.mp4"
            if assembler.embed_subtitles(current, srt_path, subtitled_path):
                current = subtitled_path

        current.replace(final_path)
        for leftover in run_dir.glob("_stage_*.mp4"):
            leftover.unlink(missing_ok=True)

        log(f"✅ 完成!成片已保存: {final_path}")
        return final_path

    # ---------------- 旁白混音与字幕 ----------------

    def _apply_narration(
        self,
        assembler: Assembler,
        storyboard: Storyboard,
        narration_audio: dict[int, Path],
        offsets: list[float],
        current: Path,
        run_dir: Path,
    ) -> tuple[Path, Path | None]:
        """把旁白混入成片并生成字幕文件;失败时沿用无旁白版本。"""
        total = assembler.probe_duration(current) or float(storyboard.total_duration)
        segments: list[tuple[float, Path, float]] = []
        segment_shots = []
        for i, shot in enumerate(storyboard.shots):
            audio = narration_audio.get(shot.index)
            if audio is None:
                continue
            start = offsets[i]
            end = offsets[i + 1] if i + 1 < len(offsets) else total
            slot = max(1.0, end - start - 0.3)  # 留 0.3 秒呼吸,避免串到下一组
            segments.append((start, audio, slot))
            segment_shots.append(shot)

        narration_path = run_dir / "_stage_narration.mp4"
        timeline = assembler.mix_narration(
            current, segments, narration_path,
            volume=float(self._config["narration"]["volume"]),
        )
        if timeline is None:
            return current, None

        entries: list[tuple[float, float, str]] = []
        for (start, effective), shot in zip(timeline, segment_shots):
            entries += tts.narration_srt_entries(
                shot.narration.strip(), start, effective
            )
        srt_path = run_dir / f"{_safe_name(storyboard.title)}.srt"
        try:
            srt_path.write_text(tts.build_srt(entries), encoding="utf-8")
        except OSError:
            return narration_path, None
        return narration_path, srt_path

    # ---------------- 预检 ----------------

    def _preflight(self) -> None:
        """快速发现配置问题,避免流程走到一半才报错。"""
        try:
            resp = requests.get(
                "https://openrouter.ai/api/v1/key",
                headers={"Authorization": f"Bearer {self._config.openrouter_api_key}"},
                timeout=10,
            )
            if resp.status_code == 401:
                raise RuntimeError(
                    "OpenRouter API KEY 无效,请检查 config.yaml 中的 openrouter_api_key"
                )
        except requests.RequestException:
            pass  # 网络抖动不拦截,后续请求失败时会再给出明确提示

        try:
            free_gb = shutil.disk_usage(self._config.output_dir).free / 2**30
            if free_gb < 1:
                self._log(f"⚠ 磁盘剩余空间仅 {free_gb:.1f}GB,可能不足以保存成片")
        except OSError:
            pass

    # ---------------- 主角参考图 ----------------

    def _prepare_reference(self, generator, run_dir: Path, storyboard: Storyboard):
        """生成或复用主角参考图,返回其 URL;失败返回 None(降级纯文生)。"""
        from .kling import image_is_valid

        ref_path = run_dir / _REFERENCE_NAME
        if image_is_valid(ref_path):
            self._log("② 复用已有主角参考图,重新上传 …")
            url = generator.upload_image(ref_path)
            if url:
                return url
        self._log("② 正在生成主角参考图(锁定全片角色外观)…")
        url = generator.generate_reference(storyboard.reference_prompt, ref_path)
        if url is None:
            self._log("  参考图不可用,本次退回纯文字模式(角色一致性略降,不影响出片)。")
        return url

    # ---------------- 断点续传 ----------------

    def _resume_or_create(
        self, description: str, bgm_options: list[str]
    ) -> tuple[Path, Storyboard]:
        """同一描述且未产出成片的任务目录 → 恢复;否则新建目录并请 LLM 写分镜。"""
        key = _description_key(description)
        aspect = str(self._config["kling"]["aspect_ratio"])

        for candidate in sorted(self._config.output_dir.glob(f"*_{key}*"), reverse=True):
            manifest_path = candidate / _MANIFEST_NAME
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("description", "").strip() != description.strip():
                    continue
                # 画幅不同的旧任务不续传(片段横竖屏不能混拼)
                if manifest.get("aspect_ratio", aspect) != aspect:
                    continue
                storyboard = Storyboard.from_dict(manifest["storyboard"])
            except Exception:  # noqa: BLE001 - 损坏的 manifest 直接忽略
                continue
            final_path = candidate / f"{_safe_name(storyboard.title)}.mp4"
            if final_path.exists():
                continue  # 已完成的任务不复用,重新生成一版
            self._log(f"① 检测到未完成的任务,继续上次进度: {candidate.name}")
            return candidate, storyboard

        self._log("① 导演模型正在撰写分镜脚本 …")
        storyboard = Director(self._config).write_storyboard(
            description, bgm_options, aspect_ratio=aspect
        )

        base = f"{time.strftime('%Y%m%d_%H%M%S')}_{_safe_name(storyboard.title)}_{key}"
        run_dir = self._config.output_dir / base
        serial = 2
        while run_dir.exists():  # 避免与旧任务目录同名,导致误复用旧片段
            run_dir = self._config.output_dir / f"{base}_{serial}"
            serial += 1
        run_dir.mkdir(parents=True)
        manifest = {
            "description": description,
            "aspect_ratio": aspect,
            "storyboard": storyboard.to_dict(),
        }
        (run_dir / _MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (run_dir / "storyboard.txt").write_text(
            self._format_storyboard(description, storyboard), encoding="utf-8"
        )
        return run_dir, storyboard

    # ---------------- 背景音乐 ----------------

    def _bgm_tracks(self) -> list[Path]:
        music_dir = app_dir() / "music"
        if not music_dir.is_dir():
            return []
        return sorted(
            p for p in music_dir.iterdir() if p.suffix.lower() in _BGM_EXTS
        )

    def _pick_bgm(self, storyboard: Storyboard, tracks: list[Path]) -> Path | None:
        if not tracks:
            return None
        by_name = {t.name: t for t in tracks}
        if storyboard.bgm_file in by_name:
            return by_name[storyboard.bgm_file]
        return random.choice(tracks)

    # ---------------- 其他 ----------------

    def _log(self, message: str) -> None:
        self._ui_log(message)
        if self._logfile is not None:
            try:
                with open(self._logfile, "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%H:%M:%S')}] {message}\n")
            except OSError:
                pass

    def _report_progress(self, done: int, total: int) -> None:
        if self._progress is not None:
            try:
                self._progress(done, total)
            except Exception:  # noqa: BLE001 - 进度回调不应影响主流程
                pass

    @staticmethod
    def _format_storyboard(description: str, storyboard: Storyboard) -> str:
        lines = [
            f"创意: {description}",
            f"标题: {storyboard.title}",
            f"概要: {storyboard.logline}",
        ]
        if storyboard.reference_prompt:
            lines.append(f"主角参考图 prompt: {storyboard.reference_prompt}")
        if storyboard.bgm_file:
            lines.append(f"背景音乐: {storyboard.bgm_file}")
        lines.append("")
        for shot in storyboard.shots:
            lines.append(f"—— 镜头组 {shot.index}: {shot.title}({shot.duration}s)")
            if shot.narration.strip():
                lines.append(f"旁白: {shot.narration.strip()}")
            for j, cut in enumerate(shot.cuts, start=1):
                prefix = f"分镜 {j}({cut.duration}s)" if len(shot.cuts) > 1 else "prompt"
                lines.append(f"{prefix}: {cut.prompt}")
            lines += [f"negative: {shot.negative_prompt}", ""]
        return "\n".join(lines)
