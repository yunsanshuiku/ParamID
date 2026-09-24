@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall_windows.ps1"
if errorlevel 1 (
    echo.
    echo ParamID 卸载失败，请把上面的错误信息发给开发者。
    pause
    exit /b 1
)
pause
