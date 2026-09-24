#Requires -Version 5.1
<#
.SYNOPSIS
    Prepares this Windows PC to run Jarvis around the clock.

.DESCRIPTION
    Sets up everything Jarvis needs on Windows, in order:
      1. WSL2 with Ubuntu 24.04 (Docker Engine runs inside it, with your NVIDIA GPU)
      2. .wslconfig settings that stop WSL from shutting down when idle
      3. a copy of this repository inside Ubuntu (~/Jarvis), then the Ubuntu setup
         (scripts/wsl/bootstrap.sh: Docker, NVIDIA Container Toolkit, autostart)
      4. Tailscale, so your phone reaches Jarvis over private HTTPS
      5. generated secrets, the first build and start, and the local AI model
      6. a boot task that starts Jarvis at power-on, even before anyone logs in
      7. power settings: no sleep on mains power, Fast Startup off

    Run it from an administrator PowerShell in the Jarvis folder:

        powershell -ExecutionPolicy Bypass -File .\scripts\windows\setup.ps1

    Use your everyday Windows account (it must be an administrator), not a
    separate admin account: WSL and the boot task belong to the account that
    runs this.

    It is safe to run again: each step checks what is already done. When
    Windows needs a restart (the first time WSL is turned on), it says so; run
    it again afterwards.

.PARAMETER Distro
    The WSL distribution to use. Default: Ubuntu-24.04.

.PARAMETER WslMemoryGB
    Memory for WSL in GB. Default: 60% of this PC's RAM (only set if .wslconfig
    doesn't already choose a value).

.PARAMETER NoStoredPassword
    Register the boot task without storing your Windows password (S4U logon).
    Some PCs can then only start WSL after you log in; the reboot test in
    docs/runbook.md shows which kind you have.

.PARAMETER SkipTailscale
    Don't install or configure Tailscale.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\windows\setup.ps1
#>
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '', Justification = 'Interactive setup script: output is for the person running it.')]
[CmdletBinding()]
param(
    [string]$Distro = 'Ubuntu-24.04',
    [ValidateRange(0, 1024)]
    [int]$WslMemoryGB = 0,
    [switch]$NoStoredPassword,
    [switch]$SkipTailscale
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$env:WSL_UTF8 = '1' # plain UTF-8 from wsl.exe instead of UTF-16
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) # read Ubuntu's UTF-8 output

$TaskName = 'Jarvis'
$RepoWin = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

# --- helpers -------------------------------------------------------------------------

function Write-Step([string]$Text) { Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Write-Done([string]$Text) { Write-Host "    $Text" -ForegroundColor Green }
function Write-Note([string]$Text) { Write-Host "    $Text" -ForegroundColor Yellow }

function Exit-Setup([string]$Text, [int]$Code = 1) {
    Write-Host "`n$Text" -ForegroundColor Yellow
    exit $Code
}

function Get-WslExe {
    # The Store version of WSL lives in Program Files; System32\wsl.exe is the
    # older inbox launcher, still used to install WSL the first time.
    $store = Join-Path $env:ProgramFiles 'WSL\wsl.exe'
    if (Test-Path $store) { return $store }
    return (Join-Path $env:SystemRoot 'System32\wsl.exe')
}

function Invoke-Quiet([scriptblock]$Command) {
    # Windows PowerShell turns redirected stderr from programs into errors;
    # run such calls with errors non-terminating.
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Command } finally { $ErrorActionPreference = $saved }
}

function Get-WslDistroList {
    $names = Invoke-Quiet { & $script:WslExe --list --quiet 2>$null }
    return @($names | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ })
}

function ConvertTo-WslPath([string]$WindowsPath) {
    $full = [IO.Path]::GetFullPath($WindowsPath)
    if ($full -notmatch '^[A-Za-z]:\\') { throw "Expected a local drive path, got $full" }
    return '/mnt/' + $full.Substring(0, 1).ToLower() + ($full.Substring(2) -replace '\\', '/')
}

