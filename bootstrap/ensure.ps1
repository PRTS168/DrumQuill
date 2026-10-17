# bootstrap\ensure.ps1 — 快速检测入口
# 用法:
#   ensure.ps1 -CheckOnly   只检测，打印结果；退出码 0=就绪 / 1=缺失
#   ensure.ps1              检测；缺失时自动调用 install.ps1 全流程（可见进度）
#   ensure.ps1 -Launch      环境就绪后启动 Web 服务并打开浏览器
# 退出码: 0 就绪 / 1 缺失（且未能自动修复）/ 2 自动安装失败（已打印手动指引）

param(
    [switch]$CheckOnly,
    [switch]$Launch
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ROOT = if ($env:DRUM_AI_ROOT) { $env:DRUM_AI_ROOT } else { Split-Path $PSScriptRoot -Parent }
$CFG_PATH = Join-Path $ROOT "config.json"

# 读取端口（config.json 的 port 字段，默认 8756）
$port = 8756
if (Test-Path $CFG_PATH) {
    try {
        $cfg = Get-Content $CFG_PATH -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($cfg.port) { $port = [int]$cfg.port }
        if ($cfg.venvDir) { $venvDirCfg = $cfg.venvDir }
    } catch {}
}

function Test-Ready {
    $problems = @()

    # venv python
    $venvPy = $null
    if ($venvDirCfg -and (Test-Path (Join-Path $venvDirCfg "Scripts\python.exe"))) {
        $venvPy = Join-Path $venvDirCfg "Scripts\python.exe"
    } elseif (Test-Path (Join-Path $ROOT ".venv\Scripts\python.exe")) {
        $venvPy = Join-Path $ROOT ".venv\Scripts\python.exe"
    }
    if (-not $venvPy) { $problems += "venv 不存在"; return $problems }

    # 关键 import（快速探针）
    $miss = & $venvPy -c @"
import importlib
miss=[]
for m in ['torch','demucs','adtof_pytorch','librosa','flask']:
    try: importlib.import_module(m)
    except Exception: miss.append(m)
print(' '.join(miss))
"@ 2>$null
    $miss = ($miss | Select-Object -Last 1)
    if ($miss.Trim()) { $problems += "Python 包缺失: $miss" }

    # Demucs 权重
    if (-not (Test-Path (Join-Path $ROOT "models\demucs_local\955717e8-8726e21a.th"))) {
        $problems += "Demucs 权重缺失"
    }
    # MuseScore
    $mscore = $null
    foreach ($p in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $p) { continue }
        $hit = Get-ChildItem "$p\MuseScore*\bin\MuseScore4.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { $mscore = $hit.FullName; break }
    }
    if (-not $mscore) { $problems += "MuseScore 4 未安装" }
    # ffmpeg
    $ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if (-not $ff -and -not (Test-Path (Join-Path $ROOT "tools\ffmpeg\bin\ffmpeg.exe"))) {
        if (-not ($cfg -and $cfg.ffmpegPath -and (Test-Path $cfg.ffmpegPath))) { $problems += "ffmpeg 缺失" }
    }
    return $problems
}

$t0 = Get-Date
$problems = @(Test-Ready)
$elapsed = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)

# 静默启动：交给 app\launcher.py（自身探测 venv/系统 python，pythonw 拉起服务，
# 无任何控制台窗口）。launcher 负责端口探测与回退。
function Start-App {
    $launcher = Join-Path $ROOT "app\launcher.py"
    if (Test-Path $launcher) {
        $py3 = Get-Command py -ErrorAction SilentlyContinue
        $pyth = Get-Command python -ErrorAction SilentlyContinue
        if ($py3) { & $py3 -3 $launcher; return }
        elseif ($pyth) { & $pyth $launcher; return }
        Write-Host "[!] 未找到 Python 解释器，无法启动。请安装 Python 3.9+ 后重试。"
    } elseif (Test-Path (Join-Path $ROOT "app\start_server.ps1")) {
        & powershell -ExecutionPolicy Bypass -File (Join-Path $ROOT "app\start_server.ps1")
    } else {
        Write-Host "[!] 未找到 app\launcher.py，仅打开浏览器。"
        Start-Process "http://127.0.0.1:$port/"
    }
}

if ($problems.Count -eq 0) {
    Write-Host "[就绪] 环境检测通过（耗时 ${elapsed}s）。"
    if ($Launch) {
        Write-Host "[启动] 静默拉起服务..."
        Start-App
    }
    exit 0
}

Write-Host "[缺失] $($problems -join '；')"
if ($CheckOnly) { exit 1 }

# 缺失时：不再弹黑色安装窗口——优先起服务（系统 python 有 Flask 即可），
# 由 Web 首启向导驱动安装；若完全无 Python，则回退命令行安装。
if ($Launch -and -not (Get-Command python -ErrorAction SilentlyContinue) -and -not (Get-Command py -ErrorAction SilentlyContinue)) {
    Write-Host "[安装] 未找到任何 Python，调用 install.ps1 命令行安装（需先安装 Python 3.9+）。"
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "install.ps1")
    $code = $LASTEXITCODE
    if ($code -eq 0 -and $Launch) { Start-App }
    exit $code
}

# 有 Python（系统 python 可跑 app.py 与 Web 安装向导）：直接静默启动，首启向导接管安装
if ($Launch) {
    Write-Host "[启动] 静默拉起服务（Web 首启向导将引导安装缺失组件）..."
    Start-App
    exit 0
}

# 无 -Launch：仅安装
Write-Host "[安装] 调用 install.ps1 自动修复..."
$install = Join-Path $PSScriptRoot "install.ps1"
& powershell -ExecutionPolicy Bypass -File $install
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host "[未完成] install.ps1 退出码 $code，请按上方手动指引处理。"
    exit $code
}
Write-Host "[完成] 依赖安装完成。"
exit 0

