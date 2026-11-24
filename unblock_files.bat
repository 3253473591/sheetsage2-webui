@echo off
rem ============================================================
rem  Unblock files after extracting this package.
rem
rem  Windows marks files downloaded from the internet with the
rem  "Mark of the Web". Running a blocked python.exe / ffmpeg.exe
rem  can fail with 0xc0000142 (DLL init failed) or silently do
rem  nothing.
rem
rem  Run this ONCE after extracting, then start 00_start.bat.
rem  If your antivirus still blocks it, add this folder to the
rem  whitelist.
rem ============================================================
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"

echo.
echo   Unblocking files in this folder ...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-ChildItem -LiteralPath '%~dp0' -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue; Write-Host 'Done.'"

echo.
echo   If 00_start.bat still shows 0xc0000142, add this folder to
echo   your antivirus whitelist and extract the archive again.
echo.
pause
endlocal
exit /b 0
