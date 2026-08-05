[CmdletBinding()]
param(
    [string]$OutputDirectory = "dist",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

function Invoke-CheckedProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [string[]]$ArgumentList = @(),
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    $Process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $ArgumentList `
        -Wait `
        -PassThru
    if ($Process.ExitCode -ne 0) {
        throw "$Description failed with exit code $($Process.ExitCode)."
    }
}

function Invoke-CheckedConsoleProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [string[]]$ArgumentList = @(),
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$Version = (& python scripts/verify_release.py).Trim()
if ($LASTEXITCODE -ne 0 -or -not $Version) {
    throw "Release metadata validation failed."
}

$BuildRoot = Join-Path $Root ".build\windows"
$Venv = Join-Path $BuildRoot "venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$NuitkaOutput = Join-Path $BuildRoot "nuitka"
$Output = Join-Path $Root $OutputDirectory
$BuildSucceeded = $false

Remove-Item $BuildRoot -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $Output -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $BuildRoot, $NuitkaOutput, $Output | Out-Null

try {
    python -m venv $Venv
    & $VenvPython -m pip install --disable-pip-version-check --upgrade pip
    & $VenvPython -m pip install --disable-pip-version-check -r requirements-build-windows.txt
    if (-not $SkipTests) {
        & $VenvPython -m pip install --disable-pip-version-check -r requirements-dev.txt
        $PreviousQtPlatform = $env:QT_QPA_PLATFORM
        $env:QT_QPA_PLATFORM = "offscreen"
        try {
            & $VenvPython -m pytest
        } finally {
            if ($null -eq $PreviousQtPlatform) {
                Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
            } else {
                $env:QT_QPA_PLATFORM = $PreviousQtPlatform
            }
        }
    }

    $WindowsVersion = "$Version.0"
    $EntryPoint = Join-Path $Root "packaging\windows\cameracopy_entry.py"
    $Icon = Join-Path $Root "cameracopy2\resources\icons\cameracopy.ico"
    $ResourceSource = Join-Path $Root "cameracopy2\resources"
    $ResourceOption = "$ResourceSource=cameracopy2/resources"

    & $VenvPython -m nuitka `
        --mode=standalone `
        --enable-plugin=pyside6 `
        --windows-console-mode=attach `
        --assume-yes-for-downloads `
        --include-data-dir=$ResourceOption `
        --include-package=cameracopy2 `
        --include-module=wmi `
        --include-package=win32com `
        --include-module=pythoncom `
        --include-module=pywintypes `
        --windows-icon-from-ico=$Icon `
        --company-name=CameraCopy `
        --product-name=CameraCopy `
        --file-description="Copy and verify camera media" `
        --file-version=$WindowsVersion `
        --product-version=$WindowsVersion `
        --output-dir=$NuitkaOutput `
        --output-filename=CameraCopy.exe `
        $EntryPoint

    $StandaloneDist = Join-Path $NuitkaOutput "cameracopy_entry.dist"
    $Executable = Join-Path $StandaloneDist "CameraCopy.exe"
    if (-not (Test-Path $Executable)) {
        throw "Nuitka did not produce $Executable"
    }

    Invoke-CheckedConsoleProcess `
        -FilePath $Executable `
        -ArgumentList @("--version") `
        -Description "Standalone version check"
    Invoke-CheckedConsoleProcess `
        -FilePath $Executable `
        -ArgumentList @("--self-test") `
        -Description "Standalone self-test"

    # Keep the distribution folder visible in the portable archive so users can
    # extract one CameraCopy directory without mixing DLLs into another folder.
    $PortableRoot = Join-Path $BuildRoot "CameraCopy"
    New-Item -ItemType Directory -Path $PortableRoot | Out-Null
    Copy-Item -Path (Join-Path $StandaloneDist "*") -Destination $PortableRoot -Recurse -Force

    $PortableZip = Join-Path $Output "CameraCopy-$Version-windows-x86_64-portable.zip"
    Compress-Archive -LiteralPath $PortableRoot -DestinationPath $PortableZip -CompressionLevel Optimal

    $MakeNsisCommand = Get-Command makensis.exe -ErrorAction SilentlyContinue
    if ($MakeNsisCommand) {
        $MakeNsisPath = $MakeNsisCommand.Source
    } else {
        $CommonNsis = "${env:ProgramFiles(x86)}\NSIS\makensis.exe"
        if (Test-Path $CommonNsis) {
            $MakeNsisPath = $CommonNsis
        } else {
            throw "makensis.exe was not found. Install NSIS before running this script."
        }
    }

    $NsisScript = Join-Path $Root "packaging\windows\CameraCopy.nsi"
    & $MakeNsisPath `
        "/DAPP_VERSION=$Version" `
        "/DAPP_VERSION_NUM=$WindowsVersion" `
        "/DAPP_DIST=$StandaloneDist" `
        "/DOUTPUT_DIR=$Output" `
        $NsisScript

    $Installer = Join-Path $Output "CameraCopy-Setup-$Version.exe"
    if (-not (Test-Path $Installer)) {
        throw "NSIS did not produce $Installer"
    }

    $SmokeInstall = "$($env:SystemDrive)\CameraCopyInstallerSmoke-$PID"
    Remove-Item $SmokeInstall -Recurse -Force -ErrorAction SilentlyContinue
    try {
        $InstallArguments = @("/S", "/D=$SmokeInstall")
        Invoke-CheckedProcess `
            -FilePath $Installer `
            -ArgumentList $InstallArguments `
            -Description "Silent installer smoke test"

        $InstalledExecutable = Join-Path $SmokeInstall "CameraCopy.exe"
        if (-not (Test-Path $InstalledExecutable)) {
            throw "Installer did not create $InstalledExecutable"
        }

        $StaleFile = Join-Path $SmokeInstall "stale-runtime-file.txt"
        Set-Content -Path $StaleFile -Value "must be removed by upgrade"
        Invoke-CheckedProcess `
            -FilePath $Installer `
            -ArgumentList $InstallArguments `
            -Description "Silent installer upgrade test"
        if (Test-Path $StaleFile) {
            throw "Installer upgrade left a stale runtime file in $SmokeInstall"
        }

        Invoke-CheckedConsoleProcess `
            -FilePath $InstalledExecutable `
            -ArgumentList @("--version") `
            -Description "Installed version check"
        Invoke-CheckedConsoleProcess `
            -FilePath $InstalledExecutable `
            -ArgumentList @("--self-test") `
            -Description "Installed self-test"

        $Uninstaller = Join-Path $SmokeInstall "Uninstall.exe"
        if (-not (Test-Path $Uninstaller)) {
            throw "Installer did not create $Uninstaller"
        }
        Invoke-CheckedProcess `
            -FilePath $Uninstaller `
            -ArgumentList @("/S") `
            -Description "Silent uninstall smoke test"
    } finally {
        Remove-Item $SmokeInstall -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item "$SmokeInstall.previous" -Recurse -Force -ErrorAction SilentlyContinue
    }

    $BuildSucceeded = $true
    Write-Host "Created:"
    Write-Host "  $PortableZip"
    Write-Host "  $Installer"
} finally {
    Remove-Item $BuildRoot -Recurse -Force -ErrorAction SilentlyContinue
    if (-not $BuildSucceeded) {
        Remove-Item $Output -Recurse -Force -ErrorAction SilentlyContinue
    }
}
