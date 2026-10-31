@echo off
rem ============================================================
rem  SheetSage2 Studio - add models later
rem
rem  Run this when you want to add a model set to an existing
rem  install, for example:
rem    - Japanese songs (the bundled aligner cannot do Japanese)
rem    - higher lyric accuracy (bigger ASR model, needs VRAM)
rem
rem  It asks the same 1-4 menu as 01_install.bat and only touches
rem  the models folder - the environment is left alone.
rem
rem  ASCII ONLY, no parentheses in echoed text (same rule as
rem  00_start.bat / 01_install.bat).
rem ============================================================
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"

set "PY=runtime\python\python.exe"
if not exist "%PY%" goto broken

"%PY%" "tools\bootstrap.py" --steps models
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" goto failed
endlocal & exit /b 0

:broken
echo.
echo   [x] %PY% not found - run 01_install.bat first.
echo.
pause
exit /b 1

:failed
echo.
echo   [x] model download failed with code %CODE%. Re-run 02_models.bat -
echo       finished files are kept and skipped.
echo.
pause
endlocal & exit /b %CODE%
