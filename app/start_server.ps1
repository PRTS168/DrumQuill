# Start the local Drum AI web app and open the browser.
# Portable: ROOT from $env:DRUM_AI_ROOT or parent of this app/ dir;
# venv/pythonw/port from config.json with 8756->8757 fallback.
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ROOT = $env:DRUM_AI_ROOT
if (-not $ROOT) { $ROOT = Split-Path $PSScriptRoot -Parent }
$APP_DIR = Join-Path $ROOT "app"

$cfg = @{}
$cfgPath = Join-Path $ROOT "config.json"
if (Test-Path $cfgPath) {
    try {
        $raw = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($p in $raw.PSObject.Properties) { $cfg[$p.Name] = $p.Value }
    } catch {
        Write-Host "[!] config.json 解析失败：$($_.Exception.Message) —— 按缺省继续"
    }
}
$venvDir = if ($cfg.Contains("venvDir") -and $cfg["venvDir"]) { $cfg["venvDir"] } else { Join-Path $ROOT ".venv" }
$py = Join-Path $venvDir "Scripts\pythonw.exe"
$appPy = Join-Path $APP_DIR "app.py"
$logFile = Join-Path $APP_DIR "app.log"

function Test-PortListening($p) {
    return [bool](Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue)
}

# port: env DRUM_AI_PORT -> config.port -> 8756; occupied -> +1
$port = 8756
if ($env:DRUM_AI_PORT) { $port = [int]$env:DRUM_AI_PORT }
elseif ($cfg.Contains("port") -and $cfg["port"]) { $port = [int]$cfg["port"] }
if (Test-PortListening $port) {
    Write-Host "[*] 端口 $port 已被占用，回退到 $($port + 1)..."
    $port = $port + 1
}

if (-not (Test-Path $py)) {
    Write-Host "[!] 未找到虚拟环境 Python：$py"
    Write-Host "    请先运行 bootstrap\install.ps1，或在 config.json 配置 venvDir。"
    exit 2
}
if (-not (Test-Path $appPy)) {
    Write-Host "[!] 未找到 $appPy"
    exit 1
}

$launched = $false
if (-not (Test-PortListening $port)) {
    $launched = $true
    $env:DRUM_AI_PORT = "$port"
    Start-Process -FilePath $py -ArgumentList "`"$appPy`"" -WorkingDirectory $APP_DIR `
        -RedirectStandardError $logFile -WindowStyle Hidden
    $tries = 0
    while ($tries -lt 40) {
        Start-Sleep -Milliseconds 500
        if (Test-PortListening $port) { break }
        $tries++
    }
}

if (Test-PortListening $port -and -not $launched) {
    # 回退后的端口仍被其他程序占用：我们从未成功启动自己的服务，
    # 绝不能把“别人的服务”当成成功并打开浏览器。
    [Console]::Error.WriteLine("[!] 端口 $port 已被其他程序占用，DrumQuill 服务未启动。")
    [Console]::Error.WriteLine("    请关闭占用该端口的程序后重试，或在 config.json 中修改 port 字段。")
    if (Test-Path $logFile) {
        [Console]::Error.WriteLine("错误日志末尾（$logFile）：")
        Get-Content $logFile -Tail 20 -Encoding UTF8 | ForEach-Object { [Console]::Error.WriteLine($_) }
    }
    exit 1
}

if (Test-PortListening $port) {
    Start-Process "http://127.0.0.1:$port"
} else {
    [Console]::Error.WriteLine("服务启动失败。错误日志末尾（$logFile）：")
    if (Test-Path $logFile) {
        Get-Content $logFile -Tail 20 -Encoding UTF8 | ForEach-Object { [Console]::Error.WriteLine($_) }
    }
    Start-Sleep -Seconds 5
}
