# Stop Polly Alert Deck (anything listening on port 18112)
$ErrorActionPreference = "Continue"
$port = 18112
$pids = @(
  Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
    Where-Object { $_.State -eq "Listen" } |
    Select-Object -ExpandProperty OwningProcess -Unique
)

# Also match run_poly.py processes
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -and ($_.CommandLine -match 'run_poly\.py') } |
  ForEach-Object { $pids += $_.ProcessId }

$pids = $pids | Where-Object { $_ -and $_ -gt 0 } | Select-Object -Unique
if (-not $pids) {
  Write-Host "Polly is not running (nothing on port $port / run_poly.py)."
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
Write-Host "Polly stopped."
