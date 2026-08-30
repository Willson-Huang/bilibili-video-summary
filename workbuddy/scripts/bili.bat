@echo off
setlocal
REM 脚本位置从 %~dp0 自动推导
REM Python 探测顺序：BILI_PYTHON → WorkBuddy whisper 环境 → python → py
set "HERE=%~dp0"
set "PY=%BILI_PYTHON%"

if "%PY%"=="" (
  set "CAND=%USERPROFILE%\.workbuddy\binaries\python\envs\whisper\Scripts\python.exe"
)
if "%PY%"=="" if exist "%CAND%" set "PY=%CAND%"
if "%PY%"=="" (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if "%PY%"=="" (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py"
)
if "%PY%"=="" (
  echo [错误] 未找到 Python 解释器。请设置 BILI_PYTHON，或安装 Python 3.10+。
  exit /b 1
)

set PYTHONPATH=
set HF_ENDPOINT=https://hf-mirror.com
set HF_HUB_DISABLE_SYMLINKS=1
set HF_HUB_DISABLE_XET=1

if /i "%PY%"=="py" (
  py -3 "%HERE%bili_asr.py" %*
) else (
  "%PY%" "%HERE%bili_asr.py" %*
)
endlocal
