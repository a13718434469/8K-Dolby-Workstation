# Windows PowerShell 5.1+; downloads only from the original projects.
[CmdletBinding()]
param(
    [switch]$CoreOnly,
    [switch]$WithDovi,
    [switch]$WithAudioAI,
    [switch]$WithMusicSR,
    [switch]$SkipDownloads,
    [switch]$SkipPython,
    [switch]$VerifyOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-ToolFiles {
    param([string]$ToolsDir, [string[]]$Files)
    foreach ($file in $Files) {
        $item = Get-Item -LiteralPath (Join-Path $ToolsDir $file) -ErrorAction SilentlyContinue
        if (-not $item -or $item.PSIsContainer -or $item.Length -eq 0) { return $false }
    }
    return $true
}

function Get-ToolDefinitions {
    # RIFE and Real-ESRGAN release bundles include their ncnn models.
    @(
        @{
            Name = 'FFmpeg'; Exe = 'ffmpeg.exe'; Core = $true
            Url = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip'
            ChecksumUrl = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/checksums.sha256'
            Files = @('ffmpeg.exe', 'ffprobe.exe')
        },
        @{
            Name = 'Real-ESRGAN'; Exe = 'realesrgan-ncnn-vulkan.exe'; Core = $true
            Sha256 = 'abc02804e17982a3be33675e4d471e91ea374e65b70167abc09e31acb412802d'
            Url = 'https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip'
            Files = @('realesrgan-ncnn-vulkan.exe', 'models/realesrgan-x4plus.bin', 'models/realesrgan-x4plus.param',
                'models/realesrgan-x4plus-anime.bin', 'models/realesrgan-x4plus-anime.param',
                'models/realesr-animevideov3-x4.bin', 'models/realesr-animevideov3-x4.param')
        },
        @{
            Name = 'RIFE'; Exe = 'rife-ncnn-vulkan.exe'; Core = $false
            Url = 'https://github.com/nihui/rife-ncnn-vulkan/releases/download/20221029/rife-ncnn-vulkan-20221029-windows.zip'
            Files = @('rife-ncnn-vulkan.exe', 'rife-v4.6/flownet.bin', 'rife-v4.6/flownet.param')
        }
    )
    # Automatic Dolby Vision is part of the core desktop workflow.
    # Keep -WithDovi accepted for compatibility with existing installations.
    & {
        @{
            Name = 'dovi_tool'; Exe = 'dovi_tool.exe'; Core = $true
            Url = 'https://github.com/quietvoid/dovi_tool/releases/download/2.3.4/dovi_tool-2.3.4-x86_64-pc-windows-msvc.zip'
            Files = @('dovi_tool.exe')
        }
    }
}

function Install-ToolBundle {
    param([hashtable]$Definition, [string]$ToolsDir, [string]$StageRoot)
    $stage = Join-Path $StageRoot $Definition.Name
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    $archive = Join-Path $stage 'bundle.zip'
    Write-Host ("Downloading {0} from its official release..." -f $Definition.Name)
    Invoke-WebRequest -Uri $Definition.Url -OutFile $archive -UseBasicParsing -TimeoutSec 1200
    if ($Definition.ContainsKey('Sha256') -and (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $Definition.Sha256) {
        throw "Unexpected $($Definition.Name) archive checksum; retry the download."
    }
    if ($Definition.ContainsKey('ChecksumUrl')) {
        $checksums = Join-Path $stage 'checksums.sha256'
        Invoke-WebRequest -Uri $Definition.ChecksumUrl -OutFile $checksums -UseBasicParsing -TimeoutSec 120
        $assetName = [System.Uri]::UnescapeDataString(([uri]$Definition.Url).Segments[-1])
        $line = Get-Content -LiteralPath $checksums | Where-Object { $_ -match ('\s+\*?' + [regex]::Escape($assetName) + '$') } | Select-Object -First 1
        if (-not $line) { throw "FFmpeg release checksum entry is missing; retry setup." }
        $expected = ($line -split '\s+')[0]
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) {
            throw "FFmpeg checksum mismatch. No files from this download were installed. Retry setup."
        }
    }
    $expanded = Join-Path $stage 'expanded'
    Expand-Archive -LiteralPath $archive -DestinationPath $expanded
    $executables = @(Get-ChildItem -LiteralPath $expanded -Recurse -File -Filter $Definition.Exe)
    if ($executables.Count -ne 1) { throw "Unexpected $($Definition.Name) archive layout." }
    $bundle = $executables[0].Directory.FullName
    # Verify all models before touching an existing installation.
    if (-not (Test-ToolFiles -ToolsDir $bundle -Files $Definition.Files)) {
        throw "The $($Definition.Name) archive is missing executable/model files."
    }
    foreach ($item in Get-ChildItem -LiteralPath $bundle) {
        Copy-Item -LiteralPath $item.FullName -Destination $ToolsDir -Recurse -Force
    }
    # FFmpeg keeps its license beside bin/, rather than beside ffmpeg.exe.
    $licenseDir = Join-Path (Split-Path -Parent $bundle) 'doc'
    if ($Definition.Name -eq 'FFmpeg' -and (Test-Path -LiteralPath $licenseDir)) {
        $destination = Join-Path $ToolsDir 'licenses/ffmpeg'
        New-Item -ItemType Directory -Path $destination -Force | Out-Null
        Get-ChildItem -LiteralPath $licenseDir -File | Where-Object { $_.Name -match 'license|copying' } | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
        }
    }
}

