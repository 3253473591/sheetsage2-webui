@echo off
rem ---------------------------------------------------------------------------
rem  SheetSage2 - launcher for the CODE-ONLY patch directory.
rem
rem  This directory carries code but no runtime, no model weights and no ffmpeg.
rem  Those live further up the tree (e.g. the dev checkout next to dist\), so this
rem  launcher WALKS UP from here looking for a Python interpreter. Walking instead
rem  of hardcoding "..\..\..\" keeps it working wherever the patch gets unpacked.
rem
rem  Search order: this directory first, then up to 4 levels up. In each level:
rem     runtime\python\python.exe            <- a full (portable) package
rem     runtime\python\Scripts\python.exe
rem     .venv\Scripts\python.exe             <- a dev checkout
rem
rem  IMPORTANT: keep this file ASCII-only.
rem  cmd.exe parses .bat files using the console OEM code page (936/GBK on a
rem  Chinese Windows), NOT UTF-8. Chinese text stored as UTF-8 gets mangled into
rem  garbage and then executed as commands. All localized output is printed by
rem  Python (UTF-8) instead, so this launcher stays ASCII and encoding-proof.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

rem UTF-8 console so Python's Chinese diagnostics are readable, not mojibake.
rem If this ever misbehaves on your machine, delete this one line.
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"

set "PY="
call :probe "%~dp0"
call :probe "%~dp0..\"
call :probe "%~dp0..\..\"
call :probe "%~dp0..\..\..\"
call :probe "%~dp0..\..\..\..\"

if not defined PY (
  echo.
  echo   [ERROR] No Python interpreter found. Searched this directory and 4
  echo           levels up, at runtime\python\python.exe and .venv\Scripts\python.exe
  echo.
  echo   This patch directory carries code only: run it from inside an unpacked
  echo   package, or put it next to a checkout that has .venv.
  echo.
  pause
  exit /b 1
)

if not exist "%~dp0app\server.py" (
  echo   [ERROR] app\server.py not found. Run this from the project root.
  pause
  exit /b 1
)

rem Cheap preflight: a wrong interpreter otherwise fails later with a raw
rem traceback that looks like a bug in the app instead of a missing dependency.
"%PY%" -c "import fastapi" 2>nul
if not "%ERRORLEVEL%"=="0" (
  echo.
  echo   [ERROR] This interpreter has no dependencies installed:
  echo           %PY%
  echo.
  echo   Install them, then run this file again:
  echo           "%PY%" -m pip install -r "%~dp0requirements-base.txt"
  echo.
  pause
  exit /b 1
)

echo.
echo   SheetSage2 transcription web tool
echo   Code:         %~dp0
echo   Interpreter:  %PY%
echo.
echo   Starting... the browser will open automatically.
echo   Close this window to stop the server.
echo.
echo   NOTE: if another SheetSage2 window is already running on port 8777,
echo         the app will just open THAT one and you will be looking at the
echo         old code. To test this directory on a free port instead:
echo             start.bat --port 8898
echo.

"%PY%" -m app.server %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo   [Server exited] code %RC%
  echo   If the port is busy, change "port" in settings.json
  echo   or close the program using that port.
  echo.
  pause
)

endlocal & exit /b %RC%


rem ---------------------------------------------------------------------------
rem  :probe <dir-with-trailing-backslash>
rem  Sets PY on the first hit. Returns immediately once PY is set, so the
rem  closest interpreter always wins.
rem ---------------------------------------------------------------------------
:probe
if defined PY exit /b 0
if exist "%~1runtime\python\python.exe" (
  set "PY=%~1runtime\python\python.exe"
  exit /b 0
)
if exist "%~1runtime\python\Scripts\python.exe" (
  set "PY=%~1runtime\python\Scripts\python.exe"
  exit /b 0
)
if exist "%~1.venv\Scripts\python.exe" (
  set "PY=%~1.venv\Scripts\python.exe"
  exit /b 0
)
exit /b 0
