# One-click drum transcription pipeline (DrumQuill 扒鼓谱) - portable + fallback chain
# Usage:
#   powershell -ExecutionPolicy Bypass -File run_pipeline.ps1 -SongPath "<song>" [-BPM 156]
#            [-SkipSeparation] [-SkipMuseScore] [-SkipFfmpeg] [-SkipAudio]
# Paths are derived from script location ($PSScriptRoot) or $env:DRUM_AI_ROOT,
# then overridden by config.json (see docs/fallback_matrix.md).
param(
    [Parameter(Mandatory = $true)][string]$SongPath,
    [double]$BPM = 0,
    [switch]$SkipSeparation,
    [ValidateSet('auto','cpu','cuda','gpu')][string]$Device = '',
    [switch]$SkipMuseScore,
    [switch]$SkipFfmpeg,
    [switch]$SkipAudio,
    # 只要谱子（跳过音频合成）
    [switch]$SkipScore,
    # 只要分离鼓声（只跑 Demucs，不转录、不排版）
    [switch]$OnlyStems,
    # 分离模型名（config demucsModel；缺省 htdemucs）
    [string]$SepModel = ""
)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ---------- 0. ROOT + config resolution -----------------------------------
$ROOT = $env:DRUM_AI_ROOT
if (-not $ROOT) { $ROOT = $PSScriptRoot }
if (-not $ROOT) { $ROOT = (Get-Location).Path }

$cfg = @{}
$cfgPath = Join-Path $ROOT "config.json"
if (Test-Path $cfgPath) {
    try {
        $raw = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($p in $raw.PSObject.Properties) { $cfg[$p.Name] = $p.Value }
    } catch {
        Write-Host "[!] config.json 解析失败：$($_.Exception.Message) —— 按缺省路径继续"
    }
}
function Cfg($name, $default) {
    if ($cfg.Contains($name) -and $null -ne $cfg[$name] -and "$($cfg[$name])" -ne "") { return $cfg[$name] }
    return $default
}

$dataDir   = Cfg "dataDir"  $ROOT
$songsDir  = Join-Path $dataDir "songs"
$midiDir   = Join-Path $dataDir "midis"
$outDir    = Join-Path $dataDir "output"
$stemsRoot = Join-Path $dataDir "stems"
$soundDir  = Cfg "soundDir" (Join-Path $ROOT "sound")
$modelsDir = Cfg "modelsDir" (Join-Path $ROOT "models")
$bpmFallback = [double](Cfg "bpmFallback" 120)
# scripts ship next to this pipeline file, not under the data root
$scrDir    = Join-Path $PSScriptRoot "scripts"

# venv python: config venvDir -> ROOT\.venv -> exit 2 (bootstrap installs it)
$venvDir = Cfg "venvDir" (Join-Path $ROOT ".venv")
# venvDir 允许写成 python.exe / pythonw.exe 的完整路径（config.json.example 的写法之一），
# 这里统一退回 venv 根目录，否则会拼出 ...\pythonw.exe\Scripts\python.exe 直接报“未找到虚拟环境”
if ("$venvDir" -match '(?i)[\\/]Scripts[\\/]pythonw?\.exe$') {
    $venvDir = Split-Path (Split-Path $venvDir -Parent) -Parent
}
$venvPy  = Join-Path $venvDir "Scripts\python.exe"
# 整合包：pyvenv.cfg 的 home 在别的机器上无效时改写为包内 runtime\python（幂等，开发机空操作）
. (Join-Path $PSScriptRoot "bootstrap\fix_venv_home.ps1") -Root $ROOT
if (-not (Test-Path $venvPy)) {
    Write-Host "[!] 未找到虚拟环境 Python：$venvPy"
    Write-Host "    请先运行 bootstrap\install.ps1 安装依赖；或在 config.json 里把 venvDir 指向已有虚拟环境。"
    exit 2
}

# MuseScore: an explicit config museScorePath is honored as-is — if it does not
# exist, MuseScore is treated as unavailable (self-made renderer takes over).
# Auto-discovery only runs when the config field is absent.
$msConfigured = $cfg.Contains("museScorePath") -and "$($cfg["museScorePath"])" -ne ""
if ($msConfigured) {
    $mscore = $cfg["museScorePath"]
    if (-not (Test-Path $mscore)) {
        Write-Host "[!] config.json 指定的 MuseScore 不存在：$mscore —— 后续将使用自研谱面渲染。"
        $mscore = $null
    }
} else {
    $mscore = $null
    foreach ($cand in @(
        "C:\Program Files\MuseScore 4\bin\MuseScore4.exe",
        "C:\Program Files (x86)\MuseScore 4\bin\MuseScore4.exe",
        "C:\Program Files\MuseScore 3\bin\MuseScore3.exe"
    )) { if (Test-Path $cand) { $mscore = $cand; break } }
}

