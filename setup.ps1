<#
Setup script for Windows (PowerShell).
Usage: open PowerShell in project root and run:
  .\setup.ps1

This will:
- create a virtualenv (.venv) if missing
- upgrade pip and install requirements
- run the bot using the venv python

Edit .env (copy .env.example -> .env) before running.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Write-Host "Ensuring .venv exists..."
if (-Not (Test-Path -Path .\.venv)) {
    python -m venv .venv
}

Write-Host "Allowing script execution for this session and preparing environment..."
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process -Force

$venvPython = Join-Path -Path . -ChildPath '.venv\Scripts\python.exe'
if (-Not (Test-Path -Path $venvPython)) {
    Write-Error "Virtualenv python not found at $venvPython"
    exit 1
}

Write-Host "Upgrading pip and installing requirements..."
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements.txt

Write-Host "Make sure you copied .env.example -> .env and filled values (DISCORD_TOKEN etc)."
Write-Host "Starting bot... (press Ctrl+C to stop)"
& $venvPython bot.py