@echo off
setlocal
REM 便携版入口：脚本位置从 %~dp0 自动推导
REM Python 探测顺序：BILI_PYTHON 环境变量 → python → py(Windows 启动器 -3)
set "HERE=%~dp0"
set "PY=%BILI_PYTHON%"

if "%PY%"=="" (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if "%PY%"=="" (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py"
)
if "%PY%"=="" (
  echo [错误] 未找到 Python 解释器。请安装 Python 3.10+，或用 BILI_PYTHON 指定路径。
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
