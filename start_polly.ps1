# Start Polly Alert Deck — single instance + open dashboard in default browser
# Deck: http://127.0.0.1:18112/
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$port = 18112
$deckUrl = "http://127.0.0.1:$port/"
$pidFile = Join-Path $Root "logs\polly.pid"
$mutexName = "Global\PollyPolyBot.AlertDeck.SingleInstance"

function Open-PollyDashboard {
  param([string]$Url = $deckUrl)
  try {
    Start-Process $Url | Out-Null
    Write-Host "Opened dashboard: $Url"
  } catch {
    Write-Host "Could not open browser automatically. Go to $Url"
  }
}

function Test-PollyListening {
  $null -ne @(
    Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
      Where-Object { $_.State -eq "Listen" }
  )[0]
}

function Get-PollyListenerPids {
  @(
    Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
      Where-Object { $_.State -eq "Listen" } |
      Select-Object -ExpandProperty OwningProcess -Unique
  ) | Where-Object { $_ -and $_ -gt 0 }
}

function Get-RunPolyPids {
  @(
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
      Where-Object { $_.CommandLine -and ($_.CommandLine -match 'run_poly\.py') } |
      Select-Object -ExpandProperty ProcessId
  ) | Where-Object { $_ -and $_ -gt 0 }
}

function Wait-PollyReady {
  param([int]$TimeoutSec = 45)
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  while ((Get-Date) -lt $deadline) {
    if (Test-PollyListening) {
      try {
        $r = Invoke-WebRequest -Uri $deckUrl -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) { return $true }
      } catch {
        try {
          $r = Invoke-WebRequest -Uri ($deckUrl.TrimEnd('/') + "/api/version") -UseBasicParsing -TimeoutSec 2
          if ($r.StatusCode -eq 200) { return $true }
        } catch { }
      }
    }
    Start-Sleep -Milliseconds 400
  }
  return $false
}

# --- Single-instance gate (launcher mutex + one server on 18112) ---
$created = $false
$mutex = New-Object System.Threading.Mutex($false, $mutexName, [ref]$created)
$gotLock = $false
try {
  $gotLock = $mutex.WaitOne(60000)
} catch {
  $gotLock = $false
}
if (-not $gotLock) {
  Write-Host "Another Start Polly is already in progress. Opening dashboard if the deck is up..."
  if (Test-PollyListening) { Open-PollyDashboard }
  exit 0
}

try {
  $listenerPids = @(Get-PollyListenerPids)
  if ($listenerPids.Count -gt 0) {
    # Already running — never start a second instance; just bring up the UI
    Write-Host "Polly already running (PID $($listenerPids -join ', ')). Single instance kept."
    $listenerPids[0] | Set-Content -Path $pidFile -Encoding ascii -Force
    Open-PollyDashboard
    exit 0
  }

  # No listener: kill orphan run_poly.py processes so we don't double-bind later
  $orphans = @(Get-RunPolyPids)
  foreach ($op in $orphans) {
    try {
      Stop-Process -Id $op -Force -ErrorAction Stop
      Write-Host "Cleared orphan run_poly.py PID $op"
    } catch { }
  }

  $py = $null
  foreach ($c in @(
    (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:USERPROFILE\AppData\Roaming\uv\python\cpython-3.11.15-windows-x86_64-none\python.exe"
  )) {
    if ($c -and (Test-Path $c) -and ($c -notmatch 'WindowsApps\\python\.exe$')) {
      $py = $c
      break
    }
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

  # Re-check after cleanup (race)
  if (Test-PollyListening) {
    Write-Host "Polly came up while preparing. Single instance kept."
    Open-PollyDashboard
    exit 0
  }

  Write-Host "Starting Polly (single instance) with $py ..."
  $proc = Start-Process -FilePath $py -ArgumentList "run_poly.py" -WorkingDirectory $Root `
    -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err `
    -PassThru
  if ($proc -and $proc.Id) {
    $proc.Id | Set-Content -Path $pidFile -Encoding ascii -Force
  }

  if (Wait-PollyReady -TimeoutSec 45) {
    Write-Host "Polly ready -> $deckUrl"
    Open-PollyDashboard
    exit 0
  }

  Write-Host "Start may have failed — check logs\polly_stderr.log"
  Get-Content $err -Tail 25 -ErrorAction SilentlyContinue
  pause
  exit 1
}
finally {
  if ($gotLock -and $mutex) {
    try { $mutex.ReleaseMutex() | Out-Null } catch { }
  }
  if ($mutex) { $mutex.Dispose() }
}
