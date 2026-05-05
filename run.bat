@echo off
chcp 65001 >nul 2>&1
title 8K杜比双认证视频自动化处理工作站

:: 优先使用 Anaconda Python
if exist "D:\Anaconda3\python.exe" (
    D:\Anaconda3\python.exe "%~dp0main.py"
) else (
    python "%~dp0main.py"
)

if %errorlevel% neq 0 (
    echo.
    echo 程序异常退出，请检查 Python 和 PyQt5 是否已安装
    pause
)
