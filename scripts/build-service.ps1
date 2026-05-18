param(
    [string]$Python = "python",
    [string]$TargetDir = "",
    [switch]$Clean,
    [switch]$Deploy,
    [switch]$Install,
    [switch]$Start
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$SpecPath = Join-Path $PSScriptRoot "juicer_service.spec"
$DistExe = Join-Path $RepoRoot "dist\juicer_service.exe"

if ([string]::IsNullOrWhiteSpace($TargetDir)) {
    if ($env:PROGRAMDATA) {
        $TargetDir = Join-Path $env:PROGRAMDATA "Juicer"
    }
    else {
        $TargetDir = Join-Path $RepoRoot "dist"
    }
}

$TargetExe = Join-Path $TargetDir "juicer_service.exe"

Push-Location $RepoRoot
try {
    if ($Clean) {
        $cleanPaths = @(
            (Join-Path $RepoRoot "build"),
            (Join-Path $RepoRoot "dist")
        )
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue -Path $cleanPaths
    }

    & $Python -m pip install --upgrade ".[windows]" pyinstaller pyinstaller-hooks-contrib
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed."
    }

    & $Python -m PyInstaller --clean --noconfirm $SpecPath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed."
    }

    if (-not (Test-Path $DistExe)) {
        throw "Expected build output was not created: $DistExe"
    }

    Write-Host "Built $DistExe"

    if ($Deploy -or $Install -or $Start) {
        New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
        Copy-Item -Force $DistExe $TargetExe
        Write-Host "Deployed $TargetExe"
    }

    if ($Install) {
        $previousPythonPath = $env:PYTHONPATH
        try {
            $env:PYTHONPATH = Join-Path $RepoRoot "src"
            & $Python -m juicer.cli service install
            if ($LASTEXITCODE -ne 0) {
                throw "Service installation failed."
            }
        }
        finally {
            $env:PYTHONPATH = $previousPythonPath
        }
    }

    if ($Start) {
        $previousPythonPath = $env:PYTHONPATH
        try {
            $env:PYTHONPATH = Join-Path $RepoRoot "src"
            & $Python -m juicer.cli service start
            if ($LASTEXITCODE -ne 0) {
                throw "Service start failed."
            }
        }
        finally {
            $env:PYTHONPATH = $previousPythonPath
        }
    }
}
finally {
    Pop-Location
}
