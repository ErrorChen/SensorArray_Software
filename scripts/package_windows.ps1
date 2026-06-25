$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $ScriptRoot ".."))
$RepoRootWithSeparator = $RepoRoot
if (-not $RepoRootWithSeparator.EndsWith([System.IO.Path]::DirectorySeparatorChar)) {
    $RepoRootWithSeparator += [System.IO.Path]::DirectorySeparatorChar
}

function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )

    Write-Host ""
    Write-Host "==> $Name"
    & $Action
}

function Resolve-RepoPath {
    param([Parameter(Mandatory = $true)][string]$RelativePath)

    $resolved = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $RelativePath))
    if (-not $resolved.StartsWith($RepoRootWithSeparator, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to access path outside repository: $resolved"
    }
    return $resolved
}

function Remove-RepoPath {
    param([Parameter(Mandatory = $true)][string]$RelativePath)

    $target = Resolve-RepoPath $RelativePath
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

function Get-FileSizeMB {
    param([Parameter(Mandatory = $true)][string]$Path)

    $item = Get-Item -LiteralPath $Path
    return [Math]::Round($item.Length / 1MB, 2)
}

function Wait-BackendHealth {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)][string]$StderrPath,
        [int]$TimeoutSeconds = 25
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastError = ""
    while ((Get-Date) -lt $deadline) {
        if ($Process.HasExited) {
            $stderr = if (Test-Path -LiteralPath $StderrPath) { Get-Content -Raw -LiteralPath $StderrPath } else { "" }
            throw "Backend exited before /health responded. ExitCode=$($Process.ExitCode)`n$stderr"
        }

        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) {
                Write-Host "Backend health OK: $($response.Content)"
                return
            }
            $lastError = "HTTP $($response.StatusCode)"
        } catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Milliseconds 300
    }

    $stderr = if (Test-Path -LiteralPath $StderrPath) { Get-Content -Raw -LiteralPath $StderrPath } else { "" }
    throw "Timed out waiting for backend health: $lastError`n$stderr"
}

function Initialize-ElectronBuilderCache {
    $cacheRoot = Resolve-RepoPath "build\electron-builder-cache"
    $winCodeSignRoot = Join-Path $cacheRoot "winCodeSign"
    $winCodeSignDir = Join-Path $winCodeSignRoot "winCodeSign-2.6.0"
    $rcedit = Join-Path $winCodeSignDir "rcedit-x64.exe"
    $sevenZip = Resolve-RepoPath "desktop\node_modules\7zip-bin\win\x64\7za.exe"

    $env:ELECTRON_BUILDER_CACHE = $cacheRoot
    if (Test-Path -LiteralPath $rcedit) {
        Write-Host "Electron Builder cache: $cacheRoot"
        return
    }
    if (-not (Test-Path -LiteralPath $sevenZip)) {
        throw "7za.exe not found. Run npm install before preparing Electron Builder cache."
    }

    New-Item -ItemType Directory -Force -Path $winCodeSignRoot | Out-Null
    $archive = $null
    $localWinCodeSignCache = Join-Path $env:LOCALAPPDATA "electron-builder\Cache\winCodeSign"
    if (Test-Path -LiteralPath $localWinCodeSignCache) {
        $archive = Get-ChildItem -LiteralPath $localWinCodeSignCache -Filter "*.7z" |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
    }
    if (-not $archive) {
        $archivePath = Join-Path $winCodeSignRoot "winCodeSign-2.6.0.7z"
        $url = "https://github.com/electron-userland/electron-builder-binaries/releases/download/winCodeSign-2.6.0/winCodeSign-2.6.0.7z"
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $archivePath
        $archive = Get-Item -LiteralPath $archivePath
    }

    if (Test-Path -LiteralPath $winCodeSignDir) {
        Remove-Item -LiteralPath $winCodeSignDir -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $winCodeSignDir | Out-Null

    & $sevenZip x -bd $archive.FullName "-o$winCodeSignDir"
    if (-not (Test-Path -LiteralPath $rcedit)) {
        throw "Failed to prepare Electron Builder winCodeSign cache: $winCodeSignDir"
    }
    Write-Host "Electron Builder cache: $cacheRoot"
}

Set-Location -LiteralPath $RepoRoot
$env:CSC_IDENTITY_AUTO_DISCOVERY = "false"

if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot "pyproject.toml"))) {
    throw "pyproject.toml not found. Run this script from the SensorArray repository."
}
if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot "desktop\package.json"))) {
    throw "desktop\package.json not found. Run this script from the SensorArray repository."
}

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$BackendExe = Join-Path $RepoRoot "dist-backend\SensorArrayBackend\SensorArrayBackend.exe"
$DesktopDir = Join-Path $RepoRoot "desktop"
$ReleaseDir = Join-Path $DesktopDir "release"
$UnpackedExe = Join-Path $ReleaseDir "win-unpacked\SensorArray.exe"
$ElectronBuilderCache = Join-Path $RepoRoot "build\electron-builder-cache"
$env:ELECTRON_BUILDER_CACHE = $ElectronBuilderCache

