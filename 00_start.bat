@echo off
rem ============================================================
rem  SheetSage2 Studio - start here
rem
rem  THREE ENTRIES:
rem    00_start.bat    launch. NEVER downloads anything.
rem    01_install.bat  first setup: env + torch + models, asks 1-4.
rem    02_models.bat   add models later (Japanese / higher accuracy).
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

if not exist "%PY%" goto need_install

rem Tester picked "not now" in 01_install.bat: say so, then launch anyway
rem so the interface is reachable. No downloads happen here, ever.
if not exist "models\.models-pending" goto launch

echo.
echo   [!] Models are not downloaded yet.
echo       To actually use the studio, run 01_install.bat once.
echo       If you only want to look at the interface, press any key to
echo       continue - recognition will not work without models.
echo.
pause >nul
goto launch

:need_install
echo.
echo   [!] Environment not ready - run 01_install.bat first.
echo       It asks which model set to download. Pick 4 there if you
echo       only want to look at the interface for now.
echo.
pause
exit /b 1

:launch
"%PY%" "scripts\launch.py" %*
if errorlevel 1 pause
endlocal
exit /b 0
