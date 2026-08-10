#Requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet('all','x64','ia32')][string]$Arch = 'all',
    [string]$CacheDir = '',
    [string]$ElectronCacheDir = '',
    [switch]$Clean,
    [switch]$SkipTests,
    [switch]$SkipBackendSmoke,
    [switch]$VerboseBuild,
    [switch]$KeepLogs
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = (Resolve-Path (Join-Path $ScriptDir '..')).Path
$DesktopDir = Join-Path $RootDir 'desktop'
$BuildDir = Join-Path $RootDir 'build'
$LogDir = Join-Path $BuildDir 'package-logs'
$VenvDir = Join-Path $RootDir '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$VenvPip = Join-Path $VenvDir 'Scripts\pip.exe'
$SrcDir = Join-Path $RootDir 'src'
$TestsDir = Join-Path $RootDir 'tests'
$DistBackendDir = Join-Path $RootDir 'dist-backend'
$BackendBundleDir = Join-Path $DistBackendDir 'SensorArrayBackend'
$BackendExe = Join-Path $BackendBundleDir 'SensorArrayBackend.exe'
$PyInstallerWorkDir = Join-Path $BuildDir 'pyinstaller'
$ReleaseDir = Join-Path $DesktopDir 'release'
$GenerateIconsScript = Join-Path $ScriptDir 'generate_icons.py'

$WinCodeSignVersion = '2.6.0'
$WinCodeSignName = 'winCodeSign-{0}' -f $WinCodeSignVersion
$WinCodeSignArchive = '{0}.7z' -f $WinCodeSignName
$NsisVersion = '3.0.4.1'
$NsisName = 'nsis-{0}' -f $NsisVersion
$NsisArchive = '{0}.7z' -f $NsisName
$NsisResourcesVersion = '3.4.1'
$NsisResourcesName = 'nsis-resources-{0}' -f $NsisResourcesVersion
$NsisResourcesArchive = '{0}.7z' -f $NsisResourcesName

$NsisNames = @('nsis',$NsisName,$NsisResourcesName)
$WinCodeSignNames = @('winCodeSign',$WinCodeSignName)

$Mirrors = @()

# If the user manually sets a non-Huawei mirror, try it first.
$EnvMirror = $env:ELECTRON_BUILDER_BINARIES_MIRROR
if ($EnvMirror -and ($EnvMirror.ToLowerInvariant().Contains('huaweicloud') -eq $false)) {
    $Mirrors += $EnvMirror
}

# Preferred order:
# 1. GitHub official
# 2. npmmirror
# 3. Huawei fallback
$Mirrors += 'https://github.com/electron-userland/electron-builder-binaries/releases/download/'
$Mirrors += 'https://npmmirror.com/mirrors/electron-builder-binaries/'

# If the user explicitly set Huawei, keep it, but only as fallback.
if ($EnvMirror -and $EnvMirror.ToLowerInvariant().Contains('huaweicloud')) {
    $Mirrors += $EnvMirror
}

$Mirrors += 'https://mirrors.huaweicloud.com/electron-builder-binaries/'
$Mirrors = $Mirrors | Select-Object -Unique

function Step([string]$m) { Write-Host ''; Write-Host ('==> {0}' -f $m) -ForegroundColor Cyan }
function Sub([string]$m) { Write-Host ('  -> {0}' -f $m) -ForegroundColor DarkCyan }
function Ok([string]$m) { Write-Host ('OK: {0}' -f $m) -ForegroundColor Green }
function Warn([string]$m) { Write-Host ('WARN: {0}' -f $m) -ForegroundColor Yellow }
function Fail([string]$m) { Write-Host ('FAIL: {0}' -f $m) -ForegroundColor Red }
function MkDir([string]$p) { if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p -Force | Out-Null } }
function Remove-SafePath([string]$p) { if (Test-Path $p) { Sub ('Remove {0}' -f $p); Remove-Item -Recurse -Force $p -ErrorAction SilentlyContinue } }
function NeedFile([string]$p,[string]$m) { if (-not (Test-Path $p -PathType Leaf)) { throw ('{0} Missing file: {1}' -f $m,$p) } }
function NeedDir([string]$p,[string]$m) { if (-not (Test-Path $p -PathType Container)) { throw ('{0} Missing directory: {1}' -f $m,$p) } }

