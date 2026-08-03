# Start Polly Alert Deck - single instance + open dashboard in default browser
# Deck: http://127.0.0.1:18112/
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$port = 18112
$deckUrl = "http://127.0.0.1:$port/"
$logs = Join-Path $Root "logs"
New-Item -ItemType Directory -Path $logs -Force | Out-Null
$pidFile = Join-Path $logs "polly.pid"
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

function Ensure-PollyCredentials {
  $credsDir = Join-Path $Root "credentials"
  $creds = Join-Path $credsDir "poly_clob.txt"
  $example = Join-Path $credsDir "poly_clob.example.txt"
  New-Item -ItemType Directory -Path $credsDir -Force | Out-Null

  if (Test-Path $creds) {
    $raw = Get-Content $creds -Raw -ErrorAction SilentlyContinue
    $hasPk = $raw -match '(?m)^\s*PRIVATE_KEY\s*=\s*0x[0-9a-fA-F]{64}\s*$'
    $hasFunder = $raw -match '(?m)^\s*FUNDER\s*=\s*0x[0-9a-fA-F]{40}\s*$'
    if ($hasPk -and $hasFunder) { return $true }
    Write-Host "WARNING: credentials\poly_clob.txt exists but PRIVATE_KEY/FUNDER look incomplete."
  }

  $seedCandidates = @(
    (Join-Path $env:USERPROFILE "vertex-ai-trader\credentials\poly_clob.txt"),
    (Join-Path (Split-Path $Root -Parent) "vertex-ai-trader\credentials\poly_clob.txt"),
    "C:\Users\mknig\vertex-ai-trader\credentials\poly_clob.txt"
  )
  foreach ($src in $seedCandidates) {
    if (-not (Test-Path $src)) { continue }
    Copy-Item -Force $src $creds
    Write-Host "Seeded credentials\poly_clob.txt from $src"
    return $true
  }

  if (-not (Test-Path $creds) -and (Test-Path $example)) {
    Copy-Item -Force $example $creds
    Write-Host "Created credentials\poly_clob.txt from example - fill PRIVATE_KEY and FUNDER before LIVE/ARM."
  } else {
    Write-Host "Missing credentials\poly_clob.txt - paper still works; LIVE/ARM needs PRIVATE_KEY + FUNDER."
  }
  return $false
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

function Resolve-PollyPython {
  $cands = @(
    (Join-Path $Root ".venv\Scripts\python.exe"),
    "$env:USERPROFILE\AppData\Roaming\uv\python\cpython-3.11.15-windows-x86_64-none\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
  )
  # PATH python last - but skip broken hermes / WindowsApps stubs
  $pathPy = Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
  if ($pathPy) { $cands += $pathPy }

  foreach ($c in $cands) {
    if (-not $c -or -not (Test-Path $c)) { continue }
    if ($c -match 'WindowsApps\\python\.exe$') { continue }
    if ($c -match 'hermes-agent\\venv') { continue }
    # Prove it can import the deck
    $probe = & $c -c "import fastapi, uvicorn; import sys; sys.path.insert(0, r'$Root'); from poly.server import main; print('ok')" 2>$null
    if ($LASTEXITCODE -eq 0 -and ($probe -match 'ok')) {
      return $c
    }
  }
  return $null
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
    Write-Host "Polly already running (PID $($listenerPids -join ', ')). Single instance kept."
    $listenerPids[0] | Set-Content -Path $pidFile -Encoding ascii -Force
    Open-PollyDashboard
    exit 0
  }

  $orphans = @(Get-RunPolyPids)
  foreach ($op in $orphans) {
    try {
      Stop-Process -Id $op -Force -ErrorAction Stop
      Write-Host "Cleared orphan run_poly.py PID $op"
    } catch { }
  }

  Ensure-PollyCredentials | Out-Null

  Write-Host "Resolving Python for Polly..."
  $py = Resolve-PollyPython
  if (-not $py) {
    Write-Host "No working Python found for Polly."
    Write-Host "From this folder run:  .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
    Write-Host "Or: python -m venv .venv  then  .\.venv\Scripts\pip install -r requirements.txt"
    pause
    exit 1
  }

  $out = Join-Path $logs "polly_stdout.log"
  $err = Join-Path $logs "polly_stderr.log"

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

  Write-Host "Start may have failed - check logs\polly_stderr.log"
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
