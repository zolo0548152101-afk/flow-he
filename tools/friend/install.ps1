# Flow-HE installer: builds Flow-HE.exe from updates\ (checks SHA-256), installs to %LOCALAPPDATA%\Flow-HE, starts it.
$ErrorActionPreference = 'Stop'
try {
  $src = Join-Path $PSScriptRoot 'updates'
  $dst = Join-Path $env:LOCALAPPDATA 'Flow-HE'
  Write-Host 'Installing Flow-HE...'
  Get-Process Flow-HE -ErrorAction SilentlyContinue | Stop-Process -Force
  Start-Sleep -Seconds 1
  New-Item -ItemType Directory -Force $dst | Out-Null
  $m = Get-Content -Raw -Encoding UTF8 (Join-Path $src 'manifest.json') | ConvertFrom-Json
  $exe = Join-Path $dst 'Flow-HE.exe'
  $o = [IO.File]::Create($exe)
  foreach ($p in $m.parts) { $b = [IO.File]::ReadAllBytes((Join-Path $src $p)); $o.Write($b, 0, $b.Length) }
  $o.Close()
  if ((Get-FileHash $exe -Algorithm SHA256).Hash.ToLower() -ne $m.sha256) { throw 'File check failed - download the folder again.' }
  [IO.File]::WriteAllText((Join-Path $dst 'updates_dir.txt'), $src, (New-Object Text.UTF8Encoding $false))
  Write-Host ('Flow-HE ' + $m.version + ' installed.')
  Start-Process $exe
} catch {
  Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
  exit 1
}
