@echo off
rem ============================================================
rem  SheetSage2 Studio - AMD Radeon (ROCm) setup
rem
rem  For AMD cards only. NVIDIA users: ignore this file and run
rem  00_start.bat directly (the normal .venv already has CUDA).
rem
rem  What it does:
rem    - copies .venv to .venv-rocm (the normal .venv is untouched)
rem    - swaps torch/torchaudio for the ROCm build from AMD
rem      (about 2 GB download, resumable)
rem    - writes .env-backend so 00_start.bat picks .venv-rocm
rem
rem  Requirements: Windows 11 64-bit, latest AMD Adrenalin driver,
rem  and a card on AMD's official support list (RX 9000 series or
rem  select RX 7000 series). RX 6000 / RDNA2 is NOT supported by
rem  AMD's Windows ROCm build.
rem
rem  See the docs folder for the full trial guide. Safe to re-run:
rem  finished downloads are kept and skipped.
rem ============================================================
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"

if not exist "01_install.bat" (
    echo.
    echo   [x] 01_install.bat not found - run this from the package root.
    echo.
    pause
    exit /b 1
)

call "01_install.bat" rocm %*
set "CODE=%ERRORLEVEL%"
endlocal & exit /b %CODE%