function Invoke-Repo([string]$Command) {
    # Runs a command in the Ubuntu copy of the repo, as your Ubuntu user, and
    # returns its exit code. Its output goes straight to the screen.
    & $script:WslExe -d $Distro --cd $script:RepoLinux --exec bash -lc $Command | Out-Host
    return $LASTEXITCODE
}

function Write-IniFile([string]$Path, [hashtable]$Sections, [string[]]$KeepExisting) {
    # Sets key=value pairs in an INI-style file (like .wslconfig), leaving other
    # lines alone. Keys named in $KeepExisting are only added when missing.
    # Returns $true when the file changed.
    $lines = New-Object System.Collections.Generic.List[string]
    if (Test-Path $Path) { foreach ($l in [IO.File]::ReadAllLines($Path)) { $lines.Add($l) } }
    $before = $lines -join "`n"
    foreach ($section in $Sections.Keys) {
        $start = -1
        for ($i = 0; $i -lt $lines.Count; $i++) {
            if ($lines[$i].Trim() -ieq "[$section]") { $start = $i; break }
        }
        if ($start -lt 0) {
            if ($lines.Count -gt 0 -and $lines[$lines.Count - 1].Trim() -ne '') { $lines.Add('') }
            $lines.Add("[$section]")
            $start = $lines.Count - 1
        }
        $end = $lines.Count
        for ($i = $start + 1; $i -lt $lines.Count; $i++) {
            if ($lines[$i].Trim() -match '^\[.+\]$') { $end = $i; break }
        }
        foreach ($key in $Sections[$section].Keys) {
            $value = $Sections[$section][$key]
            $found = -1
            for ($i = $start + 1; $i -lt $end; $i++) {
                if ($lines[$i] -match "^\s*$([regex]::Escape($key))\s*=") { $found = $i; break }
            }
            if ($found -ge 0) {
                if ($KeepExisting -notcontains $key) { $lines[$found] = "$key=$value" }
            } else {
                $lines.Insert($end, "$key=$value")
                $end++
            }
        }
    }
    if (($lines -join "`n") -eq $before) { return $false }
    if (Test-Path $Path) { Copy-Item $Path "$Path.bak-$(Get-Date -Format yyyyMMdd-HHmmss)" }
    [IO.File]::WriteAllLines($Path, $lines.ToArray()) # UTF-8 without BOM
    return $true
}

# --- 0. preflight ----------------------------------------------------------------------

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Exit-Setup 'Please run this from an administrator PowerShell (right-click Windows PowerShell > Run as administrator).'
}
$build = [Environment]::OSVersion.Version.Build
if ($build -lt 19044) {
    Exit-Setup "Windows build $build is too old for GPU support in WSL. Update to Windows 11 (or Windows 10 21H2 or later), then run this again."
}
Write-Host "Jarvis setup for $env:USERDOMAIN\$env:USERNAME (repository: $RepoWin)"

# --- 1. NVIDIA driver -------------------------------------------------------------------

Write-Step 'Checking the NVIDIA driver'
$smi = Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'
if (Test-Path $smi) {
    $gpu = Invoke-Quiet { & $smi --query-gpu=name,memory.total --format=csv,noheader 2>$null } | Select-Object -First 1
    Write-Done "Found $gpu"
} else {
    Write-Note 'No NVIDIA driver found. Install the latest driver from nvidia.com (it includes WSL support; never install one inside Ubuntu), then run this again. Jarvis still works with cloud models meanwhile.'
}

# --- 2. WSL --------------------------------------------------------------------------------

Write-Step 'Checking WSL'
$restartNeeded = $false
foreach ($feature in 'Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform') {
    $state = (Get-WindowsOptionalFeature -Online -FeatureName $feature).State
    if ($state -ne 'Enabled') {
        Write-Host "    Turning on $feature"
        $result = Enable-WindowsOptionalFeature -Online -FeatureName $feature -All -NoRestart
        if ($result.RestartNeeded -or $state -eq 'EnablePending') { $restartNeeded = $true }
    }
}
if ($restartNeeded) {
    Exit-Setup 'Windows needs a restart to finish turning on WSL. Restart, then run this script again.' 0
}
$WslExe = Get-WslExe
Write-Host '    Updating WSL to the latest version'
& $WslExe --update
if ($LASTEXITCODE -ne 0) {
    # Without access to the Microsoft Store, download the update from GitHub instead.
    & $WslExe --update --web-download
    if ($LASTEXITCODE -ne 0) { Write-Note 'wsl --update failed; carrying on with the installed version.' }
}
$WslExe = Get-WslExe
& $WslExe --set-default-version 2 | Out-Null
Write-Done 'WSL 2 is ready'

