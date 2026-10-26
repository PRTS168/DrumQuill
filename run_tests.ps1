# DrumQuill 一键测试：语法检查 + 单元测试 + 静态体检
# 用法：  powershell -ExecutionPolicy Bypass -File run_tests.ps1
# 结果：三项全部通过才返回 0；任何一项失败返回非 0 并给出摘要。
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ROOT = $PSScriptRoot
$PY = Join-Path $ROOT ".venv-test\Scripts\python.exe"
if (-not (Test-Path $PY)) { $PY = Join-Path $ROOT ".venv-gpu\Scripts\python.exe" }
if (-not (Test-Path $PY)) { $PY = "python" }

Write-Host "==== 1/3 语法检查（py_compile）===="
$codeFiles = @()
Get-ChildItem (Join-Path $ROOT "app")  -Filter *.py -File | ForEach-Object { $codeFiles += $_.FullName }
Get-ChildItem (Join-Path $ROOT "scripts") -Filter *.py -File | ForEach-Object { $codeFiles += $_.FullName }
& $PY -m py_compile $codeFiles
if ($LASTEXITCODE -ne 0) { Write-Host "[FAIL] 语法检查未通过"; exit 1 }
Write-Host "[OK] 语法检查通过（$($codeFiles.Count) 个文件）"

Write-Host "==== 2/3 单元测试（pytest）===="
Push-Location $ROOT
try {
    & $PY -m pytest tests
    if ($LASTEXITCODE -ne 0) { Write-Host "[FAIL] 单元测试未通过"; exit 1 }
} finally { Pop-Location }
Write-Host "[OK] 单元测试通过"

Write-Host "==== 3/3 静态体检（ruff，保守规则：F/E9/W6）===="
& $PY -m ruff check (Join-Path $ROOT "app") (Join-Path $ROOT "scripts") --select F,E9,W6 --output-format concise
if ($LASTEXITCODE -ne 0) { Write-Host "[FAIL] 静态体检未通过"; exit 1 }
Write-Host "[OK] 静态体检通过"

Write-Host ""
Write-Host "==== DONE ===="
