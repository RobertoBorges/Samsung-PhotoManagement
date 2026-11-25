$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw 'Set up the environment first: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt'
}
& $python app.py
