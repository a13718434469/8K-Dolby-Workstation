# 构建 Windows 便携版

开发机使用 64 位 Windows 和 CPython 3.12。使用者只需解压整个 ZIP，双击里面的中文启动程序。

1. 用构建虚拟环境安装 `python -m pip install --only-binary=:all: -r packaging/windows-requirements.txt`。
2. 预备静态 Windows x64 FFmpeg / FFprobe 发行目录。此构建使用 [Gyan git full 2026-04-19 de18feb0f0](https://www.gyan.dev/ffmpeg/builds/)。请保留该构建的 `bin`、`README.txt` 和 `LICENSE`，选择支持 `minterpolate`、`zscale`、`libx265` 和 `dovi_rpu` 的版本。
3. 将 [dovi_tool 2.3.4](https://github.com/quietvoid/dovi_tool/releases/tag/2.3.4) Windows x64 可执行文件放入项目 `tools`。使用项目的 `flashsr_official.py` 安装/校验官方 ONNX 模型，在 `tools/flashsr/model.onnx` 保留未改动文件。
4. 从 [Real-ESRGAN v0.2.5.0 官方发行包](https://github.com/xinntao/Real-ESRGAN/releases/tag/v0.2.5.0) 准备 Windows ncnn Vulkan 运行程序、运行库与通用 x4plus 权重，放入项目 `tools` 下相应位置。构建器会核对发行文件的 SHA-256 并附上许可。
5. 执行以下命令。输出目录和 ZIP 选新路径，独立于源码和缓存。脚本使用 Windows 自带的 .NET Framework C# 编译器生成启动文件，并随包写入版本和第三方来源清单。

```powershell
.\.venv\Scripts\python.exe packaging\build_portable.py --output "..\dist\B站投稿助手" --cache "..\build-cache" --ffmpeg-bin "C:\ffmpeg\bin" --zip "..\dist\B站投稿助手-Windows-x64.zip"
```

发行包内 `app` 存放可读的应用源码与工具，`runtime` 是隔离的 Windows Python 运行环境，`licenses` 列出依赖来源及许可证。应用采用独立子进程执行视频工具。重新构建后，在新目录解压 ZIP 并启动 `B站投稿助手.exe --smoke`，再用带音轨的短视频从图形界面实际导出。
