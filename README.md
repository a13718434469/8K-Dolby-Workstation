# 8K Dolby Workstation

面向 **SDR 视频**的桌面与命令行处理工作站：RIFE 插帧、Real-ESRGAN 超分、HEVC 10-bit 编码，以及 SDR / HDR10 / Dolby Vision Profile 8.1 输出封装。

支持最高 8K 目标尺寸、可选 120fps、分批超分编码和任务恢复。实际速度取决于素材、目标尺寸、GPU、编码预设与磁盘，项目不提供固定 GPU 耗时承诺。

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Platform](https://img.shields.io/badge/Desktop-Windows-orange)

## 功能与输出范围

| 功能 | 当前行为 |
| --- | --- |
| AI 插帧 | 使用 RIFE v4 模型，按目标帧率计算所需帧数；保留分数源帧率，关闭时沿用源帧率 |
| AI 超分 | Real-ESRGAN x4，按批次超分后编码；可关闭并改用普通缩放 |
| 画面尺寸 | 桌面界面提供 1080p / 4K / 8K；等比缩放并补边，不拉伸画面 |
| SDR | BT.709、HEVC 10-bit、MP4 |
| HDR10 | 将 SDR 工作画面转换为 BT.2020 / PQ、HEVC 10-bit |
| Dolby Vision 8.1 | 注入用户提供的配套 Profile 8 RPU，并检查最终 Dolby Vision 8.1 配置 |
| 音频 | 复制第一条音轨、编码 AAC / E-AC-3、替换外部音轨，或输出无音轨视频 |
| 任务控制 | 日志、阶段进度、暂停、停止、基于完成阶段和视频分片的恢复 |
| 输出校验 | 检查尺寸、帧数、帧率、像素格式和时长；验证后才发布最终文件 |

**画面与音频说明：**

- 当前输入流程只支持 SDR；检测到 PQ / HLG 源会拒绝处理。HDR 素材应先在受控流程中转换为 SDR。
- AI 工作帧是最高 1080p 的 8-bit RGB PNG。高于 1080p 的输入也会先缩小；10-bit 输出不会恢复原素材中已丢失的精度或细节。
- SDR → HDR10 是色彩空间与传递函数转换，不会恢复素材中不存在的 HDR 高光，也不等同于专业 HDR 调色。
- Dolby Vision 必须提供针对**最终画面、调色、时序和帧数**制作的 RPU。程序检查 Profile 8、帧数和最终 8.1 配置，但无法判断元数据是否与画面在视觉上匹配；它不自动生成真实杜比动态调色元数据。
- 普通 E-AC-3 编码不生成 Dolby Atmos 对象元数据。复制已有 E-AC-3 JOC 音轨时保留其原始码流；TrueHD 音轨不能在此流程中直接复制进 MP4，需选择 AAC / E-AC-3 重编码，而重编码不保留 Atmos 元数据。
- 项目不承诺杜比认证、上传平台认证标签或所有设备的播放兼容性。

## 安装与启动

自动安装脚本面向 Windows 10/11 64 位，需要 **64 位 Python 3.10+**。AI 引擎使用 Vulkan，需支持 Vulkan 的 GPU 与适配驱动；不要求 CUDA / PyTorch。关闭 AI 超分和插帧后可使用 FFmpeg 基础流程。

```powershell
git clone https://github.com/a13718434469/8K-Dolby-Workstation.git
cd 8K-Dolby-Workstation
.\setup.bat
.\run.bat
```

安装器在项目内创建 `.venv`，从工具原项目下载 FFmpeg、RIFE 及 Real-ESRGAN 的程序和模型。已存在的完整工具会跳过；FFmpeg 下载同时校验官方 SHA256。下载量较大，请预留空间。单个工具包下载最长等待 20 分钟，校验清单最长等待 2 分钟；超时后会报错，可重试或手动安装。

```powershell
# 仅安装 Python 环境和 FFmpeg；界面中关闭 AI 超分、AI 插帧即可使用
.\setup.bat -CoreOnly

# 需要处理配套 RPU 时，额外安装 dovi_tool
.\setup.bat -WithDovi

# 只检查现有安装，不安装 Python 依赖、不下载工具
.\setup.bat -VerifyOnly

# 已手动放好工具时，只安装 Python 依赖并检查工具
.\setup.bat -SkipDownloads
```

首次直接运行 `run.bat` 时，如果缺少可用的 `.venv`，会自动安装基础环境；需要 AI 功能时再运行完整的 `setup.bat`。也可直接运行：

```powershell
.\.venv\Scripts\python.exe main.py
```

界面中选择视频、输出路径和处理参数后开始。默认启用 AI 超分与插帧、8K / 120fps 目标、SDR 输出和音轨复制；未安装 AI 工具时应先关闭对应选项。目标帧率必须不低于源帧率且不超过 240fps。

## 命令行

命令行与桌面界面共用处理引擎，不需要启动 GUI：

```powershell
# 基础 SDR 编码：关闭 AI，保留源帧率
.\.venv\Scripts\python.exe cli.py -i "input.mp4" -o "output_sdr.mp4" --no-ai --no-interp --size 1920x1080

# AI 4K / 120fps，输出 HDR10；音轨重新编码为 AAC
.\.venv\Scripts\python.exe cli.py -i "input.mp4" -o "output_hdr10.mp4" --size 3840x2160 --fps 120 --color hdr10 --audio-mode aac

# Dolby Vision 8.1：RPU 必须与本次最终画面及时序匹配
.\.venv\Scripts\python.exe cli.py -i "input.mp4" -o "output_dv.mp4" --color dv81 --rpu "matched_RPU.bin"

# 恢复指定工作目录中的上次任务
.\.venv\Scripts\python.exe cli.py --resume --workspace "workspace"

# 查看完整参数
.\.venv\Scripts\python.exe cli.py --help
```

| 参数 | 用途 |
| --- | --- |
| `-i, --input` / `-o, --output` | 输入视频 / 新的输出 MP4 路径 |
| `--no-ai` / `--no-interp` | 关闭 AI 超分 / 关闭插帧并保留源帧率 |
| `--size WIDTHxHEIGHT` / `--fps` | 输出尺寸 / 目标帧率，支持 `60000/1001` 等分数 |
| `--model` | 超分模型名称，默认 `realesrgan-x4plus` |
| `--color sdr\|hdr10\|dv81` / `--rpu` | 色彩模式 / 配套 RPU 文件 |
| `--audio` / `--audio-mode copy\|aac\|eac3\|none` | 可选外部音频 / 音轨处理方式 |
| `--batch-size` | 每批处理帧数，范围 1–256，默认 64 |
| `--preset` / `--crf` | x265 编码速度与质量参数 |
| `--keep-temp` | 保留工作文件，用于检查与排错 |
| `--workspace` / `--resume` | 选择工作目录 / 恢复该目录中的上次任务 |

输出尺寸宽高必须是 16–8192 范围内的偶数。所有模式都拒绝覆盖已有输出；恢复时也请保留原输出路径，并确保该文件尚未生成。

## 音轨与任务恢复

程序使用输入视频或外部音频的第一条音轨。无音轨的视频可正常输出。`copy` 支持 MP4 可接受的 AAC、AC-3、E-AC-3、MP3 和 ALAC；短音轨保持原长，不缩短视频。AAC / E-AC-3 模式会补齐短音轨并截取到视频时长；E-AC-3 编码最多保留 5.1 声道，更多声道会下混。字幕、附件、章节和额外音轨不在当前输出范围内。

暂停在**当前外部命令结束后**生效；当前命令可能仍需较长时间。停止会终止正在执行的外部进程，保留可用处理记录。恢复重用已验证的完成阶段和视频分片，未完成的阶段或批次会重新执行。

Dolby Vision 模式的分片禁用 B 帧重排序，注入 RPU 后重新建立恒定帧率时间戳，并通过 FFmpeg 写入 MP4 所需的杜比配置。最终文件通过帧数、帧率、时长及 Profile 8.1 配置检查后才发布。

```text
workspace/
├── checkpoint.json          # 最近的未完成任务入口
├── .lock                    # 防止同一工作目录同时写入
└── jobs/<job_id>/
    ├── manifest.json        # 参数、输入文件信息、完成阶段与分片记录
    ├── source/              # 提取的工作帧
    ├── interpolated/        # 可选插帧结果
    ├── chunks/              # 已编码并校验的视频分片
    └── batch_*/             # 当前批次的工作文件
```

界面“恢复上次任务”会恢复保存的参数。命令行 `--resume` 使用对应工作目录的记录；输入内容或参数发生变化时需要新建任务。一个工作目录同时只允许一个任务；并行处理请分别指定 `--workspace`。不要删除要恢复的任务文件。

自动清理模式在阶段完成后移除可再生成的文件，成功后保留任务清单。超分按批次进行，但提取帧和插帧阶段仍会保存完整序列，长视频可能需要大量磁盘空间。界面空间值按未压缩帧和编码中间文件保守估算，不能当作压缩输出大小或精确空间保证；工作盘和输出盘都应保留足够空间。

## 手动安装与跨平台使用

可从以下原项目下载工具；保留模型目录，勿只复制 `.exe`：

- [FFmpeg](https://ffmpeg.org/download.html) / [BtbN Windows builds](https://github.com/BtbN/FFmpeg-Builds/releases)：建议使用当前完整版本，需 `ffmpeg` 与 `ffprobe`，支持 `libx265`、AAC、E-AC-3 和所有画面模式必需的 `zscale`。Dolby Vision 还要求 `dovi_rpu`（支持 `compression=0`）及 `setts` 位流过滤器；旧版或精简版可能缺少这些功能，`setup.bat -WithDovi` 会检查并提示升级。
- [Real-ESRGAN Windows bundle v0.2.5.0](https://github.com/xinntao/Real-ESRGAN/releases/tag/v0.2.5.0)：程序和 `models/` 中的模型文件。
- [RIFE ncnn Vulkan 20221029](https://github.com/nihui/rife-ncnn-vulkan/releases/tag/20221029)：程序和 `rife-v4.6/` 模型目录。
- [dovi_tool 2.3.4](https://github.com/quietvoid/dovi_tool/releases/tag/2.3.4)：仅 Dolby Vision 元数据模式需要。

```text
tools/
├── ffmpeg.exe
├── ffprobe.exe
├── realesrgan-ncnn-vulkan.exe
├── models/
│   ├── realesrgan-x4plus.bin / .param
│   ├── realesrgan-x4plus-anime.bin / .param
│   └── realesr-animevideov3-x4.bin / .param
├── rife-ncnn-vulkan.exe
├── rife-v4.6/
│   └── flownet.bin / flownet.param
└── dovi_tool.exe             # 可选
```

程序优先查找 `tools/`，随后查找 `PATH` 中的可执行文件；AI 模型仍需放在上述项目目录。Linux / macOS 请手动安装对应平台工具和 Python 依赖，使用不带 `.exe` 后缀的程序；自动安装脚本仅支持 Windows，跨平台 CI 验证基础媒体流程，不覆盖所有桌面环境与 GPU 驱动。

## 开发与验证

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v

# 可选：真实 8K 单帧 / 120fps HDR10 编码测试（需要额外内存）
$env:DOLBY_RUN_8K_SMOKE = '1'
python -m unittest discover -s tests -p test_8k.py -v
```

媒体回归测试使用 `PATH` 中真实的 FFmpeg / ffprobe，覆盖时长、分数帧率、分片合并、HDR10 信号、音轨、停止与恢复等行为；缺少媒体工具时对应测试会跳过。AI 命令测试使用替代推理结果，不能替代真实 GPU 质量验证。Dolby Vision 还需要外部配套 RPU 和播放器验证。GitHub Actions 在 Ubuntu / Windows、Python 3.10 / 3.12 上安装 FFmpeg 并运行测试；测试过程不下载 AI 工具与模型。

项目入口为 `main.py`（桌面）、`cli.py`（命令行），共用 `pipeline.py`；安装逻辑集中在 `setup.ps1`。

## 许可证

本项目代码采用 [MIT License](LICENSE)。FFmpeg、RIFE、Real-ESRGAN、dovi_tool 及模型遵循各自原项目许可证，不因安装到本项目而改用 MIT。本仓库不捆绑这些第三方程序或模型。