function Initialize-Python {
    param([string]$BaseDir, [switch]$CheckOnly)
    $venvPython = Join-Path $BaseDir '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        if ($CheckOnly) { throw 'The project .venv is missing. Run setup.bat first.' }
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCommand) { throw 'Install 64-bit Python 3.10+ from https://www.python.org/downloads/ and add it to PATH.' }
        & $pythonCommand.Source -c 'import sys,struct; sys.exit(0 if sys.version_info >= (3,10) and struct.calcsize(chr(80)) == 8 else 1)'
        if ($LASTEXITCODE -ne 0) { throw '64-bit Python 3.10+ is required.' }
        & $pythonCommand.Source -m venv (Join-Path $BaseDir '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv.' }
    }
    & $venvPython -c 'import sys,struct; sys.exit(0 if sys.version_info >= (3,10) and struct.calcsize(chr(80)) == 8 else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'The project .venv requires 64-bit Python 3.10+.' }
    if (-not $CheckOnly) {
        & $venvPython -m pip install -r (Join-Path $BaseDir 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed. Check the pip error above and retry.' }
    }
    & $venvPython -c 'from PyQt5 import QtCore, QtWidgets; import numpy; print(QtCore.PYQT_VERSION_STR, numpy.__version__)'
    if ($LASTEXITCODE -ne 0) { throw 'Python dependencies are incomplete. Run setup.bat to repair the environment.' }
}

function Test-FFmpegInstallation {
    param([string]$ToolsDir, [switch]$RequireDovi)
    $ffmpeg = Join-Path $ToolsDir 'ffmpeg.exe'
    $encoderText = (& $ffmpeg -hide_banner -encoders 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) { throw 'FFmpeg could not run. Repair tools/ or run setup.bat again.' }
    foreach ($encoder in @('libx265', 'aac', 'eac3')) {
        if ($encoderText -notmatch ('\b' + $encoder + '\b')) { throw "FFmpeg is missing the required encoder: $encoder" }
    }
    $filterText = (& $ffmpeg -hide_banner -filters 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0 -or $filterText -notmatch '\bzscale\b' -or $filterText -notmatch '\blut3d\b') {
        throw 'FFmpeg is missing zscale or lut3d. Install a current full FFmpeg build from BtbN and replace tools/ffmpeg.exe and tools/ffprobe.exe.'
    }
    if ($RequireDovi) {
        $bsfText = (& $ffmpeg -hide_banner -bsfs 2>&1 | Out-String)
        if ($LASTEXITCODE -ne 0 -or $bsfText -notmatch '(?m)^\s*dovi_rpu\s*$' -or $bsfText -notmatch '(?m)^\s*setts\s*$') {
            throw 'Dolby Vision needs FFmpeg dovi_rpu and setts bitstream filters. Upgrade tools/ffmpeg.exe and tools/ffprobe.exe to a current full BtbN build, then retry setup.bat -WithDovi.'
        }
        $doviHelp = (& $ffmpeg -hide_banner -h bsf=dovi_rpu 2>&1 | Out-String)
        if ($LASTEXITCODE -ne 0 -or $doviHelp -notmatch '(?m)^\s*-compression\s' -or $doviHelp -notmatch '(?m)^\s*none\s+0\s') {
            throw 'This FFmpeg dovi_rpu filter lacks compression=0 support. Upgrade tools/ffmpeg.exe and tools/ffprobe.exe to a current full BtbN build, then retry setup.bat -WithDovi.'
        }
    }
    $probeText = (& (Join-Path $ToolsDir 'ffprobe.exe') -v error -show_program_version -of json | Out-String)
    if ($LASTEXITCODE -ne 0) { throw 'FFprobe could not run. Repair tools/ or run setup.bat again.' }
    $null = $probeText | ConvertFrom-Json
}

