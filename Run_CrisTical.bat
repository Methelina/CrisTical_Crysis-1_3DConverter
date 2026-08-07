@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "SCRIPT_DIR=%~dp0"
set "ENV_NAME=cris_env"
set "PYTHON_EXE=%SCRIPT_DIR%%ENV_NAME%\Scripts\python.exe"
set "BIN_DIR=%SCRIPT_DIR%Bin"
set "SCRIPTS_DIR=%SCRIPT_DIR%scripts"
set "GUI_SCRIPT=%SCRIPTS_DIR%\cristical_gui.py"
set "CLI_SCRIPT=%SCRIPTS_DIR%\cdf2gltf.py"

REM ---- portability: isolate caches + add Bin to PATH ----
set "UV_CACHE_DIR=%SCRIPT_DIR%.cache\uv"
set "TMP=%SCRIPT_DIR%temp"
set "TEMP=%SCRIPT_DIR%temp"
set "PATH=%BIN_DIR%;%PATH%"

if not exist "%TMP%" mkdir "%TMP%"
if not exist "%SCRIPT_DIR%output" mkdir "%SCRIPT_DIR%output"
if not exist "%SCRIPT_DIR%.cache" mkdir "%SCRIPT_DIR%.cache"

REM ---- check environment ----
if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python environment not found.
    echo Run Install_CrisTical.bat first.
    pause
    exit /b 1
)

REM ---- help ----
if /i "%~1"=="--help" goto :show_help
if /i "%~1"=="-h" goto :show_help

REM ---- if arguments present, CLI mode ----
if not "%~1"=="" goto :cli_mode

REM ---- default: GUI ----
if not exist "%GUI_SCRIPT%" (
    echo [ERROR] GUI script not found: %GUI_SCRIPT%
    pause
    exit /b 1
)

echo [INFO] Starting CrisTical GUI...
title CrisTical - Crysis3D Converter
"%PYTHON_EXE%" "%GUI_SCRIPT%"
exit /b %ERRORLEVEL%


:cli_mode
if not exist "%CLI_SCRIPT%" (
    echo [ERROR] Converter script not found: %CLI_SCRIPT%
    pause
    exit /b 1
)

REM ---- collect all args ----
set "ARGS="
:collect_args
if "%~1"=="" goto :run_cli
set "ARGS=!ARGS! "%~1""
shift
goto :collect_args

:run_cli
echo [INFO] Running: cdf2gltf.py !ARGS!
"%PYTHON_EXE%" "%CLI_SCRIPT%" !ARGS!
exit /b %ERRORLEVEL%


:show_help
echo.
echo CrisTical Crysis3D Converter - CDF to animated glTF
echo =====================================================
echo.
echo USAGE:
echo   Run_CrisTical.bat                                 (GUI mode)
echo   Run_CrisTical.bat --cdf file --gamedir dir [...]  (CLI mode)
echo.
echo CLI OPTIONS:
echo   --cdf ^<path^>         Path to .cdf file
echo   --gamedir ^<dir^>      Game root directory (repeatable: -g d1 -g d2)
echo   --out ^<path^>         Output .gltf path (default: auto)
echo   --no-anim              Skip animation injection
echo   --no-tex               Skip texture conversion
echo   --split-anim           One glTF per animation
echo   --glb                  Output as binary .glb (single file)
echo.
echo EXAMPLES:
echo   Run_CrisTical.bat
echo   Run_CrisTical.bat --cdf alien.cdf -g "F:\Crysis\Game"
echo   Run_CrisTical.bat --cdf alien.cdf -g "F:\Crysis\Game" --split-anim
echo   Run_CrisTical.bat --cdf alien.cdf -g "F:\Crysis\Game" -o out.gltf --no-anim
echo.
pause
exit /b 0
