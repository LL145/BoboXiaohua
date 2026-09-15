"""调用视频引擎(均经 fal.ai)生成素材:主角参考图(文生图)与各镜头组视频片段。

引擎能力(时长范围、参考图上限、原生画幅、提示词语言)登记在 config.ENGINES,
本模块只负责把导演脚本转换成各端点的请求参数:
- **Gemini Omni Flash 1.1**(Google,默认):单组 3~10 秒,原生同步音频始终
  开启;参考图按 image_urls 顺序以 fal 文档的位置占位符 <IMAGE_REF_0>、
  <IMAGE_REF_1> 引用(从 0 计),@Element1 据此改写;画幅原生仅 16:9/9:16。
  该引擎不写明声音会自行配乐、单分镜不写明会自行切分镜头,提交前按声音策略
  (pipeline 设定:有旁白/背景音乐时禁配乐)与分镜数附加英文指令兜底。
- **Seedance 2.5 / 2.0**(字节跳动):多分镜拼进单条 prompt 一次连续生成
  (2.5 按时间戳分块、单组最长 30 秒;2.0 用 "Cut scene to" 衔接、最长 15 秒);
  参考图经 image_urls 送入,prompt 中以 @Image1 引用;原生支持全部画幅。
- **Kling 3**(快手):多分镜走 multi_prompt 结构化参数;有主角走 elements
  角色元素(@Element1,多图仅作同一主角的多角度参考);3:4/4:3 需裁剪。

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

from .config import ALL_ASPECTS, Config, generation_aspect
from .director import Shot

LogFn = Callable[[str], None]

# 小于该体积的文件视为无效(错误页/截断下载)
_MIN_CLIP_BYTES = 10 * 1024
_MIN_IMAGE_BYTES = 5 * 1024
_POLL_INTERVAL = 5  # 轮询任务状态的间隔(秒)

# 提示词长度上限(字符):Kling 端点超长会被 422 直接拒绝,且同样参数重试必然
# 再失败,所以提交前在本地钳制;其他引擎未公布硬上限,沿用同一保守值
_MAX_MULTI_PROMPT_CHARS = 512  # Kling multi_prompt 内单条分镜提示词
_MAX_PROMPT_CHARS = 2500       # 单条 prompt 与 negative_prompt


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
    """把导演脚本统一使用的 @Element1 占位符转换为 Seedance 端点的 @Image1 引用。"""
    return re.sub(r"@Element(\d+)", r"@Image\1", prompt)


def element_to_reference_phrases(prompt: str) -> str:
    """把 @Element1/@Image1 占位符改写为 Gemini 端点的位置占位符 <IMAGE_REF_0>
    (fal 文档语法:参考图按 image_urls 顺序从 0 编号)。"""
    return re.sub(
        r"@(?:Element|Image)(\d+)",
        lambda m: f"<IMAGE_REF_{int(m.group(1)) - 1}>",
        prompt,
    ).strip()


def _end_sentence(text: str, is_cjk: bool) -> str:
    if not text.endswith((".", "!", "?", "。", "!", "?")):
        text += "。" if is_cjk else "."
    return text


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
        parts.append(_end_sentence(prompt, is_cjk))
    return " ".join(parts)


def join_cut_prompts_timed(prompts_durations: list[tuple[str, int]]) -> str:
    """按时间戳分块把各分镜 prompt 拼成一条(Seedance 2.5 官方推荐写法)。

    长镜头组最常见的失败是"后半段漂移":没有时间轴引导时模型会用不受控的
    内容填满剩余时长。时间戳块(如 `[0-4秒] …`)把每个分镜的时长比例明确
    传给模型,节奏由导演脚本掌控;空 prompt 的分镜跳过但时长仍计入时间轴,
    保证后续块的时间戳正确。
    """
    parts: list[str] = []
    start = 0
    for prompt, duration in prompts_durations:
        end = start + max(0, int(duration))
        text = prompt.strip()
        if text:
            is_cjk = bool(_CJK_RE.search(text))
            label = f"[{start}-{end}秒] " if is_cjk else f"[{start}-{end}s] "
            parts.append(label + _end_sentence(text, is_cjk))
        start = end
    return " ".join(parts)


def reference_usage_note(
    notes: list[str], token_format: str, english: bool = False, zero_based: bool = False
) -> str:
    """生成参考素材的用途说明,附在 prompt 末尾。

    社区经验:未标注用途的参考图是效果不佳的最常见原因——每个参考素材
    都应说明用途,prompt 中用"参考图中的角色"式引用而非重新描述。
    token_format 如 "@Image{}"(Seedance,从 1 计)或 "<IMAGE_REF_{}>"
    (Gemini,zero_based 从 0 计);english 为 True 时说明文字用英文
    (用途本身照抄用户所写)。
    """
    if not notes:
        return ""
    offset = 0 if zero_based else 1
    if english:
        parts = [
            f"{token_format.format(i + offset)}: {note.strip() or 'main character reference'}"
            for i, note in enumerate(notes)
        ]
        return (
            " Reference media usage — " + "; ".join(parts)
            + ". Keep the main character's appearance strictly consistent with"
            " the reference images."
        )
    parts = [
        f"{token_format.format(i + offset)}:{note.strip() or '主角形象参考'}"
        for i, note in enumerate(notes)
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

    子类只需实现 `_build_arguments`(构造端点与请求参数)。
    """

    def __init__(
        self,
        config: Config,
        log: LogFn,
        cancel_event: threading.Event | None = None,
    ):
        self._config = config
        self._log = log
        self._cancel = cancel_event
        self._engine = config.engine
        self._spec = config.engine_spec
        self._section = config.engine_section  # 端点、分辨率等引擎专属配置
        # 声音策略:影片有旁白或将混入背景音乐时,禁止视频模型自行配乐
        # (由 pipeline 在生成前设定;Gemini 据此附加英文指令)
        self.allow_music = True
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
        """引擎实际使用的生成画幅;与目标画幅不同时成片阶段会居中裁剪。"""
        return generation_aspect(self._engine, aspect)

    def generate_reference(self, prompt: str, out_path: Path) -> str | None:
        """文生图生成主角参考图,下载到 out_path 并返回其 URL;失败返回 None。"""
        endpoint = str(self._config["image"]["endpoint"])
        aspect = str(self._config["video"]["aspect_ratio"])
        arguments = {
            "prompt": prompt,
            # 文生图端点原生支持全部画幅,无需映射
            "aspect_ratio": aspect if aspect in ALL_ASPECTS else "auto",
            "num_images": 1,
            "output_format": "png",
        }
        for attempt in (1, 2):
            try:
                result = self._submit_and_wait(
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

    def synthesize_speech(
        self, text: str, out_path: Path, rate: int = 0, index: int = 0
    ) -> bool:
        """Edge TTS 的付费后备:用 fal.ai 的 TTS 端点合成旁白到 out_path(mp3)。

        端点与音色取自 narration.fallback_endpoint / fallback_voice(留空不启用);
        rate 为语速加快百分比,映射为 MiniMax 的 speed(0.5~2.0)。
        任何失败只返回 False,不影响成片。
        """
        narration = self._config["narration"]
        endpoint = str(narration.get("fallback_endpoint") or "").strip()
        if not endpoint or not text.strip():
            return False
        voice = str(narration.get("fallback_voice") or "").strip()
        setting: dict = {"speed": round(min(2.0, 1 + max(0, rate) / 100), 2)}
        if voice:
            setting["voice_id"] = voice
        arguments = {
            "text": text.strip()[:5000],
            "voice_setting": setting,
            "audio_setting": {"format": "mp3", "sample_rate": 32000},
            "output_format": "url",
            "language_boost": "Chinese",
        }
        label = f"镜头组 {index} 旁白(fal 后备 TTS)" if index else "旁白(fal 后备 TTS)"
        try:
            self._log(f"  {label} 合成中 …")
            result = self._submit_and_wait(endpoint, arguments, timeout=300, label=label)
            self._download(result["audio"]["url"], out_path)
            return out_path.exists() and out_path.stat().st_size >= 1024
        except FatalGenerationError:
            raise
        except Exception as exc:  # noqa: BLE001 - 后备 TTS 失败只丢旁白
            self._log(f"  {label} 失败: {exc}")
            return False

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

    # ---------------- 子类共用的小工具 ----------------

    @staticmethod
    def _wants_reference(shot: Shot, references: list[tuple[str, str]] | None) -> bool:
        """有参考图且脚本里用了占位符(@Element1 / 旧脚本的 @Image1)才走参考图模式。"""
        combined = shot.combined_prompt.lower()
        return bool(references) and ("@element" in combined or "@image" in combined)

    def _pick_references(
        self, references: list[tuple[str, str]] | None
    ) -> list[tuple[str, str]]:
        """按引擎上限截取参考图(超限 pipeline 已提前告知用户)。"""
        return list(references or [])[: self._spec.max_reference_images]

    def _group_duration(self, shot: Shot) -> int:
        """镜头组时长钳到引擎单次生成范围内(导演脚本已按此范围设计,兜底而已)。"""
        low, high = self._spec.group_seconds
        return min(high, max(low, int(shot.duration)))

    def _fit(self, prompt: str, limit: int, shot: Shot, label: str = "") -> str:
        """提示词超长时按分句边界裁剪并记录日志。"""
        if len(prompt) <= limit:
            return prompt
        self._log(
            f"  ⚠ 镜头组 {shot.index}{label} 提示词超长({len(prompt)} 字符),"
            f"已裁剪到 {limit} 字符内"
        )
        return fit_prompt(prompt, limit)

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
                    f"  镜头组 {shot.index} 提交 {self._spec.name} 生成"
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
                "fal.ai API KEY 无效或无权限,请检查界面「设置」中的 fal.ai API KEY"
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


class _SinglePromptGenerator(_FalGenerator):
    """把整组分镜拼成单条 prompt、一次连续生成的引擎(Seedance 2.0/2.5、Gemini)。

    三者的请求形状相同(prompt / duration / aspect_ratio / resolution,
    参考图走 image_urls),差异用下面几个类属性描述。
    """

    _TIMED_JOIN = False              # 多分镜按时间戳分块(否则用 "Cut scene to" 衔接)
    _AUDIO_FLAG = True               # 端点是否有 generate_audio 参数
    _SEED = False                    # reference 端点是否接受 seed(取自本引擎节的 seed)
    _INT_DURATION = False            # duration 传整数(否则传字符串枚举)
    _TOKEN_FORMAT = "@Image{}"       # 参考图在 prompt 中的引用写法
    _ZERO_BASED = False              # 参考图编号从 0 计(Gemini)
    _ENGLISH_NOTE = False            # 参考图用途说明用英文
    _SHOT_DIRECTIVES = False         # 提交前附加"单镜头/禁配乐"英文指令(Gemini)

    @staticmethod
    def _convert_tokens(prompt: str) -> str:
        """参考图模式下把 @Element1 转成端点认识的引用写法。"""
        return element_to_image_tokens(prompt)

    def _build_arguments(
        self, shot: Shot, references: list[tuple[str, str]] | None
    ) -> tuple[str, dict, bool]:
        use_reference = self._wants_reference(shot, references)
        refs = self._pick_references(references) if use_reference else []
        convert = self._convert_tokens if use_reference else strip_reference_tokens
        prompts = [convert(cut.prompt) for cut in shot.cuts]
        if self._TIMED_JOIN and len(shot.cuts) > 1:
            prompt = join_cut_prompts_timed(
                [(p, cut.duration) for p, cut in zip(prompts, shot.cuts)]
            )
        else:
            prompt = join_cut_prompts(prompts)
        prompt += reference_usage_note(
            [note for _, note in refs], self._TOKEN_FORMAT,
            english=self._ENGLISH_NOTE, zero_based=self._ZERO_BASED,
        )
        if self._SHOT_DIRECTIVES:
            prompt += self._shot_directives(shot, prompt)
        prompt = self._fit(prompt, _MAX_PROMPT_CHARS, shot)

        duration = self._group_duration(shot)
        arguments: dict = {
            "prompt": prompt,
            "duration": duration if self._INT_DURATION else str(duration),
            "aspect_ratio": self.generation_aspect(self._config["video"]["aspect_ratio"]),
            "resolution": str(self._section["resolution"]),
        }
        if self._AUDIO_FLAG:
            arguments["generate_audio"] = bool(self._config["video"]["generate_audio"])
        if refs:
            arguments["image_urls"] = [url for url, _ in refs]
            if self._SEED:
                try:
                    seed = int(self._section.get("seed", -1))
                except (TypeError, ValueError):
                    seed = -1
                if seed >= 0:
                    arguments["seed"] = seed
        endpoint = str(self._section["reference_endpoint" if refs else "text_endpoint"])
        return endpoint, arguments, use_reference


    def _shot_directives(self, shot: Shot, prompt: str) -> str:
        """Gemini 不写明就会自行切分镜头、自行配乐:单分镜组写明一镜到底,
        影片有旁白/背景音乐时写明不要配乐(导演脚本已写过的不重复)。"""
        lowered = prompt.lower()
        parts: list[str] = []
        if len(shot.cuts) == 1 and "continuous shot" not in lowered:
            parts.append("Single continuous shot, no cuts.")
        if not self.allow_music and not re.search(r"\b(?:no|without)\b[^.;]*\bmusic\b", lowered):
            parts.append("No background music; ambient sound and dialogue only.")
        return (" " + " ".join(parts)) if parts else ""


class SeedanceGenerator(_SinglePromptGenerator):
    """Seedance 2.0:多分镜以 "Cut scene to" 衔接,单组 4~15 秒。"""


class Seedance25Generator(_SinglePromptGenerator):
    """Seedance 2.5:单组最长 30 秒,多分镜按时间戳分块防后半段漂移;
    reference 端点支持 seed;端点不支持 negative_prompt。"""

    _TIMED_JOIN = True
    _SEED = True


class GeminiGenerator(_SinglePromptGenerator):
    """Gemini Omni Flash 1.1:单组 3~10 秒(整数),原生音频始终开启(无开关);
    参考图按 image_urls 顺序以 <IMAGE_REF_0> 位置占位符引用;无 seed/negative_prompt;
    提交前附加单镜头/禁配乐指令。"""

    _TIMED_JOIN = True
    _AUDIO_FLAG = False
    _INT_DURATION = True
    _TOKEN_FORMAT = "<IMAGE_REF_{}>"
    _ZERO_BASED = True
    _ENGLISH_NOTE = True
    _SHOT_DIRECTIVES = True

    @staticmethod
    def _convert_tokens(prompt: str) -> str:
        return element_to_reference_phrases(prompt)


class KlingGenerator(_FalGenerator):
    """Kling 3:多分镜走 multi_prompt 结构化参数,有主角走 elements 角色元素。

    elements 只支持"同一主角的多角度参考图",不支持为每张图单独指定用途
    (pipeline 会提前告知用户)。
    """

    def _build_arguments(
        self, shot: Shot, references: list[tuple[str, str]] | None
    ) -> tuple[str, dict, bool]:
        video_cfg = self._config["video"]
        use_reference = self._wants_reference(shot, references)
        arguments: dict = {
            "aspect_ratio": self.generation_aspect(video_cfg["aspect_ratio"]),
            "generate_audio": bool(video_cfg["generate_audio"]),
        }
        if use_reference:
            urls = [url for url, _ in self._pick_references(references)]
            if "@element" in shot.combined_prompt.lower():
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
            arguments["negative_prompt"] = fit_prompt(shot.negative_prompt, _MAX_PROMPT_CHARS)
            prompts = [strip_reference_tokens(cut.prompt) for cut in shot.cuts]

        if len(shot.cuts) > 1:
            arguments["multi_prompt"] = [
                {
                    "prompt": self._fit(p, _MAX_MULTI_PROMPT_CHARS, shot, f" 分镜 {i}"),
                    "duration": str(cut.duration),
                }
                for i, (p, cut) in enumerate(zip(prompts, shot.cuts), 1)
            ]
        else:
            arguments["prompt"] = self._fit(prompts[0], _MAX_PROMPT_CHARS, shot)
            arguments["duration"] = str(self._group_duration(shot))
        endpoint = self._section["reference_endpoint" if use_reference else "text_endpoint"]
        return str(endpoint), arguments, use_reference


_GENERATORS: dict[str, type[_FalGenerator]] = {
    "gemini": GeminiGenerator,
    "seedance25": Seedance25Generator,
    "seedance": SeedanceGenerator,
    "kling": KlingGenerator,
}


def create_generator(
    config: Config, log: LogFn, cancel_event: threading.Event | None = None
) -> _FalGenerator:
    """按 video.engine 创建对应引擎的生成器(未知引擎在 config.validate 已拦截)。"""
    cls = _GENERATORS.get(config.engine) or _GENERATORS[next(iter(_GENERATORS))]
    return cls(config, log, cancel_event=cancel_event)
