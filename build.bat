@echo off
REM ==========================================================================
REM  Build the CUDA "pump" vanity engine on Windows (native, MSVC + nvcc).
REM
REM  This script auto-loads the Visual Studio C++ build environment, so you can
REM  run it from a normal PowerShell/cmd prompt:  .\build.bat
REM
REM  Requirements:
REM    * NVIDIA CUDA Toolkit (nvcc on PATH)
REM    * Visual Studio 2019/2022 OR "Build Tools for Visual Studio" with the
REM      "Desktop development with C++" workload (provides cl.exe).
REM
REM  Override GPU arch (RTX 2060 = sm_75):
REM      cmd:         set GPU_ARCH=sm_75&& .\build.bat
REM      PowerShell:  $env:GPU_ARCH="sm_75"; .\build.bat
REM ==========================================================================
setlocal enabledelayedexpansion

set "ROOT=%~dp0"
set "SRC=%ROOT%engine\src"
set "OUTDIR=%SRC%\release"
set "OUTBIN=%OUTDIR%\cuda_ed25519_vanity.exe"

where nvcc >nul 2>&1
if errorlevel 1 (
  echo ERROR: nvcc not found. Install the CUDA Toolkit, then reopen the prompt.
  exit /b 1
)

REM --- Ensure the MSVC host compiler (cl.exe) is available -------------------
where cl >nul 2>&1
if not errorlevel 1 goto have_cl

echo cl.exe not on PATH - locating Visual Studio C++ tools...
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" set "VSWHERE=%ProgramFiles%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" goto no_vs

set "VSPATH="
for /f "usebackq tokens=* delims=" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSPATH=%%i"
if not defined VSPATH goto no_vctools

set "VCVARS=%VSPATH%\VC\Auxiliary\Build\vcvars64.bat"
if not exist "%VCVARS%" goto no_vcvars

echo Loading MSVC x64 environment from:
echo    %VCVARS%
call "%VCVARS%" >nul
where cl >nul 2>&1
if errorlevel 1 goto no_cl_after

:have_cl

REM --- Determine target GPU architecture ------------------------------------
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
if errorlevel 1 goto build_failed

echo.
echo Build OK: %OUTBIN%
echo Run the search + DB storage with:  py find_pump_keys.py
exit /b 0

REM --- Error handlers -------------------------------------------------------
:no_vs
echo.
echo ERROR: Visual Studio / Build Tools not found.
echo   nvcc needs the MSVC C++ compiler (cl.exe) on Windows.
echo   Install "Build Tools for Visual Studio" and select the
echo   "Desktop development with C++" workload, then re-run:
echo     https://visualstudio.microsoft.com/downloads/  (Tools for VS -^> Build Tools)
echo.
echo   OR build under WSL2 instead (see README, "Linux / WSL2" section).
exit /b 1

:no_vctools
echo.
echo ERROR: Visual Studio is installed but the C++ tools (VC.Tools.x86.x64) are not.
echo   Re-run the Visual Studio Installer -^> Modify -^> add
echo   "Desktop development with C++", then re-run this script.
exit /b 1

:no_vcvars
echo ERROR: vcvars64.bat not found under "%VSPATH%".
exit /b 1

:no_cl_after
echo ERROR: cl.exe still not found after loading the MSVC environment.
echo        Try running from the "x64 Native Tools Command Prompt for VS".
exit /b 1

:build_failed
echo.
echo BUILD FAILED. Common causes:
echo   * Your MSVC version is newer than your CUDA Toolkit supports.
echo     Check 'nvcc --version' and install a CUDA version that supports your VS,
echo     or add an older MSVC toolset, or pass -allow-unsupported-compiler.
echo   * arch unsupported by your CUDA version -^> set GPU_ARCH=sm_75 and rerun.
exit /b 1
