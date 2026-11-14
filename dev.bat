@echo off
chcp 65001 >nul
title 钩子工坊 · 开发模式
cd /d "%~dp0"

if not exist "runtime\Scripts\python.exe" (
  echo [错误] 找不到运行时 runtime\Scripts\python.exe
  echo.
  echo 首次使用请先执行：
  echo    python -m venv runtime
  echo    runtime\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

rem 中文长句容易在 cmd 下解析错位，这里只留一行短提示，详细说明交给 run.py 打印
echo 开发模式：改代码自动重启，改网页按 Ctrl+F5 刷新
echo.

"runtime\Scripts\python.exe" run.py --reload %*
pause