Invoke-Step "Prepare Python virtual environment" {
    if (-not (Test-Path -LiteralPath $Python)) {
        $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
        if ($pyLauncher) {
            & py -3.11 -m venv .venv
        } else {
            & python -m venv .venv
        }
    }
    if (-not (Test-Path -LiteralPath $Python)) {
        throw "Python virtual environment was not created: $Python"
    }
    $pythonVersion = & $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
    Write-Host "Python: $pythonVersion"
}

Invoke-Step "Install Python dependencies" {
    & $Python -m pip install --upgrade pip
    & $Python -m pip install -e ".[dev]"
    & $Python -m pip install pyinstaller
}

Invoke-Step "Generate Windows icons" {
    & $Python scripts\generate_icons.py
}

Invoke-Step "Run Python checks" {
    & $Python -m compileall src tests scripts
    & $Python -m pytest -q
}

Invoke-Step "Build Python backend sidecar with PyInstaller" {
    Remove-RepoPath "dist-backend\SensorArrayBackend"
    Remove-RepoPath "build\pyinstaller"

    $pyinstallerArgs = @(
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        "SensorArrayBackend",
        "--distpath",
        (Resolve-RepoPath "dist-backend"),
        "--workpath",
        (Resolve-RepoPath "build\pyinstaller"),
        "--specpath",
        (Resolve-RepoPath "build\pyinstaller"),
        "--paths",
        (Resolve-RepoPath "src"),
        "--collect-submodules",
        "sensorarray_backend",
        "--collect-submodules",
        "sensorarray_app",
        "--collect-submodules",
        "serial",
        "--collect-submodules",
        "bleak",
        "--collect-submodules",
        "websockets",
        "--collect-submodules",
        "uvicorn",
        "--hidden-import",
        "serial.tools.list_ports_windows",
        (Resolve-RepoPath "src\sensorarray_backend\__main__.py")
    )
    & $Python -m PyInstaller @pyinstallerArgs

    if (-not (Test-Path -LiteralPath $BackendExe)) {
        throw "PyInstaller did not create backend exe: $BackendExe"
    }
    Write-Host "Backend sidecar: $BackendExe"
}

Invoke-Step "Smoke test Python backend sidecar" {
    $stdoutPath = Join-Path $env:TEMP "sensorarray-backend-smoke.stdout.log"
    $stderrPath = Join-Path $env:TEMP "sensorarray-backend-smoke.stderr.log"
    if (Test-Path -LiteralPath $stdoutPath) { Remove-Item -LiteralPath $stdoutPath -Force }
    if (Test-Path -LiteralPath $stderrPath) { Remove-Item -LiteralPath $stderrPath -Force }

    $backendProcess = Start-Process -FilePath $BackendExe `
        -ArgumentList @("--host", "127.0.0.1", "--port", "8765") `
        -WorkingDirectory (Split-Path -Parent $BackendExe) `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -PassThru
    try {
        Wait-BackendHealth -Url "http://127.0.0.1:8765/health" -Process $backendProcess -StderrPath $stderrPath
    } finally {
        if ($backendProcess -and -not $backendProcess.HasExited) {
            Stop-Process -Id $backendProcess.Id -Force
            Wait-Process -Id $backendProcess.Id -Timeout 5 -ErrorAction SilentlyContinue
        }
    }
}

Invoke-Step "Install Electron dependencies" {
    Push-Location -LiteralPath $DesktopDir
    try {
        npm.cmd install
    } finally {
        Pop-Location
    }
}

Invoke-Step "Prepare Electron Builder cache" {
    Initialize-ElectronBuilderCache
}

Invoke-Step "Run Electron checks" {
    Push-Location -LiteralPath $DesktopDir
    try {
        npm.cmd run typecheck
        npm.cmd run lint
        npm.cmd run test
        npm.cmd run build
    } finally {
        Pop-Location
    }
}

Invoke-Step "Build win-unpacked application" {
    Push-Location -LiteralPath $DesktopDir
    try {
        npm.cmd run package:dir
    } finally {
        Pop-Location
    }
    if (-not (Test-Path -LiteralPath $UnpackedExe)) {
        throw "win-unpacked executable not found: $UnpackedExe"
    }
    Write-Host "win-unpacked exe: $UnpackedExe"
}

Invoke-Step "Build NSIS installer" {
    Push-Location -LiteralPath $DesktopDir
    try {
        npm.cmd run package:win
    } finally {
        Pop-Location
    }

    $installer = Get-ChildItem -LiteralPath $ReleaseDir -Filter "*-nsis.exe" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $installer) {
        throw "NSIS installer was not generated under $ReleaseDir"
    }

    Write-Host "NSIS installer: $($installer.FullName)"
    Write-Host "Installer size: $(Get-FileSizeMB $installer.FullName) MB"
}
