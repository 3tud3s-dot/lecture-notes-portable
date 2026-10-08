$ErrorActionPreference='Stop'
$root=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location (Join-Path $root 'frontend')
npm run dev
if($LASTEXITCODE -ne 0){throw 'Frontend exited with an error.'}