# --- 3. .wslconfig ---------------------------------------------------------------------------

Write-Step 'Keeping WSL running when idle (.wslconfig)'
$ramGB = [math]::Floor((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
$keep = @('memory')
if ($WslMemoryGB -eq 0) { $WslMemoryGB = [math]::Max(4, [math]::Floor($ramGB * 0.6)) } else { $keep = @() }
$wslConfig = Join-Path $env:USERPROFILE '.wslconfig'
$changed = Write-IniFile -Path $wslConfig -KeepExisting $keep -Sections @{
    'wsl2'    = [ordered]@{ memory = "$($WslMemoryGB)GB"; vmIdleTimeout = '-1' }
    'general' = [ordered]@{ instanceIdleTimeout = '-1' }
}
if ($changed) {
    Write-Done "Updated $wslConfig (the old one is backed up next to it)"
    if ((Get-WslDistroList).Count -gt 0) {
        Write-Note 'Restarting WSL so the settings apply (this closes open Ubuntu windows).'
        & $WslExe --shutdown
    }
} else {
    Write-Done "$wslConfig is already set"
}

# --- 4. Ubuntu -------------------------------------------------------------------------------

Write-Step "Checking $Distro"
if ((Get-WslDistroList) -notcontains $Distro) {
    Write-Host "    Installing $Distro. When Ubuntu asks, create a username and password."
    Write-Host "    If it then shows an Ubuntu prompt, type 'exit' to come back here."
    & $WslExe --install -d $Distro
    if ((Get-WslDistroList) -notcontains $Distro) {
        Exit-Setup "$Distro isn't installed yet. Finish creating your Ubuntu user, then run this script again."
    }
}
$LinuxUser = ((& $WslExe -d $Distro --exec whoami) | Out-String).Trim()
if (-not $LinuxUser -or $LinuxUser -eq 'root') {
    Exit-Setup "$Distro has no normal user yet. Open Ubuntu from the Start menu, create your user, then run this again."
}
$LinuxHome = ((& $WslExe -d $Distro --exec printenv HOME) | Out-String).Trim()
Write-Done "$Distro is installed (user $LinuxUser)"

# --- 5. The repository inside Ubuntu ---------------------------------------------------------

Write-Step "Copying Jarvis into Ubuntu ($LinuxHome/Jarvis)"
$RepoLinux = "$LinuxHome/Jarvis"
# Run the copy script from a Linux-line-ending copy, in case Windows converted it.
New-Item -ItemType Directory -Force -Path (Join-Path $RepoWin 'data') | Out-Null
$importCopy = Join-Path $RepoWin 'data\.import-repo.sh'
$importText = [IO.File]::ReadAllText((Join-Path $RepoWin 'scripts\wsl\import-repo.sh')) -replace "`r`n", "`n"
[IO.File]::WriteAllText($importCopy, $importText) # UTF-8 without BOM
& $WslExe -d $Distro --exec bash (ConvertTo-WslPath $importCopy) (ConvertTo-WslPath $RepoWin) $RepoLinux
if ($LASTEXITCODE -ne 0) { Exit-Setup 'Copying the repository into Ubuntu failed (see the messages above).' }
Remove-Item $importCopy -ErrorAction SilentlyContinue

# --- 6. Ubuntu setup: Docker, GPU, autostart ------------------------------------------------

Write-Step 'Setting up Ubuntu: Docker Engine, GPU support, autostart (takes a few minutes)'
$bootstrap = "$RepoLinux/scripts/wsl/bootstrap.sh"
& $WslExe -d $Distro -u root --exec bash $bootstrap $LinuxUser
$code = $LASTEXITCODE
if ($code -eq 10) {
    # systemd was just switched on; it takes effect when the distro restarts.
    & $WslExe --terminate $Distro | Out-Null
    & $WslExe -d $Distro -u root --exec bash $bootstrap $LinuxUser
    $code = $LASTEXITCODE
}
if ($code -ne 0) { Exit-Setup 'The Ubuntu setup failed (see the messages above). Fix the problem, then run this script again.' }
# Restart Ubuntu so your account picks up its new docker group.
& $WslExe --terminate $Distro | Out-Null

# --- 7. Tailscale ----------------------------------------------------------------------------

$origin = $null
if (-not $SkipTailscale) {
    Write-Step 'Setting up Tailscale (private HTTPS from your phone, no open ports)'
    $ts = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    if (-not (Test-Path $ts)) {
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            Exit-Setup 'winget is missing. Install Tailscale from https://tailscale.com/download/windows, then run this again.'
        }
        & winget install --id Tailscale.Tailscale --exact --silent --accept-package-agreements --accept-source-agreements
        if (-not (Test-Path $ts)) {
            Exit-Setup 'Tailscale did not install. Install it from https://tailscale.com/download/windows, then run this again.'
        }
        Start-Sleep -Seconds 5 # let the Tailscale service start
    }
    $status = $null
    $json = Invoke-Quiet { & $ts status --json 2>$null }
    if ($json) { $status = ($json | Out-String) | ConvertFrom-Json }
    if (-not $status -or $status.BackendState -ne 'Running') {
        Write-Host '    Sign in to Tailscale: open the link below (or the browser window that appears).'
        & $ts up
        if ($LASTEXITCODE -ne 0) { Exit-Setup 'Tailscale sign-in did not finish. Run this again when you are ready.' }
    }
    # Keep Tailscale connected when nobody is logged in to Windows.
    & $ts set --unattended
    Write-Host '    Serving Jarvis over HTTPS on your tailnet. If Tailscale asks you to enable HTTPS'
    Write-Host '    certificates or Serve, open the link it shows, approve, and wait here.'
    & $ts serve --bg 8080
    if ($LASTEXITCODE -ne 0) { Write-Note 'tailscale serve failed. Fix it (see above), then run: tailscale serve --bg 8080' }
    $status = ((& $ts status --json) | Out-String) | ConvertFrom-Json
    $dnsName = ([string]$status.Self.DNSName).TrimEnd('.')
    if ($dnsName) {
        $origin = "https://$dnsName"
        Write-Done "Jarvis's address: $origin"
    } else {
        Write-Note 'Could not read this PC''s Tailscale name. Set JARVIS_PUBLIC_ORIGIN in ~/Jarvis/.env yourself.'
    }
}

# --- 8. Secrets, first start, local model -----------------------------------------------------

Write-Step 'Generating secrets and building Jarvis (the first build takes several minutes)'
if ((Invoke-Repo 'make secrets') -ne 0) { Exit-Setup 'make secrets failed (see above).' }
if ($origin) {
    if ((Invoke-Repo "scripts/wsl/set-env.sh JARVIS_PUBLIC_ORIGIN $origin") -ne 0) { Exit-Setup 'Could not update .env.' }
}
Write-Step 'Starting Jarvis'
if ((Invoke-Repo 'make up') -ne 0) { Exit-Setup 'make up failed (see above).' }
Write-Step 'Downloading the local AI model (several GB, one time)'
if ((Invoke-Repo 'make pull-models') -ne 0) { Write-Note 'The model download failed. Retry later in Ubuntu with: make pull-models' }

# --- 9. Boot task ------------------------------------------------------------------------------

Write-Step 'Registering the boot task (starts Jarvis at power-on, even before you log in)'
$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $WslExe -Argument "-d $Distro -u root --exec /usr/local/bin/jarvis-keepalive"
$atStartup = New-ScheduledTaskTrigger -AtStartup
$atStartup.Delay = 'PT30S'
$atLogOn = New-ScheduledTaskTrigger -AtLogOn -User $user
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$description = 'Keeps WSL (Ubuntu), Docker and Jarvis running. Created by Jarvis scripts/windows/setup.ps1.'
if ($NoStoredPassword) {
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Highest
    Register-ScheduledTask -TaskName $TaskName -Description $description -Action $action -Trigger $atStartup, $atLogOn `
        -Settings $taskSettings -Principal $taskPrincipal -Force | Out-Null
} else {
    Write-Host '    To run while you are logged out, Windows needs your account password. Windows'
    Write-Host '    stores it for the task (Jarvis never sees it). Signed in with a Microsoft account?'
    Write-Host '    Use that account''s password, not your PIN. (The password box may open behind this window.)'
    for ($try = 1; $try -le 3; $try++) {
        $credential = Get-Credential -UserName $user -Message 'Your Windows password, for the Jarvis boot task'
        if (-not $credential) { Exit-Setup 'Cancelled. Run this again, or add -NoStoredPassword.' }
        try {
            Register-ScheduledTask -TaskName $TaskName -Description $description -Action $action -Trigger $atStartup, $atLogOn `
                -Settings $taskSettings -User $user -Password $credential.GetNetworkCredential().Password -RunLevel Highest -Force | Out-Null
            break
        } catch {
            Write-Note "That didn't work: $($_.Exception.Message)"
            if ($try -eq 3) { Exit-Setup 'Could not register the boot task. Run this again, or add -NoStoredPassword.' }
        }
    }
}
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 5
$taskState = (Get-ScheduledTask -TaskName $TaskName).State
if ($taskState -eq 'Running') {
    Write-Done 'The boot task is registered and running'
} else {
    $lastResult = (Get-ScheduledTaskInfo -TaskName $TaskName).LastTaskResult
    Write-Note "The boot task is $taskState (last result $lastResult). See 'Reboot test' in docs/runbook.md."
}

