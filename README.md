# 8K Dolby Workstation 2.1

用于视频检查、原片保留、高精度转码和可选 AI 增强的本地工作站。桌面与命令行共用处理核心，输出经过解码和格式检查后才保存为正式文件。

本版将 **B站 Dolby Vision 8.4 交付**、**常规高质量导出**和 **AI 增强**分开，默认优先保留原片。已有 Dolby Vision / Atmos 素材可检查后保留码流；需要重新制作时，使用明确的色彩与配套元数据流程。

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Desktop](https://img.shields.io/badge/Desktop-Windows-orange)

## 选择适合素材的方案

| 界面方案 | 路线 | 适用素材与行为 |
| --- | --- | --- |
| B站杜比 · 保留原片 | `preserve` | 已有合规 Dolby Vision 8.4 素材；保留视频编码、尺寸、帧率、位深和杜比数据，封装为 MP4。已有 E-AC-3 JOC / Atmos 可原码流复制 |
| B站杜比 · HLG / 8.4 制作 | `transcode` | 从 SDR / HDR 素材制作 HLG 基础层，注入针对最终画面及时序制作的 **8.4 RPU**；需要成片质检 |
| 高质量常规导出 | `transcode` | 以源精度解码，按目标进行高精度色彩处理，最终编码为 10-bit HEVC；默认沿用源尺寸、帧率和 SDR / PQ / HLG 类型 |
| AI 增强 · 插帧与超分 | `enhance` | 按需使用 RIFE 插帧、Real-ESRGAN x4 超分和分批编码；面向 SDR，使用 8-bit RGB 工作帧 |

原片保留不会为了凑成 8K / 120fps 而重新编码。重新编码不保证与源文件完全一致，AI 超分和插帧也可能引入伪影。建议先检查短片，再处理长片。

### B站杜比与双标

- B站创作中心公开指引列出 **HLG、BT.2020、10-bit HEVC、Dolby Vision Profile 8.4、MP4 / MOV / M4V**。该页编辑于 **2021-11-22**，列出的尺寸 / 帧率为 3840×2160 / 60fps，建议码率约 30Mbps。应用将这些作为保守交付参考与提示，**不把旧文档当作当前平台的完整限制**；竖屏按相应长短边参考检查。[B站官方指引](https://www.bilibili.com/opus/560508706903943321)
- Dolby 的 **2024-03-26** 视频分享服务 FAQ 也说明应依据目标服务选择杜比配置，B站对应 Profile 8.4。PQ / Profile 8.1 不能通过改标签等同于 HLG / Profile 8.4。[Dolby 官方 FAQ](https://professionalsupport.dolby.com/s/article/Dolby-Vision-for-video-sharing-services-FAQs?language=en_US)
- 本地检查核实 Profile 8 / 基础层兼容 ID 4、HLG、BT.2020、4:2:0 10-bit、RPU / 基础层与单层声明，不能证明 RPU 与每个镜头的视觉匹配。
- “杜比视界 + 全景声双标”需要真实 Atmos 音轨。只有 FFprobe / 可选 MediaInfo 给出明确 JOC / Atmos 编码证据时，程序才确认 Atmos；普通 E-AC-3、多声道或音轨标题里的 “Atmos” 不作为证明。
- 优先复制已有 **E-AC-3 JOC / Atmos**。AAC / 普通 E-AC-3 重编码不生成 Atmos 对象元数据；TrueHD Atmos 不能在此流程中直接复制到 MP4，需要另行准备受支持的音频母版。
- **本地格式通过不等于 B站已经认可。** 最终要等待创作中心转码，并在支持的回放环境中确认杜比视界 / 全景声选项及实际效果。报告默认始终记录 `platform_verified: false`；程序不会自动发布投稿。

## 安装与启动

自动安装脚本支持 Windows 10 / 11、**64 位 Python 3.10+**。原片保留 / 转码不需要 AI GPU。RIFE 和 Real-ESRGAN 使用 Vulkan，需要支持 Vulkan 的 GPU 与正常驱动，不要求 CUDA / PyTorch。

```powershell
git clone https://github.com/a13718434469/8K-Dolby-Workstation.git
cd 8K-Dolby-Workstation

# 基础环境与 FFmpeg：可先使用保留原片 / 常规转码
.\setup.bat -CoreOnly
.\run.bat

# 完整 AI 环境和可选 dovi_tool
.\setup.bat -WithDovi

# 仅检查安装；或在已手动放好工具时仅装依赖并检查
.\setup.bat -VerifyOnly
.\setup.bat -SkipDownloads
```

`setup.bat` 不加参数会安装完整 AI 工具和模型。安装器使用项目内 `.venv`，从原项目官方发行包下载，校验 FFmpeg 官方 SHA256，跳过已完整安装的工具。工具包下载最多等待 20 分钟，校验清单最多等待 2 分钟，失败后可重试或手动安装。首次运行 `run.bat` 缺少可用环境时，会安装基础环境。

桌面入口也可直接运行：

```powershell
.\.venv\Scripts\python.exe main.py
```

### 桌面使用

1. 选择或拖入视频，异步读取源尺寸、帧率、精度、杜比配置和音轨证据。
2. 选择方案。已有 8.4 素材优先“保留原片”，需要重新编码时选择对应制作方案。
3. 指定新的 MP4 输出文件名；高级选项可调整质量、速度、AI 工作尺寸和批次大小。
4. 使用“检查素材”查看本地提示。重新制作杜比视界时选择与成片配套的 RPU。
5. 开始处理，完成后查看成片质检和输出目录，再进行平台验收。

窗口支持滚动布局，高级参数与详细日志默认收起。滚动页面不会意外改变下拉选项。程序从实际 PyQt5 包位置设置插件目录，支持含中文的安装路径。

## 画质与色彩流程

**原片保留**直接复制视频码流，检查编码、尺寸、精度、色彩和杜比配置，并比较编码视频负载的 SHA256。它保留原有 RPU 及时序，避免重新调色或插帧后沿用不匹配的元数据。

**原生高精度转码**不先经过 8-bit PNG。以源精度解码，按 SDR / PQ / HLG 信息处理，必要时使用浮点工作格式，最终量化为 4:2:0 10-bit HEVC。HDR → SDR 使用明确色调映射；SDR → HDR 不会恢复原素材不存在的高光或色彩细节。

**AI 增强**按目标帧率计算 RIFE 输出，使用 Real-ESRGAN x4 模型并分批超分 / 编码。它仍使用 8-bit SDR RGB 工作帧，不能代替原生 HDR 母版流程；高于工作尺寸的源素材会缩小。优先保画质时应选择保留原片或原生转码。

**杜比重新制作**中，8.1 使用 PQ 基础层，8.4 使用 HLG 基础层。外部 RPU 必须对应最终画面的色彩、裁切、尺寸、镜头及时序，帧数须完全匹配。改变画面或帧率后不应直接沿用源 RPU；静态模板元数据不等于专业动态调色分析。

x265 默认 CRF 为 18，数值更低通常产生更大文件。较慢预设可能以更多时间提高压缩效率。空间和耗时估算只作参考，不保证画质、精确文件大小或完成时间。

## 命令行

命令行不启动 GUI，与桌面共用核心。默认保留原视频与音轨，沿用源尺寸、帧率和色彩，AI / 插帧默认关闭。手机旋转素材在原生转码时按显示方向确定默认宽高，原片保留则保留编码尺寸与旋转信息：

```powershell
# 已有 B站 8.4 素材：原码流交付与本地检查
.\.venv\Scripts\python.exe cli.py -i "source_dv84.mp4" -o "delivery.mp4" --route preserve --delivery bilibili

# 要求明确存在且保留 Atmos，证据不足时拒绝继续
.\.venv\Scripts\python.exe cli.py -i "source_dv84_atmos.mp4" -o "delivery_dual.mp4" --route preserve --delivery bilibili --require-atmos

# 原生高精度转码，沿用源尺寸 / 帧率 / 色彩
.\.venv\Scripts\python.exe cli.py -i "master.mov" -o "high_quality.mp4" --route transcode --color source --preset slow --crf 18

# B站 8.4 制作：RPU 与最终 4K / 60fps HLG 画面配套
.\.venv\Scripts\python.exe cli.py -i "master.mov" -o "bili_dv84.mp4" --route transcode --delivery bilibili --color dv84 --size 3840x2160 --fps 60 --rpu "matched_P84_RPU.bin" --audio-mode copy

# 外部原始 E-AC-3 JOC 音轨，原码流复制并要求 Atmos 证据
.\.venv\Scripts\python.exe cli.py -i "source_dv84.mp4" -o "dual.mp4" --route preserve --delivery bilibili --audio "matched_atmos.eac3" --audio-mode copy --require-atmos

# SDR AI 增强，主动启用插帧与超分
.\.venv\Scripts\python.exe cli.py -i "sdr.mp4" -o "enhanced.mp4" --route enhance --ai --interp --work-size 1920x1080 --size 3840x2160 --fps 60 --color sdr

# 使用原任务完整参数恢复
.\.venv\Scripts\python.exe cli.py --resume --workspace "workspace"
.\.venv\Scripts\python.exe cli.py --help
```

| 参数 | 作用 |
| --- | --- |
| `-i, --input` / `-o, --output` | 输入 / 新的 MP4 输出路径 |
| `--route preserve\|transcode\|enhance` | 保留原片 / 原生转码 / AI 增强 |
| `--delivery general\|bilibili` | 常规交付 / 严格 B站杜比格式检查 |
| `--color source\|sdr\|hdr10\|hlg\|dv81\|dv84` | 沿用源色彩或明确输出类型 |
| `--size WIDTHxHEIGHT` / `--fps` | 可选尺寸 / 帧率，支持分数；原片保留沿用源值 |
| `--ai` / `--interp` | 在增强路线中启用超分 / 插帧 |
| `--no-ai` / `--no-interp` | 兼容旧命令的关闭选项 |
| `--work-size WIDTHxHEIGHT` / `--model` | AI 工作尺寸 / 模型 |
| `--rpu` | 与最终画面及时序匹配的 RPU |
| `--audio` / `--audio-mode copy\|aac\|eac3\|none` | 外部音频 / 音轨处理方式 |
| `--require-atmos` | 要求已有 Atmos 证据并保留，不生成 Atmos |
| `--preset` / `--crf` | x265 速度与质量 |
| `--batch-size` | 增强路线批次帧数，范围 1–256 |
| `--keep-temp` | 保留中间文件 |
| `--workspace` / `--resume` | 工作目录 / 恢复原任务 |

原生转码改变帧率时执行恒定帧率重采样，不自动生成 AI 运动帧；RIFE 插帧需要增强路线。重新编码的输出宽高须为偶数，宽至少 64、高至少 48，均不超过 8192；缩放保持比例并按需补边。更小画面可原码流保留，避免部分 x265 版本的极小尺寸崩溃。所有路线都拒绝覆盖现有输出或源文件；原片保留不使用 AI、插帧或色彩重编码设置。

## 音轨、元数据与隐私

使用视频或外部音频的第一条音轨。无音轨视频可以输出，但不能成为 Atmos 双标素材。复制支持 MP4 接受的 AAC、AC-3、E-AC-3、MP3、ALAC；短音轨按原长保留，不截短视频。AAC / E-AC-3 重编码会补齐短音轨并按视频时长截取；普通 E-AC-3 最多保留 5.1 声道，更多声道会下混。

B站交付封装时移除容器附加元数据和章节。本地报告可能含文件名、路径和编码信息，应作为本地工作文件管理。字幕、附件、章节和额外音轨不在当前输出范围内。

仓库与源码包只应包含代码、测试、文档和必要配置。工具 / 模型、媒体、截图、断点、临时帧、虚拟环境、日志、Cookie 和密钥不应提交。处理与质检不会自动上传媒体或自动发布 B站投稿。

## 成片质检与平台验收

每次成功输出可生成同目录的 `成片.mp4.quality.json`，记录媒体参数、检测工具、目标路线、失败 / 提示项目及依据。`platform_verified` 始终为 `false`，平台实测证据应单独保存。

本地检查包含：

- 编码、尺寸、帧率、帧数、时长、精度和色彩信号。
- 完整视频和音频解码，避免只凭“封装成功”判断成片有效。整片计帧不使用短时元数据检测超时，仍可随时停止。
- 保留原片的视频负载 SHA256、杜比配置与色彩一致性。
- B站路线的 HLG / Profile 8.4、基础层 / RPU / 单层声明和 4:2:0 10-bit。
- Atmos 编码证据；可选 MediaInfo 补充 JOC 检测，未安装不会自动下载。

技术检查不能自动评判所有 AI 伪影、镜头调色或 RPU 视觉匹配，也不替代真实播放器与平台转码。先用短片检查暗部、高光、色阶、运动边缘、音画同步及支持设备上的 HDR / Atmos 回放，再处理完整作品。

平台双标验收应使用已确认 DV 8.4 + E-AC-3 JOC 的测试片，上传 B站创作中心草稿，等待转码后核对识别结果。草稿上传不等于已完成所有清晰度转码，也不等于公开发布。

## 暂停、停止与恢复

暂停在当前外部命令完成后的安全边界生效，当前编码 / 推理可能仍需一段时间。停止会终止正在执行的处理 / 质检子进程，保留已验证完成的阶段和分片。

任务有独立目录和原子清单，同一工作目录只允许一个写入者。恢复使用原参数与源文件指纹；输入或参数改变时需要新任务。未完成阶段 / 批次会重做，已验证分片继续复用。

```text
workspace/
├── checkpoint.json          # 最近未完成任务入口
├── .lock                    # 工作目录写入锁
└── jobs/<job_id>/
    ├── manifest.json        # 参数、源指纹、阶段与分片
    ├── native.mp4           # 原生转码中间结果
    ├── source/              # 增强路线工作帧
    ├── interpolated/        # 可选 RIFE 结果
    ├── chunks/              # 已验证的编码分片
    └── batch_*/             # 当前增强批次
```

清理只处理当前任务拥有的中间文件。增强路线仍可能保存完整帧序列，长片要预留大量空间；保留原片和原生转码不先展开整片 PNG。工作盘、输出盘都要保留空间，保守估算不是最终压缩文件大小。

## 手动安装与跨平台

程序优先查找 `tools/`，随后查找 `PATH`，AI 模型仍需放在项目目录。自动安装器仅支持 Windows；Linux / macOS 可手动安装相应平台程序与 Python 依赖，使用 CLI。跨平台测试不代表所有桌面环境或驱动都已验收。

- [FFmpeg](https://ffmpeg.org/download.html) / [BtbN Windows builds](https://github.com/BtbN/FFmpeg-Builds/releases)：使用完整新版，需 ffmpeg / ffprobe、libx265、AAC、E-AC-3、`zscale`。重新注入 DV 还需支持 `compression=0` 的 `dovi_rpu` 与 `setts`。
- [Real-ESRGAN Windows v0.2.5.0](https://github.com/xinntao/Real-ESRGAN/releases/tag/v0.2.5.0)：程序与 `models/`。
- [RIFE ncnn Vulkan 20221029](https://github.com/nihui/rife-ncnn-vulkan/releases/tag/20221029)：程序与 `rife-v4.6/`。
- [dovi_tool 2.3.4](https://github.com/quietvoid/dovi_tool/releases/tag/2.3.4)：重新注入 RPU 时需要，保留原片不必重生成 RPU。
- [MediaInfo](https://mediaarea.net/en/MediaInfo)：可选命令行工具，补充 JOC / Atmos 证据。

```text
tools/
├── ffmpeg.exe / ffprobe.exe
├── realesrgan-ncnn-vulkan.exe
├── models/
│   ├── realesrgan-x4plus.bin / .param
│   ├── realesrgan-x4plus-anime.bin / .param
│   └── realesr-animevideov3-x4.bin / .param
├── rife-ncnn-vulkan.exe
├── rife-v4.6/flownet.bin / flownet.param
├── dovi_tool.exe             # 可选
└── mediainfo.exe             # 可选
```

## 开发与验证

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v

# 可选：真实 8K / 120fps HDR10 单帧编码，需额外内存
$env:DOLBY_RUN_8K_SMOKE = '1'
python -m unittest discover -s tests -p test_8k.py -v
```

测试覆盖真实 FFmpeg 文件、分数帧率、HDR / DV 元数据与帧数、音轨负载复制、原生精度、恢复 / 取消、原子输出、B站质检、Atmos 证据和 GUI 启动。原生 DV 测试需要本机 dovi_tool 及相应 FFmpeg 功能，工具缺失会明确跳过。AI 替代推理测试不代替真实 GPU 与长片画质验收。

GitHub Actions 在 Windows / Ubuntu、Python 3.10 / 3.12 上运行回归。入口是 `main.py`（桌面）、`cli.py`（命令行），核心是 `pipeline.py`（处理）与 `quality.py`（交付检查），安装逻辑在 `setup.ps1`。

## 许可证

项目代码采用 [MIT License](LICENSE)。FFmpeg、RIFE、Real-ESRGAN、dovi_tool、MediaInfo 和模型遵循各自许可证。本仓库不捆绑第三方程序 / 模型，也不因安装到项目目录而改变其许可证。
