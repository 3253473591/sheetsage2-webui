@echo off
rem ============================================================
rem  SheetSage2 Studio - install / repair (ASCII only!)
rem
rem  Same rule as 00_start.bat: this file must stay ASCII. cmd.exe
rem  re-reads a .bat by byte offset, so UTF-8 Chinese text would
rem  split lines and the tail gets executed as a command.
rem  All Chinese output comes from tools\bootstrap.py.
rem
rem  What it does (idempotent - safe to run again):
rem    - create .venv from the bundled runtime\python
rem    - install torch / torchaudio from the manifest (resumable)
rem    - pip install the project + deps (Tsinghua mirror)
rem    - download the models: it ASKS which set to fetch
rem        1  default (zh / yue / en)     ~4.3 GB  <- recommended
rem        2  Japanese extra              +1.7 GB
rem        3  higher ASR accuracy         +4.5 GB
rem        4  not now (00_start will remind you next time)
rem
rem  Usage:
rem    01_install.bat              interactive (asks 1-4)
rem    01_install.bat --check      report only, download nothing
rem    01_install.bat --models full  everything (~10.4 GB)
rem
rem  Power users can still pass --with-1.7b / --with-ja instead of
rem  picking a menu entry; testers should just pick 1-4.
rem ============================================================
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"

set "PY=runtime\python\python.exe"
if not exist "%PY%" (
    echo.
    echo   [x] %PY% not found - the package looks incomplete.
    echo.
    pause
    exit /b 1
)

"%PY%" "tools\bootstrap.py" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo   [x] install failed with code %CODE%. Re-run 01_install.bat - finished
    echo       downloads are kept and skipped.
    echo.
    pause
)
endlocal & exit /b %CODE%
