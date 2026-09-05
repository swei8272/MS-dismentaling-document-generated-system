@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 尚未安装运行环境，请先运行 install.bat。
  exit /b 1
)
".venv\Scripts\python.exe" worker.py