# ffmpeg: explicit-but-missing config means "no ffmpeg" (keep WAV);
# discovery on PATH only when the field is absent.
$ffConfigured = $cfg.Contains("ffmpegPath") -and "$($cfg["ffmpegPath"])" -ne ""
if ($ffConfigured) {
    $ffmpeg = $cfg["ffmpegPath"]
    if (-not (Test-Path $ffmpeg)) { $ffmpeg = $null }
} else {
    # 整合包：优先用包内自带的 ffmpeg（tools\ffmpeg\bin），其次才搜索 PATH
    $ffLocal = Join-Path $ROOT "tools\ffmpeg\bin\ffmpeg.exe"
    if (Test-Path $ffLocal) {
        $ffmpeg = $ffLocal
    } else {
        $fc = Get-Command ffmpeg -ErrorAction SilentlyContinue
        $ffmpeg = if ($fc) { $fc.Source } else { $null }
    }
}

# ADTOF source: env DRUM_AI_ADTOF_DIR -> config adtofDir -> ROOT candidates
$adtofDir = $env:DRUM_AI_ADTOF_DIR
if (-not $adtofDir) { $adtofDir = Cfg "adtofDir" "" }
if (-not $adtofDir -or -not (Test-Path $adtofDir)) {
    $adtofDir = $null
    foreach ($cand in @((Join-Path $ROOT "adtof"), (Join-Path $ROOT "ADTOF-pytorch"), (Join-Path $ROOT "src\ADTOF-pytorch"))) {
        if (Test-Path $cand) { $adtofDir = $cand; break }
    }
}

# ---------- song sanity ----------------------------------------------------
if (-not (Test-Path $SongPath)) {
    Write-Host "[!] 找不到音频文件：$SongPath"
    exit 1
}
$song = [IO.Path]::GetFileNameWithoutExtension($SongPath)
foreach ($d in @($songsDir, $midiDir, $outDir, $stemsRoot, $soundDir)) {
    New-Item -ItemType Directory -Force $d | Out-Null
    # PowerShell 5.1 在父路径被同名文件占据时会静默空返回，必须显式验证目录真的创建出来
    if (-not (Test-Path $d)) {
        Write-Host "[!] 数据目录无法创建：$d"
        Write-Host "    请检查 config.json 的 dataDir（当前：$dataDir）——该路径可能被同名文件占据，或当前账号无写权限。"
        exit 1
    }
}
Write-Host "[1/6] 歌曲：$song"

# ---------- 1. BPM detect (fallback: config bpmFallback) -------------------
if ($BPM -le 0) {
    Write-Host "[*] 自动检测速度（librosa）..."
    try {
        $out = & $venvPy (Join-Path $scrDir "detect_bpm.py") $SongPath
        $BPM = [double](($out | Select-Object -Last 1).ToString().Trim())
    } catch { $BPM = 0 }
    if ($BPM -le 0 -or $BPM -gt 300) {
        Write-Host "[!] 速度检测失败，使用默认速度 $bpmFallback BPM（可加 -BPM 120 手动指定）"
        $BPM = $bpmFallback
    }
}
Write-Host "[*] BPM = $BPM"

