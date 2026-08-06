"""调用视频引擎生成素材:主角参考图(文生图)与各镜头组视频片段。

支持三个视频引擎(config.yaml 的 video.engine 切换,默认 seedance25):
- **Seedance 2.0**(字节跳动,经 fal.ai):多分镜用 "Cut scene to" 语法拼进
  单条 prompt 一次连续生成;有固定主角时走 reference-to-video,参考图经
  image_urls 送入、prompt 中以 @Image1 引用(导演脚本统一写 @Element1,
  提交前自动转换);原生支持 16:9/9:16/1:1/3:4/4:3 全部画幅与音频。
- **Seedance 2.5**(字节跳动,经火山方舟官方 API,默认):fal.ai 尚未上线 2.5,
  直连火山方舟(Volcengine Ark)的视频生成任务接口(需额外配 ark_api_key);
  单组最长 30 秒一次连续生成,参考图以 base64 data URL 随请求送入
  (role: reference_image),prompt 中按方舟官方语法以 @图片1 引用。
- **Kling 3**(快手,经 fal.ai):多分镜走 multi_prompt 结构化参数;有固定
  主角时走 reference-to-video 的 elements 角色元素(@Element1);3:4/4:3
  画幅按相邻原生画幅生成、成片时居中裁剪。

公共稳健性(全部引擎一致):提交/轮询/下载/超时看门狗/取消,任一环节失败
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
# Seedance 2.0 单次生成时长范围(秒)
_SEEDANCE_MIN_SECONDS = 4
_SEEDANCE_MAX_SECONDS = 15
# Seedance 2.5(火山方舟)单次生成时长范围(秒):官方支持一次连续生成 30 秒
_ARK_MIN_SECONDS = 4
_ARK_MAX_SECONDS = 30

# 各引擎支持的参考图张数上限(超出部分按顺序丢弃,pipeline 会提前告知用户)
MAX_REFERENCE_IMAGES = {
    "seedance25": 30,  # 方舟官方:单次最多 30 张参考图
    "seedance": 9,     # fal reference-to-video:最多 9 张
    "kling": 4,        # elements 单角色的多角度参考,保守取 4 张
}

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


# 中日韩统一表意文字:用于判断 prompt 语言,选择对应的多镜头衔接语法
_CJK_RE = re.compile(r"[一-鿿]")


def strip_reference_tokens(prompt: str) -> str:
    """去掉 @Element1/@Image1/@图片1 之类的占位符(括号内的外观描述保留),用于降级纯文生。

    @Image1/@图片1 是提交阶段或旧版脚本的参考图占位符,一并处理以兼容
    旧 manifest 的断点续传。
    """
    return re.sub(r"@(?:Element|Image|图片)\d+\s*", "", prompt).strip()


def element_to_image_tokens(prompt: str) -> str:
    """把导演脚本统一使用的 @Element1 占位符转换为 fal Seedance 的 @Image1 引用。"""
    return re.sub(r"@Element(\d+)", r"@Image\1", prompt)


def element_to_ark_image_tokens(prompt: str) -> str:
    """把 @Element1/@Image1 占位符转换为火山方舟官方的 @图片1 引用语法。"""
    return re.sub(r"@(?:Element|Image)(\d+)", r"@图片\1", prompt)


def join_cut_prompts(prompts: list[str]) -> str:
    """按 Seedance 的多镜头语法把各分镜 prompt 拼成一条。

    镜头间衔接语按 prompt 语言选择:英文用 "Cut scene to",中文用「镜头切换:」
    (与官方中文提示词指南一致);语言逐条判断,兼容旧 manifest 的英文脚本。
    """
    parts: list[str] = []
    for prompt in prompts:
        prompt = prompt.strip()
        if not prompt:
            continue
        is_cjk = bool(_CJK_RE.search(prompt))
        if parts:
            prompt = ("镜头切换:" if is_cjk else "Cut scene to ") + prompt
        if not prompt.endswith((".", "!", "?", "。", "!", "?")):
            prompt += "。" if is_cjk else "."
        parts.append(prompt)
    return " ".join(parts)


def join_cut_prompts_timed(prompts_durations: list[tuple[str, int]]) -> str:
    """按时间戳分块把各分镜 prompt 拼成一条(Seedance 2.5 官方推荐写法)。

    30 秒长镜头组最常见的失败是"后半段漂移":没有时间轴引导时模型会用
    不受控的内容填满剩余时长。时间戳块(如 `[0-4秒] …`)把每个分镜的时长
    比例明确传给模型,节奏由导演脚本掌控;空 prompt 的分镜跳过但时长仍
    计入时间轴,保证后续块的时间戳正确。
    """
    parts: list[str] = []
    start = 0
    for prompt, duration in prompts_durations:
        end = start + max(0, int(duration))
        text = prompt.strip()
        if text:
            is_cjk = bool(_CJK_RE.search(text))
            if not text.endswith((".", "!", "?", "。", "!", "?")):
                text += "。" if is_cjk else "."
            label = f"[{start}-{end}秒] " if is_cjk else f"[{start}-{end}s] "
            parts.append(label + text)
        start = end
    return " ".join(parts)


def reference_usage_note(notes: list[str], token_format: str) -> str:
    """生成参考素材的用途说明,附在 prompt 末尾。

    社区经验:未标注用途的参考图是效果不佳的最常见原因——每个参考素材
    都应说明用途,prompt 中用"参考图中的角色"式引用而非重新描述。
    token_format 如 "@图片{}"(方舟)或 "@Image{}"(fal)。
    """
    if not notes:
        return ""
    parts = [
        f"{token_format.format(i)}:{note.strip() or '主角形象参考'}"
        for i, note in enumerate(notes, 1)
    ]
    return " 参考素材用途——" + ";".join(parts) + "。请保持画面中主角外观与参考图严格一致。"


def fit_prompt(prompt: str, limit: int) -> str:
    """把提示词裁剪到长度上限内:尽量在句号/逗号等分句边界截断,避免拦腰斩词。"""
    prompt = prompt.strip()
    if len(prompt) <= limit:
        return prompt
    head = prompt[:limit]
    # 取最靠后的分句边界截断,尽量少丢内容(尾部通常是 style_anchor 风格词);
    # 中英文标点都算边界
    pos = max(head.rfind(sep) for sep in (". ", "; ", ", ", "。", ";", ",", "!", "?"))
    if pos >= limit // 2:
        return head[:pos + 1].rstrip(" ,;,;")
    return head.rsplit(" ", 1)[0].rstrip(" ,;.,;。")


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
                # 参考图文生图固定走 fal(视频引擎为方舟直连时亦然)
                result = self._fal_submit_and_wait(
                    endpoint, arguments, timeout=600, label="参考图"
                )
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
        self, shot: Shot, references: list[tuple[str, str]] | None
    ) -> tuple[str, dict, bool]:
        """构造 (端点, 请求参数, 是否参考图模式),由具体引擎实现。

        references 为参考图列表 [(URL, 用途说明)];None/空表示纯文生。
        """
        raise NotImplementedError

    def generate_clip(
        self,
        shot: Shot,
        out_path: Path,
        references: list[tuple[str, str]] | None = None,
    ) -> Path:
        """生成单个镜头组并下载到 out_path;已有有效片段时直接复用(断点续传)。"""
        if clip_is_valid(out_path):
            self._log(f"  镜头组 {shot.index} 已存在,跳过生成 ↺")
            return out_path

        video_cfg = self._config["video"]
        max_retries = int(video_cfg["max_retries"])
        timeout = float(video_cfg["shot_timeout"])
        endpoint, arguments, use_reference = self._build_arguments(shot, references)

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
            return self.generate_clip(shot, out_path, references=None)
        raise RuntimeError(f"镜头组 {shot.index} 多次生成失败: {last_error}") from last_error

    # ---------------- fal 任务提交与等待 ----------------

    def _submit_and_wait(
        self, endpoint: str, arguments: dict, timeout: float, label: str
    ) -> dict:
        """提交视频任务并轮询直至完成;方舟直连引擎会覆写本方法。"""
        return self._fal_submit_and_wait(endpoint, arguments, timeout, label)

    def _fal_submit_and_wait(
        self, endpoint: str, arguments: dict, timeout: float, label: str
    ) -> dict:
        """提交 fal 任务并轮询直至完成,带超时看门狗与排队进度提示。"""
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
    """Seedance 2.0(字节跳动,经 fal.ai):可选引擎(video.engine: seedance)。"""

    _ENGINE_LABEL = "Seedance"

    def _build_arguments(
        self, shot: Shot, references: list[tuple[str, str]] | None
    ) -> tuple[str, dict, bool]:
        """多分镜以 "Cut scene to" 语法拼成单条 prompt,有主角走 image_urls 参考图。"""
        seedance = self._config["seedance"]
        video_cfg = self._config["video"]
        combined = shot.combined_prompt.lower()
        use_reference = bool(references) and (
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
        if use_reference:
            refs = references[:MAX_REFERENCE_IMAGES["seedance"]]
            prompt += reference_usage_note([note for _, note in refs], "@Image{}")
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
            arguments["image_urls"] = [url for url, _ in refs]
        return endpoint, arguments, use_reference


class ArkSeedanceGenerator(_FalGenerator):
    """Seedance 2.5(字节跳动,经火山方舟官方 API):默认视频引擎。

    fal.ai 尚未上线 Seedance 2.5,因此直连火山方舟(Volcengine Ark)的
    视频生成任务接口:POST 创建任务 → 轮询状态 → 下载成片,鉴权用 ark_api_key。
    复用基类的重试/降级/下载/取消逻辑,仅替换任务提交与参考图上传:
    参考图无需对象存储,直接编码为 base64 data URL 随请求送入
    (role: reference_image);主角参考图的自动文生图仍走 fal,
    未配置 fal_api_key 时自动跳过并降级纯文生视频。
    """

    _ENGINE_LABEL = "Seedance 2.5"

    # ---------------- 方舟请求要素 ----------------

    def _api_base(self) -> str:
        return str(self._config["seedance25"]["api_base"]).rstrip("/")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._config.ark_api_key}",
            "Content-Type": "application/json",
        }

    # ---------------- 参考图 ----------------

    def upload_image(self, path: Path) -> str | None:
        """方舟接口直接接受 base64 data URL,无需上传到 fal 存储。"""
        from .director import _encode_image

        url = _encode_image(path)
        if url is None:
            self._log(f"  读取主角图片失败: {path}")
        return url

    def generate_reference(self, prompt: str, out_path: Path) -> str | None:
        """自动文生主角参考图仍走 fal;未配 fal KEY 时跳过(降级纯文生)。"""
        if not self._config.fal_api_key:
            self._log(
                "  未配置 fal_api_key,跳过自动生成主角参考图"
                "(可在界面上传主角图片替代)。"
            )
            return None
        # fal 返回的参考图 URL 是公网可访问的,方舟可直接引用;
        # 为稳妥起见改用已下载的本地文件编码为 data URL(不依赖 fal 链接时效)
        if super().generate_reference(prompt, out_path) is None:
            return None
        return self.upload_image(out_path)

    # ---------------- 任务提交与轮询 ----------------

    def _submit_and_wait(
        self, endpoint: str, arguments: dict, timeout: float, label: str
    ) -> dict:
        """提交方舟视频生成任务并轮询,返回与 fal 相同形状的结果字典。"""
        try:
            resp = requests.post(
                endpoint, headers=self._headers(), json=arguments, timeout=60
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"{label} 任务提交失败(网络错误): {exc}") from exc
        if resp.status_code != 200:
            raise self._classify_ark(resp)
        task_id = str(resp.json().get("id") or "")
        if not task_id:
            raise RuntimeError(f"{label} 任务提交异常:方舟未返回任务 ID")

        poll_url = f"{endpoint}/{task_id}"
        deadline = time.monotonic() + timeout
        queued_notified = False
        while True:
            if self._cancel is not None and self._cancel.is_set():
                self._ark_cancel(poll_url)
                raise FatalGenerationError("已取消生成")
            if time.monotonic() > deadline:
                self._ark_cancel(poll_url)
                raise RuntimeError(f"{label} 生成超时(超过 {int(timeout)} 秒)")
            self._sleep(_POLL_INTERVAL)
            try:
                resp = requests.get(poll_url, headers=self._headers(), timeout=30)
            except requests.RequestException:
                continue  # 瞬时网络错误,继续等待
            if resp.status_code != 200:
                classified = self._classify_ark(resp)
                if isinstance(classified, FatalGenerationError):
                    raise classified
                continue
            data = resp.json()
            status = str(data.get("status") or "").lower()
            if status == "succeeded":
                url = str((data.get("content") or {}).get("video_url") or "")
                if not url:
                    raise RuntimeError(f"{label} 任务完成但方舟未返回视频地址")
                return {"video": {"url": url}}
            if status in ("failed", "cancelled", "canceled", "expired"):
                error = data.get("error") or {}
                raise RuntimeError(
                    f"{label} 生成失败({error.get('code', status)}):"
                    f" {error.get('message', '无详细信息')}"
                )
            if status == "queued" and not queued_notified:
                queued_notified = True
                self._log(f"  {label} 排队中 …")

    def _ark_cancel(self, poll_url: str) -> None:
        """尽力取消方舟任务(仅排队/运行中的任务可取消,失败不影响主流程)。"""
        try:
            requests.delete(poll_url, headers=self._headers(), timeout=15)
        except requests.RequestException:
            pass

    def _classify_ark(self, resp: requests.Response) -> Exception:
        """把方舟的 HTTP 错误翻译成用户能看懂的提示;致命错误不再重试。"""
        try:
            error = resp.json().get("error") or {}
        except ValueError:
            error = {}
        code = str(error.get("code") or "")
        message = str(error.get("message") or resp.text[:300])
        if resp.status_code in (401, 403):
            return FatalGenerationError(
                "火山方舟 API KEY 无效或无权限,请检查 config.yaml 中的 ark_api_key"
            )
        if resp.status_code == 404 or code in ("ModelNotFound", "ModelNotOpen"):
            model = str(self._config["seedance25"]["model"])
            return FatalGenerationError(
                f"火山方舟模型不可用: {model}。请确认已在方舟控制台开通该模型,"
                "并检查 config.yaml 中的 seedance25.model / seedance25.api_base"
            )
        if resp.status_code == 402 or code in ("AccountOverdueError", "QuotaExceeded"):
            return FatalGenerationError(
                "火山方舟账户余额不足或额度用尽,请前往火山引擎控制台充值"
            )
        exc = RuntimeError(f"火山方舟请求失败({code or resp.status_code}): {message}")
        if resp.status_code == 400 and code not in ("RateLimitExceeded",):
            # 参数校验/内容审核类 400 是确定性的,标记为 422 语义:
            # generate_clip 会跳过重试,直接降级/报错
            exc.status_code = 422
        return exc

    # ---------------- 请求参数 ----------------

    def _build_arguments(
        self, shot: Shot, references: list[tuple[str, str]] | None
    ) -> tuple[str, dict, bool]:
        """多分镜按时间戳分块拼成单条 prompt,有主角以 reference_image 送入。"""
        seedance25 = self._config["seedance25"]
        video_cfg = self._config["video"]
        combined = shot.combined_prompt.lower()
        use_reference = bool(references) and (
            "@element" in combined or "@image" in combined or "@图片" in combined
        )

        if use_reference:
            # 方舟官方语法:prompt 中以 @图片1 引用 content 里的第 1 张参考图
            prompts = [element_to_ark_image_tokens(cut.prompt) for cut in shot.cuts]
        else:
            prompts = [strip_reference_tokens(cut.prompt) for cut in shot.cuts]

        if len(shot.cuts) > 1:
            # 时间戳分块(官方推荐):把各分镜的时长比例明确传给模型,
            # 避免 30 秒长镜头组的"后半段漂移"
            prompt = join_cut_prompts_timed(
                [(p, cut.duration) for p, cut in zip(prompts, shot.cuts)]
            )
        else:
            prompt = join_cut_prompts(prompts)
        refs: list[tuple[str, str]] = []
        if use_reference:
            refs = references[:MAX_REFERENCE_IMAGES["seedance25"]]
            prompt += reference_usage_note([note for _, note in refs], "@图片{}")
        if len(prompt) > _MAX_SEEDANCE_PROMPT_CHARS:
            self._log(
                f"  ⚠ 镜头组 {shot.index} 提示词超长({len(prompt)} 字符),"
                f"已裁剪到 {_MAX_SEEDANCE_PROMPT_CHARS} 字符内"
            )
            prompt = fit_prompt(prompt, _MAX_SEEDANCE_PROMPT_CHARS)

        content: list[dict] = [{"type": "text", "text": prompt}]
        for url, _ in refs:
            content.append({
                "type": "image_url",
                "image_url": {"url": url},
                "role": "reference_image",
            })
        duration = min(_ARK_MAX_SECONDS, max(_ARK_MIN_SECONDS, shot.duration))
        arguments = {
            "model": str(seedance25["model"]),
            "content": content,
            # Seedance 2.5 原生支持 16:9/9:16/1:1/3:4/4:3 全部画幅,无需映射裁剪
            "ratio": str(video_cfg["aspect_ratio"]),
            "resolution": str(seedance25["resolution"]),
            "duration": duration,
            "generate_audio": bool(video_cfg["generate_audio"]),
            "watermark": False,
        }
        negative = shot.negative_prompt.strip()
        if negative:
            arguments["negative_prompt"] = fit_prompt(negative, _MAX_SINGLE_PROMPT_CHARS)
        try:
            seed = int(seedance25.get("seed", -1))
        except (TypeError, ValueError):
            seed = -1
        if seed >= 0:
            arguments["seed"] = seed
        endpoint = f"{self._api_base()}/contents/generations/tasks"
        return endpoint, arguments, use_reference


class KlingGenerator(_FalGenerator):
    """Kling 3(快手,经 fal.ai):可选视频引擎(video.engine: kling)。"""

    _ENGINE_LABEL = "Kling"

    def generation_aspect(self, aspect: str) -> str:
        return kling_generation_aspect(aspect)

    def _build_arguments(
        self, shot: Shot, references: list[tuple[str, str]] | None
    ) -> tuple[str, dict, bool]:
        """多分镜走 multi_prompt,有主角走 elements 角色元素。

        Kling 的 elements 只支持"同一主角的多角度参考图",不支持为每张图
        单独指定用途(与 Seedance 系不同,pipeline 会提前告知用户)。
        """
        kling = self._config["kling"]
        video_cfg = self._config["video"]
        combined = shot.combined_prompt.lower()
        use_reference = bool(references) and (
            "@element" in combined or "@image" in combined
        )

        aspect = kling_generation_aspect(str(video_cfg["aspect_ratio"]))
        if use_reference:
            urls = [url for url, _ in references[:MAX_REFERENCE_IMAGES["kling"]]]
            endpoint = str(kling["reference_endpoint"])
            arguments: dict = {
                "aspect_ratio": aspect,
                "generate_audio": bool(video_cfg["generate_audio"]),
            }
            if "@element" in combined:
                # elements 角色元素:第 1 张作正面图,全部图片作多角度参考
                arguments["elements"] = [{
                    "frontal_image_url": urls[0],
                    "reference_image_urls": urls,
                }]
            else:
                # 旧 manifest 的 @Image1 走 image_urls 参考图,保持断点续传兼容
                arguments["image_urls"] = urls[:1]
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
    """按 video.engine 创建对应引擎的生成器(默认 seedance25)。"""
    cls = {
        "seedance": SeedanceGenerator,
        "seedance25": ArkSeedanceGenerator,
    }.get(config.engine, KlingGenerator)
    return cls(config, log, cancel_event=cancel_event)
