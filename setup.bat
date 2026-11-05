@echo off
setlocal
chcp 65001 >nul 2>&1
title 8K Dolby Workstation - Setup
pushd "%~dp0"
if errorlevel 1 exit /b 1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
set "RESULT=%ERRORLEVEL%"
popd
if not "%RESULT%"=="0" echo Setup failed. Review the error above and run setup.bat again.
exit /b %RESULT%
