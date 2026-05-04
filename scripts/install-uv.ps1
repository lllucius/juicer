[CmdletBinding()]
param(
    [switch]$Dev,
    [switch]$Gui,
    [switch]$Windows,
    [string]$Venv = ".venv"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-UvPath {
    $Command = Get-Command uv -ErrorAction SilentlyContinue
    if ($Command) {
        return $Command.Source
    }

    $Candidates = @()
    if ($HOME) {
        $Candidates += Join-Path $HOME ".local\bin\uv.exe"
    }
    if ($env:USERPROFILE) {
        $Candidates += Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    }

    foreach ($Candidate in $Candidates) {
        if (Test-Path $Candidate) {
            return $Candidate
        }
    }

    return $null
}

function Format-PathForComparison {
    param([string]$Path)

    try {
        return [System.IO.Path]::GetFullPath($Path).TrimEnd("\").ToLowerInvariant()
    }
    catch {
        return $Path.TrimEnd("\").ToLowerInvariant()
    }
}

$Uv = Get-UvPath
if (-not $Uv) {
    Write-Host "uv is not installed; installing uv..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression

    if ($HOME) {
        $UvBin = Join-Path $HOME ".local\bin"
        $PathEntries = $env:PATH -split ";" | Where-Object { $_ } | ForEach-Object {
            Format-PathForComparison $_
        }
        if ((Test-Path $UvBin) -and ($PathEntries -notcontains (Format-PathForComparison $UvBin))) {
            $env:PATH = "$UvBin;$env:PATH"
        }
    }

    $Uv = Get-UvPath
    if (-not $Uv) {
        Write-Error "uv was installed, but the uv executable was not found in PATH or the expected .local\bin directory."
    }
}

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

& $Uv venv $Venv

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
& $Uv pip install --python $Python @InstallArgs $PackageSpec

Write-Host ""
Write-Host "Juicer installed in $Venv."
Write-Host "Activate the environment with:"
Write-Host "  $Venv\Scripts\Activate.ps1"
Write-Host "Then run:"
Write-Host "  juicer --help"
