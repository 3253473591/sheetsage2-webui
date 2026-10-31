@echo off
rem ============================================================
rem  SheetSage2 Studio - start here
rem
rem  THREE ENTRIES:
rem    00_start.bat    launch. NEVER downloads anything on its own.
rem    01_install.bat  first setup: env + torch + models, asks 1-4.
rem    02_models.bat   add models later (Japanese / higher accuracy).
rem
rem  TWO RULES for this file:
rem   1. Keep it ASCII. cmd.exe re-reads a .bat by byte offset,
rem      so UTF-8 Chinese (even in rem/echo) splits lines and the
rem      tail is executed as a command. ALL Chinese output comes
rem      from tools\bootstrap.py (--message) / scripts\launch.py.
rem   2. NO PARENTHESES in echoed text, and prefer `goto` labels
rem      over `if ... ( ... )` blocks. A stray ")" inside a block
rem      ends it early and cmd dies with
rem          "and was unexpected at this time."
rem      which looks exactly like a flash-and-close.
rem
rem  Usage:
rem    00_start.bat              preferred port 8000, then pick a mode
rem    00_start.bat 8010         preferred port 8010
rem    00_start.bat 8010 2       port + mode, skip the menu
rem
rem    set SHEETSAGE_NO_BROWSER=1    do not open a browser
rem ============================================================
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
set "RUNTIME=runtime\python\python.exe"

if not exist "%PY%" goto need_install
if not exist "models\.models-pending" goto launch

if not exist "%RUNTIME%" goto launch
"%RUNTIME%" "tools\bootstrap.py" --message models-pending
pause >nul
goto launch

:need_install
if not exist "%RUNTIME%" goto broken
"%RUNTIME%" "tools\bootstrap.py" --message need-install
echo.
echo   Install now?  Y = run 01_install.bat   N = quit
choice /c YN /n /m "  [Y/N] "
if errorlevel 2 exit /b 1
echo.
"%RUNTIME%" "tools\bootstrap.py"
if errorlevel 1 goto install_failed
goto launch

:install_failed
echo.
echo   [x] Install did not finish - run 01_install.bat to retry.
echo       Already downloaded files are kept.
echo.
pause
exit /b 1

:broken
echo.
echo   [x] runtime\python\python.exe not found.
echo       The package looks incomplete - unzip it again, keep all folders.
echo.
pause
exit /b 1

:launch
"%PY%" "scripts\launch.py" %*
if errorlevel 1 pause
endlocal
exit /b 0
