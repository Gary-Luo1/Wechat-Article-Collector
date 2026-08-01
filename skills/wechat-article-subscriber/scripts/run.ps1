#!/usr/bin/env pwsh
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$CommandArgs)

$ErrorActionPreference = "Stop"
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$scriptRoot = Split-Path -Parent $PSCommandPath

$candidates = @()
$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) { $candidates += [pscustomobject]@{ Source = $python.Source; Prefix = @() } }
$launcher = Get-Command py -ErrorAction SilentlyContinue
if ($launcher) { $candidates += [pscustomobject]@{ Source = $launcher.Source; Prefix = @("-3") } }

foreach ($candidate in $candidates) {
    $versionText = & $candidate.Source @($candidate.Prefix) -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null
    if ($LASTEXITCODE -ne 0) { continue }
    try {
        if ([version]$versionText -lt [version]"3.9") { continue }
    } catch {
        continue
    }
    & $candidate.Source @($candidate.Prefix) (Join-Path $scriptRoot "runtime.py") @CommandArgs
    exit $LASTEXITCODE
}

throw "Python 3.9+ is required (supported launchers: python or py -3)"
