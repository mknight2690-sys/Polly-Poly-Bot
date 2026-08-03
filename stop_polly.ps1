# Stop Polly Alert Deck (single instance on port 18112)
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$port = 18112
$pidFile = Join-Path $Root "logs\polly.pid"

$pids = @(
  Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
    Where-Object { $_.State -eq "Listen" } |
    Select-Object -ExpandProperty OwningProcess -Unique
)

Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -and ($_.CommandLine -match 'run_poly\.py') } |
  ForEach-Object { $pids += $_.ProcessId }

if (Test-Path $pidFile) {
  $fromFile = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
  if ($fromFile -match '^\d+$') { $pids += [int]$fromFile }
}

$pids = $pids | Where-Object { $_ -and $_ -gt 0 } | Select-Object -Unique
if (-not $pids) {
  Write-Host "Polly is not running (nothing on port $port / run_poly.py)."
  Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
  exit 0
}

foreach ($procId in $pids) {
  try {
    Stop-Process -Id $procId -Force -ErrorAction Stop
    Write-Host "Stopped PID $procId"
  } catch {
    Write-Host "Could not stop PID $procId : $_"
  }
}
Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
Write-Host "Polly stopped."
