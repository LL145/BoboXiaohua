"""生成流水线:一句话描述 → 分镜脚本 → 主角参考图 → 并行逐镜头组生成
→ 旁白配音 → 拼接成片(转场/旁白/背景音乐/字幕)。

稳健性设计:
- 生成前预检:先校验 OpenRouter KEY、fal KEY 与磁盘空间,配错即刻提示,不浪费费用;
- 断点续传:同一描述的未完成任务会复用已有分镜脚本、参考图、旁白音频和
  已生成片段,失败后再次点击「生成」只补齐缺失部分,不重复扣费;
- 主角参考图:用户可上传主角图片(随创意发给导演模型照图写外观描述),
  未上传时导演模型判断有固定主角则自动文生图;参考图作为角色元素
  (@Element1)送入每个镜头组,任何一步失败都自动降级为纯文生视频;
- 并行生成:多个镜头组同时提交视频引擎(默认 Seedance 2.0,可切 Kling),
  总耗时约等于单个镜头组;
- 单镜头组独立重试 + 超时看门狗,KEY 无效等致命错误立即终止,不空耗重试;
- 旁白与字幕:导演判断影片需要解说时,用 Edge TTS 合成旁白并生成字幕;
  TTS 不可用、混音或字幕失败,都只是放弃对应环节,绝不影响画面成片;
- 运行日志同步写入任务目录 log.txt,便于排查问题;
- 背景音乐:程序目录 music/ 下有音频文件时,由导演模型按影片情绪挑选混入;
- 费用预估:生成前按待生成镜头秒数估算 fal 费用并展示;
- 可取消:界面「取消」按钮置位 cancel_event,Kling 轮询/等待/片段下载、
  旁白合成循环与拼接各阶段之间均及时检查,以 GenerationCancelled 停止,
  已完成产物保留、可断点续传。
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import shutil
import threading
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
# 进度回调:(总进度百分比 0~100, 当前阶段文字)
ProgressFn = Callable[[int, str], None]

_MANIFEST_NAME = "manifest.json"
_REFERENCE_NAME = "reference.png"
_BGM_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


def _safe_name(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", title).strip("_")
    return name[:40] or "video"


def _description_key(description: str) -> str:
    return hashlib.sha1(description.strip().encode("utf-8")).hexdigest()[:8]


class GenerationCancelled(RuntimeError):
    """用户主动取消本次生成;已完成的产物保留,可断点续传。"""


class Pipeline:
    def __init__(
        self,
        config: Config,
        log: LogFn,
        progress: ProgressFn | None = None,
        cancel_event: threading.Event | None = None,
    ):
        self._config = config
        self._ui_log = log
        self._progress = progress
        self._cancel = cancel_event
        self._logfile: Path | None = None

    def _check_cancel(self) -> None:
        if self._cancel is not None and self._cancel.is_set():
            raise GenerationCancelled("已取消生成")

    # ---------------- 主流程 ----------------

    def run(self, description: str, reference_image: Path | None = None) -> Path:
        """执行完整流程,返回成片路径。

        reference_image 为用户上传的主角图片(可选):作为角色元素锁定主角外观,
        并随创意发给导演模型照图撰写外观描述;不提供时由导演判断是否自动生成。
        """
        if reference_image is not None:
            reference_image = Path(reference_image)
        from .kling import FatalGenerationError, clip_is_valid, create_generator

        config = self._config
        log = self._log

        assembler = Assembler(config, log)
        if not assembler.check_ffmpeg():
            raise RuntimeError(
                f"未找到 ffmpeg(查找路径: {config.ffmpeg_path})。"
                "请安装 ffmpeg 并加入 PATH,或在 config.yaml 的 ffmpeg.path 中填写完整路径。"
            )
        self._preflight()
        self._report_progress(2, "撰写分镜脚本")

        # 1. 分镜脚本:优先恢复未完成任务,否则请 LLM 新写
        bgm_tracks = self._bgm_tracks()
        run_dir, storyboard = self._resume_or_create(
            description, [t.name for t in bgm_tracks], reference_image
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

        # 费用预估:只计待生成的镜头秒数,已复用的镜头不重复计费
        price = float(config.engine_section["price_per_second"])
        if pending and price > 0:
            seconds = sum(s.duration for s in pending)
            log(
                f"  💰 预计本次视频生成费用约 {seconds} 秒 × ${price:g}/秒"
                f" ≈ ${seconds * price:.2f}(分镜脚本与参考图另计少量费用)"
            )

        self._check_cancel()
        generator = create_generator(config, log, cancel_event=self._cancel)

        # 2. 主角参考图(用户上传优先;否则导演判断有固定主角时自动生成;
        # 失败自动降级纯文生)
        reference_url: str | None = None
        if pending and (reference_image or storyboard.reference_prompt):
            self._report_progress(8, "准备主角参考图")
            reference_url = self._prepare_reference(
                generator, run_dir, storyboard, reference_image
            )

        # 3. 视频引擎并行生成各镜头
        concurrency = max(1, int(config["video"]["concurrency"]))
        if done_before:
            log(f"③ 已有 {done_before} 个镜头可复用,补齐剩余 {len(pending)} 个 …")
        else:
            log(
                f"③ {config.engine_name} 并行生成 {len(pending)} 个镜头"
                f"(并发 {concurrency},约需十几分钟)…"
            )
        self._shot_progress(done_before, len(storyboard.shots))

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
                        self._shot_progress(finished, len(storyboard.shots))
                    except FatalGenerationError as exc:
                        # KEY 无效等致命错误:取消尚未开始的镜头,立即终止
                        pool.shutdown(wait=False, cancel_futures=True)
                        if self._cancel is not None and self._cancel.is_set():
                            raise GenerationCancelled("已取消生成") from None
                        raise RuntimeError(str(exc)) from exc
                    except Exception as exc:  # noqa: BLE001
                        errors.append(str(exc))
            self._check_cancel()
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
            self._report_progress(82, "合成旁白配音")
            narration_audio = tts.synthesize_all(
                storyboard, run_dir, str(narration_cfg["voice"]), log,
                cancel=self._cancel,
            )
            step = 5

        # 5. ffmpeg 拼接:转场 → 旁白 → 背景音乐 → 字幕,逐级可降级
        self._check_cancel()
        log(f"{'④⑤⑥⑦⑧'[step - 4]} 正在拼接成片 …")
        self._report_progress(86, "拼接成片")
        final_path = run_dir / f"{_safe_name(storyboard.title)}.mp4"
        stage_path = run_dir / "_stage_concat.mp4"
        _, offsets = assembler.concat(
            clips, stage_path,
            fallback_durations=[float(s.duration) for s in storyboard.shots],
        )
        current = stage_path

        # 引擎不原生支持的画幅(Kling 的 3:4 / 4:3):片段按相邻原生画幅生成,
        # 在此居中裁剪出目标画幅(须在字幕烧录前,失败沿用生成画幅)
        aspect = str(config["video"]["aspect_ratio"])
        if generator.generation_aspect(aspect) != aspect:
            self._check_cancel()
            self._report_progress(88, "裁剪画幅")
            crop_path = run_dir / "_stage_crop.mp4"
            if assembler.crop_to_aspect(current, aspect, crop_path):
                current = crop_path

        srt_path: Path | None = None
        if narration_audio:
            self._check_cancel()
            self._report_progress(90, "混入旁白与生成字幕")
            current, srt_path = self._apply_narration(
                assembler, storyboard, narration_audio, offsets, current, run_dir
            )

        bgm = self._pick_bgm(storyboard, bgm_tracks)
        if bgm is not None:
            self._check_cancel()
            self._report_progress(93, "混入背景音乐")
            duration = assembler.probe_duration(current) or float(
                storyboard.total_duration
            )
            bgm_path = run_dir / "_stage_bgm.mp4"
            assembler.add_bgm(current, bgm, duration, bgm_path)
            current = bgm_path

        if srt_path is not None and bool(narration_cfg["subtitles"]):
            self._check_cancel()
            self._report_progress(96, "烧录字幕")
            subtitled_path = run_dir / "_stage_subtitled.mp4"
            if assembler.embed_subtitles(current, srt_path, subtitled_path):
                current = subtitled_path

        current.replace(final_path)
        for leftover in run_dir.glob("_stage_*.mp4"):
            leftover.unlink(missing_ok=True)

        self._report_progress(100, "完成")
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
        voice = str(self._config["narration"]["voice"])
        segments: list[tuple[float, Path, float]] = []
        segment_shots = []
        for i, shot in enumerate(storyboard.shots):
            audio = narration_audio.get(shot.index)
            if audio is None:
                continue
            start = offsets[i]
            end = offsets[i + 1] if i + 1 < len(offsets) else total
            slot = max(1.0, end - start - 0.3)  # 留 0.3 秒呼吸,避免串到下一组
            audio = self._fit_narration(assembler, shot, audio, slot, voice)
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
        for (start, effective, tempo), (_, audio, _), shot in zip(
            timeline, segments, segment_shots
        ):
            entries += self._srt_entries(shot, audio, start, effective, tempo)
        srt_path = run_dir / f"{_safe_name(storyboard.title)}.srt"
        try:
            srt_path.write_text(tts.build_srt(entries), encoding="utf-8")
        except OSError:
            return narration_path, None
        return narration_path, srt_path

    def _fit_narration(
        self, assembler: Assembler, shot, audio: Path, slot: float, voice: str
    ) -> Path:
        """旁白明显超长时,用更快语速重新合成一版(原生变速,音质自然);
        重合成失败则保留原音频,由混音阶段的 atempo 兜底加速。"""
        duration = assembler.probe_duration(audio)
        if duration is None or duration <= slot * 1.02:
            return audio
        rate = min(40, math.ceil((duration / slot - 1) * 100))
        fast = audio.with_name(f"{audio.stem}_r{rate}.mp3")
        if not tts.narration_is_valid(fast):
            self._log(
                f"  镜头组 {shot.index} 旁白超长({duration:.1f}s > {slot:.1f}s),"
                f"以 +{rate}% 语速重新合成 …"
            )
            tts.synthesize(
                shot.narration.strip(), voice, fast, self._log, shot.index, rate=rate
            )
        return fast if tts.narration_is_valid(fast) else audio

    @staticmethod
    def _srt_entries(
        shot, audio: Path, start: float, effective: float, tempo: float
    ) -> list[tuple[float, float, str]]:
        """字幕条目:优先用合成时记录的逐句精确时间轴,缺失时按字数比例估算。"""
        sentences = tts.load_timeline(audio)
        if sentences:
            entries: list[tuple[float, float, str]] = []
            for s, e, text in sentences:
                begin = s / tempo
                if begin >= effective:
                    break  # 被截断的尾句不再显示字幕
                entries.append((start + begin, start + min(e / tempo, effective), text))
            if entries:
                return entries
        return tts.narration_srt_entries(shot.narration.strip(), start, effective)

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

        # fal key 探测:查询一个不存在的任务,零费用;key 无效时 fal 返回 401/403,
        # 有效时仅是任务不存在(404 等),其余状态一律放行
        try:
            endpoint = str(self._config.engine_section["text_endpoint"])
            app_root = "/".join(endpoint.split("/")[:2])
            resp = requests.get(
                f"https://queue.fal.run/{app_root}/requests/"
                "00000000-0000-0000-0000-000000000000/status",
                headers={"Authorization": f"Key {self._config.fal_api_key}"},
                timeout=10,
            )
            if resp.status_code in (401, 403):
                raise RuntimeError(
                    "fal.ai API KEY 无效,请检查 config.yaml 中的 fal_api_key"
                )
        except requests.RequestException:
            pass

        try:
            free_gb = shutil.disk_usage(self._config.output_dir).free / 2**30
            if free_gb < 1:
                self._log(f"⚠ 磁盘剩余空间仅 {free_gb:.1f}GB,可能不足以保存成片")
        except OSError:
            pass

    # ---------------- 主角参考图 ----------------

    def _prepare_reference(
        self,
        generator,
        run_dir: Path,
        storyboard: Storyboard,
        user_image: Path | None = None,
    ):
        """准备主角参考图并返回其 URL;失败返回 None(降级纯文生)。

        优先级:用户上传的图片 → 任务目录中已有的参考图(断点续传)→
        按导演的 reference_prompt 文生图。
        """
        from .kling import image_is_valid

        if user_image is not None and image_is_valid(user_image):
            # 复制进任务目录(保留原扩展名,保证上传时内容类型正确),断点续传可复用
            local = run_dir / f"reference{user_image.suffix.lower()}"
            try:
                if user_image.resolve() != local.resolve():
                    shutil.copyfile(user_image, local)
            except OSError as exc:
                self._log(f"  ⚠ 复制主角图片失败: {exc}")
                local = user_image
            self._log("② 使用用户上传的主角图片作为参考图,上传中 …")
            url = generator.upload_image(local)
            if url:
                return url
        elif user_image is not None:
            self._log("  ⚠ 上传的主角图片无效(文件缺失或过小),忽略。")

        existing = next(
            (p for p in sorted(run_dir.glob("reference.*")) if image_is_valid(p)), None
        )
        if existing is not None:
            self._log("② 复用已有主角参考图,重新上传 …")
            url = generator.upload_image(existing)
            if url:
                return url
        if storyboard.reference_prompt:
            self._log("② 正在生成主角参考图(锁定全片角色外观)…")
            url = generator.generate_reference(
                storyboard.reference_prompt, run_dir / _REFERENCE_NAME
            )
            if url is not None:
                return url
        self._log("  参考图不可用,本次退回纯文字模式(角色一致性略降,不影响出片)。")
        return None

    # ---------------- 断点续传 ----------------

    def _resume_or_create(
        self,
        description: str,
        bgm_options: list[str],
        reference_image: Path | None = None,
    ) -> tuple[Path, Storyboard]:
        """同一描述且未产出成片的任务目录 → 恢复;否则新建目录并请 LLM 写分镜。"""
        key = _description_key(description)
        aspect = str(self._config["video"]["aspect_ratio"])
        target = int(self._config["video"]["target_duration"])
        engine = self._config.engine

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
                # 目标时长不同的旧任务不续传(分镜脚本按旧时长设计)
                if int(manifest.get("target_duration", target)) != target:
                    continue
                # 引擎不同的旧任务不续传(画质风格与分镜约束不同,不混拼;
                # 旧 manifest 无 engine 字段,均为 Kling 时代的任务)
                if manifest.get("engine", "kling") != engine:
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
            description, bgm_options, aspect_ratio=aspect,
            reference_image=reference_image,
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
            "target_duration": target,
            "engine": engine,
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

    def _report_progress(self, percent: int, stage: str) -> None:
        if self._progress is not None:
            try:
                self._progress(max(0, min(100, percent)), stage)
            except Exception:  # noqa: BLE001 - 进度回调不应影响主流程
                pass

    def _shot_progress(self, done: int, total: int) -> None:
        """镜头生成占总进度的 10%~80%,按完成数线性推进。"""
        percent = 10 + round(70 * done / max(1, total))
        self._report_progress(percent, f"生成镜头 {done}/{total}")

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
