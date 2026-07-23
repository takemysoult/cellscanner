@echo off
rem Export from 1C into the snapshot file and deliver it to the warehouse PC.
rem ASCII-only on purpose: .bat files are read in the OEM codepage and non-ASCII
rem comments break parsing on some machines.
rem
rem Run it from Task Scheduler on the PC that has the FULL 64-bit 1C platform:
rem   Program:   cmd.exe
rem   Arguments: /c "C:\...\scanner\tools\export_cells.bat"
rem   Start in:  C:\...\scanner
rem
rem Pass -deliver to retry delivery only (no 1C access) - used by the hourly
rem catch-up task when the warehouse PC was switched off at export time.
rem
rem Destination comes from the app settings ("Obshaya papka"); override with a
rem path argument:  export_cells.bat "\\WAREHOUSE-PC\CellScanner\cells.db"
rem
rem Exit codes: 0 = exported and delivered, 1 = export failed,
rem             2 = exported but not delivered (retry later, data is safe).
rem
rem Everything this script prints goes to %LOCALAPPDATA%\CellScanner\logs\export.log
rem so a scheduled run that fails before Python starts still leaves a trace.

setlocal
cd /d "%~dp0.."

set "LOGDIR=%LOCALAPPDATA%\CellScanner\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" 2>nul
set "LOG=%LOGDIR%\export.log"

echo ================================================================>>"%LOG%"
echo [%DATE% %TIME%] start, args=%*>>"%LOG%"

set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [%DATE% %TIME%] ERROR: python not found at %PY%>>"%LOG%"
    endlocal & exit /b 1
)

if /i "%~1"=="-deliver" (
    "%PY%" "tools\export_cells.py" --deliver-only >>"%LOG%" 2>&1
) else if "%~1"=="" (
    "%PY%" "tools\export_cells.py" >>"%LOG%" 2>&1
) else (
    "%PY%" "tools\export_cells.py" --out "%~1" >>"%LOG%" 2>&1
)
set "RC=%ERRORLEVEL%"

echo [%DATE% %TIME%] finished with code %RC%>>"%LOG%"
endlocal & exit /b %RC%
