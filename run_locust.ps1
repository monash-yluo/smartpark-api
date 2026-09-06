param(
    [int]$Users = 50,
    [int]$Pod = 2,
    [string]$HostUrl = "http://34.129.30.110",
    [string]$RunTime = "3m",
    [double]$SpawnRate = 0
)

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$locustFile = Join-Path $scriptRoot "locustfile.py"
$outputDirectory = Join-Path $scriptRoot "locust_result"
$timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$outputPrefix = Join-Path $outputDirectory "pod$Pod-users$Users-$timestamp"

if (-not (Test-Path $outputDirectory)) {
    New-Item -ItemType Directory -Path $outputDirectory | Out-Null
}

if ($SpawnRate -le 0) {
    $SpawnRate = [math]::Max(0.1, [math]::Round($Users / 60, 2))
}

$python = Join-Path $scriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "找不到 Python 环境: $python / Python environment not found: $python"
}

$arguments = @(
    "-m", "locust",
    "-f", $locustFile,
    "--headless",
    "--host", $HostUrl,
    "--users", $Users,
    "--spawn-rate", $SpawnRate,
    "--run-time", $RunTime,
    "--html", "$outputPrefix.html"
)

Write-Host "Starting Locust: users=$Users, spawn-rate=$SpawnRate, run-time=$RunTime"
Write-Host "Output prefix: $outputPrefix"

& $python @arguments
$exitCode = $LASTEXITCODE

Write-Host "Locust finished with exit code: $exitCode"
exit $exitCode