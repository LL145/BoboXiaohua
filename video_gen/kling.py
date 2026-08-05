"""调用 fal.ai 生成素材:主角参考图(文生图)与各镜头组视频片段。

支持两个视频引擎(config.yaml 的 video.engine 切换,默认 seedance):
- **Seedance 2.0**(字节跳动):多分镜用 "Cut scene to" 语法拼进单条 prompt
  一次连续生成;有固定主角时走 reference-to-video,参考图经 image_urls 送入、
  prompt 中以 @Image1 引用(导演脚本统一写 @Element1,提交前自动转换);
  原生支持 16:9/9:16/1:1/3:4/4:3 全部画幅与音频。
- **Kling 3**:多分镜走 multi_prompt 结构化参数;有固定主角时走
  reference-to-video 的 elements 角色元素(@Element1);3:4/4:3 画幅按相邻
  原生画幅生成、成片时居中裁剪。

公共稳健性(两个引擎一致):提交/轮询/下载/超时看门狗/取消,任一环节失败
自动降级为纯文生视频,绝不因参考图问题导致整体失败。
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

import requests

from .config import Config
from .director import Shot

LogFn = Callable[[str], None]

# 小于该体积的文件视为无效(错误页/截断下载)
_MIN_CLIP_BYTES = 10 * 1024
_MIN_IMAGE_BYTES = 5 * 1024
_POLL_INTERVAL = 5  # 轮询任务状态的间隔(秒)

# fal.ai Kling 端点的提示词长度硬上限(字符):超长会被 422 直接拒绝,
# 且同样参数重试必然再失败,所以提交前在本地钳制
_MAX_MULTI_PROMPT_CHARS = 512   # multi_prompt 内单条分镜提示词
_MAX_SINGLE_PROMPT_CHARS = 2500  # 单 prompt 与 negative_prompt

# Seedance 未公布 prompt 硬上限,取与 Kling 单 prompt 相同的保守值
_MAX_SEEDANCE_PROMPT_CHARS = 2500
# Seedance 单次生成时长范围(秒)
_SEEDANCE_MIN_SECONDS = 4
_SEEDANCE_MAX_SECONDS = 15

# Kling 视频端点原生支持的画幅;3:4 / 4:3 不被原生支持,
# 按相邻原生画幅生成,拼接成片时再居中裁剪出目标画幅
_NATIVE_ASPECTS = ("16:9", "9:16", "1:1")
_GENERATION_ASPECT = {"3:4": "9:16", "4:3": "16:9"}


def kling_generation_aspect(aspect: str) -> str:
    """返回 Kling 端点实际使用的生成画幅:非原生画幅映射到相邻原生画幅。"""
    aspect = str(aspect).strip()
    if aspect in _NATIVE_ASPECTS:
        return aspect
    return _GENERATION_ASPECT.get(aspect, "16:9")


def clip_is_valid(path: Path) -> bool:
    return path.exists() and path.stat().st_size >= _MIN_CLIP_BYTES


def image_is_valid(path: Path) -> bool:
    return path.exists() and path.stat().st_size >= _MIN_IMAGE_BYTES


def strip_reference_tokens(prompt: str) -> str:
    """去掉 @Element1/@Image1 之类的占位符(括号内的外观描述保留),用于降级纯文生。

    @Image1 是旧版脚本的参考图占位符,保留以兼容旧 manifest 的断点续传。
    """
    return re.sub(r"@(?:Element|Image)\d+\s*", "", prompt).strip()


def element_to_image_tokens(prompt: str) -> str:
    """把导演脚本统一使用的 @Element1 占位符转换为 Seedance 的 @Image1 引用。"""
    return re.sub(r"@Element(\d+)", r"@Image\1", prompt)


def join_cut_prompts(prompts: list[str]) -> str:
    """按 Seedance 的多镜头语法把各分镜 prompt 拼成一条:镜头间用 "Cut scene to" 衔接。"""
    parts: list[str] = []
    for prompt in prompts:
        prompt = prompt.strip()
        if not prompt:
            continue
        if parts:
            prompt = "Cut scene to " + prompt
        if not prompt.endswith((".", "!", "?")):
            prompt += "."
        parts.append(prompt)
    return " ".join(parts)


def fit_prompt(prompt: str, limit: int) -> str:
    """把提示词裁剪到长度上限内:尽量在句号/逗号等分句边界截断,避免拦腰斩词。"""
    prompt = prompt.strip()
    if len(prompt) <= limit:
        return prompt
    head = prompt[:limit]
    # 取最靠后的分句边界截断,尽量少丢内容(尾部通常是 style_anchor 风格词)
    pos = max(head.rfind(sep) for sep in (". ", "; ", ", "))
    if pos >= limit // 2:
        return head[:pos + 1].rstrip(" ,;")
    return head.rsplit(" ", 1)[0].rstrip(" ,;.")


class FatalGenerationError(RuntimeError):
    """重试无意义的错误(KEY 无效、余额不足等),应立即终止全部镜头。"""


class _FalGenerator:
    """fal.ai 生成器公共实现:提交/轮询/下载/看门狗/取消与降级重试。

    子类只需实现 `_build_arguments`(构造端点与请求参数)并设置引擎展示名。
    """

    _ENGINE_LABEL = "视频模型"

    def __init__(
        self,
        config: Config,
        log: LogFn,
        cancel_event: threading.Event | None = None,
    ):
        self._config = config
        self._log = log
        self._cancel = cancel_event
        # fal_client 通过 FAL_KEY 环境变量读取凭证
        os.environ["FAL_KEY"] = config.fal_api_key

    def _check_cancel(self) -> None:
        if self._cancel is not None and self._cancel.is_set():
            raise FatalGenerationError("已取消生成")

    def _sleep(self, seconds: float) -> None:
        """可被「取消」立即打断的等待。"""
        if self._cancel is not None:
            self._cancel.wait(seconds)
        else:
            time.sleep(seconds)

    # ---------------- 主角参考图 ----------------

    def generation_aspect(self, aspect: str) -> str:
        """引擎实际使用的生成画幅;与目标画幅不同,成片阶段会居中裁剪。"""
        return str(aspect).strip()

    def generate_reference(self, prompt: str, out_path: Path) -> str | None:
        """文生图生成主角参考图,下载到 out_path 并返回其 URL;失败返回 None。"""
        endpoint = str(self._config["image"]["endpoint"])
        aspect = str(self._config["video"]["aspect_ratio"])
        arguments = {
            "prompt": prompt,
            # 文生图端点原生支持 3:4 / 4:3,无需映射
            "aspect_ratio": aspect
            if aspect in ("16:9", "9:16", "1:1", "3:4", "4:3") else "auto",
            "num_images": 1,
            "output_format": "png",
        }
        for attempt in (1, 2):
            try:
                result = self._submit_and_wait(endpoint, arguments, timeout=600, label="参考图")
                url = result["images"][0]["url"]
                self._download(url, out_path)
                if not image_is_valid(out_path):
                    raise RuntimeError("下载的参考图无效(体积过小)")
                return url
            except FatalGenerationError:
                raise
            except Exception as exc:  # noqa: BLE001 - 参考图失败可降级,不致命
                self._log(f"  参考图第 {attempt} 次生成失败: {exc}")
                time.sleep(3)
        return None

    def upload_image(self, path: Path) -> str | None:
        """把本地参考图上传到 fal 存储,返回可供模型引用的 URL。"""
        import fal_client

        try:
            return fal_client.upload_file(path)
        except Exception as exc:  # noqa: BLE001
            self._log(f"  参考图上传失败: {exc}")
            return None

    # ---------------- 镜头片段 ----------------

    def _build_arguments(
        self, shot: Shot, reference_url: str | None
    ) -> tuple[str, dict, bool]:
        """构造 (端点, 请求参数, 是否参考图模式),由具体引擎实现。"""
        raise NotImplementedError

    def generate_clip(
        self, shot: Shot, out_path: Path, reference_url: str | None = None
    ) -> Path:
        """生成单个镜头组并下载到 out_path;已有有效片段时直接复用(断点续传)。"""
        if clip_is_valid(out_path):
            self._log(f"  镜头组 {shot.index} 已存在,跳过生成 ↺")
            return out_path

        video_cfg = self._config["video"]
        max_retries = int(video_cfg["max_retries"])
        timeout = float(video_cfg["shot_timeout"])
        endpoint, arguments, use_reference = self._build_arguments(shot, reference_url)

        last_error: Exception | None = None
        for attempt in range(1, max_retries + 2):
            self._check_cancel()
            try:
                self._log(
                    f"  镜头组 {shot.index} 提交 {self._ENGINE_LABEL} 生成"
                    + (f"({len(shot.cuts)} 个分镜连续生成)" if len(shot.cuts) > 1 else "")
                    + ("(带主角参考图)" if use_reference else "")
                    + (f"(第 {attempt} 次尝试)" if attempt > 1 else "")
                    + " …"
                )
                result = self._submit_and_wait(
                    endpoint, arguments, timeout, label=f"镜头组 {shot.index}"
                )
                self._download(result["video"]["url"], out_path)
                if not clip_is_valid(out_path):
                    raise RuntimeError("下载的片段无效(体积过小)")
                return out_path
            except FatalGenerationError:
                raise
            except Exception as exc:  # noqa: BLE001 - 逐镜头组重试,最终仍会抛出
                last_error = exc
                self._log(f"  镜头组 {shot.index} 第 {attempt} 次尝试失败: {exc}")
                if getattr(exc, "status_code", None) == 422:
                    # 参数校验错误是确定性的,同样参数重试必然再失败
                    self._log(f"  镜头组 {shot.index} 请求参数被拒,跳过重试 …")
                    break
                if attempt <= max_retries:
                    self._sleep(min(3 * attempt, 15))

        if use_reference:
            # 参考图模式反复失败 → 降级为纯文生视频(该组一致性略降,但保住成片)
            self._log(f"  镜头组 {shot.index} 参考图模式多次失败,降级为纯文生视频重试 …")
            return self.generate_clip(shot, out_path, reference_url=None)
        raise RuntimeError(f"镜头组 {shot.index} 多次生成失败: {last_error}") from last_error

    # ---------------- fal 任务提交与等待 ----------------

    def _submit_and_wait(
        self, endpoint: str, arguments: dict, timeout: float, label: str
    ) -> dict:
        """提交任务并轮询直至完成,带超时看门狗与排队进度提示。"""
        import fal_client

        try:
            handle = fal_client.submit(endpoint, arguments=arguments)
        except Exception as exc:
            raise self._classify(exc, endpoint)

        deadline = time.monotonic() + timeout
        last_position = -1
        while True:
            if self._cancel is not None and self._cancel.is_set():
                try:
                    handle.cancel()
                except Exception:  # noqa: BLE001
                    pass
                raise FatalGenerationError("已取消生成")
            try:
                status = handle.status(with_logs=False)
            except Exception as exc:  # noqa: BLE001
                classified = self._classify(exc, endpoint)
                if isinstance(classified, FatalGenerationError):
                    raise classified
                status = None  # 瞬时网络错误,继续等待
            if isinstance(status, fal_client.Completed):
                break
            if (
                isinstance(status, fal_client.Queued)
                and status.position != last_position
                and status.position > 0
            ):
                last_position = status.position
                self._log(f"  {label} 排队中(前方还有 {status.position} 个任务)…")
            if time.monotonic() > deadline:
                try:
                    handle.cancel()
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError(f"{label} 生成超时(超过 {int(timeout)} 秒)")
            self._sleep(_POLL_INTERVAL)

        try:
            return handle.get()
        except Exception as exc:
            raise self._classify(exc, endpoint)

    @staticmethod
    def _classify(exc: Exception, endpoint: str) -> Exception:
        """把 fal 的 HTTP 错误翻译成用户能看懂的提示;致命错误不再重试。"""
        code = getattr(exc, "status_code", None)
        if code in (401, 403):
            return FatalGenerationError(
                "fal.ai API KEY 无效或无权限,请检查 config.yaml 中的 fal_api_key"
            )
        if code == 402:
            return FatalGenerationError("fal.ai 余额不足,请前往 fal.ai 充值")
        if code == 404:
            return FatalGenerationError(
                f"fal.ai 模型端点不存在: {endpoint},请检查 config.yaml"
            )
        return exc

    # ---------------- 下载 ----------------

    def _download(self, url: str, out_path: Path) -> None:
        """先写临时文件再原子改名,避免半截文件被断点续传误认为有效。"""
        tmp_path = out_path.with_suffix(".part")
        try:
            with requests.get(url, stream=True, timeout=300) as resp:
                resp.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        self._check_cancel()
                        f.write(chunk)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        tmp_path.replace(out_path)
        self._log(f"  已下载 → {out_path.name}")


class SeedanceGenerator(_FalGenerator):
    """Seedance 2.0(字节跳动,经 fal.ai):默认视频引擎。"""

    _ENGINE_LABEL = "Seedance"

    def _build_arguments(
        self, shot: Shot, reference_url: str | None
    ) -> tuple[str, dict, bool]:
        """多分镜以 "Cut scene to" 语法拼成单条 prompt,有主角走 image_urls 参考图。"""
        seedance = self._config["seedance"]
        video_cfg = self._config["video"]
        combined = shot.combined_prompt.lower()
        use_reference = bool(reference_url) and (
            "@element" in combined or "@image" in combined
        )

        if use_reference:
            endpoint = str(seedance["reference_endpoint"])
            # Seedance 在 prompt 中用 @Image1 引用 image_urls 里的参考图;
            # 导演脚本统一写 @Element1,在此转换(旧脚本的 @Image1 原样可用)
            prompts = [element_to_image_tokens(cut.prompt) for cut in shot.cuts]
        else:
            endpoint = str(seedance["text_endpoint"])
            prompts = [strip_reference_tokens(cut.prompt) for cut in shot.cuts]

        prompt = join_cut_prompts(prompts)
        if len(prompt) > _MAX_SEEDANCE_PROMPT_CHARS:
            self._log(
                f"  ⚠ 镜头组 {shot.index} 提示词超长({len(prompt)} 字符),"
                f"已裁剪到 {_MAX_SEEDANCE_PROMPT_CHARS} 字符内"
            )
            prompt = fit_prompt(prompt, _MAX_SEEDANCE_PROMPT_CHARS)

        duration = min(_SEEDANCE_MAX_SECONDS, max(_SEEDANCE_MIN_SECONDS, shot.duration))
        arguments: dict = {
            "prompt": prompt,
            "duration": str(duration),
            # Seedance 原生支持 16:9/9:16/1:1/3:4/4:3 全部画幅,无需映射裁剪
            "aspect_ratio": str(video_cfg["aspect_ratio"]),
            "resolution": str(seedance["resolution"]),
            "generate_audio": bool(video_cfg["generate_audio"]),
        }
        if use_reference:
            arguments["image_urls"] = [reference_url]
        return endpoint, arguments, use_reference


class KlingGenerator(_FalGenerator):
    """Kling 3(快手,经 fal.ai):可选视频引擎(video.engine: kling)。"""

    _ENGINE_LABEL = "Kling"

    def generation_aspect(self, aspect: str) -> str:
        return kling_generation_aspect(aspect)

    def _build_arguments(
        self, shot: Shot, reference_url: str | None
    ) -> tuple[str, dict, bool]:
        """多分镜走 multi_prompt,有主角走 elements 角色元素。"""
        kling = self._config["kling"]
        video_cfg = self._config["video"]
        combined = shot.combined_prompt.lower()
        use_reference = bool(reference_url) and (
            "@element" in combined or "@image" in combined
        )

        aspect = kling_generation_aspect(str(video_cfg["aspect_ratio"]))
        if use_reference:
            endpoint = str(kling["reference_endpoint"])
            arguments: dict = {
                "aspect_ratio": aspect,
                "generate_audio": bool(video_cfg["generate_audio"]),
            }
            if "@element" in combined:
                # elements 角色元素:正面图 + 参考角度图(同图即可满足要求)
                arguments["elements"] = [{
                    "frontal_image_url": reference_url,
                    "reference_image_urls": [reference_url],
                }]
            else:
                # 旧 manifest 的 @Image1 走 image_urls 参考图,保持断点续传兼容
                arguments["image_urls"] = [reference_url]
            prompts = [cut.prompt for cut in shot.cuts]
        else:
            endpoint = str(kling["text_endpoint"])
            arguments = {
                "negative_prompt": fit_prompt(
                    shot.negative_prompt, _MAX_SINGLE_PROMPT_CHARS
                ),
                "aspect_ratio": aspect,
                "generate_audio": bool(video_cfg["generate_audio"]),
            }
            prompts = [strip_reference_tokens(cut.prompt) for cut in shot.cuts]

        limit = (
            _MAX_MULTI_PROMPT_CHARS if len(shot.cuts) > 1 else _MAX_SINGLE_PROMPT_CHARS
        )
        for i, prompt in enumerate(prompts):
            if len(prompt) > limit:
                self._log(
                    f"  ⚠ 镜头组 {shot.index} 分镜 {i + 1} 提示词超长"
                    f"({len(prompt)} 字符),已裁剪到 {limit} 字符内"
                )
                prompts[i] = fit_prompt(prompt, limit)

        if len(shot.cuts) > 1:
            arguments["multi_prompt"] = [
                {"prompt": prompt, "duration": str(cut.duration)}
                for prompt, cut in zip(prompts, shot.cuts)
            ]
        else:
            arguments["prompt"] = prompts[0]
            arguments["duration"] = str(max(3, shot.duration))
        return endpoint, arguments, use_reference


def create_generator(
    config: Config, log: LogFn, cancel_event: threading.Event | None = None
) -> _FalGenerator:
    """按 video.engine 创建对应引擎的生成器(默认 seedance)。"""
    cls = SeedanceGenerator if config.engine == "seedance" else KlingGenerator
    return cls(config, log, cancel_event=cancel_event)
