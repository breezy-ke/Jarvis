#Requires -Version 5.1
<#
.SYNOPSIS
    Installs the Jarvis satellite, so you can say "Hey Jarvis" to this PC.

.DESCRIPTION
    Sets up, for your Windows account only (no administrator needed):
      1. uv, the Python installer the satellite uses (with winget, if it isn't there yet)
      2. the satellite app, with the exact package versions this repository
         was tested with (uv fetches its own Python; nothing else changes)
      3. the wake-word models, checked against pinned SHA-256 sums
      4. pairing with Jarvis, using a one-time code from Jarvis:
         Settings > Voice > Pair the Windows app
      5. a Startup shortcut, so it runs at every logon (no console window)

    Run it from a normal PowerShell window in the Jarvis folder:

        powershell -ExecutionPolicy Bypass -File .\scripts\windows\install-satellite.ps1

    Safe to run again, for example to update the app or pair it again.

.PARAMETER Code
    The pairing code. Asked for when not given.

.PARAMETER Server
    Jarvis's address. Default: http://localhost:8080 (Jarvis on this PC).

.PARAMETER Name
    What to call this PC in Jarvis. Default: the computer name.

.PARAMETER NoStartup
    Don't start the satellite at logon.

.PARAMETER Uninstall
    Remove the satellite, its Startup shortcut and its device token.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\windows\install-satellite.ps1 -Code ABCD-2345
#>
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '', Justification = 'Interactive installer: output is for the person running it.')]
[CmdletBinding()]
param(
    [string]$Code = '',
    [string]$Server = 'http://localhost:8080',
    [string]$Name = $env:COMPUTERNAME,
    [switch]$NoStartup,
    [switch]$Uninstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Source = (Resolve-Path (Join-Path $PSScriptRoot '..\..\satellite')).Path
$ShortcutName = 'Jarvis Satellite.lnk'

# --- helpers -------------------------------------------------------------------------

function Write-Step([string]$Text) { Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Write-Done([string]$Text) { Write-Host "    $Text" -ForegroundColor Green }
function Write-Note([string]$Text) { Write-Host "    $Text" -ForegroundColor Yellow }

function Exit-Install([string]$Text, [int]$Code = 1) {
    Write-Host "`n$Text" -ForegroundColor Yellow
    exit $Code
}

function ConvertTo-PairingCode([string]$Text) {
    # "abcd 2345" or "ABCD-2345" -> "ABCD-2345"; $null if it can't be a code.
    $clean = ($Text.ToUpperInvariant() -replace '[^A-Z0-9]', '')
    if ($clean -notmatch '^[A-HJ-NP-Z2-9]{8}$') { return $null }
    return $clean.Substring(0, 4) + '-' + $clean.Substring(4)
}

function Test-ServerAddress([string]$Address) {
    $uri = $null
    if (-not [Uri]::TryCreate($Address, [UriKind]::Absolute, [ref]$uri)) { return $false }
    return ($uri.Scheme -eq 'http' -or $uri.Scheme -eq 'https') -and $uri.Host -ne ''
}

function Get-UvBinDir {
    # uv puts the tools it installs (and itself, when it installs itself) here.
    if ($env:UV_TOOL_BIN_DIR) { return $env:UV_TOOL_BIN_DIR }
    return (Join-Path $env:USERPROFILE '.local\bin')
}

function Add-ToSessionPath([string]$Dir) {
    $parts = $env:Path -split ';'
    if ($parts -notcontains $Dir) { $env:Path = "$Dir;$env:Path" }
}

function Install-Uv {
    if (Get-Command uv -ErrorAction SilentlyContinue) { return }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Exit-Install ('This PC has no winget, so uv cannot be installed automatically. Install uv ' +
            '(https://docs.astral.sh/uv/getting-started/installation/), then run this again.')
    }
    winget install --id astral-sh.uv --exact --silent --accept-source-agreements --accept-package-agreements | Out-Host
    Add-ToSessionPath (Get-UvBinDir)
    $wingetLinks = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'
    if (Test-Path $wingetLinks) { Add-ToSessionPath $wingetLinks }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Exit-Install 'uv was installed, but this window cannot see it yet. Open a new PowerShell window and run this again.'
    }
}

function Invoke-Checked([string]$What, [scriptblock]$Command) {
    & $Command
    if ($LASTEXITCODE -ne 0) { Exit-Install "$What failed (exit code $LASTEXITCODE). Fix the message above, then run this again." }
}

