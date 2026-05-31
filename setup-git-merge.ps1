param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$null = Get-Command git -ErrorAction Stop

$repoRoot = (& git rev-parse --show-toplevel 2>$null)
if (-not $repoRoot) {
    throw '請在 Git repository 內執行 setup-git-merge.ps1'
}

$repoRoot = $repoRoot.Trim()
$mergeScriptPath = Join-Path $repoRoot 'scripts/merge-routes-json.ps1'
if (-not (Test-Path -LiteralPath $mergeScriptPath)) {
    throw "找不到 merge 腳本: $mergeScriptPath"
}

$driverCommand = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$mergeScriptPath`" %O %A %B %A"

& git config --local merge.routesjson.name "JSON routes merge driver"
& git config --local merge.routesjson.driver "$driverCommand"

$configuredName = (& git config --local --get merge.routesjson.name).Trim()
$configuredDriver = (& git config --local --get merge.routesjson.driver).Trim()

Write-Host 'Git merge driver 初始化完成。' -ForegroundColor Green
Write-Host "merge.routesjson.name   = $configuredName"
Write-Host "merge.routesjson.driver = $configuredDriver"
