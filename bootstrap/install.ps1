# bootstrap\install.ps1 — DrumQuill 完整安装器（检测 -> 安装 -> 验证）
# 用法:
#   install.ps1                 正常安装（缺什么装什么）
#   install.ps1 -CheckOnly      只检测不下载，打印状态表；退出码 0=就绪 / 1=缺失
#   install.ps1 -Force          忽略已存在，强制重装关键组件
#   install.ps1 -Offline        模拟无网络：只检测并打印手动下载指引
#   install.ps1 -SkipMuseScore / -SkipFfmpeg / -SkipPip / -SkipWeights   逐项跳过（测试用）
# 退出码: 0 成功 / 1 一般错误 / 2 依赖缺失且安装失败（已打印手动指引）

param(
    [switch]$CheckOnly,
    [switch]$Force,
    [switch]$Offline,
    [switch]$SkipMuseScore,
    [switch]$SkipFfmpeg,
    [switch]$SkipPip,
    [switch]$SkipWeights
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# pip 环境隔离：宿主机残留的 PIP_* 会改变 venv 内 pip 行为——实测 PIP_USER=1 会让
# venv 内一切安装直接失败（--user 在 venv 里不可见）。venv 内安装只认脚本给的参数。
$env:PIP_USER = "0"
Remove-Item "env:PIP_EXISTS_ACTION" -ErrorAction SilentlyContinue
Remove-Item "env:PYTHONUSERBASE" -ErrorAction SilentlyContinue
Remove-Item "env:PIP_TARGET" -ErrorAction SilentlyContinue
Remove-Item "env:PIP_PREFIX" -ErrorAction SilentlyContinue

# 整合包：先修 venv 底座指向（pyvenv.cfg 的 home 无效时改写为包内 runtime\python；幂等）
. (Join-Path $PSScriptRoot "fix_venv_home.ps1")

# ---------------- 路径与配置 ----------------
$ROOT = if ($env:DRUM_AI_ROOT) { $env:DRUM_AI_ROOT } else { Split-Path $PSScriptRoot -Parent }
. (Join-Path $PSScriptRoot "download.ps1")

$CFG_PATH = Join-Path $ROOT "config.json"
$cfg = $null
if (Test-Path $CFG_PATH) {
    try { $cfg = Get-Content $CFG_PATH -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $cfg = $null }
}

# PyPI 镜像链：按顺序尝试，单镜像不可达自动换源（口径对齐 installer.py _mirror_order）
$PYPI_MIRRORS = @(
    "https://mirrors.cloud.tencent.com/pypi/simple",
    "https://mirrors.aliyun.com/pypi/simple",
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://pypi.mirrors.ustc.edu.cn/simple",
    "https://pypi.org/simple"
)
$PYPI_MIRROR = $PYPI_MIRRORS[0]  # 兼容离线指引等对单变量的引用
# 官方主源单链。原第二源 hf-mirror.com/facebook/htdemucs 已 404（RepoNotFound，
# 2026-10-26 curl -I 实测），从链中移除；离线/失败分支的手动指引直接引用本链，自动同步。
$DEMUCS_URLS   = @(
    "https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/955717e8-8726e21a.th"
)
$ADTOF_W_URLS  = @(
    "https://ghproxy.net/https://raw.githubusercontent.com/xavriley/ADTOF-pytorch/main/data/adtof_frame_rnn_pytorch_weights.pth",
    "https://gh-proxy.com/https://raw.githubusercontent.com/xavriley/ADTOF-pytorch/main/data/adtof_frame_rnn_pytorch_weights.pth",
    "https://raw.githubusercontent.com/xavriley/ADTOF-pytorch/main/data/adtof_frame_rnn_pytorch_weights.pth"
)
$ADTOF_ZIP_URLS = @(
    "https://ghproxy.net/https://github.com/xavriley/ADTOF-pytorch/archive/refs/heads/main.zip",
    "https://gh-proxy.com/https://github.com/xavriley/ADTOF-pytorch/archive/refs/heads/main.zip"
)
$FFMPEG_GZ_URL = "https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.1.1/ffmpeg-win32-x64.gz"
$FFMPEG_EXE_URL = "https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.1.1/ffmpeg-win32-x64"

# 期望文件大小（字节）
$SIZE_DEMUCS_TH  = 84141911
$SIZE_ADTOF_PTH  = 3617805
$SIZE_SF3        = 51278610
$SIZE_FFMPEG_EXE = 82797568
$SIZE_BEATS_CKPT = 81058141

# Beat This 拍点权重（torch hub 缓存；与 app/installer.py 的 BEATS_URLS 对齐）
$BEATS_URLS = @("https://cloud.cp.jku.at/public.php/dav/files/7ik4RrBKTS273gp/final0.ckpt")

# 内置 pip 依赖清单（requirements.txt 不存在时使用）
$PIP_PKGS = @(
    "torch", "demucs", "librosa", "pretty_midi", "numpy", "scipy", "soundfile",
    "flask", "requests", "cairosvg", "svglib", "reportlab", "pywebview", "matplotlib"
)

# 以 base64 方式安全执行 venv Python 片段（避开 PS5.1 原生命令行吞双引号问题）
function Invoke-PyB64 {
    param([string]$Code)
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Code))
    $_eap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"   # PS5.1：stderr 重定向的原生调用写 stderr 即终止脚本
    & $venvPy -c "import base64;exec(base64.b64decode('$b64').decode('utf-8'))" 2>$null
    $ErrorActionPreference = $_eap
}

