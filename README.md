# AI 短视频生成器(OpenRouter × Kling)

在 Windows 上运行的桌面小工具:输入一句话描述,点击「生成」,自动产出一条约 **60 秒**的高质量短视频。

## 工作原理

```
一句话描述
   │
   ▼
① LLM 导演(经 OpenRouter)     → 扩写为 6 个镜头的分镜脚本(每镜头 10 秒)
   │  默认 Claude Fable 5 (high)     可换 OpenRouter 上任意模型
   │                              并判断是否存在贯穿全片的主角
   ▼
② 主角参考图(Nano Banana 2)   → 有主角时自动生成一张参考图(约 $0.08)
   │                              无主角(纯风景等)则跳过
   ▼
③ Kling 3(fal.ai)             → 多镜头并行生成,自带环境音效
   │  有主角: reference-to-video    参考图随每个镜头送入,全片角色外观一致
   │  无主角: text-to-video         失败自动降级/重试,断点续传
   ▼
④ ffmpeg                        → 交叉溶解转场 + 首尾淡入淡出;
   │                              music/ 里有音频则由导演按情绪挑选混入
   ▼
output/日期_标题/标题.mp4
```

内部稳健性设计(用户无需任何设置):

- **角色一致性**:业界公认做法——先生成主角参考图,再用 Kling 的 reference-to-video
  把参考图带入每个镜头,主角外观在整段片段中保持一致(优于仅锁首帧的 image-to-video)。
  是否需要参考图由导演模型自动判断;参考图任何一步失败都自动降级为纯文生视频,绝不影响出片;
- **并行生成 + 看门狗**:多个镜头同时提交,单镜头独立重试并带超时保护;
  KEY 无效、余额不足等致命错误立即终止,不空耗等待;
- **断点续传**:分镜脚本、参考图与已完成片段落盘保存,同一描述再次生成时自动续接,不重复扣费;
- **生成前预检**:先校验 OpenRouter KEY 与磁盘空间,配错即刻提示;
- **成片质感**:镜头间交叉溶解转场、首尾淡入淡出、Kling 3 原生环境音效;
- **背景音乐**:把 mp3 放进 `music/` 文件夹,导演模型会按影片情绪挑选一首混入
  (压低音量、结尾淡出),文件夹为空则不加;
- **运行日志**:每次任务的完整日志写入任务目录 `log.txt`,便于排查问题。

## 安装

1. 安装 [Python 3.10+](https://www.python.org/downloads/)(勾选 *Add python.exe to PATH*)
2. 安装 [ffmpeg](https://www.gyan.dev/ffmpeg/builds/) 并加入 PATH(或在 `config.yaml` 里填绝对路径)
3. 安装依赖:

```bat
pip install -r requirements.txt
```

## 配置

编辑程序目录下的 `config.yaml`,填入两个 API KEY:

```yaml
openrouter_api_key: "sk-or-..."   # https://openrouter.ai/settings/keys
fal_api_key: "..."                # https://fal.ai/dashboard/keys
```

默认已选用各环节当前最强的模型:编剧/导演为 **Claude Fable 5**(思考深度 high),
视频为 **Kling 3 Pro**(原生音效),参考图为 **Nano Banana 2**。
想换模型只需改对应端点/模型 ID 一行,注释里有说明——例如 `llm.model` 支持
OpenRouter 上的任意模型(如 `openai/gpt-5.2`、`google/gemini-3-pro`、`deepseek/deepseek-r2`)。

## 使用

```bat
python main.py
```

在窗口里输入一句话描述,点击「🎬 生成视频」。全程约十几分钟(Kling 每个镜头需要数分钟),
进度条按镜头推进,日志实时显示,完成后点击「打开成片」。

也支持命令行模式:

```bat
python main.py "一只橘猫在雨后的东京街头漫步,霓虹灯倒映在水洼里,电影感画面"
```

## 输出

每次生成会在 `output/` 下创建独立目录:

```
output/20260803_153000_雨巷橘猫/
├── storyboard.txt   分镜脚本(中英文)
├── reference.png    主角参考图(有主角时)
├── log.txt          本次任务完整日志
├── shot_01.mp4      各镜头片段
├── ...
└── 雨巷橘猫.mp4     最终成片
```

## 常见问题

- **提示未找到 ffmpeg** — 确认已安装并加入 PATH,或在 `config.yaml` 的 `ffmpeg.path` 填写完整路径。
- **想换 Kling 版本** — 修改 `config.yaml` 中 `kling.text_endpoint` / `kling.reference_endpoint`,
  可选端点见 [fal.ai 模型页](https://fal.ai/models)。注意旧版 Kling(2.x)仅支持 5/10 秒镜头且无原生音效。
- **想换编剧模型** — 修改 `config.yaml` 中 `llm.model` 为 OpenRouter 上的任意模型 ID;
  `llm.reasoning_effort` 控制思考深度(不支持思考的模型自动忽略)。
- **想省钱** — `kling.generate_audio: false` 可关闭原生音效,视频费用约省 1/3;
  也可把端点换成 Kling 3 Standard(`v3/standard/...`,约 75 折)或旧版 2.x。
- **费用参考**(以 fal.ai 实时定价为准)— 60 秒成片:Kling 3 Pro 含音效约 $10,
  关音效约 $6.7;参考图 $0.08;分镜脚本几美分到几十美分(视模型而定)。
