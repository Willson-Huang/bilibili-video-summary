@echo off
REM 任务进度看板 —— 一键启动（独立窗口）
REM 用法: bili_dashboard.bat [运行目录或项目根] [端口]
REM   不带参数 = 自动推导（当前目录须为项目根，即含 .workbuddy）
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

REM ---------- 运行目录解析（2026-09-19 修）----------
REM 此前不带参数时 --dir 留空，progress_hub 会落到它自己的默认目录
REM (%USERPROFILE%\obsidian\progress\current，C 盘旧路径)，于是"双击就有数据"
REM 静默失败、开出空看板。现在改为显式解析，解析不出来就报错退出。
REM 不采用"向上查找 .workbuddy"：用户级 %USERPROFILE%\.workbuddy\cache 也存在，
REM 会把 skill 目录误判成项目根。只认**当前目录本身**含 .workbuddy。
REM 提示语用 ASCII：cmd 按系统 ANSI 代码页读 .bat，中文提示在错误路径上可能乱码，
REM 而看不懂的错误提示等于没有提示。

REM 参数若给的是"项目根"（含 .workbuddy），补成标准运行目录，
REM 避免把 run.json / events 写进项目根、污染知识库。
if not "%DIR%"=="" if exist "%DIR%\.workbuddy" set "DIR=%DIR%\.workbuddy\cache\progress\current"

if "%DIR%"=="" set "DIR=%BILI_PROGRESS_DIR%"
if "%DIR%"=="" if exist "%CD%\.workbuddy" set "DIR=%CD%\.workbuddy\cache\progress\current"
if "%DIR%"=="" (
  echo [ERROR] Cannot resolve the dashboard run directory.
  echo   Usage : bili_dashboard.bat [run_dir_or_project_root] [port]
  echo   Or run this from the project root ^(the dir containing .workbuddy^).
  echo   Or set BILI_PROGRESS_DIR.
  exit /b 1
)

if not exist "%DIR%" mkdir "%DIR%"

set PYTHONPATH=
"%PY%" "%HERE%progress_hub.py" --serve --port %PORT% --dir "%DIR%" --open
endlocal
