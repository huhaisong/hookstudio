@echo off
cd /d "%~dp0"
start "" /min "%~dp0runtime\Scripts\python.exe" run.py --no-browser
echo 钩子工坊开发版已在后台启动（最小化窗口）。
echo 浏览器打开：http://127.0.0.1:8100
timeout /t 3 >nul
