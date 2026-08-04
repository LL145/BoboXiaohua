"""调用 fal.ai 生成素材:主角参考图(文生图)与各镜头组视频片段(Kling)。

- 多分镜镜头组:一个镜头组内含多个分镜时,走 Kling 的 multi_prompt 一次性
  连续生成,组内画面衔接由模型原生保证(优于事后转场拼接);
- 有固定主角时走 reference-to-video 的 elements 角色元素:参考图作为
  @Element1 随每个镜头组送入模型,主角外观全片一致(优于仅锁首帧);
- 任一环节失败都会自动降级为纯文生视频,绝不因参考图问题导致整体失败。
"""

from __future__ import annotations

import os
import re
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


def clip_is_valid(path: Path) -> bool:
    return path.exists() and path.stat().st_size >= _MIN_CLIP_BYTES


def image_is_valid(path: Path) -> bool:
    return path.exists() and path.stat().st_size >= _MIN_IMAGE_BYTES


def strip_reference_tokens(prompt: str) -> str:
    """去掉 @Element1/@Image1 之类的占位符(括号内的外观描述保留),用于降级纯文生。

    @Image1 是旧版脚本的参考图占位符,保留以兼容旧 manifest 的断点续传。
    """
    return re.sub(r"@(?:Element|Image)\d+\s*", "", prompt).strip()


class FatalGenerationError(RuntimeError):
    """重试无意义的错误(KEY 无效、余额不足等),应立即终止全部镜头。"""


class KlingGenerator:
    def __init__(self, config: Config, log: LogFn):
        self._config = config
        self._log = log
        # fal_client 通过 FAL_KEY 环境变量读取凭证
        os.environ["FAL_KEY"] = config.fal_api_key

    # ---------------- 主角参考图 ----------------

    def generate_reference(self, prompt: str, out_path: Path) -> str | None:
        """文生图生成主角参考图,下载到 out_path 并返回其 URL;失败返回 None。"""
        endpoint = str(self._config["image"]["endpoint"])
        aspect = str(self._config["kling"]["aspect_ratio"])
        arguments = {
            "prompt": prompt,
            "aspect_ratio": aspect if aspect in ("16:9", "9:16", "1:1") else "auto",
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
        """按镜头组构造 Kling 请求:多分镜走 multi_prompt,有主角走 elements。"""
        kling = self._config["kling"]
        combined = shot.combined_prompt.lower()
        use_reference = bool(reference_url) and (
            "@element" in combined or "@image" in combined
        )

        if use_reference:
            endpoint = str(kling["reference_endpoint"])
            arguments: dict = {
                "aspect_ratio": kling["aspect_ratio"],
                "generate_audio": bool(kling["generate_audio"]),
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
                "negative_prompt": shot.negative_prompt,
                "aspect_ratio": kling["aspect_ratio"],
                "generate_audio": bool(kling["generate_audio"]),
            }
            prompts = [strip_reference_tokens(cut.prompt) for cut in shot.cuts]

        if len(shot.cuts) > 1:
            arguments["multi_prompt"] = [
                {"prompt": prompt, "duration": str(cut.duration)}
                for prompt, cut in zip(prompts, shot.cuts)
            ]
        else:
            arguments["prompt"] = prompts[0]
            arguments["duration"] = str(max(3, shot.duration))
        return endpoint, arguments, use_reference

    def generate_clip(
        self, shot: Shot, out_path: Path, reference_url: str | None = None
    ) -> Path:
        """生成单个镜头组并下载到 out_path;已有有效片段时直接复用(断点续传)。"""
        if clip_is_valid(out_path):
            self._log(f"  镜头组 {shot.index} 已存在,跳过生成 ↺")
            return out_path

        kling = self._config["kling"]
        max_retries = int(kling["max_retries"])
        timeout = float(kling["shot_timeout"])
        endpoint, arguments, use_reference = self._build_arguments(shot, reference_url)

        last_error: Exception | None = None
        for attempt in range(1, max_retries + 2):
            try:
                self._log(
                    f"  镜头组 {shot.index} 提交 Kling 生成"
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
                if attempt <= max_retries:
                    time.sleep(min(3 * attempt, 15))

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
            time.sleep(_POLL_INTERVAL)

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
        with requests.get(url, stream=True, timeout=300) as resp:
            resp.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        tmp_path.replace(out_path)
        self._log(f"  已下载 → {out_path.name}")
