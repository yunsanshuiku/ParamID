@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0desktop.py"
    exit /b 0
)
python desktop.py
if errorlevel 1 (
    echo 请按 README 创建 .venv 并安装 requirements-desktop.txt。
    pause
    exit /b 1
)
