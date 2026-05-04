[CmdletBinding()]
param(
    [switch]$Dev,
    [switch]$Gui,
    [switch]$Windows,
    [string]$Venv = ".venv"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv is not installed. Install it from https://docs.astral.sh/uv/getting-started/installation/"
}

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

uv venv $Venv

$Extras = @()
if ($Dev) { $Extras += "dev" }
if ($Gui) { $Extras += "gui" }
if ($Windows) { $Extras += "windows" }

$PackageSpec = "."
if ($Extras.Count -gt 0) {
    $PackageSpec = ".[" + ($Extras -join ",") + "]"
}

$InstallArgs = @()
if ($Dev) { $InstallArgs += "-e" }

$Python = Join-Path $Venv "Scripts\python.exe"
uv pip install --python $Python @InstallArgs $PackageSpec

Write-Host ""
Write-Host "Juicer installed in $Venv."
Write-Host "Activate the environment with:"
Write-Host "  $Venv\Scripts\Activate.ps1"
Write-Host "Then run:"
Write-Host "  juicer --help"