# ---------- 2. Demucs separation (fallback: whole-song transcription) ------
# 分离模型在分离之前就定下来，产物查找必须跟随这个模型目录，
# 否则切到 mdx_extra 时会去 stems\htdemucs 里翻上一个作业留下的鼓轨。
$repo = Join-Path $modelsDir "demucs_local"
$sepModel = if ("$SepModel" -ne "") { $SepModel } else { "$(Cfg 'demucsModel' 'htdemucs')" }
if (-not (Test-Path (Join-Path $repo ($sepModel + ".yaml")))) {
    Write-Host "[!] 未找到分离模型 「$sepModel」的配置文件，回退到 htdemucs。"
    $sepModel = "htdemucs"
}
$drumsWavPath = $null
if ($SkipSeparation) {
    # 只认当前模型 + 当前歌曲的那一份产物，避免误用别的歌的残留
    $candidates = @(
        (Join-Path $stemsRoot (Join-Path $sepModel (Join-Path $song "drums.wav")))
    )
    $found = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $found) {
        $found = Get-ChildItem (Join-Path $stemsRoot (Join-Path $sepModel "*\drums.wav")) -ErrorAction SilentlyContinue |
                 Where-Object { $_.Directory.Name -eq $song } |
                 Sort-Object LastWriteTime | Select-Object -Last 1 |
                 ForEach-Object { $_.FullName }
    }
    if (-not $found) {
        Write-Host "[!] -SkipSeparation 但未找到已有分离产物：$stemsRoot\$sepModel\$song\drums.wav"
        exit 1
    }
    $drumsWavPath = $found
    Write-Host "[*] 使用已有分离鼓轨：$drumsWavPath"
} else {
    # ---- 分离设备与并行度 ------------------------------------------------
    # Demucs -j 是“同时处理的音频分段数”（独立进程，各带一份模型）。
    # CPU 参考（4 线程、20 秒素材、模型已缓存）：
    #   -j 1 -> 40.0s（并行度 ~1.8 核）  -j 2 -> 31.3s（~3.0 核）  -j 4 -> 31.2s 但内存翻倍
    # GPU 参考（4GB 显存）：-d cuda 约 10s（同样素材），显存 ~1.2GB，段长 5 与 7 速度接近。
    # 设备选择：config demucsDevice = auto(默认) | cpu | cuda
    $logicalCpu = [int]$env:NUMBER_OF_PROCESSORS
    if ($logicalCpu -le 0) { $logicalCpu = 2 }
    $demucsJobs = [int](Cfg "demucsJobs" ([Math]::Max(1, [Math]::Min(4, [int][Math]::Floor($logicalCpu / 2)))))
    if ($demucsJobs -lt 1) { $demucsJobs = 1 }
    if ($demucsJobs -gt 4) { $demucsJobs = 4 }

    $devicePref = "$(Cfg 'demucsDevice' 'auto')".ToLower()
    if ("$Device" -ne "") { $devicePref = "$Device".ToLower(); if ($devicePref -eq "gpu") { $devicePref = "cuda" } }
    $gpuPy = Cfg "gpuPython" ""
    if (-not $gpuPy) {
        $cand = Join-Path $ROOT ".venv-gpu\Scripts\python.exe"
        if (Test-Path $cand) { $gpuPy = $cand }
    }
    $gpuOk = $false
    $gpuName = ""
    $gpuLadder = @(7, 5, 3)
    if ($devicePref -ne "cpu" -and $gpuPy -and (Test-Path $gpuPy)) {
        $probeScript = Join-Path $scrDir "gpu_probe.py"
        if (Test-Path $probeScript) {
            # PS 5.1 陷阱：$ErrorActionPreference="Stop" 时，重定向了 stderr 的原生进程只要往 stderr
            # 写任何一行（进度/警告/空行），该行就会变成 ErrorRecord 并终止整个管线。原生调用一律临时降级。
            $_eap = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            $probeOut = & $gpuPy $probeScript 2>$null
            $ErrorActionPreference = $_eap
            $line = ($probeOut | Select-Object -Last 1)
            if ($line) {
                try {
                    $pj = $line | ConvertFrom-Json
                    if ($pj.available -eq $true) {
                        $gpuOk = $true
                        if ($pj.name) { $gpuName = "$($pj.name)" }
                        # 段长由显存决定（小卡别一上来就 OOM），失败时按递减阶梯重试
                        if ($pj.ladder -and @($pj.ladder).Count -ge 1) {
                            $gpuLadder = @(@($pj.ladder) | ForEach-Object { [int]$_ })
                        } elseif ($pj.segment) {
                            $gpuLadder = @([int]$pj.segment)
                        }
                        Write-Host ("[*] GPU 探测：{0} · 显存 {1}MB · 算力 {2} · torch {3} / CUDA {4}" -f `
                            $pj.name, $pj.vram_mb, $pj.cc, $pj.torch, $pj.cuda)
                        if ($pj.arch_ok -eq $false) {
                            Write-Host ("[!] 当前 torch 轮子里没有 sm_{0} 的编译目标，GPU 很可能会失败；可重装 GPU 组件。" -f "$($pj.cc)".Replace(".", ""))
                        }
                    } else {
                        Write-Host ("[*] GPU 探测：不可用（{0}）—— 本次使用 CPU" -f $pj.reason)
                    }
                } catch {
                    Write-Host "[!] GPU 探测输出解析失败，按 CPU 处理。"
                }
            }
        }
    }
    # config 里显式写了 gpuSegment 就只试那一个（用户明确指定优先）
    $cfgGpuSeg = Cfg "gpuSegment" ""
    if ($gpuOk -and "$cfgGpuSeg" -ne "") {
        $gpuLadder = @([int]$cfgGpuSeg)
    }

    # 尝试序列：GPU 按"段长递减"阶梯 → 失败回落 CPU → CPU 再用更小段长。
    # 注意 htdemucs 的 --segment 只接受整数且上限 7.8（旧代码重试用的 10 会立刻报错）。
    $attempts = @()
    if ($gpuOk) {
        foreach ($sg in $gpuLadder) {
            $tag = if (@($gpuLadder).Count -gt 1) { "GPU · 段长${sg}s" } else { "GPU" }
            $attempts += @{ Exe = $gpuPy; Dev = "cuda"; Jobs = 1; Seg = [int]$sg; Tag = $tag }
        }
    }
    $attempts += @{ Exe = $venvPy; Dev = "cpu"; Jobs = $demucsJobs; Seg = 5; Tag = "CPU" }
    $attempts += @{ Exe = $venvPy; Dev = "cpu"; Jobs = $demucsJobs; Seg = 4; Tag = "CPU 重试" }

    # 分离模型：命令行 > config.demucsModel > htdemucs（已在上面解析）
    Write-Host "[2/6] 分离鼓轨（Demucs $sepModel · $($attempts[0].Tag)）..."
    $separated = $false
    if (Test-Path $repo) {
        foreach ($a in $attempts) {
            if ($a.Dev -eq "cuda") {
                Write-Host "[*] 分离设备：GPU（$gpuName · 段长 $($a.Seg)s）"
            } else {
                Write-Host "[*] 分离设备：CPU（并行 $($a.Jobs) 段，段长 $($a.Seg)s）"
            }
            & $a.Exe -m demucs $SongPath -n $sepModel --repo $repo -d $a.Dev -j $a.Jobs `
                --segment $a.Seg --overlap 0.1 --two-stems=drums -o $stemsRoot
            if ($LASTEXITCODE -eq 0) { $separated = $true; break }
            if ($a -ne $attempts[-1]) { Write-Host "[!] 分离失败（exit $LASTEXITCODE），改用下一种配置重试..." }
        }
    } else {
        Write-Host "[!] 未找到本地分离模型目录：$repo"
    }
    if ($separated) {
        # 产物目录 = stems\<模型>\<歌曲>\drums.wav（Demucs 按模型建子目录）
        $want = Join-Path $stemsRoot (Join-Path $sepModel (Join-Path $song "drums.wav"))
        if (Test-Path $want) {
            $drumsWavPath = $want
        } else {
            $got = Get-ChildItem (Join-Path $stemsRoot (Join-Path $sepModel "*\drums.wav")) -ErrorAction SilentlyContinue |
                   Where-Object { $_.Directory.Name -eq $song } |
                   Sort-Object LastWriteTime | Select-Object -Last 1
            if ($got) { $drumsWavPath = $got.FullName }
        }
    }
    if (-not $drumsWavPath) {
        Write-Host "[!] 鼓轨分离失败（模型缺失或 Demucs 运行出错）。"
        Write-Host "[!] 已降级：对整曲直接转录（精度略降、耗时更长）。"
        $drumsWavPath = $SongPath
    } else {
        Write-Host "[*] 分离鼓轨：$drumsWavPath"
    }
}

# ---------- 2b. 只分离鼓声模式：把 drums 轨道导出成成品，直接结束 ----------
if ($OnlyStems) {
    if (-not $drumsWavPath -or $drumsWavPath -eq $SongPath) {
        Write-Host "[!] 分离没成功，拿不到独立鼓轨，无法产出「只要鼓声」。"
        Write-Host "    请检查 Demucs 权重/设备设置后重试；也可以改用完整模式（会回退为整曲转录）。"
        exit 1
    }
    $drumBase = Join-Path $outDir "${song}_鼓声"
    $drumWavOut = "$drumBase.wav"
    Copy-Item $drumsWavPath $drumWavOut -Force
    $drumFinal = $drumWavOut
    if ($ffmpeg -and -not $SkipFfmpeg) {
        & $ffmpeg -y -i $drumWavOut -codec:a libmp3lame -qscale:a 2 "$drumBase.mp3" | Out-Null
        if ($LASTEXITCODE -eq 0 -and (Test-Path "$drumBase.mp3")) {
            Remove-Item $drumWavOut -Force -ErrorAction SilentlyContinue
            $drumFinal = "$drumBase.mp3"
        }
    }
    $drumStemDir = Join-Path $stemsRoot (Join-Path $sepModel $song)
    Write-Host ""
    Write-Host "==== DONE ===="
    Write-Host "鼓声     : $drumFinal"
    Write-Host "中间产物 : $drumStemDir"
    Write-Host "只做了鼓轨分离，没有转录和排版。需要谱子请用「完整」或「只要谱子」模式。"
    exit 0
}

# ---------- 3. ADTOF transcription -----------------------------------------
$rawMid = Join-Path $midiDir "$song.mid"
Write-Host "[3/6] 转录鼓点为 MIDI（ADTOF）..."
# ADTOF may be a source checkout (cd into it) OR pip-installed into the venv
# (importable from anywhere -> run without Push-Location). Directory is optional.
$adtofCwd = $null
if ($adtofDir) {
    $adtofCwd = $adtofDir
} else {
    & $venvPy -c "import adtof_pytorch" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[*] 未找到 ADTOF 源码目录，使用 venv 内已安装的 adtof_pytorch 模块。"
    } else {
        Write-Host "[!] 未找到 ADTOF 转录模块。"
        Write-Host "    请先运行 bootstrap\install.ps1 安装 ADTOF；或在 config.json 配置 adtofDir 指向 ADTOF-pytorch 源码目录。"
        exit 1
    }
}
if ($adtofCwd) { Push-Location $adtofCwd }
try {
    # 转录器：scripts\transcribe.py（逐类阈值 + 用峰值强度写力度）
    # 阈值优先级：config.adtofThresholds > 该分离模型的实测调优值 > 通用出厂值
    $transPy = Join-Path $scrDir "transcribe.py"
    $thArg = @()
    $cfgTh = "$(Cfg 'adtofThresholds' '')"
    if ($cfgTh) { $thArg = @("--thresholds", $cfgTh) }
    if (Test-Path $transPy) {
        & $venvPy $transPy --audio $drumsWavPath --out $rawMid --model $sepModel @thArg `
            --fps 100 --device cpu --report (Join-Path $midiDir "$song.转录统计.json")
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $rawMid)) {
            Write-Host "[!] 新转录器失败（exit $LASTEXITCODE），回退到 ADTOF 官方 CLI…"
            & $venvPy -m adtof_pytorch.cli --audio $drumsWavPath --out $rawMid `
                --thresholds 0.22,0.20,0.40,0.22,0.34 --fps 100 --device cpu
        }
    } else {
        & $venvPy -m adtof_pytorch.cli --audio $drumsWavPath --out $rawMid `
            --thresholds 0.22,0.20,0.40,0.22,0.34 --fps 100 --device cpu
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $rawMid)) {
        Write-Host "[!] 转录失败（exit $LASTEXITCODE）。建议：确认音频可正常播放；若刚发生分离失败，整曲转录耗时较长请耐心等待。"
        exit 1
    }
} finally { if ($adtofCwd) { Pop-Location } }

# ---------- 3b. 规则引擎：抖动去重 / 最短时值 -------------------------------
$postDedupe = [double](Cfg "postDedupeMs" 50)
$postMinLen = [double](Cfg "postMinLenMs" 30)
$postSwing  = [double](Cfg "postSwing" 0)
if ($postDedupe -gt 0 -or $postMinLen -gt 0) {
    $ppTmp = Join-Path $midiDir "$song._pp.mid"
    & $venvPy (Join-Path $scrDir "postprocess.py") $rawMid $ppTmp --dedupe-ms $postDedupe --min-len-ms $postMinLen --swing 0
    if ($LASTEXITCODE -eq 0 -and (Test-Path $ppTmp)) {
        Move-Item $ppTmp $rawMid -Force
    } else {
        Write-Host "[!] 后处理失败（不影响主流程），继续使用原始转录结果。"
    }
}

# ---------- 3c. 速度八度校验（避免 74 BPM 的歌被当成 148）-------------------
# 判据：军鼓踩 2、4 拍 -> 相邻军鼓间隔应接近 2 拍；检测值若偏快一倍，间隔会算成 4 拍。
$bpmSanity = "$(Cfg 'bpmSanity' 'true')" -ne "false"
if ($bpmSanity -and (Test-Path (Join-Path $scrDir "choose_bpm.py"))) {
    $_eap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"   # 同上：stderr 重定向的原生调用不允许打断管线
    $bpmJson = & $venvPy (Join-Path $scrDir "choose_bpm.py") $rawMid $BPM --json 2>$null | Select-Object -Last 1
    $ErrorActionPreference = $_eap
    if ($bpmJson -and $bpmJson.Trim().StartsWith("{")) {
        try {
            $bpmInfo = $bpmJson | ConvertFrom-Json
            if ($bpmInfo.chosen -and [math]::Abs([double]$bpmInfo.chosen - $BPM) -gt 0.5) {
                $oldBpm = $BPM
                $BPM = [double]$bpmInfo.chosen
                $how = [string]$bpmInfo.label
                if ($how -like "*half*") {
                    Write-Host "[*] 速度八度校验：检测 $oldBpm 判为偏快一倍（军鼓间隔 $($bpmInfo.snare_interval_s) 秒 ≈4 拍），改用 $BPM BPM"
                } elseif ($how -like "*double*") {
                    Write-Host "[*] 速度八度校验：检测 $oldBpm 判为偏慢一倍（军鼓间隔 $($bpmInfo.snare_interval_s) 秒 ≈1 拍），改用 $BPM BPM"
                } else {
                    Write-Host "[*] 速度精调：检测 $oldBpm → 按起音贴合度校正为 $BPM BPM $($bpmInfo.refit)"
                }
            } else {
                Write-Host "[*] 速度校验：$BPM BPM 通过（军鼓间隔 $($bpmInfo.snare_interval_s) 秒）"
            }
        } catch {
            Write-Host "[!] 速度八度校验结果解析失败，沿用 $BPM BPM"
        }
    }
}

# ---------- 3d. 拍栅格对齐（Beat This）:/ 按真实拍点量化，替代固定 BPM ------
$beatGrid = "$(Cfg 'beatGrid' 'true')" -ne "false"
$beatsJson = Join-Path $midiDir "$song.beats.json"
$planJson  = Join-Path $midiDir "$song.beats_plan.json"
$beatAligned = $false
if ($beatGrid -and (Test-Path (Join-Path $scrDir "track_beats.py")) -and (Test-Path (Join-Path $scrDir "align_to_beats.py"))) {
    $beatsCacheFresh = $false
    if (Test-Path $beatsJson) {
        # 缓存只在比歌曲文件新时才复用；歌曲更新后必须重跟拍点
        $beatsCacheFresh = (Get-Item $beatsJson).LastWriteTime -ge (Get-Item $SongPath).LastWriteTime
        if (-not $beatsCacheFresh) { Write-Host "[*] beats.json 比歌曲文件旧，重新跟踪节拍..." }
    }
    if (-not $beatsCacheFresh) {
        Write-Host "[*] 跟踪节拍与强拍（Beat This，一次约 30 秒，结果会缓存）..."
        $_eap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"   # 同上：track_beats 的下载进度/警告都走 stderr，绝不能因此终止管线
        & $venvPy (Join-Path $scrDir "track_beats.py") $SongPath $beatsJson 2>&1 | Select-Object -Last 2 | ForEach-Object { Write-Host "    $_" }
        $ErrorActionPreference = $_eap
    }
    if (Test-Path $beatsJson) {
        $bgMid = Join-Path $midiDir "$song.beats.mid"
        $bgAl  = Join-Path $midiDir "$song.beats_al.mid"
        & $venvPy (Join-Path $scrDir "align_to_beats.py") $rawMid $beatsJson $bgMid $planJson
        if ($LASTEXITCODE -eq 0 -and (Test-Path $bgMid)) {
            $beatAligned = $true
            if (Test-Path $planJson) {
                try { $planInfo = Get-Content $planJson -Raw -Encoding UTF8 | ConvertFrom-Json
                      if ($planInfo.bpm_median) { $BPM = [double]$planInfo.bpm_median } } catch {}
            }
            Write-Host "[*] 已按真实拍点对齐（跳过固定 BPM 量化与小节相位搜索）"
        } else {
            Write-Host "[!] 拍栅格对齐失败，回落到固定 BPM 路线"
        }
    }
}

# ---------- 4. tempo/align/musicxml chain (no data fallback, friendly exit) -
$bpmInt = [int]$BPM
$bpmMid = Join-Path $midiDir "${song}_${bpmInt}.mid"
$alMid  = Join-Path $midiDir "${song}_${bpmInt}_al.mid"
if ($beatAligned) {
    $bpmMid = Join-Path $midiDir "$song.beats.mid"
    $alMid  = Join-Path $midiDir "$song.beats_al.mid"
}
$xml    = Join-Path $outDir "$song.musicxml"
Write-Host "[4/6] 校正速度与小节线（@ $BPM BPM）..."
if (-not $beatAligned) {
& $venvPy (Join-Path $scrDir "fix_tempo_tick.py") $rawMid $bpmMid $BPM
if ($LASTEXITCODE -ne 0) {
    Write-Host "[!] fix_tempo_tick 失败（exit $LASTEXITCODE）—— MIDI 数据无法降级处理。请把上方输出发给开发者；中间文件在 $midiDir。"
    exit 1
}
}
# 量化：相位对齐 + 软吸附（只吸偏离栅格 <tol 的音符）。实测硬吸附会掉 6.5 点 F1，
# 软吸附 tol=0.25 时与"完全不量化"的准确度一致，同时约 55% 音符精确落在整数 16 分上。
$quantTol = [double](Cfg "quantizeTol" 0.25)
if ($quantTol -lt 0) { $quantTol = 0 }
if ($quantTol -gt 0.5) { $quantTol = 0.5 }
$fixAlignExit = 0
if (-not $beatAligned) {
    & $venvPy (Join-Path $scrDir "fix_align.py") $bpmMid $alMid $BPM --auto --tol $quantTol
    $fixAlignExit = $LASTEXITCODE   # 原生调用后立即存专用变量，避免被后续调用覆盖
} else {
    Copy-Item $bpmMid $alMid -Force
}
if ($fixAlignExit -eq 0 -and $postSwing -gt 0 -and (Test-Path $alMid)) {
    $swTmp = Join-Path $midiDir "$song._swing.mid"
    & $venvPy (Join-Path $scrDir "postprocess.py") $alMid $swTmp --dedupe-ms 0 --min-len-ms 0 --swing $postSwing
    $swingExit = $LASTEXITCODE   # 摇摆失败不得污染 fix_align 的退出码判断
    if ($swingExit -eq 0 -and (Test-Path $swTmp)) { Move-Item $swTmp $alMid -Force }
    else { Write-Host "[!] 摇摆量化失败（不影响主流程）。" }
}
if ($fixAlignExit -ne 0) {
    Write-Host "[!] fix_align 失败（exit $fixAlignExit）—— 请把上方输出发给开发者；中间文件在 $midiDir。"
    exit 1
}
if ($SkipScore) {
    Write-Host "[4/6] 已跳过乐谱生成（模式：只要音频/鼓声）—— 仍会校正速度与对齐，供音频合成使用。"
} else {
    if ("$(Cfg 'showTempoChanges' '')" -eq "true") { $env:DRUM_SHOW_TEMPO = "1" }
    if ($beatAligned -and (Test-Path $planJson)) {
        & $venvPy (Join-Path $scrDir "gen_musicxml.py") $alMid $xml $BPM "$song" "-" 4 16 $planJson
    } else {
        & $venvPy (Join-Path $scrDir "gen_musicxml.py") $alMid $xml $BPM "$song"
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[!] gen_musicxml 失败（exit $LASTEXITCODE）—— 请把上方输出发给开发者；中间 MIDI：$alMid。"
        exit 1
    }
}

# ---------- 5. PDF render (fallback: self-made reportlab renderer) ----------
$pdf = Join-Path $outDir "${song}_鼓谱.pdf"
$mscoreOk = ((-not $SkipMuseScore) -and $mscore -and (Test-Path $mscore))
$pdfDone = $false
if ($SkipScore) {
    Write-Host "[5/6] 已跳过 PDF 渲染（模式：不要谱子）。"
} else {
Write-Host "[5/6] 渲染 PDF..."
if ($mscoreOk) {
    Write-Host "[*] 使用 MuseScore 渲染：$mscore"
    $proc = Start-Process -FilePath $mscore -ArgumentList ('"' + $xml + '"', '-o', '"' + $pdf + '"') -PassThru -Wait
    if (Test-Path $pdf) {
        $pdfDone = $true
    } else {
        Write-Host "[!] MuseScore 未产出 PDF（exit $($proc.ExitCode)），改用自研谱面渲染。"
    }
} else {
    if ($SkipMuseScore) { Write-Host "[*] 已指定 -SkipMuseScore，跳过 MuseScore。" }
    elseif (-not $mscore) { Write-Host "[!] 未找到 MuseScore，使用自研谱面渲染生成 PDF。" }
}
if (-not $pdfDone) {
    Write-Host "[*] 自研渲染：对齐 MIDI -> 谱面 PDF（reportlab）..."
    & $venvPy (Join-Path $scrDir "render_fallback.py") $alMid $pdf $BPM "$song"
    if ($LASTEXITCODE -eq 0 -and (Test-Path $pdf)) {
        Write-Host "[!] 已用自研渲染生成 PDF（MuseScore 不可用，排版精度略低但可读）。"
    } else {
        Write-Host "[!] 自研渲染也失败了（exit $LASTEXITCODE）。请检查对齐 MIDI：$alMid"
        exit 1
    }
}
}

# ---------- 6. drum audio (fallback: midi_to_wav numpy synth) --------------
Write-Host "[6/6] 合成鼓声音频..."
$mp3 = Join-Path $outDir "${song}_鼓谱音频.mp3"
$wav = Join-Path $outDir "${song}_鼓谱音频.wav"
$audioFinal = $null
$introPadded = $false
$gridOffset = 0.0
if ($SkipAudio) {
    Write-Host "[*] 已指定 -SkipAudio，跳过音频合成。"
} elseif (-not (Test-Path $alMid)) {
    Write-Host "[!] 未找到对齐后 MIDI，跳过音频。"
} else {
    $wavMade = $false
    if ($mscoreOk) {
        $tmpMid = Join-Path $soundDir "tmp_render.mid"
        $tmpWav = Join-Path $outDir "tmp_render.wav"
        Copy-Item $alMid $tmpMid -Force
        & $mscore $tmpMid -o $tmpWav | Out-Null
        if (Test-Path $tmpWav) { Copy-Item $tmpWav $wav -Force; $wavMade = $true }
        Remove-Item $tmpMid, $tmpWav -Force -ErrorAction SilentlyContinue
        if (-not $wavMade) { Write-Host "[!] MuseScore 音频渲染未产出 WAV，改用 numpy 合成器。" }
    }
    if (-not $wavMade) {
        & $venvPy (Join-Path $scrDir "midi_to_wav.py") $alMid $wav $BPM
        if ($LASTEXITCODE -eq 0 -and (Test-Path $wav)) { $wavMade = $true }
        else { Write-Host "[!] 音频合成失败（exit $LASTEXITCODE），跳过音频。" }
    }
    if ($wavMade) {
        # 渲染自校验 + 自愈：合成 WAV 的时长必须与拍点网格跨度一致（±1.5s）。
        # 若渲染器（MuseScore）对速度的解释与网格不符，自动改用网格精确的内置合成器
        # 重渲——动态谱永远不会拿到一段"速度不对"的音频。结果写入 plan.render 供
        # scoremap 端点做契约校验。
        try {
            $ffp2 = Get-Command ffprobe -ErrorAction SilentlyContinue
            if ($ffp2 -and $planJson -and (Test-Path $planJson)) {
                $pjR = Get-Content $planJson -Raw -Encoding UTF8 | ConvertFrom-Json
                $spanR = 0.0
                foreach ($bR in $pjR.bars) { $spanR += ([double]$bR.end - [double]$bR.start) }
                $t0v = 0.0
                try { if ($pjR.bars -and $pjR.bars.Count -gt 0) { $t0v = [double]$pjR.bars[0].start } } catch { $t0v = 0.0 }
                if ($spanR -gt 10) {
                    $dR = 0.0
                    try { $dR = [double](& $ffp2.Source -v error -show_entries format=duration -of default=nw=1:nk=1 $wav 2>$null) } catch { $dR = 0.0 }
                    if ($dR -gt 0 -and ([Math]::Abs($dR - $spanR) -gt 1.5)) {
                        Write-Host ("[!] 渲染时长 {0:N1}s 与网格 {1:N1}s 偏差超 1.5s —— 改用内置合成器重渲（网格精确）。" -f $dR, $spanR)
                        $wav2 = Join-Path $outDir "tmp_exact.wav"
                        & $venvPy (Join-Path $scrDir "midi_to_wav.py") $alMid $wav2 $BPM | Out-Null
                        if ($LASTEXITCODE -eq 0 -and (Test-Path $wav2)) {
                            $dR2 = 0.0
                            try { $dR2 = [double](& $ffp2.Source -v error -show_entries format=duration -of default=nw=1:nk=1 $wav2 2>$null) } catch { $dR2 = 0.0 }
                            if ($dR2 -gt 0 -and ([Math]::Abs($dR2 - $spanR) -le 1.5)) {
                                Move-Item $wav2 $wav -Force
                                Write-Host ("  ✓ 内置合成器重渲完成：{0:N1}s（与网格一致）" -f $dR2)
                            } else {
                                Write-Host "  [!] 重渲后仍偏差，保留原渲染（对齐守卫会报警）。"
                            }
                            Remove-Item $wav2 -Force -ErrorAction SilentlyContinue
                        } else {
                            Write-Host "  [!] 内置合成器重渲失败，保留原渲染。"
                        }
                    }
                    $dF = 0.0
                    try { $dF = [double](& $ffp2.Source -v error -show_entries format=duration -of default=nw=1:nk=1 $wav 2>$null) } catch { $dF = 0.0 }
                    # 口径统一（三处一致）：
                    #   dF       = 合成段时长（拼接前测量）      ↔ spanR     （网格跨度）
                    #   durFinal = 最终交付时长（dF + 前奏 t0v）  ↔ spanR+t0v（期望总长）
                    # 前端 checkAlign 用 durFinal 与 span+t0 比对——后端判定必须同口径。
                    # 注意：自愈段移到拼接前之后，dF 是拼接前合成段；旧注释「dF 是拼接后总长」已过时。
                    $durFinal = if ($t0v -gt 0.5) { $dF + $t0v } else { $dF }
                    $renderAligned = ($durFinal -gt 0 -and ([Math]::Abs($durFinal - ($spanR + $t0v)) -le 1.5))
                    $pjR | Add-Member -NotePropertyName "render" -NotePropertyValue ([pscustomobject]@{
                        audio = (Split-Path $wav -Leaf)
                        duration = [math]::Round($durFinal, 2)
                        span = [math]::Round($spanR, 2)
                        aligned = $renderAligned
                    }) -Force
                    $pjR | ConvertTo-Json -Depth 6 | Set-Content $planJson -Encoding UTF8
                }
            }
        } catch { }
        # 动态谱对齐：谱面第 1 小节从原曲 t0 处开始，而合成音频从网格起点（0s）开始；
        # 把原曲 [0, t0] 的前奏拼到合成音频前面，鼓谱音频即回到原曲时间线上 ——
        # 动态谱光标、进度条、时间显示全部自然对齐（前端零改动）。
        if ($ffmpeg -and -not $SkipFfmpeg) {
            $t0 = 0.0
            try {
                if ($planJson -and (Test-Path $planJson)) {
                    $pjI = Get-Content $planJson -Raw -Encoding UTF8 | ConvertFrom-Json
                    if ($pjI.bars -and $pjI.bars.Count -gt 0) { $t0 = [double]$pjI.bars[0].start }
                }
            } catch { $t0 = 0.0 }
            if ($t0 -gt 0.5 -and (Test-Path $SongPath)) {
                $wavIntro = Join-Path $outDir "tmp_intro.wav"
                & $ffmpeg -y -i $SongPath -i $wav -filter_complex "[0:a]atrim=0:$t0,asetpts=PTS-STARTPTS[a0];[a0][1:a]concat=n=2:v=0:a=1[aout]" -map "[aout]" -ac 2 -ar 44100 $wavIntro | Out-Null
                if ($LASTEXITCODE -eq 0 -and (Test-Path $wavIntro)) {
                    Move-Item $wavIntro $wav -Force
                    $introPadded = $true
                    $gridOffset = $t0
                    Write-Host ("[*] 已把原曲前奏（0-{0:N1}s）拼入鼓谱音频：动态谱按原曲时间线对齐。" -f $t0)
                } else {
                    Write-Host "[!] 前奏拼接失败，保留原合成音频。"
                }
            }
        }
        if ($SkipFfmpeg) {
            Write-Host "[*] 已指定 -SkipFfmpeg，保留 WAV 不转 MP3。"
            $audioFinal = $wav
        } elseif (-not $ffmpeg) {
            Write-Host "[!] 未找到 ffmpeg，保留 WAV 音频（MP3 为可选项，不影响谱面使用）。"
            $audioFinal = $wav
        } else {
            & $ffmpeg -y -i $wav -codec:a libmp3lame -qscale:a 2 $mp3 | Out-Null
            if ($LASTEXITCODE -eq 0 -and (Test-Path $mp3)) {
                $audioFinal = $mp3
                Remove-Item $wav -Force -ErrorAction SilentlyContinue
            } else {
                Write-Host "[!] ffmpeg 转 MP3 失败（exit $LASTEXITCODE），保留 WAV。"
                $audioFinal = $wav
            }
        }
    }
}

Write-Host ""
# 动态谱对齐守卫：合成音频的时长必须与拍点网格一致（±2s），否则动态谱会随播放漂移。
# 这类问题历史上曾静默溜进发布版（速度标记丢失时 MuseScore 按平直速度渲染），在此显式报警。
try {
    if ($planJson -and (Test-Path $planJson) -and $audioFinal -and (Test-Path $audioFinal)) {
        $planSpan = 0.0
        $pjg = Get-Content $planJson -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($bg in $pjg.bars) { $planSpan += ([double]$bg.end - [double]$bg.start) }
        $ffp = Get-Command ffprobe -ErrorAction SilentlyContinue
        if ($planSpan -gt 10 -and $ffp) {
            $adur = 0.0
            try { $adur = [double](& $ffp.Source -v error -show_entries format=duration -of default=nw=1:nk=1 $audioFinal 2>$null) } catch { $adur = 0.0 }
            $expect = $planSpan + $gridOffset
            if ($adur -gt 0 -and ([Math]::Abs($adur - $expect) -gt 2)) {
                Write-Host ("[!] 对齐警报：合成音频 {0:N1}s 与期望 {1:N1}s 相差超 2s —— 动态谱会与音乐漂移，请反馈（附 app\job.log）" -f $adur, $expect)
            }
        }
    }
} catch { }
Write-Host "==== DONE ===="
if (-not $SkipScore) {
    Write-Host "PDF      : $pdf"
    Write-Host "MusicXML : $xml"
}
Write-Host "MIDI     : $alMid"
if ($audioFinal) { Write-Host "Audio    : $audioFinal" } else { Write-Host "Audio    : （未生成）" }
Write-Host "中间产物 : $stemsRoot\$sepModel\$song\ 与 $midiDir\"
Write-Host "PDF 可用任意 PDF 阅读器打开；MusicXML 可在 MuseScore 4 中继续编辑。"