function Invoke-Cmd {
    param(
        [Parameter(Mandatory=$true)][string]$Exe,
        [string[]]$ArgList=@(),
        [string]$Cwd=$RootDir,
        [string]$Log=''
    )
    $argText = $ArgList -join ' '
    Sub ('{0} {1}' -f $Exe,$argText)
    if ($Log) { MkDir (Split-Path -Parent $Log) }
    Push-Location $Cwd
    $oldEap = $ErrorActionPreference
    $lines = New-Object System.Collections.Generic.List[string]
    try {
        $ErrorActionPreference = 'Continue'
        & $Exe @ArgList 2>&1 | ForEach-Object {
            $line = $_.ToString()
            Write-Host $line
            [void]$lines.Add($line)
        }
        $exit = $LASTEXITCODE
        $ErrorActionPreference = $oldEap
        $txt = $lines -join [Environment]::NewLine
        if ($Log) { Set-Content -Path $Log -Value $txt -Encoding UTF8 }
        if ($null -ne $exit -and $exit -ne 0) {
            throw ((@('Command failed with exit code {0}' -f $exit,'','Command:',('{0} {1}' -f $Exe,$argText),'','Working directory:',$Cwd,'','Log:',$Log,'','Output:',$txt)) -join [Environment]::NewLine)
        }
        return $txt
    }
    finally {
        $ErrorActionPreference = $oldEap
        Pop-Location
    }
}

$StartedProcesses = New-Object System.Collections.Generic.List[System.Diagnostics.Process]
function Add-Proc([System.Diagnostics.Process]$p) { [void]$StartedProcesses.Add($p) }
function Stop-Tree([int]$ProcessId) { try { if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) { & taskkill.exe /PID $ProcessId /T /F | Out-Null } } catch { Warn ('Failed to stop PID={0}. {1}' -f $ProcessId,$_.Exception.Message) } }
function Stop-Started { foreach ($p in $StartedProcesses) { try { if ($null -ne $p -and -not $p.HasExited) { Stop-Tree $p.Id } } catch {} } }

function Resolve-Caches {
    Step 'Resolve Electron caches'
    if ($ElectronCacheDir) { $env:ELECTRON_CACHE = $ElectronCacheDir } elseif (-not $env:ELECTRON_CACHE) { $env:ELECTRON_CACHE = 'C:\electron-cache' }
    if ($CacheDir) { $env:ELECTRON_BUILDER_CACHE = $CacheDir } elseif (-not $env:ELECTRON_BUILDER_CACHE) { $env:ELECTRON_BUILDER_CACHE = 'C:\electron-builder-cache' }
    MkDir $env:ELECTRON_CACHE
    MkDir $env:ELECTRON_BUILDER_CACHE
    Write-Host ('Electron cache:         {0}' -f $env:ELECTRON_CACHE)
    Write-Host ('Electron Builder cache: {0}' -f $env:ELECTRON_BUILDER_CACHE)
    Write-Host ('Electron mirror:        {0}' -f $env:ELECTRON_MIRROR)
    Write-Host ('Builder mirror order:   {0}' -f ($Mirrors -join ' | '))
    if ($RootDir.Contains('OneDrive')) { Warn 'Project path is under OneDrive. Cache is kept outside the project.' }
}

function CacheDirs([string[]]$names) {
    $d = @()
    foreach ($n in $names) {
        $d += Join-Path $env:ELECTRON_BUILDER_CACHE $n
        $d += Join-Path $env:ELECTRON_BUILDER_CACHE ('Cache\{0}' -f $n)
        if ($env:LOCALAPPDATA) { $d += Join-Path $env:LOCALAPPDATA ('electron-builder\Cache\{0}' -f $n) }
    }
    return ($d | Select-Object -Unique)
}

