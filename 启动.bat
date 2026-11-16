@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem ------------------------------------------------------------
rem  myWheel 启动器
rem  在这个窗口里挑一个"带 tkinter"的 Python，用它打开 launcher.pyw。
rem  为什么要挑：电脑上可能装了好几套 Python，精简版不带 tkinter，
rem  直接双击 .pyw 撞上它就毫无反应。
rem ------------------------------------------------------------

set "PYW="

for /f "delims=" %%P in ('where pythonw 2^>nul') do (
    if not defined PYW (
        "%%P" -c "import tkinter" >nul 2>&1
        if !errorlevel! equ 0 set "PYW=%%P"
    )
)

if defined PYW (
    start "" "!PYW!" "launcher.pyw"
    exit /b
)

echo.
echo   没有找到带 tkinter 的 Python，启动器打不开窗口。
echo.
echo   可以改用终端直接跑服务：
echo       python server.py
echo.
echo   或者装一个带 tkinter 的 Python（比如 Anaconda / python.org 的安装包）。
echo.
pause