function Invoke-WorkstationSetup {
    $baseDir = $PSScriptRoot
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or -not [Environment]::Is64BitOperatingSystem) {
        throw 'These prebuilt tools require 64-bit Windows. Install tools manually on other platforms.'
    }
    $toolsDir = Join-Path $baseDir 'tools'
    if (-not $SkipPython) { Initialize-Python -BaseDir $baseDir -CheckOnly:$VerifyOnly }
    $definitions = @(Get-ToolDefinitions | Where-Object { -not $CoreOnly -or $_.Core -or $_.Name -eq 'dovi_tool' })
    $missing = @($definitions | Where-Object { -not (Test-ToolFiles -ToolsDir $toolsDir -Files $_.Files) })
    if ($missing.Count -gt 0 -and ($SkipDownloads -or $VerifyOnly)) {
        throw ('Missing or empty tool/model files: ' + (($missing | ForEach-Object { $_.Name }) -join ', ') + '. Run setup.bat, or install the official bundles listed in README.md.')
    }
    if ($missing.Count -gt 0) {
        New-Item -ItemType Directory -Path $toolsDir -Force | Out-Null
        # Staging stays under the project; only this exact generated directory is removed.
        $stageRoot = Join-Path $baseDir ('.setup-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $stageRoot | Out-Null
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            $ProgressPreference = 'SilentlyContinue'
            foreach ($definition in $missing) { Install-ToolBundle -Definition $definition -ToolsDir $toolsDir -StageRoot $stageRoot }
        } finally {
            $resolvedStage = [IO.Path]::GetFullPath($stageRoot)
            $allowedPrefix = [IO.Path]::GetFullPath($baseDir).TrimEnd('\') + '\.setup-'
            if (-not $resolvedStage.StartsWith($allowedPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Refusing to clean an unexpected setup path.' }
            Remove-Item -LiteralPath $resolvedStage -Recurse -Force
        }
    }
    foreach ($definition in $definitions) {
        if (-not (Test-ToolFiles -ToolsDir $toolsDir -Files $definition.Files)) { throw "Verification failed: $($definition.Name)" }
        Write-Host ("Ready: {0}" -f $definition.Name) -ForegroundColor Green
    }
    Test-FFmpegInstallation -ToolsDir $toolsDir -RequireDovi
    if ($WithMusicSR -or (-not $SkipPython -and -not $SkipDownloads -and -not $VerifyOnly)) {
        $modelPython = Join-Path $baseDir '.venv/Scripts/python.exe'
        if (-not (Test-Path -LiteralPath $modelPython)) { $modelPython = (Get-Command python -ErrorAction Stop).Source }
        $modelOpts = @()
        if ($SkipDownloads -or $VerifyOnly) { $modelOpts = @('--verify-only') }
        & $modelPython (Join-Path $baseDir 'music_sr_backend.py') @modelOpts
        if ($LASTEXITCODE -ne 0) { throw 'Music super-resolution model installation or verification failed.' }
        & $modelPython (Join-Path $baseDir 'flashsr_official.py') @modelOpts
        if ($LASTEXITCODE -ne 0) { throw 'Official FlashSR model installation or verification failed.' }
    }
    if ($WithAudioAI) {
        $modelPython = Join-Path $baseDir '.venv/Scripts/python.exe'
        if (-not (Test-Path -LiteralPath $modelPython)) { $modelPython = (Get-Command python -ErrorAction Stop).Source }
        $modelOpts = @()
        if ($SkipDownloads -or $VerifyOnly) { $modelOpts = @('--verify-only') }
        & $modelPython (Join-Path $baseDir 'install_audio_model.py') @modelOpts
        if ($LASTEXITCODE -ne 0) { throw 'RNNoise model installation or verification failed.' }
        $filters = (& (Join-Path $toolsDir 'ffmpeg.exe') -hide_banner -filters 2>&1 | Out-String)
        if ($LASTEXITCODE -ne 0 -or $filters -notmatch '\barnndn\b') { throw 'AI audio needs FFmpeg with the arnndn filter.' }
    }
    Write-Host 'Setup complete. Start the application with run.bat.' -ForegroundColor Green
}

# Dot-sourcing exposes helpers for offline verification without installing anything.
if ($MyInvocation.InvocationName -ne '.') {
    try { Invoke-WorkstationSetup; exit 0 }
    catch { Write-Error $_ -ErrorAction Continue; exit 1 }
}
