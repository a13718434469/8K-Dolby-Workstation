@echo off
chcp 65001 >nul 2>&1
title 8K杜比工作站 - 环境安装

echo ════════════════════════════════════════════
echo   8K杜比双认证视频自动化处理工作站
echo           一键环境安装
echo ════════════════════════════════════════════
echo.

:: ─── 检测 Python ──────────────────────────
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [失败] 未检测到 Python!
    echo        请先安装 Python 3.8+ https://www.python.org/downloads/
    pause
    exit /b 1
)
for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PY_VER=%%i
echo [通过] Python %PY_VER%

:: ─── 安装 PyQt5 ──────────────────────────
echo.
echo [1/4] 安装 Python 依赖...
pip install PyQt5 -q
if %errorlevel% equ 0 (
    echo [通过] PyQt5 安装成功
) else (
    echo [失败] PyQt5 安装失败，请检查网络
    pause
    exit /b 1
)

:: ─── 创建 tools 目录 ──────────────────────
if not exist "tools" mkdir tools
echo [2/4] 准备 tools 目录...

:: ─── 下载工具 ────────────────────────────
echo [3/4] 下载第三方工具...

set TOOLS_URL=https://github.com/a13718434469/8K-Dolby-Workstation/releases/download/v1.0/tools_pack.zip
set TOOLS_ZIP=tools_pack_temp.zip

if exist "tools\ffmpeg.exe" if exist "tools\realesrgan-ncnn-vulkan.exe" if exist "tools\rife-ncnn-vulkan.exe" if exist "tools\dovi_tool.exe" (
    echo [跳过] 所有工具已存在
) else (
    echo  正在下载工具包（约 300MB）...
    echo  请耐心等待...
    
    powershell -Command "
        \$ProgressPreference = 'SilentlyContinue'
        try {
            Invoke-WebRequest -Uri '%TOOLS_URL%' -OutFile '%TOOLS_ZIP%' -UseBasicParsing
            Write-Host '  下载完成，正在解压...'
            Expand-Archive -Path '%TOOLS_ZIP%' -DestinationPath 'tools\' -Force
            Remove-Item -Force '%TOOLS_ZIP%'
            Write-Host '  解压完成'
        } catch {
            Write-Host '  下载失败: ' \$_.Exception.Message
            exit 1
        }
    "
    
    if %errorlevel% neq 0 (
        echo [警告] 自动下载失败，尝试备用镜像...
        echo  正在从镜像站下载...
        
        powershell -Command "
            \$ProgressPreference = 'SilentlyContinue'
            try {
                Invoke-WebRequest -Uri 'https://ghproxy.net/https://github.com/a13718434469/8K-Dolby-Workstation/releases/download/v1.0/tools_pack.zip' -OutFile '%TOOLS_ZIP%' -UseBasicParsing
                Expand-Archive -Path '%TOOLS_ZIP%' -DestinationPath 'tools\' -Force
                Remove-Item -Force '%TOOLS_ZIP%'
                Write-Host '  解压完成'
            } catch {
                Write-Host '  镜像下载也失败了'
                exit 1
            }
        "
        
        if %errorlevel% neq 0 (
            echo [失败] 自动下载失败，请手动下载工具放入 tools 目录
            echo        下载地址见 README.md
            pause
            exit /b 1
        )
    )
)

:: ─── 验证 ────────────────────────────────
echo [4/4] 验证工具完整性...
echo.

set ALL_OK=1
if not exist "tools\ffmpeg.exe"                  ( echo [缺失] ffmpeg.exe        & set ALL_OK=0 )
if not exist "tools\ffprobe.exe"                 ( echo [缺失] ffprobe.exe       & set ALL_OK=0 )
if not exist "tools\realesrgan-ncnn-vulkan.exe"  ( echo [缺失] Real-ESRGAN       & set ALL_OK=0 )
if not exist "tools\rife-ncnn-vulkan.exe"        ( echo [缺失] RIFE 引擎        & set ALL_OK=0 )
if not exist "tools\rife-v4.6\flownet.bin"       ( echo [缺失] RIFE 模型        & set ALL_OK=0 )
if not exist "tools\dovi_tool.exe"               ( echo [缺失] dovi_tool        & set ALL_OK=0 )

if %ALL_OK% equ 1 (
    echo ════════════════════════════════════════════
    echo  环境配置完成！所有工具已就绪 ✅
    echo ════════════════════════════════════════════
    echo.
    echo  启动方式: 双击 run.bat
    echo.
) else (
    echo ════════════════════════════════════════════
    echo  部分工具缺失，请手动补充后重试
    echo ════════════════════════════════════════════
    pause
    exit /b 1
)

pause
