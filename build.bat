@echo off
REM ==========================================================================
REM  Build the CUDA "pump" vanity engine on Windows (native, MSVC + nvcc).
REM
REM  Requirements:
REM    * NVIDIA CUDA Toolkit (nvcc on PATH)
REM    * Visual Studio Build Tools with the C++ workload (provides cl.exe)
REM
REM  EASIEST: run this from the "x64 Native Tools Command Prompt for VS",
REM  which puts cl.exe on PATH so nvcc can find the host compiler.
REM
REM  GPU arch: auto-detected via nvidia-smi; override with e.g.
REM      set GPU_ARCH=sm_75
REM      build.bat
REM  (RTX 2060 = sm_75, RTX 30xx = sm_86, RTX 40xx = sm_89)
REM ==========================================================================
setlocal enabledelayedexpansion

set "ROOT=%~dp0"
set "SRC=%ROOT%engine\src"
set "OUTDIR=%SRC%\release"
set "OUTBIN=%OUTDIR%\cuda_ed25519_vanity.exe"

where nvcc >nul 2>&1
if errorlevel 1 (
  echo ERROR: nvcc not found. Install the CUDA Toolkit and reopen the prompt.
  exit /b 1
)

REM --- Determine target arch ------------------------------------------------
set "ARCH=%GPU_ARCH%"
if "%ARCH%"=="" (
  for /f "usebackq tokens=* delims=" %%i in (`nvidia-smi --query-gpu^=compute_cap --format^=csv^,noheader 2^>nul`) do set "CAP=%%i"
  if defined CAP (
    set "CAP=!CAP: =!"
    set "CAP=!CAP:.=!"
    if not "!CAP!"=="" set "ARCH=sm_!CAP!"
  )
)
if "%ARCH%"=="" (
  echo Could not auto-detect GPU; defaulting to sm_75 ^(RTX 2060^).
  set "ARCH=sm_75"
)
echo Building for GPU architecture: %ARCH%

if not exist "%OUTDIR%" mkdir "%OUTDIR%"

REM --- Compile vanity.cu as a standalone executable -------------------------
echo Compiling engine ...
nvcc -O3 -arch=%ARCH% -I"%SRC%\cuda-headers" "%SRC%\cuda-ecc-ed25519\vanity.cu" -o "%OUTBIN%"
if errorlevel 1 (
  echo.
  echo BUILD FAILED. Common causes:
  echo   * cl.exe not on PATH  -^> run from "x64 Native Tools Command Prompt for VS"
  echo   * arch unsupported by your CUDA version -^> set GPU_ARCH=sm_75 ^& rerun
  exit /b 1
)

echo.
echo Build OK: %OUTBIN%
echo Run the search + DB storage with:  python find_pump_keys.py
endlocal
