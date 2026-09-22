@echo off
rem ---------------------------------------------------------------------------
rem  SheetSage2 minimal transcription web tool - launcher
rem
rem  IMPORTANT: keep this file ASCII-only.
rem  cmd.exe parses .bat files using the console OEM code page (936/GBK on a
rem  Chinese Windows), NOT UTF-8. Chinese text stored as UTF-8 gets mangled into
rem  garbage and then executed as commands. All localized output is printed by
rem  Python (UTF-8) instead, so this launcher stays ASCII and encoding-proof.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo.
  echo   [ERROR] Python environment not found:
  echo           %PY%
  echo.
  echo   Create it first, then install dependencies:
  echo           uv venv --python 3.12 .venv
  echo           uv pip install --python .venv\Scripts\python.exe -r requirements.txt
  echo.
  pause
  exit /b 1
)

if not exist "%~dp0app\server.py" (
  echo   [ERROR] app\server.py not found. Run this from the project root.
  pause
  exit /b 1
)

echo.
echo   SheetSage2 transcription web tool
echo   Starting... the browser will open automatically.
echo   Close this window to stop the server.
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
