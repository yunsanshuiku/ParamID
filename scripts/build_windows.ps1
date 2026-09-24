# Build from a clean checkout with a selected Python environment.
[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$InnoCompiler = "",
    [switch]$SkipInstall,
    [switch]$SkipInstaller
)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $Root
if (-not $Python) {
    if ($env:PARAMID_PYTHON) { $Python = $env:PARAMID_PYTHON }
    elseif (Test-Path -LiteralPath (Join-Path $Root ".venv\Scripts\python.exe")) {
        $Python = Join-Path $Root ".venv\Scripts\python.exe"
    } else { $Python = (Get-Command python -ErrorAction Stop).Source }
}
if (-not $SkipInstall) {
    & $Python -m pip install -r requirements-desktop.txt
    if ($LASTEXITCODE -ne 0) { throw "桌面依赖安装失败。" }
}
& $Python -m desktop --self-test
if ($LASTEXITCODE -ne 0) { throw "桌面入口自检失败。" }
& $Python -m PyInstaller --clean --noconfirm desktop.spec
if ($LASTEXITCODE -ne 0) { throw "桌面程序构建失败。" }
$release = Join-Path $Root "release"
New-Item -ItemType Directory -Force -Path $release | Out-Null
$portable = [IO.Path]::GetFullPath((Join-Path $release "ParamID-Portable"))
# Verify the exact generated directory before recursive removal.
$expected = [IO.Path]::GetFullPath((Join-Path $Root "release\ParamID-Portable"))
if ($portable -ne $expected -or (Split-Path -Parent $portable) -ne $release) {
    throw "发布目录校验失败。"
}
if (Test-Path -LiteralPath $portable) {
    if ((Get-Item -LiteralPath $portable).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "发布目录不能是链接。"
    }
    Remove-Item -LiteralPath $portable -Recurse -Force
}
New-Item -ItemType Directory -Path $portable | Out-Null
Get-ChildItem -LiteralPath (Join-Path $Root "dist\ParamID") -Force |
    Copy-Item -Destination $portable -Recurse -Force
foreach ($name in @("install_windows.ps1", "uninstall_windows.ps1")) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination $portable -Force
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "install_windows.cmd") -Destination (Join-Path $portable "安装 ParamID.cmd")
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "uninstall_windows.cmd") -Destination (Join-Path $portable "卸载 ParamID.cmd")
Copy-Item -LiteralPath (Join-Path $Root "installer\install-notes.txt") -Destination (Join-Path $portable "安装说明.txt")
foreach ($name in @("LICENSE", "THIRD_PARTY_NOTICES.md")) {
    Copy-Item -LiteralPath (Join-Path $Root $name) -Destination $portable -Force
}
$exe = Join-Path $portable "ParamID.exe"
if (-not (Test-Path -LiteralPath $exe)) { throw "发布目录缺少 ParamID.exe。" }
$check = Start-Process -FilePath $exe -ArgumentList "--self-test" -WindowStyle Hidden -Wait -PassThru
if ($check.ExitCode -ne 0) { throw "打包程序自检失败。" }
$zip = Join-Path $release "ParamID-Portable.zip"
Compress-Archive -Path (Join-Path $portable "*") -DestinationPath $zip -CompressionLevel Optimal -Force
if (-not $SkipInstaller) {
    $candidates = @()
    if ($InnoCompiler) {
        $candidates += (Resolve-Path -LiteralPath $InnoCompiler -ErrorAction Stop).Path
    } else {
        $command = Get-Command iscc -ErrorAction SilentlyContinue
        if ($command) { $candidates += $command.Source }
        foreach ($programDir in @([Environment]::GetEnvironmentVariable("ProgramFiles(x86)"), $env:ProgramFiles, (Join-Path $env:LOCALAPPDATA "Programs"))) {
            if ($programDir) { $candidates += Join-Path $programDir "Inno Setup 6\ISCC.exe" }
        }
    }
    $compiler = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($compiler) {
        & $compiler /Qp "installer\ParamID.iss"
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup 构建失败。" }
    } else { Write-Warning "未找到 Inno Setup 6。已生成便携 ZIP；可安装编译器后重新构建。" }
}
Write-Host "构建完成：$release"
