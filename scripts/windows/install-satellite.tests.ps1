# Tests for the pure helpers in install-satellite.ps1 (changes nothing on this PC).
#   powershell -ExecutionPolicy Bypass -File .\scripts\windows\install-satellite.tests.ps1
# CI runs it on Windows PowerShell 5.1 and PowerShell 7.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$scriptPath = Join-Path $PSScriptRoot 'install-satellite.ps1'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) { throw "install-satellite.ps1 has syntax errors: $($parseErrors[0].Message)" }
$wanted = 'ConvertTo-PairingCode', 'Test-ServerAddress', 'Add-ToSessionPath'
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

# Pairing codes: 8 characters from Jarvis's alphabet (no 0/O or 1/I), dash optional.
Assert-Equal 'ABCD-2345' (ConvertTo-PairingCode 'ABCD-2345') 'keeps a well-formed code'
Assert-Equal 'ABCD-2345' (ConvertTo-PairingCode ' abcd 2345 ') 'tidies spaces and case'
Assert-Equal 'ABCD-2345' (ConvertTo-PairingCode 'ABCD2345') 'adds the dash'
Assert-Equal $null (ConvertTo-PairingCode 'ABCD-234') 'refuses a short code'
Assert-Equal $null (ConvertTo-PairingCode 'ABC0-2345') 'refuses a zero (never in a code)'
Assert-Equal $null (ConvertTo-PairingCode '') 'refuses nothing'

# Server addresses.
Assert-Equal $true (Test-ServerAddress 'http://localhost:8080') 'accepts http://localhost:8080'
Assert-Equal $true (Test-ServerAddress 'https://pc.tail1234.ts.net') 'accepts a Tailscale address'
Assert-Equal $false (Test-ServerAddress 'localhost:8080') 'refuses an address without http'
Assert-Equal $false (Test-ServerAddress 'ftp://pc') 'refuses other schemes'

# PATH for this session only, without duplicates.
$saved = $env:Path
try {
    $env:Path = 'C:\one;C:\two'
    Add-ToSessionPath 'C:\three'
    Assert-Equal 'C:\three;C:\one;C:\two' $env:Path 'adds a folder in front'
    Add-ToSessionPath 'C:\one'
    Assert-Equal 'C:\three;C:\one;C:\two' $env:Path 'never adds a folder twice'
} finally {
    $env:Path = $saved
}

if ($script:failures -gt 0) {
    Write-Output "$($script:failures) test(s) failed"
    exit 1
}
Write-Output 'All install-satellite helper tests passed.'
