# AI 短视频生成器(Claude × Kling)

在 Windows 上运行的桌面小工具:输入一句话描述,点击「生成」,自动产出一条约 **60 秒**的高质量短视频。

## 工作原理

```
一句话描述
   │
   ▼
① Claude(编剧 + 导演)          → 扩写为 6 个镜头的分镜脚本(每镜头 10 秒)
   │                              固定风格签名 + 角色描述逐字复用,保证跨镜头一致
   ▼
② Kling(fal.ai)               → 多镜头并行生成,单镜头独立重试
   │                              失败后重新点「生成」只补缺失镜头(断点续传)
   ▼
③ ffmpeg                        → 拼接成片;music/ 里有音频则自动混入背景音乐
   │
   ▼
output/日期_标题/标题.mp4
```

内部稳健性设计(用户无需任何设置):

- **并行生成**:多个镜头同时提交 Kling,总耗时约等于单个镜头(数分钟);
- **断点续传**:分镜脚本与已完成片段落盘保存,同一描述再次生成时自动续接,不重复扣费;
- **一致性**:Claude 先确定统一的风格签名(色调/光线/胶片感)与角色外观描述,并在每个镜头 prompt 中逐字复用;
- **背景音乐**:把 mp3 放进 `music/` 文件夹即自动随机混入(压低音量、结尾淡出),文件夹为空则输出纯净成片。

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
anthropic_api_key: "sk-ant-..."   # https://platform.claude.com/
fal_api_key: "..."                # https://fal.ai/dashboard/keys
```

其余配置(Kling 模型端点、镜头时长、画幅比例、目标总时长、输出目录、ffmpeg 路径)均可在该文件中调整,注释里有说明。

## 使用

```bat
python main.py
```

在窗口里输入一句话描述,点击「🎬 生成视频」。全程约十几分钟(Kling 每个镜头需要数分钟),进度会实时显示在日志区,完成后点击「打开成片」。

也支持命令行模式:

```bat
python main.py "一只橘猫在雨后的东京街头漫步,霓虹灯倒映在水洼里,电影感画面"
```

## 输出

每次生成会在 `output/` 下创建独立目录:

```
output/20260803_153000_雨巷橘猫/
├── storyboard.txt   分镜脚本(中英文)
├── shot_01.mp4      各镜头片段
├── ...
└── 雨巷橘猫.mp4     最终成片
```

## 常见问题

- **提示未找到 ffmpeg** — 确认已安装并加入 PATH,或在 `config.yaml` 的 `ffmpeg.path` 填写完整路径。
- **想换 Kling 版本** — 修改 `config.yaml` 中 `kling.endpoint`,可选端点见 [fal.ai 模型页](https://fal.ai/models)。
- **费用** — Claude 一次分镜脚本约几美分;Kling 按片段计费(以 fal.ai 定价为准),6 个 10 秒镜头为主要成本。