# --- 10. Power ----------------------------------------------------------------------------------

Write-Step 'Power settings: never sleep on mains power; Fast Startup off'
& powercfg /change standby-timeout-ac 0
& powercfg /change hibernate-timeout-ac 0
# Fast Startup makes "shut down" a kind of hibernation, and boot tasks don't run after it.
New-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' -Name HiberbootEnabled -Value 0 -PropertyType DWord -Force | Out-Null
Write-Done 'Done'

# --- 11. Check and hand over -----------------------------------------------------------------------

Write-Step 'Checking everything (make doctor)'
Invoke-Repo 'make doctor' | Out-Null
$alreadySetUp = $false
try {
    $authStatus = Invoke-RestMethod -Uri 'http://127.0.0.1:8080/api/auth/status' -TimeoutSec 10
    $alreadySetUp = [bool]$authStatus.setup_complete
} catch {
    Write-Note "Couldn't ask Jarvis whether it's set up yet: $($_.Exception.Message)"
}
if ($alreadySetUp) {
    Write-Done 'Jarvis already has your passkey: sign in as usual.'
} else {
    Write-Step 'Your first-run setup code'
    Invoke-Repo 'make setup-token' | Out-Null
}

Write-Host ''
Write-Host 'Jarvis is set up.' -ForegroundColor Green
if ($origin) {
    Write-Host "  Open $origin on this PC or on your phone (with Tailscale on),"
} else {
    Write-Host '  Open your Jarvis address (see JARVIS_PUBLIC_ORIGIN in ~/Jarvis/.env),'
}
if (-not $alreadySetUp) { Write-Host '  enter the setup code above, and create your passkey.' }
Write-Host ''
Write-Host 'Still to do (docs/setup.md explains each):'
Write-Host '  - Tailscale admin console: turn off key expiry for this PC, or it disconnects in 180 days.'
Write-Host '  - Google (Gmail and Calendar) and AI keys: docs/setup.md, steps 3 and 4.'
Write-Host '  - Turn on BitLocker or Device Encryption. In the BIOS, set "restore on AC power loss" to on.'
Write-Host '  - Do the reboot test in docs/runbook.md once, to prove Jarvis comes back with nobody logged in.'
