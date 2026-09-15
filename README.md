# AI 短视频生成器(OpenRouter × fal.ai)

跨平台桌面小工具(Windows / macOS / Linux):输入一句话描述,点击「生成」,自动产出一条约 **60 秒**的高质量短视频。

## 工作原理

```
一句话描述
   │
   ▼
① LLM 导演(经 OpenRouter)     → 扩写为分镜脚本,镜头数量与每镜头时长
   │                              由导演按叙事节奏决定
   │  默认 Qwen 3.8 Max (medium)    可换 OpenRouter 上任意模型
   │                              并判断是否存在贯穿全片的主角
   ▼
② 主角参考图(Nano Banana 2)   → 有主角时自动生成一张参考图(约 $0.08)
   │                              无主角(纯风景等)则跳过
   ▼
③ Gemini Omni Flash 1.1(fal.ai) → 多镜头并行生成,自带同步音频与配音,
   │                              单镜头组 3~10 秒一次连续生成
   │  有主角: 参考图随每个镜头送入,全片角色外观一致
   │  无主角: 纯文生视频             失败自动降级/重试,断点续传
   │  可在界面上切换 Seedance 2.5 / Seedance 2.0 / Kling 3(均经 fal.ai)
   ▼
④ ffmpeg                        → 按导演决定的硬切 / 交叉溶解拼接 + 首尾淡入淡出;
   │                              旁白混入时原生音轨自动闪避;
   │                              music/ 里有音频则由导演按情绪挑选混入
   ▼
output/日期_标题/标题.mp4
```

内部稳健性设计(用户无需任何设置):

- **角色一致性**:业界公认做法——先生成主角参考图,再用 reference-to-video
  把参考图带入每个镜头,主角外观在整段片段中保持一致(优于仅锁首帧的 image-to-video)。
  是否需要参考图由导演模型自动判断;参考图任何一步失败都自动降级为纯文生视频,绝不影响出片;
- **并行生成 + 看门狗**:多个镜头同时提交,单镜头独立重试并带超时保护;
  KEY 无效、余额不足等致命错误立即终止,不空耗等待;
- **断点续传**:分镜脚本、参考图与已完成片段落盘保存,同一描述再次生成时自动续接,不重复扣费;
- **生成前预检**:先校验 OpenRouter KEY 与磁盘空间,配错即刻提示;
- **成片质感**:镜头组之间由导演像真实剪辑一样决定硬切或交叉溶解(同场景硬切、
  时间跳跃才溶解)、首尾淡入淡出、视频模型原生音效与配音;有旁白或背景音乐时
  会提示视频模型不要自行配乐,旁白响起时原生音轨自动压低(侧链闪避);
- **旁白后备**:Edge TTS(免费)不可用时自动改用 fal.ai 的 MiniMax 中文 TTS
  (约 $0.10/千字),解说型影片不会因免费服务被封而丢掉旁白;
- **背景音乐**:把 mp3 放进 `music/` 文件夹,导演模型会按影片情绪挑选一首混入
  (压低音量、结尾淡出),文件夹为空则不加;
- **运行日志**:每次任务的完整日志写入任务目录 `log.txt`,便于排查问题。

## 安装

### 方式一:免安装版(推荐)

从 [Releases](../../releases) 下载对应平台的压缩包——**无需安装 Python 和 ffmpeg**(均已内置):

| 平台 | 下载文件 | 解压后 |
| --- | --- | --- |
| Windows(64 位) | `AI-Video-Generator_win64.zip` | 双击 `AI短视频生成器.exe` |
| macOS(Apple 芯片) | `AI-Video-Generator_macos-arm64.tar.gz` | 首次**右键 → 打开** `AI短视频生成器.app` |
| Linux(x86-64) | `AI-Video-Generator_linux64.tar.gz` | 终端运行 `./AI短视频生成器` |

首次运行会在程序旁自动生成 `config.yaml`(macOS 生成在 `.app` 旁边),
在窗口顶部的「设置」区填入两个 API KEY 即可使用(点「生成」时自动保存);
成片输出到同目录的 `output/` 文件夹。

