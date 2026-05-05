@echo off
chcp 65001 >nul 2>&1
title 8K杜比双认证视频自动化处理工作站

:: 检测 tools 完整性
set MISSING=0
if not exist "tools\ffmpeg.exe" set MISSING=1
if not exist "tools\rife-ncnn-vulkan.exe" set MISSING=1
if not exist "tools\realesrgan-ncnn-vulkan.exe" set MISSING=1
if not exist "tools\dovi_tool.exe" set MISSING=1

if %MISSING% equ 1 (
    echo [提示] 检测到缺少工具，运行 setup.bat 自动下载...
    call setup.bat
    if %errorlevel% neq 0 (
        pause
        exit /b 1
    )
)

:: 检测 Python
python -c "import PyQt5" >nul 2>&1
if %errorlevel% neq 0 (
    echo [安装] 正在安装 PyQt5...
    pip install PyQt5
)

python "%~dp0main.py"

if %errorlevel% neq 0 (
    echo.
    echo 程序异常退出，请检查：
    echo   - Python 3.8+ 是否已安装
    echo   - tools 目录是否包含所有工具
    pause
)
