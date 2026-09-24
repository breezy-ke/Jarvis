# Tests for the pure helpers in setup.ps1 (no admin rights, no changes to this PC).
#   powershell -ExecutionPolicy Bypass -File .\scripts\windows\setup.tests.ps1
# CI runs it on Windows PowerShell 5.1 and PowerShell 7.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Load only the helper functions from setup.ps1, without running the script.
$setupPath = Join-Path $PSScriptRoot 'setup.ps1'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($setupPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) { throw "setup.ps1 has syntax errors: $($parseErrors[0].Message)" }
$wanted = 'Write-IniFile', 'ConvertTo-WslPath'
$ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false) |
    Where-Object { $wanted -contains $_.Name } |
    ForEach-Object { . ([scriptblock]::Create($_.Extent.Text)) }

$script:failures = 0
function Assert-Equal($Expected, $Actual, [string]$Name) {
    if ($Expected -ceq $Actual) {
        Write-Output "ok   $Name"
    } else {
        Write-Output "FAIL $Name`n  expected: $Expected`n  actual:   $Actual"
        $script:failures++
    }
}

$dir = Join-Path ([IO.Path]::GetTempPath()) ("jarvis-setup-tests-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $dir | Out-Null
try {
    $sections = @{
        'wsl2'    = [ordered]@{ memory = '16GB'; vmIdleTimeout = '-1' }
        'general' = [ordered]@{ instanceIdleTimeout = '-1' }
    }

    # A new file gets both sections.
    $new = Join-Path $dir 'new.wslconfig'
    $changed = Write-IniFile -Path $new -Sections $sections -KeepExisting @('memory')
    Assert-Equal $true $changed 'creates a missing file'
    $text = [IO.File]::ReadAllText($new)
    Assert-Equal $true ($text -match '(?m)^\[wsl2\]\r?\nmemory=16GB\r?\nvmIdleTimeout=-1') 'writes the wsl2 section'
    Assert-Equal $true ($text -match '(?m)^\[general\]\r?\ninstanceIdleTimeout=-1') 'writes the general section'
    Assert-Equal $false (Write-IniFile -Path $new -Sections $sections -KeepExisting @('memory')) 'a second run changes nothing'
    Assert-Equal 0 @(Get-ChildItem $dir -Filter 'new.wslconfig.bak-*').Count 'no backup when nothing changed'

    # An existing file keeps your settings and comments; only our keys change.
    $existing = Join-Path $dir 'existing.wslconfig'
    [IO.File]::WriteAllLines($existing, [string[]]@(
            '# my settings',
            '[wsl2]',
            'memory=24GB',
            'processors=8',
            'vmIdleTimeout = 60000',
            '',
            '[experimental]',
            'autoMemoryReclaim=gradual'
        ))
    $changed = Write-IniFile -Path $existing -Sections $sections -KeepExisting @('memory')
    Assert-Equal $true $changed 'updates an existing file'
    $lines = [IO.File]::ReadAllLines($existing)
    Assert-Equal '# my settings' $lines[0] 'keeps comments'
    Assert-Equal 'memory=24GB' ($lines | Where-Object { $_ -like 'memory=*' }) 'keeps your memory setting'
    Assert-Equal 'vmIdleTimeout=-1' ($lines | Where-Object { $_ -like 'vmIdleTimeout*' }) 'replaces vmIdleTimeout in place'
    Assert-Equal 1 @($lines | Where-Object { $_ -like 'vmIdleTimeout*' }).Count 'no duplicate keys'
    Assert-Equal 'processors=8' ($lines | Where-Object { $_ -like 'processors=*' }) 'keeps other keys'
    Assert-Equal 'autoMemoryReclaim=gradual' ($lines | Where-Object { $_ -like 'autoMemoryReclaim=*' }) 'keeps other sections'
    $wsl2 = [array]::IndexOf($lines, '[wsl2]')
    $experimental = [array]::IndexOf($lines, '[experimental]')
    $idle = [array]::IndexOf($lines, 'vmIdleTimeout=-1')
    Assert-Equal $true ($idle -gt $wsl2 -and $idle -lt $experimental) 'keys stay in their section'
    Assert-Equal 'instanceIdleTimeout=-1' ($lines | Where-Object { $_ -like 'instanceIdleTimeout*' }) 'adds the general section'
    Assert-Equal 1 @(Get-ChildItem $dir -Filter 'existing.wslconfig.bak-*').Count 'backs up the old file'

    # An explicit -WslMemoryGB replaces your memory value.
    Write-IniFile -Path $existing -Sections $sections -KeepExisting @() | Out-Null
    Assert-Equal 'memory=16GB' ((([IO.File]::ReadAllLines($existing)) | Where-Object { $_ -like 'memory=*' })) 'an explicit memory value wins'

    if ($env:OS -eq 'Windows_NT') {
        Assert-Equal '/mnt/c/Users/Sam Doe/Jarvis' (ConvertTo-WslPath 'C:\Users\Sam Doe\Jarvis') 'converts a Windows path'
        Assert-Equal '/mnt/d/code/Jarvis/data/x.sh' (ConvertTo-WslPath 'D:\code\Jarvis\data\x.sh') 'lowercases the drive letter'
    }
} finally {
    Remove-Item -Recurse -Force $dir
}

if ($script:failures -gt 0) {
    Write-Output "$script:failures test(s) failed"
    exit 1
}
Write-Output 'All setup.ps1 helper tests passed'
