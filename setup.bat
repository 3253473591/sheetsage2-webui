@echo off
rem ============================================================
rem  SheetSage2 Studio - first run / repair (ASCII only!)
rem
rem  Same rule as webui.bat: this file must stay ASCII. cmd.exe
rem  re-reads a .bat by byte offset, so UTF-8 Chinese text would
rem  split lines and the tail gets executed as a command.
rem  All Chinese output comes from tools\bootstrap.py.
rem
rem  What it does (idempotent - safe to run again):
rem    - create .venv from the bundled runtime\python
rem    - install torch / torchaudio from the manifest (resumable)
rem    - pip install the project + deps (Tsinghua mirror)
rem    - download the models (minimal by default; 1.7B and the
rem      Japanese aligner are optional)
rem
rem  Usage:
rem    setup.bat                 interactive (asks about 1.7B)
rem    setup.bat --check         report only, download nothing
rem    setup.bat --with-1.7b     also fetch the 1.7B ASR model
rem    setup.bat --with-ja       also fetch the Qwen aligner (Japanese only;
rem                              zh/yue/en use the bundled TIFA aligner)
rem    setup.bat --models full   everything (~10.4 GB)
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
    echo   [x] setup failed with code %CODE%. Re-run setup.bat - finished downloads
    echo       are kept and skipped.
    echo.
    pause
)
endlocal & exit /b %CODE%
