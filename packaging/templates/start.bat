@echo off
rem ===========================================================================
rem  SheetSage2 transcribe - launcher
rem
rem  IMPORTANT: this file must stay ASCII-only.
rem  cmd.exe parses .bat files with the console OEM code page (936/GBK on a
rem  Chinese Windows), NOT UTF-8. Any Chinese text stored here would be mangled
rem  and executed as garbage commands. All localized output is printed by
rem  Python (UTF-8) instead.
rem ===========================================================================
setlocal enableextensions
cd /d "%~dp0"
title SheetSage2 transcribe

set "PY=%~dp0runtime\python\python.exe"
if exist "%PY%" goto :run

set "PY=%~dp0runtime\python\Scripts\python.exe"
if exist "%PY%" goto :run

set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%PY%" goto :run

rem ---- no runtime yet: this is the lite package, run the first-time setup ----
if exist "%~dp0install.bat" (
  echo.
  echo   [setup] Portable runtime not found - starting first-time setup.
  echo   [setup] This needs internet access and may take a while.
  echo.
  call "%~dp0install.bat"
  echo.
)

set "PY=%~dp0runtime\python\python.exe"
if exist "%PY%" goto :run
set "PY=%~dp0runtime\python\Scripts\python.exe"
if exist "%PY%" goto :run
goto :nopython

:run
if not exist "%~dp0app\server.py" (
  echo.
  echo   [ERROR] app\server.py is missing.
  echo           Keep every file in this folder together after extracting.
  echo.
  pause
  exit /b 1
)

echo.
echo   SheetSage2 transcribe
echo   Starting... the browser will open automatically.
echo   Keep this window open. Close it (or press Ctrl+C) to stop the server.
echo.

"%PY%" -m app.server %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo   [Server exited] code %RC%
  echo.
  echo   - Port busy?  edit "port" in settings.json
  echo   - Stuck?      double-click selfcheck.bat, then paste the generated
  echo                 report to any AI assistant (see ASK_AI.md).
  echo.
  pause
)
endlocal & exit /b %RC%

:nopython
echo.
echo   [ERROR] No usable Python runtime was found.
echo.
echo   Full package : runtime\python\python.exe should exist. Re-extract the
echo                  archive (do not copy files out of it one by one).
echo   Lite package : double-click install.bat (or just this file again) and
echo                  let the first-time setup finish.
echo.
pause
endlocal & exit /b 1
