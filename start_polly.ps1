# Start Polly Alert Deck on http://127.0.0.1:18112
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$port = 18112
$existing = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
  Where-Object { $_.State -eq "Listen" } |
  Select-Object -ExpandProperty OwningProcess -Unique
if ($existing) {
  Write-Host "Polly already running on port $port (PID $($existing -join ', '))"
  Write-Host "Deck: http://127.0.0.1:$port/"
  exit 0
}

$py = $null
foreach ($c in @(
  (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
  "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
  "$env:USERPROFILE\AppData\Roaming\uv\python\cpython-3.11.15-windows-x86_64-none\python.exe"
)) {
  if ($c -and (Test-Path $c)) { $py = $c; break }
}
if (-not $py) {
  Write-Host "Python not found. Install Python 3.11+ and retry."
  pause
  exit 1
}

$logs = Join-Path $Root "logs"
New-Item -ItemType Directory -Path $logs -Force | Out-Null
$out = Join-Path $logs "polly_stdout.log"
$err = Join-Path $logs "polly_stderr.log"

Write-Host "Starting Polly with $py ..."
Start-Process -FilePath $py -ArgumentList "run_poly.py" -WorkingDirectory $Root `
  -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err

Start-Sleep -Seconds 2
$listen = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
  Where-Object { $_.State -eq "Listen" }
if ($listen) {
  Write-Host "Polly started -> http://127.0.0.1:$port/"
} else {
  Write-Host "Start may have failed — check logs\polly_stderr.log"
  Get-Content $err -Tail 20 -ErrorAction SilentlyContinue
  pause
}