# 按镜像顺序尝试 pip install：任一成功即返回 True，全部失败返回 False
function Invoke-PipInstall {
    param([string[]]$PipArgs, [int]$TimeoutSec = 60)
    foreach ($m in $PYPI_MIRRORS) {
        Write-Host "    pip 源: $m"
        $_eap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"   # 同上：pip 的警告/报错都走 stderr，绝不能因此终止安装
        & $venvPy -m pip install @PipArgs -i $m --timeout $TimeoutSec --retries 2 --prefer-binary 2>$null
        $ErrorActionPreference = $_eap
        if ($LASTEXITCODE -eq 0) { return $true }
        Write-Host "    该源失败，换下一个镜像..."
    }
    return $false
}

# 汇总表
$script:status = @()
function Add-Status { param([string]$Component, [string]$State, [string]$Source, [string]$Note = "")
    $script:status += [pscustomobject]@{ 组件 = $Component; 状态 = $State; 来源 = $Source; 备注 = $Note }
}

$script:missingCount = 0
$script:failedCount  = 0

function SayOfflineManual { param([string]$Title, [string[]]$Urls, [string]$DestPath, [string]$SizeNote = "")
    Write-Host "  [离线模式] 缺失: $Title"
    foreach ($u in $Urls) { Write-Host "      手动下载: $u" }
    Write-Host "      放置到:   $DestPath"
    if ($SizeNote) { Write-Host "      $SizeNote" }
}

# ---------------- 1. Python / venv ----------------
Write-Host "`n[1/6] 检查 Python 虚拟环境..."
$t0 = Get-Date
$venvDir = $null
if ($cfg -and $cfg.venvDir -and (Test-Path (Join-Path $cfg.venvDir "Scripts\python.exe"))) {
    $venvDir = $cfg.venvDir
} elseif (Test-Path (Join-Path $ROOT ".venv\Scripts\python.exe")) {
    $venvDir = Join-Path $ROOT ".venv"
}

$venvPy = if ($venvDir) { Join-Path $venvDir "Scripts\python.exe" } else { $null }
$venvOk = $false
if ($venvPy -and (Test-Path $venvPy)) {
    $_eap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $ver = & $venvPy -c "import sys;print('%d.%d.%d'%sys.version_info[:3])" 2>$null
    $ErrorActionPreference = $_eap
    $maj, $min = [int]($ver -split "\.")[0], [int]($ver -split "\.")[1]
    if ($maj -ge 3 -and $min -ge 9) { $venvOk = $true; Write-Host "  venv 就绪: $venvPy (Python $ver)" }
    else { Write-Host "  [警告] venv Python 版本 $ver 低于 3.9"; $venvPy = $null }
}

