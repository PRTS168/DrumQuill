# bootstrap\download.ps1
# 断点续传下载器：curl.exe -C - 优先，BITS 备选；多源镜像链；下载后校验。
# 被 install.ps1 / ensure.ps1 dot-source 后调用 Invoke-DrumDownload。
# 不允许硬编码机器路径。

$ErrorActionPreference = "Stop"

function Get-CurlExe {
    $c = "C:\Windows\system32\curl.exe"
    if (Test-Path $c) { return $c }
    $g = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($g) { return $g.Source }
    return $null
}

function Test-FileSizeOK {
    param([string]$Path, [long]$ExpectedSize, [double]$Tolerance = 0.02)
    if (-not (Test-Path $Path)) { return $false }
    $len = (Get-Item $Path).Length
    if ($ExpectedSize -le 0) {
        # 大小未知的下载（ffmpeg.gz 等）：0 字节占位/失败残留绝不能算「已存在且大小正常」
        # （与 installer.py _file_ok 的 size<=0 语义对齐）
        return ($len -gt 0)
    }
    $lo = [long]($ExpectedSize * (1 - $Tolerance))
    $hi = [long]($ExpectedSize * (1 + $Tolerance))
    return ($len -ge $lo -and $len -le $hi)
}

# 单源尝试（带续传）。返回 $true 表示下载完成且大小校验通过。
function Invoke-DownloadOne {
    param(
        [string]$Url,
        [string]$OutFile,
        [long]$ExpectedSize,
        [int]$MaxTimeSec,
        [int]$Tries = 2
    )
    $curl = Get-CurlExe
    if (-not $curl) { return $false }

    $dstDir = Split-Path $OutFile -Parent
    if (-not (Test-Path $dstDir)) { New-Item -ItemType Directory -Force $dstDir | Out-Null }

    for ($i = 1; $i -le $Tries; $i++) {
        if (Test-FileSizeOK -Path $OutFile -ExpectedSize $ExpectedSize) { return $true }

        # 续传：若已有部分文件，curl -C - 追加；服务器不支持 Range 时删除残留重来一次
        $partial = Test-Path $OutFile
        Write-Host ("    [下载] 第 {0}/{1} 次: {2}" -f $i, $Tries, $Url)
        # --fail：HTTP 4xx/5xx 时 curl 以非零退出且不写 body，错误页不会再被当成下载内容落盘
        $args = @("-L", "--fail", "--connect-timeout", "15", "--max-time", "$MaxTimeSec",
                  "-sS", "-#", "-o", $OutFile, $Url)
        if ($partial) { $args = @("-L", "-C", "-", "--fail", "--connect-timeout", "15", "--max-time", "$MaxTimeSec",
                  "-sS", "-#", "-o", $OutFile, $Url) }
        & $curl @args
        $code = $LASTEXITCODE
        if ($code -ne 0 -and (Test-Path $OutFile) -and (Get-Item $OutFile).Length -eq 0) {
            # 失败留下的 0 字节占位文件删掉（真正的半截数据保留，供 -C - 续传）
            Write-Host "    [清理] 本次尝试失败，删除 0 字节残留: $OutFile"
            Remove-Item $OutFile -Force -ErrorAction SilentlyContinue
        }

        # 416 Range Not Satisfiable（curl exit 33）：残片已 >= 远端文件实际长度
        # （远端文件变小/残片损坏）。不清残片则带 -C - 永远 416，2 次重试内全源失败。
        if ($partial -and $code -eq 33) {
            Write-Host "    [提示] 服务器拒绝续传区间（416），清除残片后整段重新下载。"
            Remove-Item $OutFile -Force -ErrorAction SilentlyContinue
            & $curl -L --fail --connect-timeout 15 --max-time $MaxTimeSec -sS -# -o $OutFile $Url
            $code = $LASTEXITCODE
            if ($code -ne 0 -and (Test-Path $OutFile) -and (Get-Item $OutFile).Length -eq 0) {
                Remove-Item $OutFile -Force -ErrorAction SilentlyContinue
            }
        }

        if ($code -eq 0 -and (Test-FileSizeOK -Path $OutFile -ExpectedSize $ExpectedSize)) {
            return $true
        }

        # -C - 时若服务器不支持断点（exit 33/39 类错误），删掉残留重新整段下载
        if ($partial -and $code -ne 0) {
            $_eap = $ErrorActionPreference
            $ErrorActionPreference = "Continue"   # PS5.1：stderr 重定向的原生调用写 stderr 即终止
            $head = & $curl -sS -I --connect-timeout 10 $Url 2>$null
            $ErrorActionPreference = $_eap
            $acceptsRange = ($head -match "accept-ranges:\s*bytes")
            if (-not $acceptsRange) {
                Write-Host "    [提示] 该源不支持断点续传，清除残留后重新下载。"
                Remove-Item $OutFile -Force -ErrorAction SilentlyContinue
                & $curl -L --fail --connect-timeout 15 --max-time $MaxTimeSec -sS -# -o $OutFile $Url
                $code = $LASTEXITCODE
                if ($code -ne 0 -and (Test-Path $OutFile) -and (Get-Item $OutFile).Length -eq 0) {
                    Remove-Item $OutFile -Force -ErrorAction SilentlyContinue
                }
                if ($code -eq 0 -and (Test-FileSizeOK -Path $OutFile -ExpectedSize $ExpectedSize)) { return $true }
            }
        }
        if (Test-FileSizeOK -Path $OutFile -ExpectedSize $ExpectedSize) { return $true }
        Write-Host ("    [失败] 第 {0} 次未完成 (curl exit={1})" -f $i, $code)
        Start-Sleep -Seconds 2
    }
    return $false
}

