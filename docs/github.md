# 上传 GitHub 与发布版本

本目录已按独立源码仓库组织。建议仓库名 `paramid`，描述：

> ParamID：支持离线 / 在线、ARX、窗口频域及多通道参数辨识的 Windows 桌面工具。

## 使用 GitHub Desktop

1. 解压源码 ZIP，进入含 `README.md`、`desktop.py` 的 `ParamID` 目录。
2. 在 GitHub Desktop 中选择 **File → Add local repository**，选择该目录；若提示还不是仓库，使用提示中的 **create a repository** 创建。不要另外生成 README、许可证或 .gitignore。
3. 检查文件列表，确认主要是 `paramid/`、`web/`、`docs/`、`tests/` 等源码。项目的 .gitignore 已排除环境、实验结果和构建文件。
4. 提交到 `main`，再点击 **Publish repository**，按需要选择公开或私有。

MIT 许可证已附在仓库中；是否公开由创建仓库时的可见性决定。

## 使用命令行

在 GitHub 新建空仓库，命名 `paramid`；不要勾选自动添加 README、.gitignore、License。然后在本地项目根目录执行：

```powershell
git init -b main
git add .
git status
git commit -m "Initial release of ParamID"
git remote add origin https://github.com/YOUR_USERNAME/paramid.git
git push -u origin main
```

将 `YOUR_USERNAME` 换为你的账号。若 Git 提示缺少提交身份，按你自己的姓名/邮箱配置；GitHub 登录可使用 GitHub Desktop 或 Git Credential Manager。若目录已初始化，`git init` 会保留已有仓库。

## 检查上传内容

提交源码、仿真示例、测试、文档和 `.github/`。不要把 Python 环境、采集记录、`outputs/`、`build/`、`dist/`、`release/` 提交到源码仓库，根目录已有忽略规则。

使用源码 ZIP 时，应解压后上传文件，保留 `.github/` 等以点开头的目录，避免只上传一个 ZIP 而无法浏览源码。

推送后打开 **Actions → Tests** 查看核心测试和界面检查。实际 CI 是否成功以 GitHub 的运行结果为准。

## 发布可安装软件

先按 [桌面构建说明](desktop.md) 构建，或手动运行 **Actions → Build Windows → Run workflow** 并下载构建附件。

在 **Releases → Draft a new release** 中创建标签（当前版本可用 `v0.7.0`），填入变更说明，然后上传：

- `ParamID-Setup-0.7.0.exe`：供普通 Windows 用户安装。
- `ParamID-Portable.zip`：供便携使用。
- 可选源码 ZIP：GitHub 也会自动提供对应标签的源码下载。

若构建机未找到 Inno Setup，只有便携 ZIP，不会生成安装 EXE。工作流不会自动创建公开 Release。

## 重新整理源码包

```powershell
python scripts/package_source.py
```

默认生成 `release/ParamID-source.zip`。可使用 `--output` 指定其他路径。脚本按源码类型白名单收集文件，保留 `.github/`，排除构建目录、环境和运行结果。压缩包统一含 `ParamID/` 顶层目录。
