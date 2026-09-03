$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $Root 'env.ps1')
$python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "Python venv not found: $python" }
Set-Location $Root
& $python (Join-Path $Root 'llbot_bridge.py')
exit $LASTEXITCODE

