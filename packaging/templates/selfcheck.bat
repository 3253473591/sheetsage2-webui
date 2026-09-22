@echo off
rem ===========================================================================
rem  SheetSage2 transcribe - self check
rem
rem  Writes 环境快照.txt (UTF-8, Chinese file name) next to this file and
rem  prints it here. Paste that file to any AI assistant when something breaks.
rem  IMPORTANT: keep this file ASCII-only - cmd.exe would mangle Chinese text.
rem ===========================================================================
setlocal enableextensions
cd /d "%~dp0"
title SheetSage2 transcribe - self check

set "PY=%~dp0runtime\python\python.exe"
if exist "%PY%" goto :run
set "PY=%~dp0runtime\python\Scripts\python.exe"
if exist "%PY%" goto :run
set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%PY%" goto :run

echo.
echo   [ERROR] No Python runtime found, so the self check cannot run.
echo           Double-click install.bat first, then run this file again.
echo.
pause
exit /b 1

:run
"%PY%" "%~dp0tools\env_report.py"
set "RC=%ERRORLEVEL%"
echo.
echo   A snapshot report was written to this folder.
echo   Paste it to any AI assistant when asking for help (see ASK_AI.md).
echo.
pause
endlocal & exit /b %RC%
