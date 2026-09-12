@echo off
setlocal enabledelayedexpansion

REM ==============================================================
REM  Xbox XBE Ghidra naming pipeline - Windows batch version
REM ==============================================================
REM  Usage:
REM    run_ghidra.bat                     - analyze + export funcs/symbols
REM    run_ghidra.bat decompile 4000      - also decompile up to 4000
REM    run_ghidra.bat decompile all       - decompile everything (slow)
REM ==============================================================

set "HERE=%~dp0"
set "REPO=%HERE%..\..\"

REM --- Prompt for paths if not set via environment ---
if "%XBE%"=="" (
    set /p "XBE=Enter path to Xbox default.xbe: "
)
if "%GHIDRA_HOME%"=="" (
    set /p "GHIDRA_HOME=Enter path to Ghidra install folder (e.g. C:\tools\ghidra\): "
)

REM --- Strip surrounding quotes if present ---
set "XBE=%XBE:"=%"
set "GHIDRA_HOME=%GHIDRA_HOME:"=%"

REM --- Validate inputs ---
if "%XBE%"=="" (
    echo ERROR: XBE path is required.
    exit /b 1
)
if not exist "%XBE%" (
    echo ERROR: XBE not found at %XBE%
    exit /b 1
)
if not exist "%GHIDRA_HOME%" (
    echo ERROR: GHIDRA_HOME not found at %GHIDRA_HOME%
    exit /b 1
)

set "HEADLESS=%GHIDRA_HOME%\support\analyzeHeadless.bat"
if not exist "%HEADLESS%" (
    echo ERROR: analyzeHeadless.bat not found at %HEADLESS%
    exit /b 1
)

REM --- Set up directories ---
set "WORK=%HERE%work"
set "PROJ_DIR=%WORK%\ghidra_project"
set "EXPORT_DIR=%HERE%export"
set "SCRIPT_DIR=%HERE%ghidra_scripts"
set "FLAT=%WORK%\xbe_flat.bin"
set "PROG_NAME=xbe_flat.bin"

REM Derive project name from XBE parent directory
for %%I in ("%XBE%\.") do set "PROJ_NAME=%%~nxI"
if "%PROJ_NAME%"=="" set "PROJ_NAME=xbe"
if "%PROJ_NAME%"=="." set "PROJ_NAME=xbe"
if "%PROJ_NAME%"=="\" set "PROJ_NAME=xbe"

REM --- Parse optional arguments ---
set "DO_DECOMPILE=nodecompile"
set "DECOMP_LIMIT=4000"
set "DECOMP_TIMEOUT=30"
if not "%~1"=="" set "DO_DECOMPILE=%~1"
if not "%~2"=="" set "DECOMP_LIMIT=%~2"
if not "%~3"=="" set "DECOMP_TIMEOUT=%~3"

REM --- Create working directories ---
if not exist "%WORK%" mkdir "%WORK%"
if not exist "%PROJ_DIR%" mkdir "%PROJ_DIR%"
if not exist "%EXPORT_DIR%" mkdir "%EXPORT_DIR%"

echo ==============================================================
echo  Xbox XBE Ghidra naming pipeline
echo ==============================================================
echo  GHIDRA_HOME : %GHIDRA_HOME%
echo  XBE         : %XBE%
echo  Work dir    : %WORK%
echo  Export dir  : %EXPORT_DIR%
echo  Decompile   : %DO_DECOMPILE% (limit=%DECOMP_LIMIT% timeout=%DECOMP_TIMEOUT%s)
echo ==============================================================

REM --- Step 1: Build flat image from XBE ---
echo [1/3] Building flat image from XBE ...
py -3 "%HERE%extract_for_ghidra.py" "%XBE%" --out-dir "%WORK%"
if errorlevel 1 (
    echo ERROR: extract_for_ghidra.py failed.
    exit /b 1
)

REM --- Resolve default IMPORT / ANALYZE flags ---
if "%IMPORT%"=="" set "IMPORT=1"
if "%ANALYZE%"=="" set "ANALYZE=1"

REM --- Step 2+3: Import + Analyze + Export ---
if "%IMPORT%"=="1" (
    echo [2/3] Importing flat image + analyzing + exporting ...

    set "ANALYSIS_ARGS="
    if "%ANALYZE%"=="0" set "ANALYSIS_ARGS=-noanalysis"

    "%HEADLESS%" "%PROJ_DIR%" "%PROJ_NAME%" ^
        -import "%FLAT%" ^
        -loader BinaryLoader ^
        -loader-baseAddr 0x10000 ^
        -processor "x86:LE:32:default" ^
        -cspec windows ^
        -overwrite ^
        !ANALYSIS_ARGS! ^
        -scriptPath "%SCRIPT_DIR%" ^
        -preScript SetAnalysisOptions.java ^
        -postScript ExportXbeNames.py "%EXPORT_DIR%" "%DO_DECOMPILE%" "%DECOMP_LIMIT%" "%DECOMP_TIMEOUT%"
) else (
    echo [2/3] Re-opening existing program + exporting (no re-import) ...

    set "NOANALYZE_FLAG=-noanalysis"
    if "%ANALYZE%"=="1" set "NOANALYZE_FLAG="

    "%HEADLESS%" "%PROJ_DIR%" "%PROJ_NAME%" ^
        -process "%PROG_NAME%" ^
        !NOANALYZE_FLAG! ^
        -scriptPath "%SCRIPT_DIR%" ^
        -postScript ExportXbeNames.py "%EXPORT_DIR%" "%DO_DECOMPILE%" "%DECOMP_LIMIT%" "%DECOMP_TIMEOUT%"
)

echo ==============================================================
echo  Done. Exports in: %EXPORT_DIR%
dir /b "%EXPORT_DIR%" 2>nul
echo ==============================================================

endlocal
