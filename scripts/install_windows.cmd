@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_windows.ps1"
if errorlevel 1 (
    echo.
    echo ParamID 安装失败，请把上面的错误信息发给开发者。
    pause
    exit /b 1
)
echo.
echo ParamID 安装完成，可以从开始菜单或桌面快捷方式启动。
pause
