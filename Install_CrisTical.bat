@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title CrisTical Crysis 1 3DConverter by L.'.L.'.

echo.
echo   ===========================================
echo     CrisTical Crysis 1 3DConverter v1.0
echo     Python 3.11 + Assimp 6.0.5 + bpy + DearPyGui
echo     Crysis 3 CDF to glTF/GLB converter
echo.
echo   ===========================================
echo     Author: Soror L.'.L.'.
echo   ===========================================
echo.

set "S=%~dp0"
set "ENV_NAME=cris_env"
set "ENV_DIR=%S%%ENV_NAME%"
set "PYTHON_EXE=%ENV_DIR%\Scripts\python.exe"
set "BIN_DIR=%S%Bin"
set "UV_VERSION=0.11.6"
set "ASSIMP_VERSION=6.0.5"
set "UV_PY_VERSION=3.11"

REM ---- portability isolation (same as Colibri: uv caches inside the project) ----
set "UV_CACHE_DIR=%S%.cache\uv"
set "TMP=%S%.cache\tmp"
set "TEMP=%S%.cache\tmp"

mkdir "%BIN_DIR%" 2>nul
mkdir "%S%output" 2>nul
mkdir "%S%.cache\uv" 2>nul
mkdir "%S%.cache\tmp" 2>nul

for %%D in (uv_tmp 7z_tmp assimp_tmp) do (if exist "%S%%%D" rmdir /s /q "%S%%%D")
for %%F in (uv.zip 7za.zip assimp.zip) do (if exist "%S%%%F" del /q "%S%%%F")

goto :stage_uv


REM ==================================================================
REM  :download URL TARGET_FILE  —  curl first, PowerShell fallback
REM ==================================================================
:download
set "DL_URL=%~1"
set "DL_OUT=%~2"
set "DL_LABEL=%~3"
echo   Downloading %DL_LABEL% (curl)...
curl -L -s -S -o "%DL_OUT%" "%DL_URL%" --connect-timeout 30 --max-time 300 2>nul
if exist "%DL_OUT%" for %%Z in ("%DL_OUT%") do if %%~zZ GEQ 1000 exit /b 0
echo   ^> curl failed, retrying with PowerShell...
del /q "%DL_OUT%" 2>nul
powershell -NoProfile -Command ^
  "$ProgressPreference='SilentlyContinue';" ^
  "try { Invoke-WebRequest -Uri '%DL_URL%' -OutFile '%DL_OUT%' -ErrorAction Stop } catch { exit 1 }"
if exist "%DL_OUT%" for %%Z in ("%DL_OUT%") do if %%~zZ GEQ 1000 exit /b 0
echo   [ERROR] Download failed for %DL_LABEL%
del /q "%DL_OUT%" 2>nul
exit /b 1


REM ==================================================================
REM  :extract ZIP DEST_DIR  —  tar first, PowerShell fallback
REM ==================================================================
:extract
set "EX_ZIP=%~1"
set "EX_DST=%~2"
if exist "%EX_DST%" rmdir /s /q "%EX_DST%"
mkdir "%EX_DST%" 2>nul
echo   Extracting with tar...
tar -xf "%EX_ZIP%" -C "%EX_DST%" 2>nul
dir /b "%EX_DST%\*" >nul 2>&1 && exit /b 0
echo   ^> tar failed, retrying with PowerShell...
rmdir /s /q "%EX_DST%" 2>nul
mkdir "%EX_DST%" 2>nul
powershell -NoProfile -Command "Expand-Archive -Path '%EX_ZIP%' -DestinationPath '%EX_DST%' -Force"
exit /b 0


REM ==================================================================
REM  STAGE 1/7  —  uv
REM ==================================================================
:stage_uv
echo.
echo  [1/7] uv package manager v%UV_VERSION%
if exist "%S%uv.exe" (
    echo   [OK] uv.exe already present
) else (
    call :download "https://releases.astral.sh/github/uv/releases/download/%UV_VERSION%/uv-x86_64-pc-windows-msvc.zip" "%S%uv.zip" "uv %UV_VERSION%" || goto :fail
    call :extract "%S%uv.zip" "%S%uv_tmp"
    for /r "%S%uv_tmp" %%F in (uv.exe) do copy /y "%%F" "%S%uv.exe" >nul 2>nul
    for /r "%S%uv_tmp" %%F in (uvx.exe) do copy /y "%%F" "%S%uvx.exe" >nul 2>nul
    rmdir /s /q "%S%uv_tmp" 2>nul
    del /q "%S%uv.zip" 2>nul
    if not exist "%S%uv.exe" (
        echo   [ERROR] uv.exe not found in archive
        goto :fail
    )
    echo   [OK] uv.exe installed
)
goto :stage_python


REM ==================================================================
REM  STAGE 2/7  —  Python 3.11 (uv-managed, includes tkinter)
REM ==================================================================
:stage_python
echo.
echo  [2/7] Python %UV_PY_VERSION% (uv-managed, includes tkinter)
"%S%uv.exe" python install %UV_PY_VERSION%
if %ERRORLEVEL% neq 0 (
    echo   [ERROR] Failed to install Python %UV_PY_VERSION% via uv
    goto :fail
)
echo   [OK] Python %UV_PY_VERSION% ready
goto :stage_venv


