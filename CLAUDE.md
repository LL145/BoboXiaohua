# CLAUDE.md

AI 短视频生成器:输入一句话描述,自动产出一条约 60 秒的短视频。
Tkinter 桌面小工具,跨平台(Windows / macOS / Linux),面向无技术背景的用户,
因此所有面向用户的文案(日志、报错、配置注释)均为中文,且强调"绝不因局部失败毁掉整次任务"。

## 运行与打包

```bash
pip install -r requirements.txt   # fal-client / PyYAML / requests(GUI 用标准库 tkinter)
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
   `Storyboard`(含 `Shot` 列表)。镜头数量与每镜头时长(3~15 秒整数)由模型按叙事
   节奏决定,代码只约束总时长在 `video.target_duration` ±15% 内并用 `_clamp_duration`
   钳制;`kling.clip_duration` 仅是模型未给时长时的回退值。
   请求带 `response_format: json_schema`,不支持结构化输出的模型由 `_extract_json` 容错兜底。
2. **kling.py** — `KlingGenerator` 调 fal.ai 逐镜头生成视频。有固定主角时先文生图出参考图,
   走 reference-to-video 锁定角色外观;参考图任何一步失败自动降级纯文生视频。
   `FatalGenerationError`(KEY 无效/余额不足/端点不存在)立即终止全部镜头,其余错误逐镜头重试。
3. **assembler.py** — ffmpeg 拼接:优先 xfade 交叉溶解 + 首尾淡入淡出(需重编码),
   失败回退 concat 无损拼接;`music/` 目录有音频时由导演挑选一首混入(bgm)。
4. **config.py** — 读取程序目录 `config.yaml`,与 `_DEFAULTS` 深合并;`app_dir()` 兼容
   PyInstaller 冻结与 macOS .app 布局。**新增配置项必须同时更新 `_DEFAULTS`、
   `config.yaml` 的中文注释,必要时补 `validate()`。**
5. **gui.py** — Tkinter 界面;工作线程经队列把日志/进度转回主线程,不直接碰控件。

## 关键约定

- **断点续传**:任务目录(`output/日期_标题_<描述哈希>/`)落盘 `manifest.json`
  (描述、画幅、storyboard)与各 `shot_XX.mp4`。同一描述再次生成时复用已有脚本与片段,
  只补缺失镜头——所以 `Shot`/`Storyboard` 字段变更要保持 `from_dict` 对旧 manifest 兼容
  (用 `.get()` + 默认值)。
- **提示词一致性**:分镜脚本要求每镜头 prompt 逐字重复 style_anchor 与角色外观描述,
  禁止跨镜头指代(各镜头独立生成);有主角时 prompt 用 `@Image1 (外观描述)` 引用参考图,
  降级纯文生时由 `strip_reference_tokens` 去掉占位符。
- **JSON schema 保守化**:`_STORYBOARD_SCHEMA` 会被 OpenRouter 透传给任意上游模型,
  只用各家 strict 模式普遍支持的关键字(type/description/required 等),
  数值范围等约束写进 description 并在 Python 侧钳制。
- **错误分层**:瞬时错误(网络、5xx、解析失败)自动重试;致命错误抛
  `FatalGenerationError`/`RuntimeError` 并给出用户能看懂的中文提示与解决办法。
- 中文注释、中文用户文案;代码风格遵循现有模块(dataclass、类型标注、`from __future__ import annotations`)。