if (-not $venvOk) {
    # 计数口径：只有「本次运行结束时仍缺失」才计 missingCount；安装路径的成功/失败
    # 分别体现在状态表「已创建」与 failedCount，避免首装全部成功却因计数不回退而 exit 1。
    if ($CheckOnly -or $Offline) { $script:missingCount++ }
    if ($CheckOnly) {
        Add-Status "Python venv" "缺失" "(待创建)" "需要 Python>=3.9"
    } elseif ($Offline) {
        SayOfflineManual -Title "Python 虚拟环境" -Urls @("https://www.python.org/downloads/") `
            -DestPath "$ROOT\.venv (由本安装器用系统 Python 自动创建)" -SizeNote "安装 Python 3.9+ 后重新运行本脚本"
        Add-Status "Python venv" "待安装(离线)" "系统 Python"
    } else {
        # 用系统 python 创建 venv（红线：只用于建 venv，绝不向系统 python 装包）
        # "py -3" 必须按两段参数调用：旧代码只取了 $parts[0]，把 -3 丢掉后
        # py 启动器可能选到完全不同的 Python 版本。
        $cands = @(
            @{ Name = "py -3"; Exe = "py";  ArgList = @("-3") },
            @{ Name = "python"; Exe = "python"; ArgList = @() }
        )
        $created = $false
        foreach ($c in $cands) {
            $exe = Get-Command $c.Exe -ErrorAction SilentlyContinue
            if (-not $exe) { continue }
            $target = if ($cfg -and $cfg.venvDir) { $cfg.venvDir } else { Join-Path $ROOT ".venv" }
            Write-Host "  用 $($c.Name) 创建 venv -> $target"
            $argList = @($c.ArgList) + @("-m", "venv", $target)
            & $c.Exe @argList
            if ($LASTEXITCODE -eq 0 -and (Test-Path (Join-Path $target "Scripts\python.exe"))) {
                $venvDir = $target; $venvPy = Join-Path $target "Scripts\python.exe"; $created = $true
                break
            }
        }
        if ($created) {
            # 升级 pip（仅 venv 内）
            $null = Invoke-PipInstall -PipArgs @("--upgrade", "pip") -TimeoutSec 30
            $venvOk = $true
            Add-Status "Python venv" "已创建" "$venvDir"
        } else {
            Write-Host "  [错误] 未能创建 venv。请手动安装 Python 3.9+（https://www.python.org/downloads/）后重跑。"
            $script:failedCount++
            Add-Status "Python venv" "失败" "-"
        }
    }
} else {
    Add-Status "Python venv" "就绪" $venvDir
}

# ---------------- 2. pip 依赖 ----------------
Write-Host "`n[2/6] 检查 Python 依赖..."
# cairosvg 需要原生 cairo 库（libcairo-2.dll），缺失时项目内有 render_fallback.py 兜底，按可选处理
$IMPORT_LIST = @("torch","demucs","librosa","pretty_midi","numpy","scipy","soundfile",
                 "flask","requests","svglib","reportlab","webview","matplotlib","adtof_pytorch","beat_this")
$OPT_IMPORTS = @("cairosvg")
$missingImports = @()
$missingOpt = @()
if ($venvPy) {
    $listLit = ($IMPORT_LIST | ForEach-Object { "'" + $_ + "'" }) -join ","
    $optLit  = ($OPT_IMPORTS | ForEach-Object { "'" + $_ + "'" }) -join ","
    $probeCode = @"
import importlib
def probe(mods):
    miss=[]
    for m in mods:
        try: importlib.import_module(m)
        except Exception: miss.append(m)
    return miss
print('HARD:'+' '.join(probe([$listLit])))
print('OPT:'+' '.join(probe([$optLit])))
"@
    $probe = Invoke-PyB64 $probeCode
    $hardLine = $probe | Where-Object { $_ -match "^HARD:" } | Select-Object -Last 1
    $optLine  = $probe | Where-Object { $_ -match "^OPT:" } | Select-Object -Last 1
    $missingImports = @((($hardLine -replace "^HARD:","")) -split "\s+" | Where-Object { $_ })
    $missingOpt     = @((($optLine  -replace "^OPT:","")) -split "\s+" | Where-Object { $_ })
}
if ($missingOpt.Count -gt 0) {
    Write-Host "  [提示] 可选包不可用（不影响主流程，有兜底）: $($missingOpt -join ', ')"
}

if (-not $venvPy) {
    Write-Host "  [跳过] venv 不存在，依赖无法检查"
    Add-Status "pip 依赖" "未检查" "(待 venv 就绪)"
} elseif ($missingImports.Count -eq 0) {
    Write-Host "  全部关键依赖可 import"
    Add-Status "pip 依赖" "就绪" "venv 内"
} else {
    Write-Host "  缺失包: $($missingImports -join ', ')"
    if ($CheckOnly -or $SkipPip -or $Offline) { $script:missingCount++ }
    if ($CheckOnly -or $SkipPip) {
        Add-Status "pip 依赖" "缺失" ($missingImports -join ",")
    } elseif ($Offline) {
        SayOfflineManual -Title "Python 依赖包" -Urls @($PYPI_MIRROR) `
            -DestPath "venv 内（联网重跑 install.ps1 自动安装）"
        Add-Status "pip 依赖" "待安装(离线)" ($missingImports -join ",")
    } else {
        # adtof-pytorch 不在 PyPI：先处理源码包安装
        if ($missingImports -contains "adtof_pytorch") {
            Write-Host "  adtof_pytorch 不在 PyPI，从 GitHub 源码安装..."
            $zipPath = Join-Path $env:TEMP "adtof_src.zip"
            if (Invoke-DrumDownload -UrlChain $ADTOF_ZIP_URLS -OutFile $zipPath `
                    -MaxTimeSec 600 -ManualGuideName "ADTOF-pytorch 源码包") {
                $ext = Join-Path $env:TEMP "adtof_src"
                if (Test-Path $ext) { Remove-Item $ext -Recurse -Force }
                Expand-Archive -Path $zipPath -DestinationPath $ext -Force
                $sub = Get-ChildItem $ext -Directory | Select-Object -First 1
                Write-Host "  pip install 源码目录: $($sub.FullName)"
                if (-not (Invoke-PipInstall -PipArgs @($sub.FullName) -TimeoutSec 30)) {
                    $script:failedCount++
                }
                Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
                Remove-Item $ext -Recurse -Force -ErrorAction SilentlyContinue
            } else {
                $script:failedCount++
            }
        }
        # 其余包走 requirements.txt 或内置清单（过滤 adtof-pytorch，PyPI 上不存在）
        $reqFile = Join-Path $ROOT "requirements.txt"
        $pkgs = @()
        if (Test-Path $reqFile) {
            $pkgs = @((Get-Content $reqFile | Where-Object { $_.Trim() -and $_.Trim() -notmatch "^#" }) |
                      Where-Object { $_ -notmatch "adtof" })
        } else {
            $pkgs = $PIP_PKGS
        }
        if ($pkgs.Count -gt 0) {
            Write-Host "  pip 安装 $($pkgs.Count) 个包（多镜像自动回退）..."
            if (-not (Invoke-PipInstall -PipArgs $pkgs -TimeoutSec 60)) {
                Write-Host "  [错误] 所有镜像均安装失败"
            }
        }
        # 复核
        $probeCode = "import importlib`nmiss=[]`nfor m in [$listLit]:`n    try: importlib.import_module(m)`n    except Exception: miss.append(m)`nprint(' '.join(miss))"
        $probe2 = Invoke-PyB64 $probeCode
        $still = @(($probe2 | Select-Object -Last 1) -split "\s+" | Where-Object { $_ })
        if ($still.Count -eq 0) {
            Write-Host "  依赖安装完成"
            Add-Status "pip 依赖" "已安装" "清华镜像 + ADTOF 源码"
        } else {
            Write-Host "  [错误] 仍无法 import: $($still -join ', ')"
            $script:failedCount++
            Add-Status "pip 依赖" "部分失败" ($still -join ",")
        }
    }
}