function Get-StartupShortcutPath {
    return (Join-Path ([Environment]::GetFolderPath('Startup')) $ShortcutName)
}

function Stop-Satellite {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param()
    $running = @(Get-Process -Name 'jarvis-satellite-tray', 'jarvis-satellite' -ErrorAction SilentlyContinue)
    if ($running.Count -gt 0 -and $PSCmdlet.ShouldProcess('Jarvis satellite', 'Stop the running app')) {
        $running | Stop-Process -Force
    }
}

# --- uninstall ----------------------------------------------------------------------

if ($Uninstall) {
    Write-Step 'Removing the Jarvis satellite'
    Stop-Satellite
    if (Get-Command jarvis-satellite -ErrorAction SilentlyContinue) {
        jarvis-satellite unpair | Out-Host
    }
    $shortcut = Get-StartupShortcutPath
    if (Test-Path $shortcut) { Remove-Item $shortcut }
    if (Get-Command uv -ErrorAction SilentlyContinue) { uv tool uninstall jarvis-satellite | Out-Host }
    Write-Done 'Removed. Also remove this PC in Jarvis: Settings > Voice.'
    exit 0
}

if (-not (Test-ServerAddress $Server)) {
    Exit-Install "-Server must look like http://host:port, not '$Server'."
}

# --- 1. uv ---------------------------------------------------------------------------

Write-Step 'Checking uv (the Python installer)'
Install-Uv
Write-Done (uv --version)

# --- 2. the app ---------------------------------------------------------------------

Write-Step 'Installing the satellite app'
Stop-Satellite # an update replaces files that a running copy has open
$constraints = Join-Path ([IO.Path]::GetTempPath()) 'jarvis-satellite-constraints.txt'
# The exact versions from satellite\uv.lock, so you get what was tested.
Invoke-Checked 'Reading the tested versions' { uv export --project $Source --frozen --no-dev --no-emit-project --no-hashes --quiet --output-file $constraints }
Invoke-Checked 'Installing the satellite' { uv tool install --force --python 3.12 --constraints $constraints $Source }
Remove-Item $constraints -ErrorAction SilentlyContinue
Add-ToSessionPath (Get-UvBinDir)
Write-Done 'Installed jarvis-satellite.'

# --- 3. wake-word models ------------------------------------------------------------

Write-Step 'Downloading the wake-word models (checksum-pinned)'
Invoke-Checked 'Downloading the models' { jarvis-satellite fetch-models }

# --- 4. pairing ---------------------------------------------------------------------

Write-Step 'Pairing with Jarvis'
$status = (jarvis-satellite status) -join "`n"
if ($Code -eq '' -and $status -match '\(paired\)') {
    Write-Done 'Already paired. (To pair again, run this with -Code.)'
} else {
    while ($true) {
        if ($Code -eq '') {
            Write-Note 'In Jarvis, open Settings > Voice > Pair the Windows app, and type the code here.'
            $Code = Read-Host '    Pairing code'
        }
        $normal = ConvertTo-PairingCode $Code
        if ($null -eq $normal) {
            Write-Note 'That does not look like a pairing code (8 letters and digits, like ABCD-2345).'
            $Code = ''
            continue
        }
        jarvis-satellite pair $normal --server $Server --name $Name | Out-Host
        if ($LASTEXITCODE -eq 0) { break }
        $again = Read-Host '    Try another code? (y/n)'
        if ($again -notmatch '^[yY]') { Exit-Install 'Not paired. Run this again when you have a new code.' }
        $Code = ''
    }
}

# --- 5. start at logon --------------------------------------------------------------

$tray = Join-Path (Get-UvBinDir) 'jarvis-satellite-tray.exe'
$shortcut = Get-StartupShortcutPath
if ($NoStartup) {
    if (Test-Path $shortcut) { Remove-Item $shortcut }
    Write-Note 'Not starting at logon (-NoStartup).'
} else {
    Write-Step 'Starting it at every logon'
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($shortcut)
    $link.TargetPath = $tray
    $link.WorkingDirectory = $env:USERPROFILE
    $link.Description = 'Jarvis satellite: say Hey Jarvis'
    $link.Save()
    Write-Done "Startup shortcut: $shortcut"
}

Write-Step 'Starting the satellite'
Start-Process -FilePath $tray
Write-Done 'Look for the round icon in the system tray, then say "Hey Jarvis".'
Write-Note 'If it never hears you: Windows Settings > Privacy & security > Microphone >'
Write-Note '"Let desktop apps access your microphone" must be On. Then: jarvis-satellite test-mic'
