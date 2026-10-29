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
# 整合包：pyvenv.cfg 的 home 在别的机器上无效时改写为包内 runtime\python（幂等，开发机空操作）
. (Join-Path $PSScriptRoot "fix_venv_home.ps1") -Root $ROOT

# 读取端口（config.json 的 port 字段，默认 8756）
$port = 8756
if (Test-Path $CFG_PATH) {
    try {
        $cfg = Get-Content $CFG_PATH -Raw -Encoding UTF8 | ConvertFrom-Json
        try { if ($cfg.port) { $port = [int]$cfg.port } } catch { $port = 8756 }   # 坏配置文件端口非数字不能弄死 ensure
        if ($cfg.venvDir) { $venvDirCfg = $cfg.venvDir }
    } catch {}
}

function Test-Ready {
    # 就绪口径与 installer.py COMPONENTS 对齐：
    #   Block = required=True（venv / 核心包），缺失无法扒谱
    #   Warn  = required=False（Demucs 权重 / MuseScore / ffmpeg），缺失只降级、不阻断
    $block = @()
    $warn = @()

    # venv python
    $venvPy = $null
    if ($venvDirCfg -and (Test-Path (Join-Path $venvDirCfg "Scripts\python.exe"))) {
        $venvPy = Join-Path $venvDirCfg "Scripts\python.exe"
    } elseif (Test-Path (Join-Path $ROOT ".venv\Scripts\python.exe")) {
        $venvPy = Join-Path $ROOT ".venv\Scripts\python.exe"
    }
    if (-not $venvPy) { return @{ Block = @("venv 不存在"); Warn = @() } }

    # 关键 import（快速探针）。清单对齐 installer.py PIP_KEY_IMPORTS 与
    # install.ps1 IMPORT_LIST——pip 半途失败的经典残留态（缺 numpy/soundfile/mido 等）
    # 以前探针查不出来，ensure 报「就绪」、管线跑到中途才炸。
    $_eap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"   # PS5.1：stderr 重定向的原生调用写 stderr 即终止
    $probeOut = & $venvPy -c @"
import importlib, os
miss=[]
for m in ['torch','demucs','beat_this','librosa','pretty_midi','numpy','scipy','soundfile','flask','requests','mido']:
    try: importlib.import_module(m)
    except Exception: miss.append(m)
print('MISS:'+' '.join(miss))
try:
    import adtof_pytorch
    w=os.path.join(os.path.dirname(adtof_pytorch.__file__),'data','adtof_frame_rnn_pytorch_weights.pth')
    print('ADTOFW:'+('ok' if os.path.isfile(w) and os.path.getsize(w)>1000000 else 'missing'))
except Exception:
    print('ADTOFW:missing')
"@ 2>$null
    $ErrorActionPreference = $_eap
    # 探针无输出（解释器崩溃/torch 段错误）时不能当成「包齐全」——也不能再
    # 对 $null 调 .Trim()（旧逻辑在 EAP=Stop 下直接把脚本弄死）。
    $missLine = @($probeOut | Where-Object { $_ -match "^MISS:" } | Select-Object -Last 1)
    if ($missLine.Count -eq 0) {
        $block += "venv Python 探针无响应（解释器/依赖损坏，需重跑安装）"
    } else {
        $miss = ($missLine[0] -replace "^MISS:","").Trim()
        if ($miss) { $block += "Python 包缺失: $miss" }
    }

    # ADTOF 转录权重（Block：包已装但权重缺失的半装态，跑管线必炸；
    # 拖放链会经 install.ps1 [4/6] 自动补齐 3.6MB 权重）
    $adtofLine = @($probeOut | Where-Object { $_ -match "^ADTOFW:" } | Select-Object -Last 1)
    if ($adtofLine.Count -gt 0 -and $adtofLine[0] -match "missing") {
        $block += "ADTOF 转录权重缺失"
    }
    # Demucs 权重（可选，首次运行可下载）
    if (-not (Test-Path (Join-Path $ROOT "models\demucs_local\955717e8-8726e21a.th"))) {
        $warn += "Demucs 权重缺失（首次运行可自动下载，分离质量降级）"
    }
    # MuseScore（可选，有内置渲染兜底）
    $mscore = $null
    foreach ($p in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $p) { continue }
        $hit = Get-ChildItem "$p\MuseScore*\bin\MuseScore4.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { $mscore = $hit.FullName; break }
    }
    if (-not $mscore) { $warn += "MuseScore 4 未安装（将使用内置渲染，排版降级）" }
    # ffmpeg（可选）
    $ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if (-not $ff -and -not (Test-Path (Join-Path $ROOT "tools\ffmpeg\bin\ffmpeg.exe"))) {
        if (-not ($cfg -and $cfg.ffmpegPath -and (Test-Path $cfg.ffmpegPath))) {
            $warn += "ffmpeg 缺失（部分音频格式转码可能受限）"
        }
    }
    return @{ Block = $block; Warn = $warn }
}

$t0 = Get-Date
$ready = Test-Ready
$block = @($ready.Block)
$warn = @($ready.Warn)
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

if ($warn.Count) {
    Write-Host "[提示] $($warn -join '；')"
}

if ($block.Count -eq 0) {
    Write-Host "[就绪] 环境检测通过（耗时 ${elapsed}s；可选组件缺失不影响主流程）。"
    if ($Launch) {
        Write-Host "[启动] 静默拉起服务..."
        Start-App
    }
    exit 0
}

Write-Host "[缺失] $($block -join '；')"
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

