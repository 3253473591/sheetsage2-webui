@echo off
rem ===========================================================================
rem  SheetSage2 transcribe - first-time setup (LITE package only)
rem
rem  IMPORTANT: this file must stay ASCII-only (see start.bat for why).
rem  Everything user-facing in Chinese is printed by Python.
rem
rem  What this does:
rem    1. download a portable CPython 3.12.11 into runtime\python  (via uv, no install)
rem    2. drop uv's "externally managed" marker so pip may use it
rem    3. hand over to tools\install_deps.py for pip deps + model weights
rem ===========================================================================
setlocal enableextensions
cd /d "%~dp0"
title SheetSage2 transcribe - first-time setup

set "UV=%~dp0runtime\uv.exe"
if not exist "%UV%" (
  echo.
  echo   [ERROR] runtime\uv.exe is missing.
  echo           Re-extract the archive; do not move single files around.
  echo.
  pause
  exit /b 1
)

if exist "%~dp0runtime\python\python.exe" (
  echo   [setup] Portable runtime already present - skipping the download.
  goto :deps
)

echo.
echo   [setup] Step 1/2: downloading Python 3.12.11 (about 30 MB)...
echo.

"%UV%" python install 3.12.11 --install-dir "%~dp0runtime\_py"
if errorlevel 1 goto :fail_python

if not exist "%~dp0runtime\python\python.exe" (
  for /d %%D in ("%~dp0runtime\_py\cpython-*") do move "%%D" "%~dp0runtime\python" >nul 2>nul
)
if not exist "%~dp0runtime\python\python.exe" (
  echo.
  echo   [ERROR] Could not unpack the downloaded Python.
  echo           Look inside runtime\_py and report what you see.
  echo.
  pause
  exit /b 1
)
if exist "%~dp0runtime\python\Lib\EXTERNALLY-MANAGED" del "%~dp0runtime\python\Lib\EXTERNALLY-MANAGED"

:deps
echo.
echo   [setup] Step 2/2: installing dependencies and model weights...
echo.
"%~dp0runtime\python\python.exe" "%~dp0tools\install_deps.py"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :fail_deps

echo.
echo   ==========================================================
echo   Setup finished. Double-click start.bat to launch.
echo   ==========================================================
echo.
pause
endlocal & exit /b 0

:fail_python
echo.
echo   [ERROR] Downloading Python failed.
echo           Check your network or proxy, then run this file again.
echo           Tip: set the mirror first, e.g.
echo                set UV_PYTHON_INSTALL_MIRROR=https://ghproxy.net/https://github.com/astral-sh/python-build-standalone/releases/download
echo.
pause
endlocal & exit /b 1

:fail_deps
echo.
echo   [ERROR] Dependency setup failed (exit code %RC%).
echo           Read the messages above, then run this file again -
echo           finished steps are skipped, nothing is downloaded twice.
echo.
echo           Still stuck? Double-click selfcheck.bat and paste the
echo           report to any AI assistant (see ASK_AI.md).
echo.
pause
endlocal & exit /b %RC%
