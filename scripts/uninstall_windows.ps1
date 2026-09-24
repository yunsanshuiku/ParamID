# Remove the current-user ParamID installation and shortcuts. Experiment data is retained.
$ErrorActionPreference = "Stop"
$install = Join-Path $env:LOCALAPPDATA "Programs\ParamID"
foreach ($linkPath in @(
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\ParamID.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "ParamID.lnk")
)) {
    if (Test-Path $linkPath) { Remove-Item -LiteralPath $linkPath -Force }
}
$install = [IO.Path]::GetFullPath($install)
$programs = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Programs"))
if ((Split-Path -Parent $install) -ne $programs -or (Split-Path -Leaf $install) -ne "ParamID") {
    throw "卸载路径校验失败。"
}
if (Test-Path -LiteralPath $install) {
    if ((Get-Item -LiteralPath $install).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "卸载目录不能是链接。"
    }
    Remove-Item -LiteralPath $install -Recurse -Force
}
Write-Host "ParamID 程序已卸载；实验数据仍保留在 $env:LOCALAPPDATA\ParamID"