# ---------------- 3. Demucs 权重 ----------------
Write-Host "`n[3/6] 检查 Demucs 本地权重..."
$demDir = Join-Path $ROOT "models\demucs_local"
$demTh  = Join-Path $demDir "955717e8-8726e21a.th"
$demYml = Join-Path $demDir "htdemucs.yaml"
$demOk = (Test-FileSizeOK -Path $demTh -ExpectedSize $SIZE_DEMUCS_TH)

if ($demOk) {
    Write-Host "  权重存在: $demTh"
    if (-not (Test-Path $demYml)) {
        Set-Content -Path $demYml -Value "models: ['955717e8']" -Encoding ASCII
        Write-Host "  已补写 $demYml"
    }
    Add-Status "Demucs 权重" "就绪" $demDir
} else {
    if ($CheckOnly -or $SkipWeights -or $Offline) { $script:missingCount++ }
    if ($CheckOnly -or $SkipWeights) {
        Add-Status "Demucs 权重" "缺失" "(约84MB)"
    } elseif ($Offline) {
        SayOfflineManual -Title "Demucs htdemucs 权重（约 84MB）" -Urls $DEMUCS_URLS `
            -DestPath $demTh -SizeNote "约 84 MB"
        Add-Status "Demucs 权重" "待下载(离线)" "-"
    } else {
        $ok = Invoke-DrumDownload -UrlChain $DEMUCS_URLS -OutFile $demTh `
                  -ExpectedSize $SIZE_DEMUCS_TH -MaxTimeSec 1800 -ManualGuideName "Demucs 权重（约84MB）"
        if ($ok) {
            Set-Content -Path $demYml -Value "models: ['955717e8']" -Encoding ASCII
            Add-Status "Demucs 权重" "已下载" $demDir
        } else { $script:failedCount++; Add-Status "Demucs 权重" "失败" "-" }
    }
}