# BITS 备选（无 curl 或 curl 全程失败时）
function Invoke-DownloadBits {
    param([string]$Url, [string]$OutFile)
    try {
        $dstDir = Split-Path $OutFile -Parent
        if (-not (Test-Path $dstDir)) { New-Item -ItemType Directory -Force $dstDir | Out-Null }
        Write-Host "    [BITS] 备用通道下载: $Url"
        Start-BitsTransfer -Source $Url -Destination $OutFile -ErrorAction Stop
        return (Test-Path $OutFile)
    } catch {
        Write-Host "    [BITS] 失败: $($_.Exception.Message)"
        return $false
    }
}

# 主入口：多源镜像链。全部失败后打印手动下载指引并返回 $false。
function Invoke-DrumDownload {
    param(
        [Parameter(Mandatory = $true)][string[]]$UrlChain,
        [Parameter(Mandatory = $true)][string]$OutFile,
        [long]$ExpectedSize = 0,
        [int]$MaxTimeSec = 1800,
        [string]$ManualGuideName = ""   # 失败时打印给用户的文件用途说明
    )
    # 已存在且大小合格：直接跳过
    if (Test-FileSizeOK -Path $OutFile -ExpectedSize $ExpectedSize) {
        Write-Host "    [跳过] 已存在且大小正常: $OutFile"
        return $true
    }

    foreach ($url in $UrlChain) {
        if (Invoke-DownloadOne -Url $url -OutFile $OutFile -ExpectedSize $ExpectedSize -MaxTimeSec $MaxTimeSec) {
            Write-Host "    [完成] -> $OutFile ($([math]::Round((Get-Item $OutFile).Length/1MB,1)) MB)"
            return $true
        }
        Write-Host "    [换源] 本源失败，尝试下一个镜像..."
    }

    # curl 全灭再试一次 BITS（仅第一源）
    if (Invoke-DownloadBits -Url $UrlChain[0] -OutFile $OutFile) {
        if (Test-FileSizeOK -Path $OutFile -ExpectedSize $ExpectedSize) { return $true }
    }

    # 全部失败：打印手动指引
    Write-Host ""
    Write-Host "================== [需要手动下载] ==================" -ForegroundColor Red
    if ($ManualGuideName) { Write-Host "缺失组件: $ManualGuideName" }
    Write-Host "请在浏览器/下载工具中打开以下任一地址手动下载："
    foreach ($u in $UrlChain) { Write-Host "  - $u" }
    Write-Host "下载完成后放到这个绝对路径（目录不存在请自行创建）："
    Write-Host "  $OutFile"
    if ($ExpectedSize -gt 0) { Write-Host ("文件大小应约为 {0:N1} MB" -f ($ExpectedSize/1MB)) }
    Write-Host "===================================================" -ForegroundColor Red
    return $false
}

# 解压 gzip 单文件（Windows 无 gunzip，用 .NET）
function Expand-GzipFile {
    param([string]$GzPath, [string]$DestPath)
    try {
        $in = [System.IO.File]::OpenRead($GzPath)
        $gz = New-Object System.IO.Compression.GzipStream($in, [System.IO.Compression.CompressionMode]::Decompress)
        $out = [System.IO.File]::Create($DestPath)
        $gz.CopyTo($out)
        $out.Close(); $gz.Close(); $in.Close()
        return (Test-Path $DestPath)
    } catch {
        Write-Host "    [解压] gzip 解压失败: $($_.Exception.Message)"
        return $false
    }
}

