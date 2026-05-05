# 8K Dolby Workstation

> **8K杜比双认证视频自动化处理工作站**
> 一键将普通视频转为 8K 120fps 杜比视界 + 杜比全景声，可直接上传B站获得双认证标签

![Python](https://img.shields.io/badge/Python-3.8+-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Platform](https://img.shields.io/badge/Platform-Windows-orange)
![GPU](https://img.shields.io/badge/GPU-NVIDIA-blueviolet)

## ✨ 功能

将普通视频自动处理为 **8K 120fps** 杜比视界（Dolby Vision）+ 杜比全景声（Dolby Atmos）视频，可直接上传 **B站获得双认证标签**。

## 🎯 特色

| 功能 | 说明 |
|------|------|
| 🧠 **AI 插帧** | RIFE 两轮 2x 插帧，1080p → 120fps |
| 🔬 **AI 超分** | Real-ESRGAN 4x 放大到 8K（7680×4320）|
| 🌈 **杜比视界 HDR** | x265 编码 + dovi_tool RPU 注入 |
| 🔊 **杜比全景声** | EAC3 5.1/7.1 声道编码 |
| 💾 **磁盘空间预估** | 选好视频立刻显示所需空间 |
| ⏱ **处理时长预估** | 开始前预估总时长，实时进度显示 |
| 🔄 **暂停/继续** | 随时暂停恢复处理 |
| 🧩 **断点续处理** | 关机重启后从中断处继续 |
| 🧹 **自动清理** | 每阶段完成自动删除临时文件 |

## 🔧 处理流程

```
输入视频 → 拆帧 → AI插帧(RIFE) → AI超分(Real-ESRGAN) → HDR编码 → 音频编码 → MP4封装 → 8K输出
```

## 🚀 快速开始

### 环境要求

- **系统:** Windows 10/11
- **Python:** 3.8+（推荐 Anaconda）
- **GPU:** NVIDIA（建议 8GB+ 显存）

### 一键安装

```bash
# 1. 克隆仓库
git clone https://github.com/a13718434469/8K-Dolby-Workstation.git
cd 8K-Dolby-Workstation

# 2. 双击 setup.bat — 自动下载所有工具 + 安装依赖
```

`setup.bat` 会自动下载：
- ✅ FFmpeg（视频处理核心）
- ✅ Real-ESRGAN（AI 超分引擎 + 模型）
- ✅ RIFE（AI 插帧引擎 + v4.6 模型）
- ✅ dovi_tool（杜比 RPU 注入）
- ✅ PyQt5（Python 依赖）

### 手动安装

如果自动下载速度慢，也可以手动下载工具放入 `tools/` 目录：

```
tools/
├── ffmpeg.exe + ffprobe.exe     # https://ffmpeg.org
├── realesrgan-ncnn-vulkan.exe   # Real-ESRGAN
├── rife-ncnn-vulkan.exe         # RIFE 引擎
├── rife-v4.6/flownet.bin        # RIFE 模型
└── dovi_tool.exe                # dovi_tool
```

### 启动

```bash
# 方式一：双击 run.bat（自动检测环境）
# 方式二：
python main.py
```

## 📁 项目结构

```
8K-Dolby-Workstation/
├── main.py              # 主程序（PyQt5 界面）
├── run.bat              # 启动脚本（自动检测环境）
├── setup.bat            # 一键安装脚本（下载工具+依赖）
├── setup.ps1            # PowerShell 安装脚本
├── requirements.txt     # Python 依赖
├── tools/               # 第三方工具（自动下载）
└── workspace/           # 工作目录（自动创建）
```

## ⚙️ 性能参考

| GPU | 显存 | 1080p→8K 每帧耗时 | 10分钟视频总耗时 |
|-----|------|-------------------|-----------------|
| RTX 4090 | 24GB | ~1.5s | ~6小时 |
| RTX 3080 | 12GB | ~3s | ~12小时 |
| RTX 3060 | 12GB | ~3s | ~12小时 |
| RTX 2060 | 6GB | ~6s | ~24小时 |

> 实际耗时受视频内容复杂度影响，以上为估算值。

## 📝 MIT 开源许可证
