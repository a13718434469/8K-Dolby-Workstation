# ================================================================
# 8K杜比双认证视频自动化处理工作站 - 环境安装脚本
# ================================================================

$ErrorActionPreference = "Continue"
$BaseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $BaseDir

Write-Host "================================================" -ForegroundColor Cyan
Write-Host " 8K杜比双认证视频自动化处理工作站  安装脚本" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# 1. 安装 Python 依赖
Write-Host "[1/2] 安装 Python 依赖 (PyQt5)..." -ForegroundColor Yellow
pip install -r requirements.txt
if ($LASTEXITCODE -eq 0) {
    Write-Host "  -> Python 依赖安装成功" -ForegroundColor Green
} else {
    Write-Host "  -> Python 依赖安装失败，请检查 pip" -ForegroundColor Red
}

# 2. 创建 tools 目录
Write-Host ""
Write-Host "[2/2] 检查 tools 目录..." -ForegroundColor Yellow
$ToolsDir = Join-Path $BaseDir "tools"
if (-not (Test-Path $ToolsDir)) {
    New-Item -ItemType Directory -Path $ToolsDir | Out-Null
    Write-Host "  -> 已创建 tools 目录" -ForegroundColor Green
} else {
    Write-Host "  -> tools 目录已存在" -ForegroundColor Green
}

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host " 请手动下载以下工具放入 tools 目录:" -ForegroundColor Yellow
Write-Host ""
Write-Host "  1. FFmpeg (ffmpeg.exe + ffprobe.exe)" -ForegroundColor White
Write-Host "     https://github.com/BtbN/FFmpeg-Builds/releases" -ForegroundColor Gray
Write-Host ""
Write-Host "  2. Real-ESRGAN (realesrgan-ncnn-vulkan.exe + models)" -ForegroundColor White
Write-Host "     https://github.com/xinntao/Real-ESRGAN/releases" -ForegroundColor Gray
Write-Host ""
Write-Host "  3. RIFE (rife-ncnn-vulkan.exe + rife-v4.6 模型目录)" -ForegroundColor White
Write-Host "     https://github.com/nihui/rife-ncnn-vulkan/releases" -ForegroundColor Gray
Write-Host ""
Write-Host "  4. dovi_tool (dovi_tool.exe)" -ForegroundColor White
Write-Host "     https://github.com/quietvoid/dovi_tool/releases" -ForegroundColor Gray
Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "安装完成! 双击 run.bat 启动程序" -ForegroundColor Green
Write-Host ""
pause
