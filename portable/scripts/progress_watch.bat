@echo off
rem Progress viewer: reprint the progress table every 2 seconds.
rem Usage: progress_watch.bat [run_dir_or_project_root]
rem   Without an argument: BILI_PROGRESS_DIR is used when set, otherwise walk
rem   up from the current directory to the nearest folder holding .workbuddy.
rem   Ctrl-C or closing the window exits.
setlocal
set "HERE=%~dp0"
set "HUBPY=%HERE%progress_hub.py"
set "ARG1=%~1"
set "PY=%BILI_PYTHON%"
if not defined PY for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY for /f "delims=" %%i in ('where py 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY echo [ERROR] Python not found. Set BILI_PYTHON or install Python 3.10+.
if not defined PY pause
if not defined PY exit /b 1
if not exist "%HUBPY%" echo [ERROR] progress_hub.py was not found next to this launcher.
if not exist "%HUBPY%" pause
if not exist "%HUBPY%" exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Continue'; $py = $env:PY; $hub = $env:HUBPY; $arg = $env:ARG1; if ($arg) { $run = $arg; if (Test-Path (Join-Path $arg '.workbuddy')) { $run = Join-Path $arg '.workbuddy\cache\progress\current' } } elseif ($env:BILI_PROGRESS_DIR) { $run = $env:BILI_PROGRESS_DIR } else { $d = [System.IO.DirectoryInfo](Get-Location).Path; while ($d -and -not (Test-Path (Join-Path $d.FullName '.workbuddy'))) { $d = $d.Parent }; if ($d) { $run = Join-Path $d.FullName '.workbuddy\cache\progress\current' } else { $run = $null } }; if (-not $run -or -not (Test-Path $run)) { Write-Host ('[ERROR] progress directory not found: ' + $run); Write-Host 'Pass the project folder as the first argument, or set BILI_PROGRESS_DIR.'; Read-Host 'Press Enter to close'; exit 1 } else { while ($true) { Clear-Host; & $py $hub --status --dir $run; Start-Sleep 2 } }"