REM ==================================================================
REM  STAGE 3/7  —  Python venv (idempotent)
REM ==================================================================
:stage_venv
echo.
echo  [3/7] Python venv (%ENV_NAME%)
set "NEED_CREATE=1"
if exist "%PYTHON_EXE%" (
    if exist "%ENV_DIR%\pyvenv.cfg" (
        findstr /I /C:"python_embeded" "%ENV_DIR%\pyvenv.cfg" >nul 2>nul
        if errorlevel 1 set "NEED_CREATE=0"
    )
)
if "!NEED_CREATE!"=="0" (
    echo   [OK] Environment already present — skipping
    goto :stage_cleanup
)
if exist "%ENV_DIR%" (
    echo   [WARN] Recreating environment (references removed embedded Python)
    rmdir /s /q "%ENV_DIR%"
)
echo   Creating venv...
"%S%uv.exe" venv "%ENV_DIR%" --python %UV_PY_VERSION%
set "UV_RC=%ERRORLEVEL%"
if not exist "%PYTHON_EXE%" (
    echo   [ERROR] Failed to create venv (rc=%UV_RC%^)
    goto :fail
)
echo   [OK] Environment created


REM ==================================================================
REM  STAGE 4/7  —  cleanup legacy artifacts
REM ==================================================================
:stage_cleanup
if exist "%S%python_embeded" (
    rmdir /s /q "%S%python_embeded"
    echo   [INFO] Removed legacy embedded Python (python_embeded)
)
if exist "%S%.uv_python" (
    rmdir /s /q "%S%.uv_python"
    echo   [INFO] Removed legacy .uv_python staging dir
)


REM ==================================================================
REM  STAGE 5/7  —  pip packages
REM ==================================================================
:stage_pip
echo.
echo  [5/7] Python packages (pyassimp numpy pillow trimesh pygltflib bpy dearpygui)
echo   Installing packages (this may take 5-15 minutes)...
"%S%uv.exe" pip install --python "%PYTHON_EXE%" pyassimp numpy pillow trimesh pygltflib bpy dearpygui
set "PIP_RC=%ERRORLEVEL%"
if !PIP_RC! neq 0 (echo   [WARN] pip returned code !PIP_RC!)
echo   [OK] Packages stage completed


REM ==================================================================
REM  STAGE 6/7  —  7-Zip portable
REM ==================================================================
echo.
echo  [6/7] 7-Zip portable (7za.exe)
if exist "%BIN_DIR%\7za.exe" (echo   [OK] 7za.exe already present & goto :stage_assimp)

call :download "https://www.7-zip.org/a/7za920.zip" "%S%7za.zip" "7za920.zip"
if %ERRORLEVEL% neq 0 (echo   [WARN] 7-Zip download failed & goto :stage_assimp)

call :extract "%S%7za.zip" "%S%7z_tmp"
copy /y "%S%7z_tmp\7za.exe" "%BIN_DIR%\7za.exe" >nul 2>nul
rmdir /s /q "%S%7z_tmp" 2>nul
del /q "%S%7za.zip" 2>nul
if exist "%BIN_DIR%\7za.exe" (echo   [OK] 7za.exe installed) else (echo   [WARN] extraction failed)


REM ==================================================================
REM  STAGE 7/7  —  Assimp DLL
REM ==================================================================
:stage_assimp
echo.
echo  [7/7] Assimp %ASSIMP_VERSION% (assimp.dll)
if exist "%BIN_DIR%\assimp.dll" (echo   [OK] assimp.dll already present & goto :done)

call :download "https://github.com/assimp/assimp/releases/download/v%ASSIMP_VERSION%/windows-x64-v%ASSIMP_VERSION%.zip" "%S%assimp.zip" "Assimp %ASSIMP_VERSION%"
if %ERRORLEVEL% neq 0 (echo   [ERROR] Assimp download failed & goto :done)

call :extract "%S%assimp.zip" "%S%assimp_tmp"
for /r "%S%assimp_tmp" %%F in (assimp-vc143-mt.dll) do copy /y "%%F" "%BIN_DIR%\assimp.dll" >nul 2>nul
rmdir /s /q "%S%assimp_tmp" 2>nul
del /q "%S%assimp.zip" 2>nul
if exist "%BIN_DIR%\assimp.dll" (echo   [OK] assimp.dll installed) else (echo   [ERROR] DLL not found after extraction)


REM ==================================================================
REM  FINISH
REM ==================================================================
:done
set "PATH=%BIN_DIR%;%PATH%"
echo.
echo   +--------------------------------------------------------------+
echo   ^|                INSTALLATION COMPLETE                         ^|
echo   +--------------------------------------------------------------+
echo.
echo   Location: %S%
echo   Python:   %PYTHON_EXE%
echo   Assimp:   %BIN_DIR%\assimp.dll
echo   7-Zip:    %BIN_DIR%\7za.exe
echo.
echo   Launch the converter:
echo     Run_CrisTical.bat                     (GUI mode)
echo     Run_CrisTical.bat --cdf model.cdf -g GameDir   (CLI mode)
echo.
pause
exit /b 0

:fail
echo   [FATAL] Installation aborted
pause
exit /b 1
