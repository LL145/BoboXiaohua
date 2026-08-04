# CLAUDE.md

AI 短视频生成器:输入一句话描述,自动产出一条约 60 秒的短视频。
Tkinter 桌面小工具,跨平台(Windows / macOS / Linux),面向无技术背景的用户,
因此所有面向用户的文案(日志、报错、配置注释)均为中文,且强调"绝不因局部失败毁掉整次任务"。

## 项目原则(所有改动优先遵循)

1. **简单**:用户只需输入一句话、点击生成,即可得到一段 60 秒左右的视频。
   不给界面加不必要的选项,复杂度藏进 `config.yaml` 的默认值里;新功能默认零配置可用。
2. **先进**:默认使用当前最先进的模型与编排方式(编剧模型、视频/图像端点、
   结构化输出、参考图锁角色等)。上游出了更强的模型或端点时,应更新
   `_DEFAULTS` 与 `config.yaml` 的默认值,而不是让用户自己去换。

## 运行与打包

```bash
pip install -r requirements.txt   # fal-client / PyYAML / requests / edge-tts(GUI 用标准库 tkinter)
python main.py                    # 桌面界面
python main.py "一句话描述"        # 命令行模式,直接生成
python build.py                   # PyInstaller 打包 + 内置 ffmpeg,产出 dist/ 发布包
```

- 运行依赖 `config.yaml`(程序目录下),需填 `openrouter_api_key` 与 `fal_api_key`;
  源码运行还需本机 ffmpeg/ffprobe(打包版已内置)。
- 无测试套件、无 CI lint;改动后至少用 `python -c "import ast; ast.parse(open('...').read())"`
  或 `python -m py_compile` 做语法检查,纯逻辑可写临时脚本验证。

## 流水线架构(video_gen/)

一次生成 = `Pipeline.run()`(pipeline.py)串起以下阶段,数据流单向:

1. **director.py** — LLM 编剧+导演,经 OpenRouter(OpenAI 兼容接口)把一句话扩写为
   `Storyboard`(含镜头组 `Shot` 列表,每组内含 1~6 个分镜 `Cut`)。组内分镜由 Kling
   一次连续生成(multi_prompt,组总长 3~15 秒、单分镜 1~15 秒),组间才用转场;
   代码约束总时长在 `video.target_duration` ±15% 内并用 `_clamp_duration`/`_build_cuts`
   钳制;`kling.clip_duration` 仅是模型未给时长时的回退值。单条分镜 prompt 要求模型
   控制在 450 字符内(Kling multi_prompt 有 512 字符硬上限)。用户可上传主角图片:
   随创意以多模态消息发给导演模型照图撰写 @Element1 外观描述,模型不支持图片输入时
   自动去图重试(文字说明仍告知存在用户参考图)。导演同时决定声音形态:
   解说型逐组写中文旁白(`narration` 字段),沉浸型全部置空;角色台词直接写进分镜
   prompt(中文引号台词),由 Kling 原生配音。请求带 `response_format: json_schema`,
   不支持结构化输出的模型由 `_extract_json` 容错兜底。
2. **kling.py** — `KlingGenerator` 调 fal.ai 逐镜头组生成视频。参考图优先用用户上传的
   主角图片(pipeline 复制进任务目录并上传),否则有固定主角时先文生图,作为
   `elements` 角色元素(prompt 中 `@Element1`)送入 reference-to-video
   锁定角色外观;参考图任何一步失败自动降级纯文生视频(`strip_reference_tokens`
   去掉占位符,兼容旧 manifest 的 `@Image1`/`image_urls`)。提交前 `fit_prompt`
   按分句边界把提示词钳制到端点硬上限内(multi_prompt 单条 512 字符,单
   prompt/negative_prompt 2500)。`FatalGenerationError`(KEY 无效/余额不足/端点
   不存在)立即终止全部镜头组;422 参数校验错误是确定性的,跳过重试直接降级/报错;
   其余错误逐组重试。画幅上 Kling 端点原生仅支持 16:9/9:16/1:1;3:4、4:3 经
   `generation_aspect` 映射为 9:16/16:9 生成,拼接后由 `assembler.crop_to_aspect`
   居中裁剪出目标画幅(在字幕烧录之前;导演 prompt 会提示把主体放画面中部)。
3. **tts.py** — Edge TTS(免费)合成导演写的中文旁白,逐组落盘
   `narration_XX.mp3`(断点续传复用),同步记录逐句精确时间轴
   `narration_XX.timeline.json`(SentenceBoundary 事件)供字幕对齐,
   旧版 edge-tts 只有词边界时按句子字数归组推算;SRT 生成优先用该时间轴,
   缺失时回退按字数比例估算。edge-tts 缺失/网络失败只丢旁白,不影响成片。
4. **assembler.py** — ffmpeg 拼接:优先 xfade 交叉溶解 + 首尾淡入淡出(需重编码),
   失败回退 concat 无损拼接;`concat()` 返回各镜头组在成片时间轴上的偏移,供旁白
   与字幕定位。旁白超长时先用 edge-tts 语速参数(+N%,≤40)重合成(音质自然),
   仍超长才 atempo 加速≤1.4 并截断;字幕优先烧录(libass,使用随程序分发的
   `fonts/` 内 Noto Sans SC 字体,缺失时回退平台系统字体),失败退 mp4 软字幕;
   `music/` 目录有音频时由导演挑选一首混入(bgm)。每级失败都沿用上一级产物。
5. **config.py** — 读取程序目录 `config.yaml`,与 `_DEFAULTS` 深合并;`app_dir()` 兼容
   PyInstaller 冻结与 macOS .app 布局。**新增配置项必须同时更新 `_DEFAULTS`、
   `config.yaml` 的中文注释,必要时补 `validate()`。**
6. **gui.py** — Tkinter 界面;工作线程经队列把日志/进度转回主线程,不直接碰控件。

## 关键约定

- **断点续传**:任务目录(`output/日期_标题_<描述哈希>/`)落盘 `manifest.json`
  (描述、画幅、目标时长、storyboard)与各 `shot_XX.mp4`。同一描述再次生成时复用
  已有脚本与片段,只补缺失镜头;画幅或目标时长不同的旧任务不续传——所以 `Shot`/`Storyboard` 字段变更要保持 `from_dict` 对旧 manifest 兼容
  (用 `.get()` + 默认值)。
- **提示词一致性**:分镜脚本要求每个分镜 prompt 逐字重复 style_anchor 与角色外观描述,
  禁止跨组/跨分镜指代(镜头组之间相互独立生成);有主角时 prompt 用
  `@Element1 (外观描述)` 引用角色元素,降级纯文生时由 `strip_reference_tokens`
  去掉占位符(同时兼容旧脚本的 `@Image1`)。
- **JSON schema 保守化**:`_STORYBOARD_SCHEMA` 会被 OpenRouter 透传给任意上游模型,
  只用各家 strict 模式普遍支持的关键字(type/description/required 等),
  数值范围等约束写进 description 并在 Python 侧钳制。
- **错误分层**:瞬时错误(网络、5xx、解析失败)自动重试;致命错误抛
  `FatalGenerationError`/`RuntimeError` 并给出用户能看懂的中文提示与解决办法。
- 中文注释、中文用户文案;代码风格遵循现有模块(dataclass、类型标注、`from __future__ import annotations`)。
