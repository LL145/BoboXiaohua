# AI 短视频生成器(OpenRouter × Kling)

跨平台桌面小工具(Windows / macOS / Linux):输入一句话描述,点击「生成」,自动产出一条约 **60 秒**的高质量短视频。

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

### 方式一:免安装版(推荐)

从 [Releases](../../releases) 下载对应平台的压缩包——**无需安装 Python 和 ffmpeg**(均已内置):

| 平台 | 下载文件 | 解压后 |
| --- | --- | --- |
| Windows(64 位) | `AI-Video-Generator_win64.zip` | 双击 `AI短视频生成器.exe` |
| macOS(Apple 芯片) | `AI-Video-Generator_macos-arm64.tar.gz` | 首次**右键 → 打开** `AI短视频生成器.app` |
| Linux(x86-64) | `AI-Video-Generator_linux64.tar.gz` | 终端运行 `./AI短视频生成器` |

首次运行会在程序旁自动生成 `config.yaml`(macOS 生成在 `.app` 旁边),
点击「打开配置文件」填入两个 API KEY 即可使用;成片输出到同目录的 `output/` 文件夹。

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

打开免安装版程序(或源码运行 `python main.py`)。

在窗口里输入一句话描述,选择**横屏 16:9**(B 站/YouTube)或**竖屏 9:16**
(抖音/快手/视频号),点击「🎬 生成视频」。全程约十几分钟(Kling 每个镜头需要数分钟),
进度条按镜头推进,日志实时显示,完成后点击「打开成片」。
导演模型会按所选画幅设计构图,横竖屏的未完成任务互相独立、各自断点续传。

也支持命令行模式:

```bash
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

- **提示未找到 ffmpeg** — 免安装版已内置 ffmpeg,不会出现此问题;源码运行时请确认
  已安装并加入 PATH,或在 `config.yaml` 的 `ffmpeg.path` 填写完整路径。
- **macOS 提示"已损坏"或"无法验证开发者"** — 因为程序未做付费签名。首次启动请
  右键(按住 Control 点击)→ 打开;若仍被拦截,在终端执行
  `xattr -cr AI短视频生成器.app` 后再打开。
- **想换 Kling 版本** — 修改 `config.yaml` 中 `kling.text_endpoint` / `kling.reference_endpoint`,
  可选端点见 [fal.ai 模型页](https://fal.ai/models)。注意旧版 Kling(2.x)仅支持 5/10 秒镜头且无原生音效。
- **想换编剧模型** — 修改 `config.yaml` 中 `llm.model` 为 OpenRouter 上的任意模型 ID;
  `llm.reasoning_effort` 控制思考深度(不支持思考的模型自动忽略)。
- **想省钱** — `kling.generate_audio: false` 可关闭原生音效,视频费用约省 1/3;
  也可把端点换成 Kling 3 Standard(`v3/standard/...`,约 75 折)或旧版 2.x。
- **费用参考**(以 fal.ai 实时定价为准)— 60 秒成片:Kling 3 Pro 含音效约 $10,
  关音效约 $6.7;参考图 $0.08;分镜脚本几美分到几十美分(视模型而定)。

## 发布新版本(维护者)

打一个以 **`v` 开头**的 tag,GitHub Actions 会自动构建三个平台的压缩包并附到同名 Release:

```bash
git tag v1.1.0
git push origin v1.1.0
```

也可以直接在 GitHub 网页上 *Releases → Draft a new release* 创建新标签发布。
注意标签**必须以 `v` 开头**(如 `v1.1.0`,而不是 `1.1.0`),否则不会触发自动构建;
构建约需几分钟,完成后三个平台的压缩包会自动出现在该 Release 的 Assets 中。
