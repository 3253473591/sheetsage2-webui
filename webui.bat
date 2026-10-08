@echo off
rem ============================================================
rem  SheetSage2 Studio launcher (ASCII only!)
rem
rem  TWO RULES for this file:
rem   1. Keep it ASCII. cmd.exe re-reads a .bat by byte offset,
rem      so UTF-8 Chinese (even in rem/echo) splits lines and the
rem      tail is executed as a command. All Chinese output comes
rem      from scripts\launch.py / tools\bootstrap.py.
rem   2. NO PARENTHESES in echoed text, and prefer `goto` labels
rem      over `if ... ( ... )` blocks. A stray ")" inside a block
rem      ends it early and cmd dies with
rem          "and was unexpected at this time."
rem      which looks exactly like a flash-and-close to the tester.
rem
rem  Usage:
rem    webui.bat              preferred port 8000, then pick a mode
rem    webui.bat 8010         preferred port 8010
rem    webui.bat 8010 2       port + mode, skip the menu
rem
rem    set SHEETSAGE_NO_BROWSER=1    do not open a browser
rem
rem  First run: .venv does not exist yet, so we hand over to
rem  tools\bootstrap.py, which uses the bundled Python.
rem ============================================================
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
set "RUNTIME=runtime\python\python.exe"

if exist "%PY%" goto launch

echo.
echo   First run - preparing the environment.
echo   Downloads torch about 1.8 GB, plus the models, minimal set
echo   about 6.0 GB. The 1.7B ASR model is optional.
echo   It is resumable: if it stops, run webui.bat again.
echo.

if not exist "%RUNTIME%" goto broken

"%RUNTIME%" "tools\bootstrap.py"
if errorlevel 1 goto setup_failed

:launch
"%PY%" "scripts\launch.py" %*
if errorlevel 1 pause
endlocal
exit /b 0

:broken
echo   [x] runtime\python\python.exe not found.
echo       The package looks incomplete - unzip it again, keep all folders.
echo.
pause
exit /b 1

:setup_failed
echo.
echo   [x] Setup did not finish.
echo       Run setup.bat to retry - already downloaded files are kept.
echo.
pause
exit /b 1
