[CmdletBinding()]
param(
    [switch]$Dev,
    [switch]$Gui,
    [switch]$Windows,
    [string]$Venv = ".venv"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-PythonPath {
    foreach ($Name in @("py", "python", "python3")) {
        $Command = Get-Command $Name -ErrorAction SilentlyContinue
        if ($Command) {
            return $Command.Source
        }
    }

    return $null
}

function Get-PythonUserScriptsPath {
    $Python = Get-PythonPath
    if (-not $Python) {
        return $null
    }

    $UserBase = & $Python -m site --user-base 2>$null | Select-Object -First 1
    if (-not $UserBase) {
        return $null
    }

    return Join-Path $UserBase "Scripts"
}

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
    $PythonUserScripts = Get-PythonUserScriptsPath
    if ($PythonUserScripts) {
        $Candidates += Join-Path $PythonUserScripts "uv.exe"
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
    $BootstrapPython = Get-PythonPath
    if (-not $BootstrapPython) {
        Write-Error "uv is not installed and Python was not found. Install Python or uv, then run this script again."
    }

    Write-Host "uv is not installed; installing uv with Python pip..."
    & $BootstrapPython -m pip install --user uv
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to install uv with Python pip. Ensure pip is available and network access is working, then run this script again."
    }

    $UvBin = Get-PythonUserScriptsPath
    if ($UvBin) {
        $PathEntries = @()
        if ($env:PATH) {
            foreach ($PathEntry in $env:PATH -split ";") {
                if (-not [string]::IsNullOrWhiteSpace($PathEntry)) {
                    $PathEntries += Format-PathForComparison $PathEntry
                }
            }
        }
        if ((Test-Path $UvBin) -and ($PathEntries -notcontains (Format-PathForComparison $UvBin))) {
            $env:PATH = "$UvBin;$env:PATH"
        }
    }

    $Uv = Get-UvPath
    if (-not $Uv) {
        Write-Error "uv installation script ran, but uv.exe was not found in PATH or the Python user Scripts directory. Add the uv install directory to PATH or install uv manually, then run this script again."
    }
}

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

# Build the uv sync argument list.
$SyncArgs = @()

if (-not $Dev) {
    $SyncArgs += "--no-group", "dev"
    $SyncArgs += "--no-group", "build"
}
if ($Gui) {
    $SyncArgs += "--extra", "gui"
}
if ($Windows) {
    $SyncArgs += "--extra", "windows"
}
if ($Dev) {
    $SyncArgs += "--group", "dev"
}
if ($Venv -ne ".venv") {
    $SyncArgs += "--venv", $Venv
}

& $Uv sync @SyncArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "uv sync failed."
}

Write-Host ""
Write-Host "Juicer installed in $Venv."
Write-Host "Activate the environment with:"
Write-Host "  $Venv\Scripts\Activate.ps1"
Write-Host "Then run:"
Write-Host "  juicer --help"
