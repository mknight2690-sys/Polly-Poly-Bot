# Create Start / Stop Polly shortcuts on the current user's Desktop
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Desktop = [Environment]::GetFolderPath("Desktop")
$Wsh = New-Object -ComObject WScript.Shell

function New-PollyShortcut {
  param(
    [string]$Name,
    [string]$TargetBat,
    [string]$Description
  )
  $path = Join-Path $Desktop "$Name.lnk"
  $sc = $Wsh.CreateShortcut($path)
  $sc.TargetPath = $TargetBat
  $sc.WorkingDirectory = $Root
  $sc.WindowStyle = 7  # minimized
  $sc.Description = $Description
  $sc.Save()
  Write-Host "Created $path"
}

New-PollyShortcut -Name "Start Polly" -TargetBat (Join-Path $Root "Start Polly.bat") `
  -Description "Start Polly Alert Deck (http://127.0.0.1:18112)"
New-PollyShortcut -Name "Stop Polly" -TargetBat (Join-Path $Root "Stop Polly.bat") `
  -Description "Stop Polly Alert Deck"
Write-Host "Done. Check your Desktop for Start Polly / Stop Polly."
