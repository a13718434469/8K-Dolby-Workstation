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

### 安装

```bash
# 1. 克隆仓库
git clone https://github.com/a13718434469/8K-Dolby-Workstation.git
cd 8K-Dolby-Workstation

# 2. 安装依赖
pip install PyQt5

# 3. 下载工具
```

将以下工具放入 `tools/` 目录：

| 工具 | 说明 | 获取方式 |
|------|------|----------|
| `ffmpeg.exe` + `ffprobe.exe` | 视频处理核心 | [FFmpeg官网](https://ffmpeg.org/download.html) |
| `realesrgan-ncnn-vulkan.exe` | AI超分引擎 | [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) |
| `rife-ncnn-vulkan.exe` | AI插帧引擎 | [RIFE-ncnn-Vulkan](https://github.com/nihui/rife-ncnn-vulkan) |
| `rife-v4.6/` | RIFE模型 | 同上 |
| `dovi_tool.exe` | 杜比RPU注入 | [dovi_tool](https://github.com/quietvoid/dovi_tool) |

### 启动

```bash
python main.py
# 或双击 run.bat
```

## 📁 项目结构

```
8K-Dolby-Workstation/
├── main.py          # 主程序（PyQt5界面）
├── run.bat          # 启动脚本
├── setup.ps1        # 环境配置脚本
├── requirements.txt # Python依赖
├── tools/           # 第三方工具（需手动下载）
│   ├── ffmpeg.exe
│   ├── ffprobe.exe
│   ├── realesrgan-ncnn-vulkan.exe
│   ├── rife-ncnn-vulkan.exe
│   ├── rife-v4.6/
│   └── dovi_tool.exe
└── workspace/       # 工作目录（自动创建）
```

## ⚙️ 性能参考

| GPU | 显存 | 1080p→8K 每帧耗时 | 10分钟视频总耗时 |
|-----|------|-------------------|-----------------|
| RTX 4090 | 24GB | ~1.5s | ~6小时 |
| RTX 3080 | 12GB | ~3s | ~12小时 |
| RTX 3060 | 12GB | ~3s | ~12小时 |
| RTX 2060 | 6GB | ~6s | ~24小时 |

> 实际耗时受视频内容复杂度影响，以上为估算值。

## 📝 许可证

MIT开源许可证