# ---------------- 3b/6. Beat This 拍点权重 ----------------
Write-Host "`n[3b/6] 检查 Beat This 拍点权重..."
$btDir  = Join-Path $ROOT "models\torch\hub\checkpoints"
$btCkpt = Join-Path $btDir "beat_this-final0.ckpt"
$btOk = Test-FileSizeOK -Path $btCkpt -ExpectedSize $SIZE_BEATS_CKPT

if ($btOk) {
    Write-Host "  权重存在: $btCkpt"
    Add-Status "Beat This 权重" "就绪" $btDir
} else {
    if ($CheckOnly -or $SkipWeights -or $Offline) { $script:missingCount++ }
    if ($CheckOnly -or $SkipWeights) {
        Add-Status "Beat This 权重" "缺失" "(约77MB)"
    } elseif ($Offline) {
        SayOfflineManual -Title "Beat This 拍点权重（约 77MB）" -Urls $BEATS_URLS `
            -DestPath $btCkpt -SizeNote "约 77MB；源站较慢，支持断点续传，可多次重跑本脚本接著下"
        Add-Status "Beat This 权重" "待下载(离线)" "-"
    } else {
        # 源站（cloud.cp.jku.at）速度不稳定：大超时 + 断点续传；下载失败不算致命——
        # 扣谱主流程不受影响（按全曲固定速度记谱），UI 里可随时补装。
        $ok = Invoke-DrumDownload -UrlChain $BEATS_URLS -OutFile $btCkpt `
                  -ExpectedSize $SIZE_BEATS_CKPT -MaxTimeSec 3600 -ManualGuideName "Beat This 权重（约77MB）"
        if ($ok) { Add-Status "Beat This 权重" "已下载" $btDir }
        else {
            Write-Host "  [提示] Beat This 权重未下载完：不影响扣谱，谱面按全曲固定速度记；"
            Write-Host "         可稍后在设置页点「补装」继续（支持断点续传）。"
            $script:missingCount++
            Add-Status "Beat This 权重" "未完成(可补装)" "-"
        }
    }
}

