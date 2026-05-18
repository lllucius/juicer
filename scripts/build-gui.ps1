param(
    [string]$TargetDir = "",
    [switch]$Clean,
    [switch]$Deploy
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$SpecPath = Join-Path $PSScriptRoot "juicer_gui.spec"
$DistExe = Join-Path $RepoRoot "dist\juicer-gui.exe"

if ([string]::IsNullOrWhiteSpace($TargetDir)) {
    if ($env:PROGRAMDATA) {
        $TargetDir = Join-Path $env:PROGRAMDATA "Juicer"
    }
    else {
        $TargetDir = Join-Path $RepoRoot "dist"
    }
}

$TargetExe = Join-Path $TargetDir "juicer-gui.exe"

# Build from the repository root so the package and PyInstaller spec resolve
# paths consistently no matter where the script starts.
Push-Location $RepoRoot
try {
    if ($Clean) {
        $cleanPaths = @(
            (Join-Path $RepoRoot "build"),
            (Join-Path $RepoRoot "dist")
        )
        $cleanPaths | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    }

    # Sync the project with the build dependency group so PyInstaller is available.
    # The gui and windows extras are included so PySide6 and pywin32 are bundled.
    uv sync --group build --extra gui --extra windows
    if ($LASTEXITCODE -ne 0) {
        throw "uv sync failed."
    }

    uv run pyinstaller --clean --noconfirm $SpecPath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed."
    }

    if (-not (Test-Path $DistExe)) {
        throw "Expected build output was not created: $DistExe"
    }

    Write-Host "Built $DistExe"

    if ($Deploy) {
        New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
        Copy-Item -Force $DistExe $TargetExe
        Write-Host "Deployed $TargetExe"
    }
}
finally {
    Pop-Location
}
