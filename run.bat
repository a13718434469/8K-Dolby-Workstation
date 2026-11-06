@echo off
setlocal
chcp 65001 >nul 2>&1
title 8K Dolby Workstation
pushd "%~dp0"
if errorlevel 1 exit /b 1
if not exist "%~dp0.venv\Scripts\python.exe" goto bootstrap
"%~dp0.venv\Scripts\python.exe" -c "from PyQt5 import QtWidgets; import numpy; from pipeline import find_tool,TOOLS_DIR; [find_tool(t) for t in ('ffmpeg','ffprobe','dovi_tool','realesrgan-ncnn-vulkan')]; assert (TOOLS_DIR/'models'/'realesrgan-x4plus.bin').is_file()" >nul 2>&1
if errorlevel 1 goto bootstrap
goto launch

:bootstrap
echo Preparing the workspace, video tools and official image enhancement model.
call "%~dp0setup.bat" -CoreOnly
if errorlevel 1 goto failed

:launch
"%~dp0.venv\Scripts\python.exe" "%~dp0consumer_app.py" %*
set "RESULT=%ERRORLEVEL%"
popd
if not "%RESULT%"=="0" (
    echo Application exited with an error. Check the message above.
    pause
)
exit /b %RESULT%

:failed
popd
pause
exit /b 1