# ---------------- 4. ADTOF 权重 ----------------
Write-Host "`n[4/6] 检查 ADTOF 权重..."
$adtofPkgDir = $null
if ($venvPy) {
    $_eap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $adtofPkgDir = & $venvPy -c "import os,adtof_pytorch;print(os.path.dirname(adtof_pytorch.__file__))" 2>$null | Select-Object -Last 1
    $ErrorActionPreference = $_eap
}
$adtofPth = if ($adtofPkgDir) { Join-Path $adtofPkgDir "data\adtof_frame_rnn_pytorch_weights.pth" } else { $null }
$adtofOk = ($adtofPth -and (Test-FileSizeOK -Path $adtofPth -ExpectedSize $SIZE_ADTOF_PTH))

if ($adtofOk) {
    Write-Host "  权重存在: $adtofPth"
    Add-Status "ADTOF 权重" "就绪" $adtofPkgDir
} elseif (-not $adtofPkgDir) {
    Write-Host "  [跳过] venv/adtof_pytorch 不可用，权重无法检查"
    Add-Status "ADTOF 权重" "未检查" "(待 venv 就绪)"
} else {
    if ($CheckOnly -or $SkipWeights -or $Offline) { $script:missingCount++ }
    if ($CheckOnly -or $SkipWeights) {
        Add-Status "ADTOF 权重" "缺失" "(约3.6MB)"
    } elseif ($Offline) {
        SayOfflineManual -Title "ADTOF 权重（约 3.6MB）" -Urls $ADTOF_W_URLS -DestPath $adtofPth
        Add-Status "ADTOF 权重" "待下载(离线)" "-"
    } else {
        $ok = Invoke-DrumDownload -UrlChain $ADTOF_W_URLS -OutFile $adtofPth `
                  -ExpectedSize $SIZE_ADTOF_PTH -MaxTimeSec 300 -ManualGuideName "ADTOF 权重（约3.6MB）"
        if ($ok) { Add-Status "ADTOF 权重" "已下载" $adtofPkgDir }
        else { $script:failedCount++; Add-Status "ADTOF 权重" "失败" "-" }
    }
}

# ---------------- 5. MuseScore 4 + 音色库 ----------------
Write-Host "`n[5/6] 检查 MuseScore 4..."
$mscoreExe = $null
$candPaths = @()
$pf = @($env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ } | Select-Object -Unique
foreach ($p in $pf) { $candPaths += Get-ChildItem "$p\MuseScore*\bin\MuseScore4.exe" -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName } }
if ($candPaths) { $mscoreExe = $candPaths | Select-Object -First 1 }

# 音色库
$sf3Path = Join-Path $ROOT "sound\MS_Basic.sf3"
$sf3Ok = Test-FileSizeOK -Path $sf3Path -ExpectedSize $SIZE_SF3

if ($mscoreExe) {
    Write-Host "  MuseScore: $mscoreExe"
    Add-Status "MuseScore 4" "就绪" $mscoreExe
} else {
    if ($CheckOnly -or $SkipMuseScore -or $Offline) { $script:missingCount++ }
    if ($CheckOnly -or $SkipMuseScore) {
        Add-Status "MuseScore 4" "缺失" "(需 GUI 安装)"
    } elseif ($Offline) {
        SayOfflineManual -Title "MuseScore 4（打谱渲染 PDF 必需）" -Urls @("https://musescore.org/en/download") `
            -DestPath "默认安装到 C:\Program Files\MuseScore 4\bin\MuseScore4.exe"
        Add-Status "MuseScore 4" "待安装(离线)" "-"
    } else {
        $choco = Get-Command choco -ErrorAction SilentlyContinue
        if ($choco) {
            Write-Host "  用 Chocolatey 安装 MuseScore..."
            & choco install musescore -y --no-progress
            if ($LASTEXITCODE -eq 0) {
                $candPaths = @(); foreach ($p in $pf) { $candPaths += Get-ChildItem "$p\MuseScore*\bin\MuseScore4.exe" -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName } }
                if ($candPaths) { $mscoreExe = $candPaths | Select-Object -First 1 }
            }
        }
        if (-not $mscoreExe) {
            Write-Host "  [提示] 自动安装失败（可能需要管理员权限）。请手动到 https://musescore.org/en/download 下载安装 MuseScore 4.x。"
            $script:failedCount++
        }
        Add-Status "MuseScore 4" $(if ($mscoreExe) { "已安装" } else { "需手动" }) $(if ($mscoreExe) { $mscoreExe } else { "musescore.org" })
    }
}

# 音色库：优先从 MuseScore 安装目录复制
if ($sf3Ok) {
    Write-Host "  音色库: $sf3Path"
    Add-Status "MS Basic.sf3" "就绪" "sound\"
} else {
    if ($CheckOnly -or $SkipMuseScore -or $Offline) { $script:missingCount++ }
    $sf3Src = if ($mscoreExe) { Join-Path (Split-Path (Split-Path $mscoreExe -Parent) -Parent) "sound\MS Basic.sf3" } else { $null }
    if ($CheckOnly -or $SkipMuseScore) {
        Add-Status "MS Basic.sf3" "缺失" "(约51MB)"
    } elseif ($Offline) {
        SayOfflineManual -Title "MuseScore 基础音色库 MS Basic.sf3（约 51MB）" -Urls @("https://musescore.org/en/download") `
            -DestPath $sf3Path -SizeNote "安装 MuseScore 后可从其安装目录 sound\MS Basic.sf3 复制"
        Add-Status "MS Basic.sf3" "待下载(离线)" "-"
    } else {
        if ($sf3Src -and (Test-Path $sf3Src)) {
            Write-Host "  从 MuseScore 目录复制音色库: $sf3Src"
            $sf3Dir = Split-Path $sf3Path -Parent
            if (-not (Test-Path $sf3Dir)) { New-Item -ItemType Directory -Force $sf3Dir | Out-Null }
            Copy-Item $sf3Src $sf3Path -Force
            $sf3Ok = Test-FileSizeOK -Path $sf3Path -ExpectedSize $SIZE_SF3
        } else {
            Write-Host "  [提示] 未在 MuseScore 目录找到 MS Basic.sf3，请手动安装 MuseScore 后重跑本脚本。"
            $script:failedCount++
        }
        Add-Status "MS Basic.sf3" $(if ($sf3Ok) { "已复制" } else { "需手动" }) $(if ($sf3Ok) { $sf3Src } else { "-" })
    }
}

# ---------------- 6. ffmpeg ----------------
Write-Host "`n[6/6] 检查 ffmpeg..."
$ffmpegExe = $null
if ($cfg -and $cfg.ffmpegPath -and (Test-Path $cfg.ffmpegPath)) { $ffmpegExe = $cfg.ffmpegPath }
if (-not $ffmpegExe) { $g = Get-Command ffmpeg -ErrorAction SilentlyContinue; if ($g) { $ffmpegExe = $g.Source } }
$ffLocal = Join-Path $ROOT "tools\ffmpeg\bin\ffmpeg.exe"

if ($ffmpegExe) {
    Write-Host "  ffmpeg: $ffmpegExe"
    Add-Status "ffmpeg" "就绪" $ffmpegExe
} else {
    if ($CheckOnly -or $SkipFfmpeg -or $Offline) { $script:missingCount++ }
    if ($CheckOnly -or $SkipFfmpeg) {
        Add-Status "ffmpeg" "缺失" "(音频转码)"
    } elseif ($Offline) {
        SayOfflineManual -Title "ffmpeg Windows 静态版" -Urls @($FFMPEG_GZ_URL, $FFMPEG_EXE_URL) `
            -DestPath $ffLocal -SizeNote "解压后约 80MB"
        Add-Status "ffmpeg" "待下载(离线)" "-"
    } else {
        $gzPath = Join-Path $ROOT "tools\ffmpeg\ffmpeg.gz"
        $ok = Invoke-DrumDownload -UrlChain @($FFMPEG_GZ_URL, $FFMPEG_EXE_URL) -OutFile $gzPath `
                  -ExpectedSize 0 -MaxTimeSec 1200 -ManualGuideName "ffmpeg 静态构建"
        if ($ok) {
            # gz 解压；若下到的是未压缩 exe（第二源）则直接复制
            $dstDir = Split-Path $ffLocal -Parent
            if (-not (Test-Path $dstDir)) { New-Item -ItemType Directory -Force $dstDir | Out-Null }
            # 只读 2 个字节判断魔数（旧代码 ReadAllBytes 会把整个 ~80MB 读进内存，空文件还会抛）
            $head = @(0, 0)
            try {
                $fs = [System.IO.File]::OpenRead($gzPath)
                try { $head[0] = $fs.ReadByte(); $head[1] = $fs.ReadByte() } finally { $fs.Dispose() }
            } catch { Write-Host "  [警告] 无法读取下载文件：$($_.Exception.Message)" }
            if ($head[0] -eq 0x1F -and $head[1] -eq 0x8B) {
                Expand-GzipFile -GzPath $gzPath -DestPath $ffLocal | Out-Null
            } else {
                Copy-Item $gzPath $ffLocal -Force
            }
            Remove-Item $gzPath -Force -ErrorAction SilentlyContinue
            $ffOk = $null
            if (Test-Path $ffLocal) {
                $_eap = $ErrorActionPreference
                $ErrorActionPreference = "Continue"
                $ffOk = (& $ffLocal -version 2>$null)
                $ErrorActionPreference = $_eap
            }
            if ($ffOk) {
                $ffmpegExe = $ffLocal
                # 写入 config.json 的 ffmpegPath
                try {
                    if ($null -eq $cfg) {
                        # 原文件解析失败（例如 JSON 注释/语法错）：绝不覆盖，
                        # 否则会把用户的 venvDir / dataDir / adtofDir 全冲掉。
                        Write-Host "  [警告] config.json 无法解析，为避免覆盖已有配置，未写入 ffmpegPath。"
                        Write-Host "         请手动在 config.json 里加： `"ffmpegPath`": `"$ffLocal`""
                    } else {
                        $obj = $cfg
                        $obj | Add-Member -NotePropertyName ffmpegPath -NotePropertyValue $ffLocal -Force
                        # PS 5.1 的 -Encoding UTF8 会写 BOM，而 Python 侧 json.load 遇 BOM 直接抛异常，
                        # 等于整份配置被静默丢弃。这里显式写无 BOM 的 UTF-8。
                        $json = $obj | ConvertTo-Json -Depth 5
                        [System.IO.File]::WriteAllText($CFG_PATH, $json, (New-Object System.Text.UTF8Encoding($false)))
                        Write-Host "  已写入 config.json: ffmpegPath"
                    }
                } catch { Write-Host "  [警告] 写入 config.json 失败，请手动添加 ffmpegPath" }
            }
        }
        if ($ffmpegExe) { Add-Status "ffmpeg" "已安装" $ffLocal }
        else { $script:failedCount++; Add-Status "ffmpeg" "失败" "npmmirror" }
    }
}

# ---------------- 汇总 ----------------
Write-Host "`n==================== 依赖就绪表 ===================="
$script:status | Format-Table -AutoSize | Out-String | Write-Host
Write-Host ("缺失 {0} 项，安装失败 {1} 项" -f $script:missingCount, $script:failedCount)

if ($CheckOnly) {
    if ($script:missingCount -eq 0 -and $script:failedCount -eq 0) { Write-Host "结论: 全部就绪"; exit 0 }
    Write-Host "结论: 存在缺失项"; exit 1
}
if ($script:failedCount -gt 0) {
    Write-Host "结论: 有组件未能自动装好，请按上方 [需要手动下载] 指引处理后重跑本脚本。"
    exit 2
}
if ($script:missingCount -gt 0) {
    Write-Host "结论: 仍有缺失项（可能是离线模式或跳过开关）。"
    if ($Offline) { exit 2 }
    exit 1
}
Write-Host "结论: 全部依赖就绪，可以启动（运行 bootstrap\ensure.ps1 -Launch）。"
exit 0

