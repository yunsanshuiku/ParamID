# 桌面运行与 Windows 构建

桌面版使用 pywebview / Edge WebView2 提供独立 Windows 窗口，自动管理本机计算服务。界面与计算代码共用一套源码，无需启动外部浏览器。内部 HTTP 只绑定 `127.0.0.1`，端口由系统分配。

## 环境

- Windows 10 / 11 x64，Python 3.10–3.12。
- Microsoft Edge WebView2 Runtime。
- 构建便携包：PyInstaller，已列入 `requirements-desktop.txt`。
- 构建安装 EXE：另外安装 Inno Setup 6，遵守所用版本的许可条件。

在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
.\.venv\Scripts\python.exe desktop.py
```

如果要使用已验证的直接依赖组合，将依赖文件换为 `requirements-tested.txt`。它不是完整锁文件，传递依赖仍由 pip 解析。

## 构建

```powershell
.\scripts\build_windows.ps1 -Python .\.venv\Scripts\python.exe -SkipInstall
```

`-Python` 可省略，脚本依次使用 `PARAMID_PYTHON`、项目 `.venv`、PATH 中的 `python`。省略 `-SkipInstall` 时会先安装桌面依赖。

脚本执行桌面服务自检、PyInstaller 打包和打包后自检，生成：

| 路径 | 内容 |
| --- | --- |
| `dist/ParamID/` | PyInstaller 程序目录，运行需要整个目录 |
| `release/ParamID-Portable.zip` | 便携包、安装/卸载辅助脚本、许可说明 |
| `release/ParamID-Setup-0.7.0.exe` | 找到 Inno Setup 时生成的安装包 |

指定非标准位置的编译器：

```powershell
.\scripts\build_windows.ps1 -Python .\.venv\Scripts\python.exe -SkipInstall -InnoCompiler 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
```

仅打包便携版可加 `-SkipInstaller`。以上目录均为构建时产生，不包含在干净源码包中。

若 PowerShell 的执行策略阻止运行脚本，可为本次进程执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1 -SkipInstall
```

## 使用发布包

安装 EXE 双击即可安装到当前用户目录，通常无需管理员权限。便携 ZIP 必须完整解压，随后运行 `ParamID.exe`；也可双击其中的“安装 ParamID.cmd”创建开始菜单和桌面快捷方式。

实验记录、设备模板和界面数据默认在 `%LOCALAPPDATA%\ParamID`。卸载程序不自动删除实验数据。环境变量 `PARAMID_DATA_DIR` 可以设置独立数据目录。

启动故障可查看该目录中的 `desktop-crash.log` 或 `desktop.log`。确认安装了 WebView2 Runtime；串口拒绝访问时，应确认端口号及是否被其他采集程序占用。

## 自检与界面验证

```powershell
.\.venv\Scripts\python.exe desktop.py --self-test
.\.venv\Scripts\python.exe desktop.py --smoke-test outputs/desktop-smoke.json
```

`--self-test` 检查本机计算服务。`--smoke-test` 会实际打开窗口，自动加载仿真数据并辨识，写出结果后关闭；需要可用的桌面会话与 WebView2。

[Build Windows 工作流](../.github/workflows/windows-build.yml) 可在 GitHub Actions 手动触发，构建产物作为工作流附件保存，不自动发布。正式版本发布方式见 [GitHub 指南](github.md)。
