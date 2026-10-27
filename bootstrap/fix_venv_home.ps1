# bootstrap\fix_venv_home.ps1 — 整合包 venv 底座修复器
# 作用：整合包自带的 .venv 在打包机上创建，pyvenv.cfg 的 home 写的是打包机的 Python 绝对路径；
#       换一台机器该路径不存在，venv 的 python.exe 会直接报 "No Python at ..." 起不来。
#       本脚本把 home 改写为包内自带的 runtime\python（绝对路径），幂等可重复调用。
# 适用：所有会执行 venv Python 的入口（launcher / ensure / run_pipeline / start_server）
#       在首次触碰 venv 之前先调用一次。开发机（home 有效）调用是空操作。
# 参数：-Root 仓库根目录；缺省取本脚本所在目录的上一级。
param([string]$Root)
$ErrorActionPreference = 'SilentlyContinue'
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
$cfgPath = Join-Path $Root '.venv\pyvenv.cfg'
$runtime = Join-Path $Root 'runtime\python'
# 包内没有自带 runtime（纯源码运行）时无事可做
if (-not (Test-Path $cfgPath)) { exit 0 }
if (-not (Test-Path (Join-Path $runtime 'python.exe'))) { exit 0 }
$lines = @(Get-Content $cfgPath -Encoding UTF8)
$idx = -1; $homeVal = $null
for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i] -match '^\s*home\s*=') {
        $homeVal = ($lines[$i] -split '=', 2)[1].Trim()
        $idx = $i; break
    }
}
if ($idx -lt 0) { exit 0 }
# home 为绝对路径且底下真有 python.exe 才算有效；相对路径/不存在的路径一律改写
$homeOk = $false
if ($homeVal -and ([IO.Path]::IsPathRooted($homeVal))) {
    $homeOk = Test-Path (Join-Path $homeVal 'python.exe')
}
if ($homeOk) { exit 0 }
$lines[$idx] = 'home = ' + $runtime
# UTF-8 无 BOM（与 Python venv 模块写法一致；PS5.1 的 -Encoding UTF8 会带 BOM，不能用）
[IO.File]::WriteAllText($cfgPath, (($lines -join "`r`n") + "`r`n"), (New-Object System.Text.UTF8Encoding($false)))
exit 0
