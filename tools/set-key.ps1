<#
.SYNOPSIS
    Saves one of the app's API keys as a user environment variable, typed hidden.

.DESCRIPTION
    The key is read with its input hidden: it is not shown on screen, not kept in the shell's
    history and not written to any file. It is saved where the app reads keys, the user's
    environment variables (config.KEY_NAMES). Processes started afterwards see it; ones already
    running (the sync, the chart app) keep their old environment until restarted. Without a name
    it lists the keys, marks the ones already set, and asks which one.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\set-key.ps1 TIINGO_API_KEY

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\set-key.ps1 TIINGO_API_KEY -Remove
#>
param(
    [ValidateSet('MASSIVE_API_KEY', 'ALPACA_API_KEY_ID', 'ALPACA_API_SECRET_KEY', 'APCA_API_KEY_ID',
                 'APCA_API_SECRET_KEY', 'TIINGO_API_KEY')]
    [string]$Name,
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'

if (-not $Name) {
    $choices = @(((Get-Command $PSCommandPath).Parameters['Name'].Attributes |
        Where-Object { $_ -is [System.Management.Automation.ValidateSetAttribute] }).ValidValues)
    for ($i = 0; $i -lt $choices.Count; $i++) {
        $set = if ([Environment]::GetEnvironmentVariable($choices[$i], 'User')) { '  (set)' } else { '' }
        Write-Host ('  {0}. {1}{2}' -f ($i + 1), $choices[$i], $set)
    }
    $pick = Read-Host 'Which key (number)'
    if ($pick -notmatch '^\d+$' -or [int]$pick -lt 1 -or [int]$pick -gt $choices.Count) {
        Write-Host 'Nothing changed.'
        exit 1
    }
    $Name = $choices[[int]$pick - 1]
}

if ($Remove) {
    [Environment]::SetEnvironmentVariable($Name, $null, 'User')
    Write-Host "$Name removed from your user environment variables."
    exit 0
}

$secure = Read-Host "$Name (typed hidden)" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim()
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)  # the plain copy in unmanaged memory, wiped
}
if (-not $value) {
    Write-Host 'Nothing typed: nothing changed.'
    exit 1
}
$had = [bool][Environment]::GetEnvironmentVariable($Name, 'User')
[Environment]::SetEnvironmentVariable($Name, $value, 'User')
$length = $value.Length
$value = $null
$done = if ($had) { 'replaced' } else { 'saved' }
Write-Host "$Name $done ($length characters) in your user environment variables."
Write-Host 'Processes started from now on read it; restart the ones already running (the sync, the chart app).'