> macOS 版未做付费签名,首次启动请**右键(或按住 Control 点击)→ 打开**,
> 才会出现「仍要打开」按钮;Intel 芯片的旧款 Mac 请用源码运行。
> 打包版由仓库的 GitHub Actions 自动构建(Actions → *Build packages*),
> 也可以在本机执行 `python build.py` 打包当前平台。

### 方式二:源码运行

1. 安装 [Python 3.10+](https://www.python.org/downloads/)(Windows 勾选 *Add python.exe to PATH*)
2. 安装 ffmpeg 并加入 PATH(或在 `config.yaml` 里填绝对路径):
   Windows 用 [gyan.dev 构建](https://www.gyan.dev/ffmpeg/builds/),macOS 用 `brew install ffmpeg`,
   Linux 用 `sudo apt install ffmpeg`(或对应发行版的包管理器)
3. 安装依赖:

```bash
pip install -r requirements.txt
```

## 配置

打开软件,在窗口顶部的「设置」区填入两个 API KEY(点「💾 保存设置」或直接点
「生成」都会保存到程序目录的 `config.yaml`):

- **OpenRouter KEY**:<https://openrouter.ai/settings/keys>,用于编剧/导演模型;
- **fal.ai KEY**:<https://fal.ai/dashboard/keys>,所有视频引擎与参考图均经 fal.ai 生成。

同一区域还可以选择:

- **编剧模型**:默认 **Qwen 3.8 Max**(支持看图,思考深度 medium),下拉框列出
  常用模型(`z-ai/glm-5.3`、`anthropic/claude-fable-5`、`openai/gpt-5.2`、
  `google/gemini-3-pro` 等),也可以直接输入 OpenRouter 上的任意模型 ID
  (上传参考图时需选支持图片输入的模型,否则导演只能按文字说明推断);
- **视频引擎**:默认 **Gemini Omni Flash 1.1**(720p 仅 $0.10/秒,单组
  3~10 秒,原生同步音频始终开启,参考图最多 10 张);可切换 **Seedance 2.5**
  (单镜头组最长 30 秒一次连续生成,参考图最多 30 张)、**Seedance 2.0**
  或 **Kling 3**;
- **分辨率**:随引擎变化(Gemini 与 Seedance 2.0 最高 4k,Seedance 2.5 为
  480p/720p/1080p;Kling 端点不接受分辨率参数),分辨率越高费用越高。

参考图固定使用 **Nano Banana 2**。分镜提示词按引擎自动选择语言(Seedance 系用
中文,官方一等支持;Kling 与 Gemini 用英文),**角色台词一律默认中文普通话**
(创意里明确要求其他语言时除外;可在 `config.yaml` 的 `video.dialogue_language`
修改默认值)。

音效、并发数、旁白音色、转场等进阶参数在 `config.yaml` 中修改(点「更多设置
(配置文件)」可直接打开),一般保持默认即可。

## 使用

打开免安装版程序(或源码运行 `python main.py`)。

在窗口里输入一句话描述,选择画幅——**横屏 16:9**(B 站/YouTube)、**竖屏 9:16**
(抖音/快手/视频号)、**方形 1:1**、**横幅 4:3** 或**竖幅 3:4**(小红书等),
再选择大约时长(**30 秒 / 1 分钟 / 2 分钟**),点击「🎬 生成视频」。
全程约十几分钟(视频模型每个镜头需要数分钟),进度条按镜头推进,日志实时显示,
完成后点击「打开成片」。
导演模型会按所选画幅与时长设计构图和节奏(Gemini 引擎下 1:1 / 4:3 / 3:4、Kling
引擎下 4:3 与 3:4 由相邻画幅生成后自动居中裁剪,Seedance 原生支持全部画幅)。
画幅、时长或引擎不同的未完成任务互相独立、各自断点续传。

想指定主角长相时,可点「🖼 上传参考图(可多选)」选择一张或多张图片(如宠物照片、
角色三视图、场景照):每张图可注明用途(主角正面/侧面/场景参考/风格参考等),
参考图会锁定全片画面元素,导演模型也会照着图撰写分镜;不上传则由 AI 自动判断并生成主角形象。

> 多图支持随引擎而异:**Gemini Omni Flash**(默认)最多 10 张、**Seedance 2.5**
> 最多 30 张、**Seedance 2.0** 最多 9 张,各图的用途说明会写入提示词
> (角色三视图能显著提升角色一致性);
> **Kling** 仅把多张图作为**同一主角的多角度参考**,单独的用途说明不生效。
> 生成日志中也会提示当前引擎的支持情况。

也支持命令行模式(可在描述后附一张或多张参考图路径,`路径=用途` 可注明用途):

```bash
python main.py "一只橘猫在雨后的东京街头漫步,霓虹灯倒映在水洼里,电影感画面"
python main.py "我家猫咪在大厂上班的一天" my_cat.jpg
python main.py "我家猫咪在大厂上班的一天" front.jpg=主角正面 side.jpg=主角侧面
```

## 输出

每次生成会在 `output/` 下创建独立目录:

```
output/20260803_153000_雨巷橘猫/
├── storyboard.txt   分镜脚本(中英文)
├── reference.png    主角参考图(有主角时;用户上传的多张图为 reference_01.* 等,
│                    各图用途记录在 references.json)
├── log.txt          本次任务完整日志
├── shot_01.mp4      各镜头片段
├── ...
└── 雨巷橘猫.mp4     最终成片
```

## 常见问题

- **提示未找到 ffmpeg** — 免安装版已内置 ffmpeg,不会出现此问题;源码运行时请确认
  已安装并加入 PATH,或在 `config.yaml` 的 `ffmpeg.path` 填写完整路径。
- **macOS 提示"已损坏"或"无法验证开发者"** — 因为程序未做付费签名。首次启动请
  右键(按住 Control 点击)→ 打开;若仍被拦截,在终端执行
  `xattr -cr AI短视频生成器.app` 后再打开。
- **想换视频引擎/分辨率** — 直接在界面「设置」区的下拉框里选(Gemini Omni Flash 1.1 /
  Seedance 2.5 / Seedance 2.0 / Kling 3,均经 fal.ai);各引擎的端点在
  `config.yaml` 的 `gemini.*` / `seedance25.*` / `seedance.*` / `kling.*` 中修改,
  可选端点见 [fal.ai 模型页](https://fal.ai/models)。
  注意旧版 Kling(2.x)仅支持 5/10 秒镜头且无原生音效。
- **想换编剧模型** — 在界面「编剧模型」下拉框里选或直接输入 OpenRouter 上的任意
  模型 ID;`config.yaml` 的 `llm.reasoning_effort` 控制思考深度(不支持思考的模型自动忽略)。
- **想复现/对比生成结果** — 把 `seedance25.seed` 固定为非负整数,相同参数下可
  复现同一结果,便于微调提示词后对比;默认 -1 为每次随机(Seedance 2.5 端点仅带
  参考图的镜头组支持 seed;Seedance 2.0、Kling 与 Gemini 不支持)。
- **想省钱** — 默认的 Gemini 已是最省的引擎(720p 约 $0.10/秒),分辨率降到
  `360p` 仅 $0.03/秒;Kling 引擎下 `video.generate_audio: false` 还能再省约 1/3
  (Seedance 系开关音效同价,Gemini 音频始终开启)。
- **费用参考**(以 fal.ai 实时定价为准;生成前日志会按所选引擎与分辨率给出
  本次预估)— 60 秒成片:Gemini Omni Flash 720p 约 $6(360p 约 $1.8,1080p 约 $9,
  4k 约 $18);Seedance 2.5(按 token 计费)720p 约 $28、480p 约 $13、1080p 约 $70;
  Seedance 2.0 标准档 720p 约 $18(480p 约 $8,1080p 约 $41,4k 约 $94);
  Kling 3 Pro 含音效约 $10,关音效约 $6.7;参考图 $0.08;后备 TTS 约 $0.03;
  分镜脚本几美分到几十美分(视模型而定)。

## 发布新版本(维护者)

打一个以 **`v` 开头**的 tag,GitHub Actions 会自动构建三个平台的压缩包并附到同名 Release:

```bash
git tag v1.1.0
git push origin v1.1.0
```

也可以直接在 GitHub 网页上 *Releases → Draft a new release* 创建新标签发布。
注意标签**必须以 `v` 开头**(如 `v1.1.0`,而不是 `1.1.0`),否则不会触发自动构建;
构建约需几分钟,完成后三个平台的压缩包会自动出现在该 Release 的 Assets 中。
