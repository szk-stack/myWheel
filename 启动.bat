@echo off
rem ============================================================
rem  myWheel launcher
rem
rem  Finds the first pythonw.exe on PATH that can import tkinter,
rem  then opens launcher.pyw with it.
rem
rem  Why this is needed: some slim Python builds have no tkinter.
rem  A .pyw opened by one of those fails silently - no window,
rem  no error message, just nothing.
rem ============================================================

cd /d "%~dp0"

set "PYW="

for /f "delims=" %%P in ('where pythonw 2^>nul') do (
    if not defined PYW (
        "%%P" -c "import tkinter" >nul 2>&1
        if not errorlevel 1 set "PYW=%%P"
    )
)

if not defined PYW goto nopython

start "" "%PYW%" "launcher.pyw"
exit /b


:nopython
echo.
echo   No Python with tkinter found on this machine.
echo.
echo   Option 1 - run the server directly in a terminal:
echo       python server.py
echo.
echo   Option 2 - install a Python that includes tkinter
echo       (python.org installer or Anaconda).
echo.
pause
