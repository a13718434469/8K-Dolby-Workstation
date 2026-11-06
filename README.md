# 8K Dolby Workstation 2.7

用于视频检查、原片保留、高精度转码和可选 AI 增强的本地工作站。桌面与命令行共用处理核心，输出经过解码和格式检查后才保存为正式文件。

桌面推荐 **免费本地制作：杜比视界 + 按素材选音质**。拖入视频即可，也可另外拖入要使用的音频。已有 E-AC-3 JOC 时可保留 Atmos；有符合规格的无损母版时可交付 Hi-Res；只有普通 AAC / MP3 时保留原音。免费模式不需要杜比账号、商业编码器或 AWS。需要从普通音轨新编码 Atmos 时，可切换到独立的“双杜比”方案，见 [Atmos 安装与验收](ATMOS_SETUP.md)。

**2.4 验证状态：**音频母版、后端接入、严格质检、取消恢复和界面流程已经实现并完成本地回归；开发环境尚未配置可运行的授权 DEE 或 AWS 编码账户，**普通 PCM → 真正 JOC 尚未实跑，B站双标也尚未验收**。预制 Atmos 夹具证明的是保留、适配与校验，不代替新 Atmos 编码测试。本轮没有上传媒体、创建收费云任务或代用户发布投稿。

普通 SDR 的高光扩展采用独立、保色相的连续曲线；已有 HDR 不重复扩展。仅杜比视界、常规导出、原片保留和 AI 增强仍可手动选择；命令行旧默认仍为原片保留。

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Desktop](https://img.shields.io/badge/Desktop-Windows-orange)

## 选择适合素材的方案

| 界面方案 | 路线 | 适用素材与行为 |
| --- | --- | --- |
| 免费本地制作（推荐） | 自动选择 `preserve` / `transcode` | 合规 8.4 画面保留，其他画面自动制作。音频优先保留现成 JOC，其次为 Hi-Res 规格无损，再按实际源音质输出 |
| B站双杜比 · 自动制作 | 自动选择 `preserve` / `transcode` | 画面目标为 8.4；已有 JOC 保留，普通音轨通过配置的真实编码器制作 Atmos。最终两种格式均须通过本地检查 |
| 原片保留（不重新编码） | `preserve` | 可直接封装为 MP4 的普通视频；保持视频码流，自带音轨直接使用，也可单独替换音频 |
| B站杜比 · 保留原片 | `preserve` | 已有合规 Dolby Vision 8.4 素材；保留视频编码、尺寸、帧率、位深和杜比数据，封装为 MP4。已有 E-AC-3 JOC / Atmos 可原码流复制 |
| 仅杜比视界 · 自动制作 | `transcode` | 从 SDR / HDR 素材制作 HLG 基础层，分析每个最终编码帧后生成 **8.4 RPU**；也可手动提供配套 RPU。此方案不要求 Atmos |
| 高质量常规导出 | `transcode` | 以源精度解码，按目标进行高精度色彩处理，最终编码为 10-bit HEVC；默认沿用源尺寸、帧率和 SDR / PQ / HLG 类型 |
| AI 增强 · 插帧与超分 | `enhance` | 按需使用 RIFE 插帧、Real-ESRGAN x4 超分和分批编码；面向 SDR，使用 8-bit RGB 工作帧 |

原片保留不会为了凑成 8K / 120fps 而重新编码。重新编码不保证与源文件完全一致，AI 超分和插帧也可能引入伪影。建议先检查短片，再处理长片。

### 免费制作与 Hi-Res 投稿

**想改善发闷的音乐或视频原声**，在免费方案的“声音处理”里选择“AI 音质增强 · 补充高频细节”。工作站检测源音轨的有效频带，用本地 NovaSR 模型预测高频，只把推测的高频成分加入原声，并保留左右声道。符合条件时输出 48 kHz、16-bit ALAC，报告注明 AI 重制。这一处理适用于单声道和立体声的带限素材；完整频带、高解析无损母带和现成 Atmos 等按检测结果保留。高频是估算值，效果可通过同音量试听判断。[NovaSR 作者的模型说明与音乐示例](https://huggingface.co/YatharthS/NovaSR)

常规 Windows 安装会准备音质增强模型。已有环境可运行 `.\.venv\Scripts\python.exe music_sr_backend.py` 安装，附加 `--verify-only` 只做本地校验。命令行在 `--auto-free` 或 `--audio-mode hires` 后添加 `--music-sr`。程序在本机处理音频，在输出前校验声道、样本数与最终封装。

**有底噪的对白**可在“声音处理”里另选“人声降噪 · 减轻对白底噪”。工作站会在本机运行 RNNoise 模型，压低人声之间的风扇声等背景噪声。适用于普通有损单声道和立体声录音。它会保留部分原声，处理结果按源采样率保存为 16-bit ALAC。

首次使用 AI 降噪可运行 `setup.bat -CoreOnly -WithAudioAI`。已有环境只需运行 `.\.venv\Scripts\python.exe install_audio_model.py` 安装约 298 KB 的模型；`--verify-only` 可离线校验模型。命令行在 `--auto-free` 或 `--audio-mode hires` 后添加 `--ai-denoise`。程序只在安装时从作者的固定版本下载模型并核对 SHA-256，之后可离线处理。参见 [RNNoise 模型来源](https://github.com/GregorR/rnnoise-models/tree/3eee541a283fd3b8f81b85b1748e3b9ccbefa04d/somnolent-hogwash-2018-09-01) 与 [FFmpeg arnndn 滤镜](https://ffmpeg.org/ffmpeg-filters.html#arnndn)。

[B站官方 Hi-Res 投稿须知](https://www.bilibili.com/blackboard/activity-m1vS5T9l2t.html)列出的音频门槛是支持的无损编码、至少 48 kHz、至少 24 bit；其中包含 ALAC、FLAC、PCM。说明将 48 至不足 96 kHz 和 96 kHz 及以上分作两个平台输出档位。Hi-Res 描述声音的采样和保存精度，区别于描述声场的 Dolby Atmos。

工作站不会把 16 bit、44.1 kHz 或有损音源升规格来取得标识。常见整数 PCM / FLAC 母版按源采样率输出为 ALAC；超过 ALAC 表达能力的精度或布局视素材保存为 PCM。导出后将最终音轨与按视频时间轴对齐的源音轨逐样本核对。它能验证文件和采样，无法证明来历未知的 WAV 在更早阶段是否曾被有损压缩。优先用自己的录音、工程直接导出的母版。

投稿时上传最终 MP4；若创作中心提供“Hi-Res 无损音质”，请勾选。等转码完成，在支持的播放器上分别查看“杜比视界”和“Hi-Res 无损”。没有 Atmos 的片子不用于测试“杜比全景声”。历史活动报名页不是账号或平台当下开放情况的证明。

### B站杜比与双标

- B站创作中心公开指引列出 **HLG、BT.2020、10-bit HEVC、Dolby Vision Profile 8.4、MP4 / MOV / M4V**。该页编辑于 **2021-11-22**，列出的尺寸 / 帧率为 3840×2160 / 60fps，建议码率约 30Mbps。应用将这些作为保守交付参考与提示，**不把旧文档当作当前平台的完整限制**；竖屏按相应长短边参考检查。[B站官方指引](https://www.bilibili.com/opus/560508706903943321)
- Dolby 的 **2024-03-26** 视频分享服务 FAQ 也说明应依据目标服务选择杜比配置，B站对应 Profile 8.4。PQ / Profile 8.1 不能通过改标签等同于 HLG / Profile 8.4。[Dolby 官方 FAQ](https://professionalsupport.dolby.com/s/article/Dolby-Vision-for-video-sharing-services-FAQs?language=en_US)
- 本地检查核实 Profile 8 / 基础层兼容 ID 4、HLG、BT.2020、4:2:0 10-bit、RPU / 基础层与单层声明，不能证明 RPU 与每个镜头的视觉匹配。
- “杜比视界 + 全景声双标”需要真实 Atmos 音轨。只有 FFprobe / 可选 MediaInfo 给出明确 JOC / Atmos 编码证据时，程序才确认 Atmos；普通 E-AC-3、多声道或音轨标题里的 “Atmos” 不作为证明。
- 优先复制已有 **E-AC-3 JOC / Atmos**。普通素材的自动 Atmos 路线使用 48kHz / 24-bit 空间 PCM 母版，经真实编码器生成 JOC；FFmpeg 自带普通 E-AC-3 编码器不能替代此步骤。TrueHD Atmos 会解码为受支持的声道素材重新制作，原对象信息不会直接保留。
- B站当前公开帮助列出 E-AC-3 Atmos、6（5.1）或 8（7.1）声道，推荐 48kHz，码率写作“320以上kbps”。本程序新生成音轨严格检查 48kHz、至少 320kbps、真实 JOC 证据和时长；AWS 请求使用 768kbps。[B站官方 Atmos 帮助](https://www.bilibili.com/blackboard/help.html#/?qid=300&pid=297)
- **本地格式通过不等于 B站已经认可。** 最终要等待创作中心转码，并在支持的回放环境中确认杜比视界 / 全景声选项及实际效果。报告默认始终记录 `platform_verified: false`；程序不会自动发布投稿。

## 安装与启动

自动安装脚本支持 Windows 10 / 11、**64 位 Python 3.10+**。原片保留 / 转码不需要 AI GPU。RIFE 和 Real-ESRGAN 使用 Vulkan，需要支持 Vulkan 的 GPU 与正常驱动，不要求 CUDA / PyTorch。

```powershell
git clone https://github.com/a13718434469/8K-Dolby-Workstation.git
cd 8K-Dolby-Workstation

# 基础环境、FFmpeg、dovi_tool 与 Python 依赖
.\setup.bat -CoreOnly
.\run.bat

# 另装完整 AI 环境；-WithDovi 保留为兼容旧命令的选项
.\setup.bat -WithDovi

# 仅检查安装；或在已手动放好工具时仅装依赖并检查
.\setup.bat -VerifyOnly
.\setup.bat -SkipDownloads
```

`setup.bat` 不加参数会安装完整 AI 工具和模型。安装器使用项目内 `.venv`，从原项目官方发行包下载，校验 FFmpeg 官方 SHA256，跳过已完整安装的工具。工具包下载最多等待 20 分钟，校验清单最多等待 2 分钟，失败后可重试或手动安装。首次运行 `run.bat` 缺少可用环境时，会安装基础环境。

Python 依赖包含 `boto3`。安装工作站不会获得 Dolby 商业编码器许可证，也不会创建 AWS 账户、存储桶或服务角色。自动生成 Atmos 需要按 [配置说明](ATMOS_SETUP.md) 选择可用后端；仅保留已有 JOC 不需要新编码器。

桌面入口也可直接运行：

```powershell
.\.venv\Scripts\python.exe main.py
```

### 桌面使用

1. 把视频拖到窗口，或点击“添加文件”。整段视频直接使用原音；也可同时拖入一个视频与一个音频，或者先后添加。
2. 检查界面显示的导出方案与音轨。选免费方案即可本地制作。若您明确要把普通音轨编码为新的 Atmos，请切换到双杜比，再配置已授权 DEE 或 AWS。输出文件不会覆盖原文件。
3. 点击“开始处理”。如需替换音轨，将音频拖到音轨区域；该区域也可从另一视频容器读取音轨。“使用原音”可撤销替换。双杜比方案没有音轨时会要求添加音频；编码器未就绪时不会开始昂贵的视频处理。
4. 需要修改输出尺寸、色彩、AI 或杜比制作参数时再展开“调整设置”。8.4 默认自动生成逐帧元数据，无需手动寻找 RPU；8.1 仍需提供外部配套 RPU。若素材的实际帧仍缺少动态范围信息且带有 BT.2020 信号，先选择源动态范围（SDR / HLG / PQ）再开始。
5. 完成后打开输出目录，按需查看本地质检结果。

桌面默认正常最大化，保留系统标题栏和任务栏；使用标准窗口按钮可还原大小。界面使用较大字体与按钮，并适配高 DPI。高级参数、质检详情和日志默认收起，滚动不会意外改变下拉选项。

智能默认会保留已经合规的 8.4 画面；其他普通 SDR / HLG / PQ 素材使用 CPU 高精度自动制作，输出沿用源尺寸与帧率并保守限制在 4K / 60fps，不自动开启 AI。双杜比目标下，WAV、FLAC、AAC 等普通音轨进入 Atmos 制作；仅杜比视界或常规导出方案可使用 AAC。把音频改为普通编码或静音，不会取消双杜比的严格要求；需同时明确改选其他交付方案。一次只处理一个视频，多视频或无效文件会明确提示且保留当前输入。导入检查保持响应，开始处理后锁定素材，避免混用。

应用图标由 `assets/app.svg` 和 `app_identity.py` 提供，多尺寸本地渲染，无网络素材依赖。程序从实际 PyQt5 包位置设置插件目录，支持含中文的安装路径。

## 画质与色彩流程

**原片保留**直接复制视频码流，检查编码、尺寸、精度、色彩和杜比配置，并比较编码视频负载的 SHA256。它保留原有 RPU 及时序，避免重新调色或插帧后沿用不匹配的元数据。

**原生高精度转码**不先经过 8-bit PNG。以源精度解码，按 SDR / PQ / HLG 信息处理，必要时使用浮点工作格式，最终量化为 4:2:0 10-bit HEVC。HDR → SDR 使用明确色调映射；SDR → HDR 不会恢复原素材不存在的高光或色彩细节。

**AI 增强**按目标帧率计算 RIFE 输出，使用 Real-ESRGAN x4 模型并分批超分 / 编码。它仍使用 8-bit SDR RGB 工作帧，不能代替原生 HDR 母版流程；高于工作尺寸的源素材会缩小。优先保画质时应选择保留原片或原生转码。

**杜比重新制作**中，8.1 使用 PQ 基础层，8.4 使用 HLG 基础层。外部 RPU 必须对应最终画面的色彩、裁切、尺寸、镜头及时序，帧数须完全匹配。改变画面或帧率后不应直接沿用源 RPU；静态模板元数据不等于专业动态调色分析。

### 自动 HDR 重制与动态元数据

自动 8.4 流程只编码一次 HLG 基础层，再分析**最终编码后的每一帧**，因此元数据对应最终尺寸、帧率与画面。SDR 先转为浮点线性 BT.2020，采用整片固定、连续且保持 RGB 比例的高光扩展曲线（默认峰值目标 600nits，可设 100–1000）；HDR 源仅进行需要的色彩转换，不重复 SDR 扩展。该处理不会恢复源片已经剪掉的高光细节。Profile 5 的专用 IPT 色彩转换不在此 CPU 路线内，应先输出正确 HLG / PQ 母版；原码流保留不受影响。

分析器使用 NumPy 在 CPU 上流式处理原生 4:2:0 像素，从已安装 dovi_tool 取得实际 Profile 8.4 composer，按同一映射重建 PQ RGB，再测量逐帧 min / max / avg 并生成 L1。不会把通用 HLG 亮度统计直接套入另一套 RPU 映射。原始测量值与 CM4 合法范围夹限后的实写值同时保存；封装后再次提取全部 RPU，逐帧核对 L1 与数量。

这是**依据公开规范实现的独立分析**，不宣称等同 Dolby 原厂专有算法、长时窗分析、人工调色或平台认证。空间降采样、色度插值与暗场夹限的具体选择见 [分析方法与开源声明](DOLBY_ANALYSIS_NOTICE.md)。Atmos 音频走独立制作与编码链，见下节。

每帧解码与分析需要额外 CPU 时间；像素按帧和小块处理，不把整片展开成 RGB 文件。dovi_tool 会载入完整元数据列表，其内存用量随片长增长，长片仍须留出足够内存。最终核验覆盖全部帧 L1、帧数及首帧 composer，报告分别注明范围。默认清理媒体中间件，但保留任务目录 `auto-dolby/` 中的逐帧统计、RPU 和方法报告，便于复核。缓存绑定基础层与 RPU 哈希；损坏或改变时拒绝沿用。

x265 默认 CRF 为 18，数值更低通常产生更大文件。较慢预设可能以更多时间提高压缩效率。空间和耗时估算只作参考，不保证画质、精确文件大小或完成时间。

### 自动 Atmos 制作

普通音轨先生成 48kHz / 24-bit PCM：本地 DEE 为 5.1.4，AWS 为固定顺序的 9.1.6。立体声保留前方声像，以较轻的差分环境声补充空间；标准 5.1 / 7.1 保留主要声道关系，单声道保持中置，不凭空制造移动对象。此过程是**空间混音创作**，不会恢复原素材不存在的声音对象或真实高度信息。未知多声道顺序会被拒绝，避免错置声道。

随后由已配置的真实后端编码，工作站检查 JOC 证据、5.1 / 7.1 交付声道、48kHz、码率、时长及完整解码，再与画面无损封装。最终音频负载 SHA256 必须与验证后的音轨一致。任务缓存绑定源音频内容、混音、编码器及配置哈希；损坏或改变时拒绝沿用。后端退出成功、扩展名正确或“Atmos”标题均不足以通过。

时间对齐根据素材内音视频起点处理；外部替换音频按与视频零点配对。原片带有复杂剪辑列表、变速或人工偏移的素材，仍需用短片检查口型和声画同步。详见 [Atmos 制作范围与验收](ATMOS_SETUP.md)。

## 命令行

命令行不启动 GUI，与桌面共用核心。默认保留原视频与音轨，沿用源尺寸、帧率和色彩，AI / 插帧默认关闭。手机旋转素材在原生转码时按显示方向确定默认宽高，原片保留则保留编码尺寸与旋转信息：

```powershell
# 免费本地：使用视频自带音轨；有独立母带可追加 --audio "master.wav"
.\.venv\Scripts\python.exe cli.py -i "source.mp4" -o "free.mp4" --auto-free

# 双杜比：使用本机已授权 DEE 和匹配版本的命令配置
.\.venv\Scripts\python.exe cli.py -i "source.mp4" -o "dual.mp4" --auto-double-dolby --dee-path "C:\Dolby\dee_ddpjoc_encoder.exe" --dee-profile "C:\Dolby\workstation.dee-profile.json"

# 双杜比：已有真实 Atmos 音轨时直接保留，无需配置新编码器
.\.venv\Scripts\python.exe cli.py -i "source.mp4" -o "dual_from_existing.mp4" --auto-double-dolby --audio "matched_atmos.eac3"

# 仅杜比视界自动制作；不要求 Atmos
.\.venv\Scripts\python.exe cli.py -i "source.mp4" -o "automatic_dv84.mp4" --auto-dolby

# 同时配入已有真实Atmos音轨，并要求最终保留
.\.venv\Scripts\python.exe cli.py -i "source.mp4" -o "automatic_dual.mp4" --auto-dolby --audio "matched_atmos.eac3" --require-atmos

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
| `--auto-dolby` | 自动选择合规 8.4 原片保留或 4K60 范围内的 8.4 制作 |
| `--auto-free` | 同样的画面目标，音频在本地优先保留已有 JOC、无损母版或普通原音 |
| `--auto-double-dolby` | 自动 8.4 画面 + `atmos_auto` 音频，并强制要求最终真正 Atmos；不能与 `--auto-dolby` 同用 |
| `--rpu-mode auto\|external` | 8.4 自动逐帧分析 / 手动配套 RPU；指定 `--rpu` 默认使用外部模式 |
| `--hdr-remaster auto\|off` / `--hdr-peak-nits` | SDR 高光扩展开关与 100–1000nits 峰值目标，HDR 源不重复扩展 |
| `--source-transfer auto\|bt709\|arib-std-b67\|smpte2084` | 自动识别或明确源素材的 SDR / HLG / PQ 解释 |
| `--audio` / `--audio-mode hires\|atmos_auto\|copy\|aac\|eac3\|none` | 外部音频 / 音轨方式；`hires` 选择免费本地音频路线，`atmos_auto` 保留已有 JOC 或调用真实后端生成 |
| `--require-atmos` | 要求最终存在真正 Atmos；单独使用不会自动选择编码后端 |
| `--atmos-backend dee\|aws` / `--dee-path` / `--dee-profile` | 选择音频后端 / 本地已授权编码程序 / 匹配安装版本的命令配置 |
| `--aws-region` / `--aws-bucket` / `--aws-role-arn` / `--aws-profile` / `--aws-prefix` | 云端区域、私有存储桶、服务角色、本机凭证配置名与临时目录；[详细设置](ATMOS_SETUP.md) |
| `--allow-cloud-upload` | 本次允许上传 PCM 母版并产生 AWS 费用；恢复云端任务也需要明确传入 |
| `--preset` / `--crf` | x265 速度与质量 |
| `--batch-size` | 增强路线批次帧数，范围 1–256 |
| `--keep-temp` | 保留中间文件 |
| `--workspace` / `--resume` | 工作目录 / 恢复原任务 |

原生转码改变帧率时执行恒定帧率重采样，不自动生成 AI 运动帧；RIFE 插帧需要增强路线。重新编码的输出宽高须为偶数，宽至少 64、高至少 48，均不超过 8192；缩放保持比例并按需补边。更小画面可原码流保留，避免部分 x265 版本的极小尺寸崩溃。所有路线都拒绝覆盖现有输出或源文件；原片保留不使用 AI、插帧或色彩重编码设置。

## 音轨、元数据与隐私

外部音频优先于内嵌音轨。免费模式在选定文件内按音质选择一条音轨；其他模式使用第一条音轨。纯静音视频可选“仅杜比视界”。直接复制的短音轨按原长保留，不截短视频。无损重编码、自动 Atmos 母版及普通 AAC / E-AC-3 重编码会补齐短音轨并按视频时长截取；普通 E-AC-3 最多保留 5.1 声道，更多声道会下混。

B站交付封装时移除容器附加元数据和章节。本地报告可能含文件名、路径和编码信息，应作为本地工作文件管理。字幕、附件、章节和额外音轨不在当前输出范围内。

仓库与源码包只应包含代码、测试、文档和必要配置。工具 / 模型、商业许可证、媒体、截图、断点、临时帧、虚拟环境、日志、Cookie、AWS 配置和密钥不应提交。默认本地处理；**只有明确启用 AWS 后才上传 PCM 音频母版**，画面仍在本地制作。云端流程使用指定私有 S3 临时目录并尝试清理本任务对象，失败会报告；程序不会自动发布 B站投稿。AWS 上传与计费同意不保存在界面偏好中，凭证由系统 SDK 凭证链提供。

## 成片质检与平台验收

每次成功输出可生成同目录的 `成片.mp4.quality.json`，记录媒体参数、检测工具、目标路线、失败 / 提示项目及依据。`platform_verified` 始终为 `false`，平台实测证据应单独保存。

本地检查包含：

- 编码、尺寸、帧率、帧数、时长、精度和色彩信号。
- 完整视频和音频解码，避免只凭“封装成功”判断成片有效。整片计帧不使用短时元数据检测超时，仍可随时停止。
- 保留原片的视频负载 SHA256、杜比配置与色彩一致性。
- B站路线的 HLG / Profile 8.4、基础层 / RPU / 单层声明和 4:2:0 10-bit。
- Atmos 编码证据、声道、采样率、码率及生成音轨时长；可选 MediaInfo 补充 JOC 检测，未安装不会自动下载。
- Atmos 生成母版与编码缓存哈希、完整解码、最终封装音频负载的一致性。云端下载成功只证明传输完成，仍须通过这些本地音频检查。
- 自动制作的基础层哈希、实际测量/实写 L1、RPU 哈希、封装后的逐帧元数据一致性，以及实际执行的 HDR 重制方式。

技术检查不能自动评判所有 AI 伪影、镜头调色或 RPU 视觉匹配，也不替代真实播放器与平台转码。先用短片检查暗部、高光、色阶、运动边缘、音画同步及支持设备上的 HDR / Atmos 回放，再处理完整作品。

平台验收由用户自行进行：在 **WEB 创作中心**上传符合条件的短测试片，在 **高级设置 → 杜比音效**勾选音效选项。官方帮助说明全景声在稿件**开放浏览后**出现，因此仅草稿未出现标识不能判定失败。等待目标清晰度和音频转码完成，再在支持设备与对应账号回放权限下核对两个选项和实际效果；不要用未经确认可公开使用的第三方测试音轨发布。[B站官方上传说明](https://www.bilibili.com/blackboard/help.html#/?qid=300&pid=297)

## 暂停、停止与恢复

暂停在当前外部命令完成后的安全边界生效，当前编码 / 推理可能仍需一段时间。停止会终止正在执行的处理 / 质检子进程，保留已验证完成的阶段和分片。

任务有独立目录和原子清单，同一工作目录只允许一个写入者。恢复使用原参数与源文件指纹；输入或参数改变时需要新任务。未完成阶段 / 批次会重做，已验证分片继续复用。

云端任务另有本地 `downloaded.mp4.cloud-job.json` 记录。任务状态不确定、记录损坏或已下载结果不一致时，程序禁止自动再提交收费任务，须先去 AWS 确认原任务。完整、匹配哈希的本地下载可复用。恢复云任务需本次启用云上传与计费，CLI 使用 `--resume --workspace "workspace" --allow-cloud-upload`；不要删除记录来绕过重复计费保护。

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

- [FFmpeg](https://ffmpeg.org/download.html) / [BtbN Windows builds](https://github.com/BtbN/FFmpeg-Builds/releases)：使用完整新版，需 ffmpeg / ffprobe、libx265、AAC、E-AC-3、`zscale` 和 `lut3d`。重新注入 DV 还需支持 `compression=0` 的 `dovi_rpu` 与 `setts`。
- [Real-ESRGAN Windows v0.2.5.0](https://github.com/xinntao/Real-ESRGAN/releases/tag/v0.2.5.0)：程序与 `models/`。
- [RIFE ncnn Vulkan 20221029](https://github.com/nihui/rife-ncnn-vulkan/releases/tag/20221029)：程序与 `rife-v4.6/`。
- [dovi_tool 2.3.4](https://github.com/quietvoid/dovi_tool/releases/tag/2.3.4)：基础安装包含，自动生成/注入 RPU 时需要，保留原片不必重生成 RPU。
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
├── dovi_tool.exe             # 自动杜比制作必需
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

测试覆盖真实 FFmpeg 文件、分数帧率、HDR / DV 元数据与帧数、音轨负载复制、原生精度、恢复 / 取消、原子输出、B站质检、Atmos 证据和 GUI 启动。2.4 新增真实 PCM 母版检查、普通 E-AC-3 拒绝、缓存篡改、最终音频被替换的负例，以及 AWS 请求模型、取消、清理和异常恢复的离线测试。原生 DV 测试需要本机 dovi_tool 及相应 FFmpeg 功能，工具缺失会明确跳过；预制 JOC 测试通过 `DOLBY_ATMOS_FIXTURE` 指定本地夹具。

**测试范围不包含本轮真实 DEE / AWS 新 JOC 编码或 B站识别。**预制 JOC 适配测试不会证明普通音轨已被编码成 Atmos，模拟 AWS 测试也不会证明服务端已接受请求。AI 替代推理测试不代替真实 GPU 与长片画质验收。部署者须完成 [真实编码及平台验收](ATMOS_SETUP.md) 后再据实声明支持情况。

GitHub Actions 在 Windows / Ubuntu、Python 3.10 / 3.12 上运行回归。入口是 `main.py`（桌面）、`cli.py`（命令行），核心是 `pipeline.py`（处理）、`quality.py`（交付检查）、`atmos_encoding.py`（PCM / DEE）与 `atmos_cloud.py`（云编码），安装逻辑在 `setup.ps1`。

## 许可证

项目代码采用 [MIT License](LICENSE)。FFmpeg、RIFE、Real-ESRGAN、dovi_tool、MediaInfo、boto3 和模型遵循各自许可证。DEE 由用户按 Dolby 授权条件取得，AWS 由用户按服务条款使用与付费；项目开源许可不授予第三方商业软件许可。本仓库不捆绑第三方程序、模型或许可证。
