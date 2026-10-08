$ErrorActionPreference='Stop'
$root=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root
& (Join-Path $root '.venv/Scripts/python.exe') -B -u -m lecture_asr.local_server --duration 6000 --summary
if($LASTEXITCODE -ne 0){throw 'Backend exited with an error.'}
