# Install ParamID for the current Windows user without administrator rights.
[CmdletBinding()]
param(
    [string]$Source = "",
    [switch]$NoLaunch
)
$ErrorActionPreference = "Stop"
$sourceCandidates = @()
if ($Source) { $sourceCandidates += (Resolve-Path -LiteralPath $Source).Path }
$sourceCandidates += (Join-Path $PSScriptRoot "..\dist\ParamID")
$sourceCandidates += $PSScriptRoot
$sourceCandidates += (Join-Path $PSScriptRoot "ParamID")
$source = $sourceCandidates | Where-Object { Test-Path (Join-Path $_ "ParamID.exe") } | Select-Object -First 1
if (-not $source) { throw "找不到 ParamID.exe。请从项目目录运行，或指定 -Source 为包含 ParamID.exe 的目录。" }

$install = Join-Path $env:LOCALAPPDATA "Programs\ParamID"
New-Item -ItemType Directory -Force -Path $install | Out-Null
Copy-Item -Path (Join-Path $source "*") -Destination $install -Recurse -Force

$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
New-Item -ItemType Directory -Force -Path $startMenu | Out-Null
$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
foreach ($linkPath in @(
    (Join-Path $startMenu "ParamID.lnk"),
    (Join-Path $desktop "ParamID.lnk")
)) {
    $link = $shell.CreateShortcut($linkPath)
    $link.TargetPath = Join-Path $install "ParamID.exe"
    $link.WorkingDirectory = $install
    $link.Description = "ParamID 通用参数辨识工作台"
    $link.Save()
}
Write-Host "ParamID 已安装到：$install"
Write-Host "实验数据保存到：$(Join-Path $env:LOCALAPPDATA 'ParamID')"
if (-not $NoLaunch) { Start-Process (Join-Path $install "ParamID.exe") }