function Clear-NsisCache {
    Step 'Clear Electron Builder NSIS cache'
    foreach ($d in (CacheDirs $NsisNames)) { Remove-SafePath $d }
    $roots = @($env:ELECTRON_BUILDER_CACHE,(Join-Path $env:ELECTRON_BUILDER_CACHE 'Cache'))
    if ($env:LOCALAPPDATA) { $roots += Join-Path $env:LOCALAPPDATA 'electron-builder\Cache' }
    foreach ($r in $roots) {
        if (Test-Path $r) {
            Get-ChildItem -Path $r -Recurse -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.Name.ToLowerInvariant().Contains('nsis') -or $_.Name.ToLowerInvariant().EndsWith('.7z') -or $_.Name.ToLowerInvariant().EndsWith('.tmp') -or $_.Name.ToLowerInvariant().EndsWith('.download') } |
                ForEach-Object { Remove-Item -Path $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
        }
    }
}
function Clear-WinCodeSignCache { Step 'Clear Electron Builder winCodeSign cache'; foreach ($d in (CacheDirs $WinCodeSignNames)) { Remove-SafePath $d } }
function Clear-InstallerToolCache { Step 'Clear Electron Builder installer tool cache'; Clear-NsisCache; Clear-WinCodeSignCache }

function Is-BuilderCacheError([string]$text) {
    if ($null -eq $text) { return $false }
    $s = $text.ToLowerInvariant()
    foreach ($m in @('nsis','wincodesign','electron-builder-binaries','err_electron_builder_cannot_execute','downloadartifact','eof','timeout','connection reset','forcibly closed','cannot create symbolic link','libcrypto','libssl')) {
        if ($s.Contains($m)) { return $true }
    }
    return $false
}
function Normalize-Mirror([string]$Mirror) { if ($Mirror.EndsWith('/')) { return $Mirror }; return ($Mirror + '/') }
function Get-SevenZipPath {
    $p = Join-Path $DesktopDir 'node_modules\7zip-bin\win\x64\7za.exe'
    if (Test-Path $p) { return $p }
    $cmd = Get-Command '7z.exe' -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $cmd = Get-Command '7za.exe' -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw '7za.exe was not found. Run npm install in desktop first.'
}

function Download-FileWithRetry {
    param([string]$Url,[string]$OutFile,[int]$Retries=3)
    for ($i=1; $i -le $Retries; $i++) {
        try {
            Sub ('Download {0} attempt {1}' -f $Url,$i)
            $tmp = '{0}.tmp' -f $OutFile
            Remove-Item -Force $tmp -ErrorAction SilentlyContinue
            Invoke-WebRequest -Uri $Url -OutFile $tmp -UseBasicParsing -TimeoutSec 180
            $item = Get-Item $tmp -ErrorAction Stop
            if ($item.Length -lt 1024) { throw ('Downloaded file is too small: {0} bytes' -f $item.Length) }
            Move-Item -Force $tmp $OutFile
            return
        }
        catch {
            Warn ('Download failed: {0}' -f $_.Exception.Message)
            Remove-Item -Force ('{0}.tmp' -f $OutFile) -ErrorAction SilentlyContinue
            if ($i -eq $Retries) { throw }
            Start-Sleep -Seconds ([Math]::Min(15,2*$i))
        }
    }
}

function Test-WinCodeSignCacheReady {
    $target = Join-Path (Join-Path $env:ELECTRON_BUILDER_CACHE 'winCodeSign') $WinCodeSignName
    if (-not (Test-Path $target -PathType Container)) { return $false }
    $rcedit = Get-ChildItem -Path $target -Recurse -File -Filter 'rcedit*.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    return ($null -ne $rcedit)
}

