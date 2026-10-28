<#
.SYNOPSIS
    Sets or removes a data provider's API keys as user environment variables, typed hidden.

.DESCRIPTION
    Every data provider is a plugin of the app (src/massive_collate/sources/registry.py). The
    script asks the app which there are (python -m massive_collate plugins --json: names only,
    never a key's value) and offers a menu: each provider, its keys and whether they are set.
    Pick one to set or replace its keys, or to remove them, to choose its plan, or to rescan it;
    a provider added as a plugin appears here by itself.

    Each plugin refreshes its own plan info: a rescan (python -m massive_collate capabilities
    <name>, a few requests) asks the provider what the key really gets, and a plan it finds is used
    wherever you have not set one. A plan you choose here always wins; Tiingo's API cannot tell
    its plans apart (starter, power, commercial), so its plan (TIINGO_PLAN) is only what you choose.

    A key is read with its input hidden: it is not shown on screen, not kept in the shell's
    history and not written to any file. It is saved where the app reads keys, the user's
    environment variables. Processes started afterwards see it; ones already running (the sync,
    the chart app) keep their old environment until restarted.

    With a name it goes straight there: a provider (its keys in turn) or one key's variable.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\set-key.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\set-key.ps1 tiingo

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\set-key.ps1 TIINGO_API_KEY -Remove

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\set-key.ps1 -List

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\set-key.ps1 tiingo -Rescan
#>
param(
    [Parameter(Position = 0)]
    [string]$Name,     # a provider (tiingo) or one key's variable (TIINGO_API_KEY); without it, the menu
    [switch]$Remove,
    [switch]$List,     # show the providers and their keys, change nothing
    [switch]$Rescan,   # with a provider: ask it again what the key gets (its capabilities and plan)
    [string]$Python    # the app's Python (default: the repository's .venv)
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Restart = 'Processes started from now on read it; restart the ones already running (the sync, the chart app).'

function Get-Python {
    $py = $Python
    if (-not $py) { $py = Join-Path $Root '.venv\Scripts\python.exe' }
    if (-not (Test-Path $py)) { $py = 'python' }
    $py
}

function Get-Providers {
    $py = Get-Python
    Push-Location $Root
    try {
        $json = & $py -m massive_collate plugins --json
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($code -ne 0 -or -not $json) {
        throw "Could not list the providers ($py -m massive_collate plugins --json)."
    }
    $listed = ($json | Out-String) | ConvertFrom-Json
    foreach ($e in @($listed.errors)) {
        if ($e) { Write-Host ('  left out: {0}: {1}' -f $e.where, $e.why) }
    }
    @($listed.providers)
}

function Get-KeyState($key) {
    foreach ($n in @($key.name) + @($key.aliases)) {
        if ($n -and [Environment]::GetEnvironmentVariable($n, 'User')) {
            if ($n -eq $key.name) { return 'set' }
            return "set as $n"
        }
    }
    'not set'
}

function Show-Providers($providers) {
    $keyed = @($providers | Where-Object { @($_.keys).Count -gt 0 })
    $free = @($providers | Where-Object { @($_.keys).Count -eq 0 })
    Write-Host 'Data providers (each a plugin of the app):'
    for ($i = 0; $i -lt $keyed.Count; $i++) {
        $p = $keyed[$i]
        $keys = (@($p.keys) | ForEach-Object { '{0}: {1}' -f $_.name, (Get-KeyState $_) }) -join ', '
        $note = ''
        if (@($p.keyless).Count -gt 0) { $note = '  ({0} without keys)' -f (@($p.keyless) -join ', ') }
        Write-Host ('  {0}. {1,-10} {2}{3}' -f ($i + 1), $p.title, $keys, $note)
        foreach ($v in @($p.planVars | Where-Object { $_ -and @($_.choices).Count -gt 0 })) {
            Write-Host ('     {0}={1} ({2})' -f $v.name, $v.value, (Get-Origin $v.origin))
        }
    }
    if ($free.Count -gt 0) {
        Write-Host ('  -  {0}: no key needed' -f ((@($free) | ForEach-Object { $_.title }) -join ', '))
    }
    $keyed
}

function Get-Origin([string]$origin) {
    switch ($origin) { 'you' { 'set by you' } 'scan' { 'found by its scan' } default { 'default' } }
}

function Set-Plan($p, $var) {
    $choices = @($var.choices)
    Write-Host ("{0} is {1} ({2}). Choices: {3}" -f $var.name, $var.value, (Get-Origin $var.origin), ($choices -join ', '))
    if ($p.planNote) { Write-Host "Note: $($p.planNote)." }
    $pick = (Read-Host "$($var.name) (a choice; 'scan' to leave it to the scan or the default; Enter keeps it)").Trim().ToLower()
    if (-not $pick) { Write-Host "$($var.name) unchanged."; return }
    if ($pick -eq 'scan') {
        if (Remove-Key $var.name) { Write-Host $Restart } else { Write-Host "$($var.name) was not set by you." }
        return
    }
    if ($choices -notcontains $pick) { Write-Host "Not one of: $($choices -join ', '). $($var.name) unchanged."; return }
    [Environment]::SetEnvironmentVariable($var.name, $pick, 'User')
    Write-Host "$($var.name)=$pick saved in your user environment variables."
    Write-Host $Restart
}

function Invoke-Rescan($p) {
    Write-Host "Asking $($p.title) what the key gets (a few requests, within its limits)..."
    $py = Get-Python
    Push-Location $Root
    try {
        & $py -m massive_collate capabilities $p.name
    } finally {
        Pop-Location
    }
}

function Set-Key([string]$Name) {
    $secure = Read-Host "$Name (typed hidden)" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim()
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)  # the plain copy in unmanaged memory, wiped
    }
    if (-not $value) {
        Write-Host "Nothing typed: $Name unchanged."
        return $false
    }
    $had = [bool][Environment]::GetEnvironmentVariable($Name, 'User')
    [Environment]::SetEnvironmentVariable($Name, $value, 'User')
    $length = $value.Length
    $value = $null
    $done = if ($had) { 'replaced' } else { 'saved' }
    Write-Host "$Name $done ($length characters) in your user environment variables."
    $true
}

function Remove-Key([string]$Name) {
    if (-not [Environment]::GetEnvironmentVariable($Name, 'User')) { return $false }
    [Environment]::SetEnvironmentVariable($Name, $null, 'User')
    Write-Host "$Name removed from your user environment variables."
    $true
}

function Set-ProviderKeys($p) {
    $changed = $false
    foreach ($k in @($p.keys)) {
        if (Set-Key $k.name) { $changed = $true }
    }
    if ($changed) { Write-Host $Restart }
}

function Remove-ProviderKeys($p) {
    $names = @($p.keys | ForEach-Object { @($_.name) + @($_.aliases) } | Where-Object { $_ })
    $removed = $false
    foreach ($n in $names) {
        if (Remove-Key $n) { $removed = $true }
    }
    if (-not $removed) { Write-Host "$($p.title): no key was set." }
}

function Edit-Provider($p) {
    Write-Host ''
    Write-Host "$($p.title): $($p.about)"
    if ($p.signup) { Write-Host "Keys from $($p.signup)" }
    $names = (@($p.keys) | ForEach-Object { $_.name }) -join ', '
    $plans = @($p.planVars | Where-Object { $_ -and @($_.choices).Count -gt 0 })
    Write-Host "  1. Set or replace $names (Enter at one keeps it)"
    Write-Host "  2. Remove $names"
    for ($i = 0; $i -lt $plans.Count; $i++) {
        Write-Host ('  {0}. Plan: {1}={2} ({3})' -f ($i + 3), $plans[$i].name, $plans[$i].value, (Get-Origin $plans[$i].origin))
    }
    $last = $plans.Count + 3
    if ($p.scan) { Write-Host "  $last. Rescan: ask $($p.title) what the key gets (its capabilities and plan)" }
    $pick = Read-Host 'Choice (number; Enter to go back)'
    if ($pick -eq '1') {
        Set-ProviderKeys $p
    } elseif ($pick -eq '2') {
        $sure = Read-Host "Remove $($p.title)'s keys? (y/N)"
        if ($sure -match '^(y|yes)$') { Remove-ProviderKeys $p } else { Write-Host 'Nothing changed.' }
    } elseif ($pick -match '^\d+$' -and [int]$pick -ge 3 -and [int]$pick -lt $last) {
        Set-Plan $p $plans[[int]$pick - 3]
    } elseif ($p.scan -and $pick -eq "$last") {
        Invoke-Rescan $p
    }
}

$providers = Get-Providers

if ($List) {
    $null = Show-Providers $providers
    exit 0
}

if ($Name) {
    $p = $providers | Where-Object { $_.name -eq $Name.ToLower() } | Select-Object -First 1
    if ($p) {
        if ($Rescan) {
            if (-not $p.scan) { Write-Host "$($p.title) has no capability scan."; exit 0 }
            Invoke-Rescan $p
            exit 0
        }
        if (@($p.keys).Count -eq 0) {
            Write-Host "$($p.title) needs no key."
            exit 0
        }
        if ($Remove) { Remove-ProviderKeys $p } else { Set-ProviderKeys $p }
        exit 0
    }
    $variables = @($providers | ForEach-Object { @($_.keys) } | ForEach-Object { @($_.name) + @($_.aliases) } |
        Where-Object { $_ })
    $variable = $variables | Where-Object { $_ -eq $Name.ToUpper() } | Select-Object -First 1
    if (-not $variable) {
        Write-Host "No provider or key named $Name. Providers: $((@($providers) | ForEach-Object { $_.name }) -join ', ')"
        Write-Host "Keys: $($variables -join ', ')"
        exit 1
    }
    if ($Remove) {
        if (-not (Remove-Key $variable)) { Write-Host "$variable was not set." }
    } elseif (Set-Key $variable) {
        Write-Host $Restart
    }
    exit 0
}

while ($true) {
    Write-Host ''
    $keyed = @(Show-Providers $providers)
    if ($keyed.Count -eq 0) {
        exit 0
    }
    $pick = Read-Host 'Provider (number; Enter to quit)'
    if (-not $pick) { exit 0 }
    if ($pick -notmatch '^\d+$' -or [int]$pick -lt 1 -or [int]$pick -gt $keyed.Count) {
        Write-Host 'No such provider.'
        continue
    }
    Edit-Provider $keyed[[int]$pick - 1]
}
