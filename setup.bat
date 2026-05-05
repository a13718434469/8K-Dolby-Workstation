@echo off
chcp 65001 >nul 2>&1
title 8K杜比工作站 - 环境安装

echo ================================================
echo  8K杜比双认证视频自动化处理工作站  环境安装
echo ================================================
echo.

:: 检测 Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [失败] 未检测到 Python，请先安装 Python 3.8+
    echo        下载: https://www.python.org/downloads/
    pause
    exit /b 1
)
python --version
echo [通过] Python 检测成功
echo.

:: 安装 PyQt5
echo [1/3] 安装 Python 依赖...
pip install PyQt5
if %errorlevel% neq 0 (
    echo [失败] PyQt5 安装失败
    pause
    exit /b 1
)
echo [通过] 依赖安装成功
echo.

:: 创建 tools 目录
if not exist "tools" mkdir tools
echo [2/3] 检查 tools 目录...
echo.

:: 检测已有工具
set NEED_DOWNLOAD=0
if not exist "tools\ffmpeg.exe" set NEED_DOWNLOAD=1
if not exist "tools\realesrgan-ncnn-vulkan.exe" set NEED_DOWNLOAD=1
if not exist "tools\rife-ncnn-vulkan.exe" set NEED_DOWNLOAD=1
if not exist "tools\rife-v4.6\flownet.bin" set NEED_DOWNLOAD=1
if not exist "tools\dovi_tool.exe" set NEED_DOWNLOAD=1

if %NEED_DOWNLOAD% equ 0 (
    echo [通过] 所有工具已就绪，无需下载
) else (
    echo [下载] 检测到缺少工具，正在下载...
    echo   (首次下载约 300MB，请耐心等待)
    echo.
    
    :: 用 PowerShell 下载
    powershell -Command "
        Write-Host '下载 FFmpeg...' -ForegroundColor Yellow;
        if (-not (Test-Path 'tools\ffmpeg.exe')) {
            Invoke-WebRequest -Uri 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip' -OutFile 'ffmpeg_temp.zip';
            Expand-Archive -Path 'ffmpeg_temp.zip' -DestinationPath 'ffmpeg_temp' -Force;
            Copy-Item 'ffmpeg_temp\ffmpeg-master-latest-win64-gpl\bin\ffmpeg.exe' 'tools\';
            Copy-Item 'ffmpeg_temp\ffmpeg-master-latest-win64-gpl\bin\ffprobe.exe' 'tools\';
            Remove-Item -Recurse -Force 'ffmpeg_temp', 'ffmpeg_temp.zip';
            Write-Host '  FFmpeg OK' -ForegroundColor Green;
        }
        
        Write-Host '下载 Real-ESRGAN...' -ForegroundColor Yellow;
        if (-not (Test-Path 'tools\realesrgan-ncnn-vulkan.exe')) {
            Invoke-WebRequest -Uri 'https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.0/realesrgan-ncnn-vulkan-v0.2.0-windows.zip' -OutFile 'esrgan_temp.zip';
            Expand-Archive -Path 'esrgan_temp.zip' -DestinationPath 'tools\' -Force;
            Remove-Item -Force 'esrgan_temp.zip';
            Write-Host '  Real-ESRGAN OK' -ForegroundColor Green;
        }
        
        Write-Host '下载 RIFE + 模型...' -ForegroundColor Yellow;
        if (-not (Test-Path 'tools\rife-ncnn-vulkan.exe')) {
            Invoke-WebRequest -Uri 'https://github.com/nihui/rife-ncnn-vulkan/releases/download/20221029/rife-ncnn-vulkan-20221029-windows.zip' -OutFile 'rife_temp.zip';
            Expand-Archive -Path 'rife_temp.zip' -DestinationPath 'tools\' -Force;
            Remove-Item -Force 'rife_temp.zip';
            Write-Host '  RIFE OK' -ForegroundColor Green;
        }
        if (-not (Test-Path 'tools\rife-v4.6\flownet.bin')) {
            if (-not (Test-Path 'tools\rife-v4.6')) { New-Item -ItemType Directory -Path 'tools\rife-v4.6' | Out-Null }
            Invoke-WebRequest -Uri 'https://github.com/nihui/rife-ncnn-vulkan/releases/download/20221029/rife-v4.6.zip' -OutFile 'rife_model.zip';
            Expand-Archive -Path 'rife_model.zip' -DestinationPath 'tools\rife-v4.6\' -Force;
            Remove-Item -Force 'rife_model.zip';
            Write-Host '  RIFE模型 OK' -ForegroundColor Green;
        }
        
        Write-Host '下载 dovi_tool...' -ForegroundColor Yellow;
        if (-not (Test-Path 'tools\dovi_tool.exe')) {
            Invoke-WebRequest -Uri 'https://github.com/quietvoid/dovi_tool/releases/download/2.1.2/dovi_tool-2.1.2-x86_64-pc-windows-msvc.zip' -OutFile 'dovi_temp.zip';
            Expand-Archive -Path 'dovi_temp.zip' -DestinationPath 'tools\' -Force;
            Remove-Item -Force 'dovi_temp.zip';
            Write-Host '  dovi_tool OK' -ForegroundColor Green;
        }
        
        Write-Host '' -ForegroundColor Green;
        Write-Host '所有工具下载完成!' -ForegroundColor Green;
    "
)

echo.
echo [3/3] 检查结果:
if exist "tools\ffmpeg.exe"     ( echo   [OK] FFmpeg ) else ( echo   [缺] FFmpeg )
if exist "tools\ffprobe.exe"    ( echo   [OK] ffprobe ) else ( echo   [缺] ffprobe )
if exist "tools\realesrgan-ncnn-vulkan.exe" ( echo   [OK] Real-ESRGAN ) else ( echo   [缺] Real-ESRGAN )
if exist "tools\rife-ncnn-vulkan.exe"       ( echo   [OK] RIFE ) else ( echo   [缺] RIFE )
if exist "tools\rife-v4.6\flownet.bin"      ( echo   [OK] RIFE模型 ) else ( echo   [缺] RIFE模型 )
if exist "tools\dovi_tool.exe"              ( echo   [OK] dovi_tool ) else ( echo   [缺] dovi_tool )

echo.
echo ================================================
echo  安装完成！启动方式:
echo  双击 run.bat 或运行 python main.py
echo ================================================
pause