function Ensure-WinCodeSignCache {
    param([string]$Mirror)
    Step 'Prepare winCodeSign cache without darwin symlinks'
    if (Test-WinCodeSignCacheReady) { Ok ('winCodeSign cache ready: {0}' -f $WinCodeSignName); return }
    $root = Join-Path $env:ELECTRON_BUILDER_CACHE 'winCodeSign'
    $target = Join-Path $root $WinCodeSignName
    Remove-SafePath $root
    MkDir $root
    $archivePath = Join-Path $root $WinCodeSignArchive
    $url = '{0}{1}/{2}' -f (Normalize-Mirror $Mirror),$WinCodeSignName,$WinCodeSignArchive
    Download-FileWithRetry -Url $url -OutFile $archivePath -Retries 3
    $sevenZip = Get-SevenZipPath
    MkDir $target
    Invoke-Cmd -Exe $sevenZip -ArgList @('x','-bd','-y',$archivePath,('-o{0}' -f $target),'-xr!darwin','-xr!*darwin*') -Cwd $root | Out-Null
    if (-not (Test-WinCodeSignCacheReady)) { throw ('winCodeSign cache is still not valid after extraction: {0}' -f $target) }
    Remove-Item -Force $archivePath -ErrorAction SilentlyContinue
    Ok ('winCodeSign cache prepared: {0}' -f $target)
}

function Test-NsisCacheReady {
    $root = Join-Path $env:ELECTRON_BUILDER_CACHE 'nsis'
    $nsisTarget = Join-Path $root $NsisName
    $resTarget = Join-Path $root $NsisResourcesName
    if (-not (Test-Path $nsisTarget -PathType Container)) { return $false }
    if (-not (Test-Path $resTarget -PathType Container)) { return $false }
    $makensis = Get-ChildItem -Path $nsisTarget -Recurse -File -Filter 'makensis.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $makensis) { return $false }
    $resAny = Get-ChildItem -Path $resTarget -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $resAny) { return $false }
    return $true
}

function Expand-ArchiveWith7Zip {
    param([string]$ArchivePath,[string]$TargetDir)
    $sevenZip = Get-SevenZipPath
    Remove-SafePath $TargetDir
    MkDir $TargetDir
    Invoke-Cmd -Exe $sevenZip -ArgList @('x','-bd','-y',$ArchivePath,('-o{0}' -f $TargetDir)) -Cwd (Split-Path -Parent $ArchivePath) | Out-Null
}

function Ensure-NsisCache {
    param([string]$Mirror)
    Step 'Prepare NSIS cache'
    if (Test-NsisCacheReady) { Ok 'NSIS cache ready'; return }
    $root = Join-Path $env:ELECTRON_BUILDER_CACHE 'nsis'
    Remove-SafePath $root
    MkDir $root
    $base = Normalize-Mirror $Mirror
    $nsisArchivePath = Join-Path $root $NsisArchive
    $resArchivePath = Join-Path $root $NsisResourcesArchive
    $nsisUrl = '{0}{1}/{2}' -f $base,$NsisName,$NsisArchive
    $resUrl = '{0}{1}/{2}' -f $base,$NsisResourcesName,$NsisResourcesArchive
    Download-FileWithRetry -Url $nsisUrl -OutFile $nsisArchivePath -Retries 3
    Download-FileWithRetry -Url $resUrl -OutFile $resArchivePath -Retries 3
    Expand-ArchiveWith7Zip -ArchivePath $nsisArchivePath -TargetDir (Join-Path $root $NsisName)
    Expand-ArchiveWith7Zip -ArchivePath $resArchivePath -TargetDir (Join-Path $root $NsisResourcesName)
    Remove-Item -Force $nsisArchivePath -ErrorAction SilentlyContinue
    Remove-Item -Force $resArchivePath -ErrorAction SilentlyContinue
    if (-not (Test-NsisCacheReady)) { throw ('NSIS cache is still not valid after extraction: {0}' -f $root) }
    Ok ('NSIS cache prepared: {0}' -f $root)
}

