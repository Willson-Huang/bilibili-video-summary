@echo off
REM 任务进度看板 —— 一键启动（独立窗口）
REM 用法: bili_dashboard.bat [运行目录] [端口]
REM   不带参数 = 用默认运行目录与 8765 端口
setlocal
set "HERE=%~dp0"
set "DIR=%~1"
set "PORT=%~2"
if "%PORT%"=="" set "PORT=8765"

set "PY=%BILI_PYTHON%"
if "%PY%"=="" set "CAND=%USERPROFILE%\.workbuddy\binaries\python\envs\whisper\Scripts\python.exe"
if "%PY%"=="" if exist "%CAND%" set "PY=%CAND%"
if "%PY%"=="" (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if "%PY%"=="" (
  echo [错误] 未找到 Python 解释器。请设置 BILI_PYTHON。
  exit /b 1
)

set PYTHONPATH=
if "%DIR%"=="" (
  "%PY%" "%HERE%progress_hub.py" --serve --port %PORT% --open
) else (
  "%PY%" "%HERE%progress_hub.py" --serve --port %PORT% --dir "%DIR%" --open
)
endlocal