function Invoke-EB {
    param([string[]]$ArgList,[string]$Label)
    MkDir $LogDir
    $attempt = 0
    $last = ''
    foreach ($mirror in $Mirrors) {
        $attempt++
        Step ('{0} attempt {1}' -f $Label,$attempt)
        Write-Host ('Using Electron Builder binary mirror: {0}' -f $mirror)
        $env:ELECTRON_BUILDER_BINARIES_MIRROR = $mirror
        Clear-InstallerToolCache
        try {
            Ensure-WinCodeSignCache -Mirror $mirror
            if ($Label -eq 'nsis-installer') { Ensure-NsisCache -Mirror $mirror }
        }
        catch {
            $last = $_.Exception.Message
            Warn ('Preparing Electron Builder cache failed with mirror: {0}' -f $mirror)
            Warn $last
            Clear-InstallerToolCache
            Start-Sleep -Seconds ([Math]::Min(12,2*$attempt))
            continue
        }
        $log = Join-Path $LogDir ('electron-builder-{0}-attempt-{1}.log' -f $Label.Replace(' ','-'),$attempt)
        try {
            $out = Invoke-Cmd -Exe 'npx.cmd' -ArgList (@('electron-builder') + $ArgList + @('--publish','never')) -Cwd $DesktopDir -Log $log
            Ok ('{0} succeeded with mirror: {1}' -f $Label,$mirror)
            if (-not $KeepLogs) { Remove-Item -Force $log -ErrorAction SilentlyContinue }
            return $out
        }
        catch {
            $last = $_.Exception.Message
            $logText = ''
            if (Test-Path $log) { $logText = Get-Content -Raw -Path $log -ErrorAction SilentlyContinue }
            $combined = ($last + [Environment]::NewLine + $logText)
            Warn ('{0} failed with mirror: {1}' -f $Label,$mirror)
            Warn ('Log file: {0}' -f $log)
            if (Is-BuilderCacheError $combined) {
                Warn 'Detected Electron Builder tool/cache/download failure. Clear cache and try next mirror.'
                Clear-InstallerToolCache
                Start-Sleep -Seconds ([Math]::Min(12,2*$attempt))
                continue
            }
            throw
        }
    }
    throw ((@('{0} failed after all Electron Builder binary mirrors.' -f $Label,'','Tried mirrors:',($Mirrors -join [Environment]::NewLine),'','Last error:',$last)) -join [Environment]::NewLine)
}

function Resolve-SystemPython { $c = Get-Command 'python.exe' -ErrorAction SilentlyContinue; if ($c) { return $c.Source }; $c = Get-Command 'py.exe' -ErrorAction SilentlyContinue; if ($c) { return $c.Source }; throw 'Python was not found.' }
function Get-PythonArch([string]$py) { $arch = & $py -c 'import platform; print(platform.architecture()[0])'; if ($LASTEXITCODE -ne 0) { throw ('Failed to detect Python architecture using {0}' -f $py) }; return $arch.Trim() }
function Ensure-Venv {
    Step 'Prepare Python virtual environment'
    if (-not (Test-Path $VenvPython)) { $sys = Resolve-SystemPython; Sub ('Creating venv with {0}' -f $sys); Invoke-Cmd -Exe $sys -ArgList @('-m','venv',$VenvDir) -Cwd $RootDir | Out-Null }
    NeedFile $VenvPython 'Python venv was not created correctly.'
    NeedFile $VenvPip 'Pip in venv was not created correctly.'
    $ver = & $VenvPython --version
    $arch = Get-PythonArch $VenvPython
    Write-Host ('Python: {0}' -f $ver)
    Write-Host ('Python architecture: {0}' -f $arch)
    if ($Arch -eq 'all' -and $arch -ne '32bit') { Warn ('Combined NSIS can include x64 and ia32 Electron, but this backend sidecar is {0}.' -f $arch); Warn 'For real 32-bit Windows support, backend sidecar also needs a 32-bit PyInstaller build.' }
    if ($Arch -eq 'ia32' -and $arch -ne '32bit') { Warn ('You requested ia32, but backend sidecar is built with {0} Python.' -f $arch) }
}
function Install-PyDeps { Step 'Install Python dependencies'; Invoke-Cmd -Exe $VenvPython -ArgList @('-m','pip','install','--upgrade','pip') -Cwd $RootDir | Out-Null; Invoke-Cmd -Exe $VenvPython -ArgList @('-m','pip','install','-e','.') -Cwd $RootDir | Out-Null; Invoke-Cmd -Exe $VenvPython -ArgList @('-m','pip','install','pyinstaller') -Cwd $RootDir | Out-Null }
function Generate-Icons { Step 'Generate Windows icons'; if (Test-Path $GenerateIconsScript) { Invoke-Cmd -Exe $VenvPython -ArgList @($GenerateIconsScript) -Cwd $RootDir | Out-Null } else { Warn 'scripts\generate_icons.py not found. Skipping icon generation.' } }
function Run-PyChecks {
    if ($SkipTests) { Warn 'Skipping Python checks.'; return }
    Step 'Run Python checks'
    $t = @()
    if (Test-Path $SrcDir) { $t += 'src' }
    if (Test-Path $TestsDir) { $t += 'tests' }
    if (Test-Path $ScriptDir) { $t += 'scripts' }
    Invoke-Cmd -Exe $VenvPython -ArgList (@('-m','compileall') + $t) -Cwd $RootDir | Out-Null
    Invoke-Cmd -Exe $VenvPython -ArgList @('-m','pytest') -Cwd $RootDir | Out-Null
}
function Build-Backend {
    Step 'Build Python backend sidecar with PyInstaller'
    Remove-SafePath $DistBackendDir
    MkDir $PyInstallerWorkDir
    $entry = Join-Path $SrcDir 'sensorarray_backend\__main__.py'
    NeedFile $entry 'Backend entry point is missing.'
    $a = @('-m','PyInstaller','--noconfirm','--clean','--onedir','--name','SensorArrayBackend','--distpath',$DistBackendDir,'--workpath',(Join-Path $PyInstallerWorkDir 'SensorArrayBackend'),'--specpath',$PyInstallerWorkDir,'--paths',$SrcDir,'--collect-all','sensorarray_backend','--collect-all','sensorarray_app','--collect-submodules','serial','--collect-submodules','bleak','--collect-submodules','websockets','--collect-submodules','uvicorn')
    $ico = Join-Path $DesktopDir 'assets\icons\sensorarray-icon.ico'
    if (Test-Path $ico) { $a += @('--icon',$ico) }
    $a += $entry
    Invoke-Cmd -Exe $VenvPython -ArgList $a -Cwd $RootDir | Out-Null
    NeedFile $BackendExe 'PyInstaller backend sidecar build failed.'
    Write-Host ('Backend sidecar: {0}' -f $BackendExe)
}
function Test-Http([string]$url,[int]$sec=20) { $end = (Get-Date).AddSeconds($sec); $last = ''; while ((Get-Date) -lt $end) { try { $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { return $r.Content } } catch { $last = $_.Exception.Message; Start-Sleep -Milliseconds 400 } }; throw ('HTTP endpoint did not become ready: {0} Last error: {1}' -f $url,$last) }
function Smoke-Backend {
    if ($SkipBackendSmoke) { Warn 'Skipping backend smoke.'; return }
    Step 'Smoke test Python backend sidecar'
    NeedFile $BackendExe 'Backend sidecar does not exist.'
    $ok = $false
    $proc = $null
    foreach ($port in @(8888,6666,8765,9010,9011,9012)) {
        try {
            $si = New-Object System.Diagnostics.ProcessStartInfo
            $si.FileName = $BackendExe
            $si.WorkingDirectory = $BackendBundleDir
            $si.UseShellExecute = $false
            $si.RedirectStandardOutput = $true
            $si.RedirectStandardError = $true
            $si.Arguments = ('--host 127.0.0.1 --port {0}' -f $port)
            $proc = New-Object System.Diagnostics.Process
            $proc.StartInfo = $si
            [void]$proc.Start()
            Add-Proc $proc
            $url = 'http://127.0.0.1:{0}/health' -f $port
            $content = Test-Http $url 12
            Write-Host ('Backend health OK: {0}' -f $content)
            Write-Host ('Selected backend smoke port: {0}' -f $port)
            Ok ('Backend sidecar smoke passed: {0}' -f $url)
            $ok = $true
            break
        }
        catch {
            if ($null -ne $proc -and -not $proc.HasExited) { Stop-Tree $proc.Id }
            Warn ('Backend smoke failed on port {0}. {1}' -f $port,$_.Exception.Message)
        }
    }
    if (-not $ok) { throw 'Backend sidecar smoke failed on all candidate ports.' }
    if ($null -ne $proc -and -not $proc.HasExited) { Stop-Tree $proc.Id }
}
function Ensure-Node { Step 'Check Node.js and npm'; foreach ($x in @('node.exe','npm.cmd','npx.cmd')) { if (-not (Get-Command $x -ErrorAction SilentlyContinue)) { throw ('{0} was not found.' -f $x) } }; Write-Host ('Node: {0}' -f (& node.exe --version)); Write-Host ('npm:  {0}' -f (& npm.cmd --version)) }
function Install-Electron { Step 'Install Electron dependencies'; NeedDir $DesktopDir 'Desktop project directory is missing.'; Invoke-Cmd -Exe 'npm.cmd' -ArgList @('install','--no-audit','--no-fund') -Cwd $DesktopDir | Out-Null }
function Run-ElectronChecks { Step 'Run Electron checks'; Invoke-Cmd -Exe 'npm.cmd' -ArgList @('run','typecheck') -Cwd $DesktopDir | Out-Null; Invoke-Cmd -Exe 'npm.cmd' -ArgList @('run','lint') -Cwd $DesktopDir | Out-Null; if (-not $SkipTests) { Invoke-Cmd -Exe 'npm.cmd' -ArgList @('run','test') -Cwd $DesktopDir | Out-Null } else { Warn 'Skipping Electron tests.' }; Invoke-Cmd -Exe 'npm.cmd' -ArgList @('run','build') -Cwd $DesktopDir | Out-Null }
function ArchArgs { if ($Arch -eq 'x64') { return @('--x64') }; if ($Arch -eq 'ia32') { return @('--ia32') }; return @('--x64','--ia32') }
function Build-Dir { Step 'Build win-unpacked application'; Invoke-EB -ArgList (@('--win','dir') + (ArchArgs)) -Label 'win-unpacked' | Out-Null }
function Build-Nsis { Step 'Build NSIS installer'; Invoke-EB -ArgList (@('--win','nsis') + (ArchArgs)) -Label 'nsis-installer' | Out-Null }
function Installers { if (-not (Test-Path $ReleaseDir)) { return @() }; return @(Get-ChildItem -Path $ReleaseDir -File -Filter '*.exe' -ErrorAction SilentlyContinue | Where-Object { $_.Name -notmatch 'SensorArrayBackend' -and $_.DirectoryName -eq $ReleaseDir } | Sort-Object LastWriteTime -Descending) }
function PeArch([string]$exe) { $b = [IO.File]::ReadAllBytes($exe); if ($b.Length -lt 64) { return 'unknown' }; $o = [BitConverter]::ToInt32($b,0x3C); if ($o -le 0 -or ($o + 6) -ge $b.Length) { return 'unknown' }; $sig = [Text.Encoding]::ASCII.GetString($b,$o,4); if ($sig -ne "PE`0`0") { return 'unknown' }; $m = [BitConverter]::ToUInt16($b,$o+4); if ($m -eq 0x014c) { return 'ia32' }; if ($m -eq 0x8664) { return 'x64' }; if ($m -eq 0xaa64) { return 'arm64' }; return ('0x{0:X4}' -f $m) }
function Verify { Step 'Verify release artifacts'; NeedDir $ReleaseDir 'Release directory was not generated.'; $ins = Installers; if ($ins.Count -eq 0) { throw ('NSIS installer was not generated under {0}' -f $ReleaseDir) }; Write-Host 'Installer artifact(s):'; foreach ($i in $ins) { Write-Host ('  {0} [{1} MB]' -f $i.FullName,([Math]::Round($i.Length/1MB,2))) }; foreach ($p in @('win-unpacked\SensorArray.exe','win-ia32-unpacked\SensorArray.exe','win-x64-unpacked\SensorArray.exe')) { $e = Join-Path $ReleaseDir $p; if (Test-Path $e) { Write-Host ('Unpacked app: {0}' -f $e) } }; $cfg = Join-Path $ReleaseDir 'builder-effective-config.yaml'; if (Test-Path $cfg) { Write-Host ('Builder config: {0}' -f $cfg) } else { Warn 'builder-effective-config.yaml was not found.' }; NeedFile $BackendExe 'Backend sidecar was not built.'; Ok 'Artifact verification passed.' }
function ArchSummary { Step 'Executable architecture summary'; if (Test-Path $BackendExe) { Write-Host ('Backend sidecar: {0}  {1}' -f (PeArch $BackendExe),$BackendExe) }; foreach ($p in @('win-unpacked\SensorArray.exe','win-ia32-unpacked\SensorArray.exe','win-x64-unpacked\SensorArray.exe')) { $e = Join-Path $ReleaseDir $p; if (Test-Path $e) { Write-Host ('Electron app:    {0}  {1}' -f (PeArch $e),$e) } }; if ($Arch -eq 'all') { Warn 'Combined NSIS can include x64 plus ia32 Electron. Confirm backend sidecar architecture before true 32-bit distribution.' } }
function Clean-All { Step 'Clean build artifacts'; foreach ($p in @($DistBackendDir,$PyInstallerWorkDir,$ReleaseDir,(Join-Path $DesktopDir 'dist'),(Join-Path $DesktopDir 'dist-electron'))) { Remove-SafePath $p } }
function Summary { Step 'Packaging summary'; Write-Host ('Root:                    {0}' -f $RootDir); Write-Host ('Desktop:                 {0}' -f $DesktopDir); Write-Host ('Release:                 {0}' -f $ReleaseDir); Write-Host ('Requested arch:          {0}' -f $Arch); Write-Host ('Electron cache:          {0}' -f $env:ELECTRON_CACHE); Write-Host ('Electron Builder cache:  {0}' -f $env:ELECTRON_BUILDER_CACHE); Write-Host ('Electron mirror:         {0}' -f $env:ELECTRON_MIRROR); if ($env:ELECTRON_BUILDER_BINARIES_MIRROR) { Write-Host ('Builder binaries mirror: {0}' -f $env:ELECTRON_BUILDER_BINARIES_MIRROR) }; $ins = Installers; if ($ins.Count -gt 0) { Write-Host ''; Write-Host 'Installer(s):'; foreach ($i in $ins) { Write-Host ('  {0}' -f $i.FullName) } } }

try {
    Step 'SensorArray Windows packaging'
    Write-Host 'Mode: NSIS installer. Arch all builds one combined x64 plus ia32 installer.'
    Resolve-Caches
    Ensure-Node
    if ($Clean) { Clean-All }
    Ensure-Venv
    Install-PyDeps
    Generate-Icons
    Run-PyChecks
    Build-Backend
    Smoke-Backend
    Install-Electron
    Run-ElectronChecks
    Clear-InstallerToolCache
    Build-Dir
    Build-Nsis
    Verify
    ArchSummary
    Summary
    Ok 'Windows packaging completed successfully.'
}
catch {
    Fail 'Windows packaging failed.'
    Write-Host ''
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ''
    try { Summary } catch {}
    exit 1
}
finally {
    Stop-Started
}