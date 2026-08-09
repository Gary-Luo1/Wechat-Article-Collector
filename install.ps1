#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [ValidateSet("agents", "codex", "claude", "copilot", "openclaw", "hermes", "all")]
    [string]$Target = "agents",
    [string]$InstallPath,
    [switch]$NoDeps
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $PSCommandPath
$sourceDir = Join-Path $scriptRoot "skills\wechat-article-subscriber"

function Find-Python {
    $candidates = @()
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { $candidates += [pscustomobject]@{ Source = $python.Source; Prefix = @() } }
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) { $candidates += [pscustomobject]@{ Source = $launcher.Source; Prefix = @("-3") } }
    foreach ($candidate in $candidates) {
        $versionText = & $candidate.Source @($candidate.Prefix) -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null
        if ($LASTEXITCODE -eq 0) {
            try {
                if ([version]$versionText -ge [version]"3.9") { return $candidate }
            } catch { }
        }
    }
    throw "Python 3.9+ is required (supported launchers: python or py -3)"
}

$pythonCommand = Find-Python

function Invoke-Python([string[]]$Arguments, [string]$FailureMessage) {
    & $pythonCommand.Source @($pythonCommand.Prefix) @Arguments
    if ($LASTEXITCODE -ne 0) { throw $FailureMessage }
}

function Get-DataHome {
    if ($env:WECHAT_ARTICLE_HOME) {
        $expanded = [Environment]::ExpandEnvironmentVariables($env:WECHAT_ARTICLE_HOME)
        if ($expanded -eq "~") {
            $expanded = $env:USERPROFILE
        } elseif ($expanded.StartsWith("~\") -or $expanded.StartsWith("~/")) {
            $expanded = Join-Path $env:USERPROFILE $expanded.Substring(2)
        }
        return [IO.Path]::GetFullPath($expanded)
    }
    $appDataRoot = if ($env:APPDATA) { $env:APPDATA } else { $env:LOCALAPPDATA }
    if (-not $appDataRoot) { throw "APPDATA or LOCALAPPDATA is required" }
    return Join-Path $appDataRoot "wechat-article-subscriber"
}

function Get-TargetParent([string]$Kind) {
    $profileRoot = if ($env:WECHAT_SKILL_INSTALL_ROOT) { $env:WECHAT_SKILL_INSTALL_ROOT } else { $env:USERPROFILE }
    switch ($Kind) {
        "agents" { return Join-Path $profileRoot ".agents\skills" }
        "codex" {
            $codexRoot = if ($env:WECHAT_SKILL_INSTALL_ROOT) {
                Join-Path $profileRoot ".codex"
            } elseif ($env:CODEX_HOME) {
                $env:CODEX_HOME
            } else {
                Join-Path $profileRoot ".codex"
            }
            return Join-Path $codexRoot "skills"
        }
        "claude" { return Join-Path $profileRoot ".claude\skills" }
        "copilot" { return Join-Path $profileRoot ".copilot\skills" }
        "openclaw" { return Join-Path $profileRoot ".openclaw\skills" }
        "hermes" { return Join-Path $profileRoot ".hermes\skills" }
    }
}

function New-BackupPath([string]$Destination) {
    $timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
    $candidate = "$Destination.backup.$timestamp"
    $counter = 1
    while (Test-Path -LiteralPath $candidate) {
        $candidate = "$Destination.backup.$timestamp.$counter"
        $counter++
    }
    return $candidate
}

function Prepare-Skill([string]$Kind) {
    if ($InstallPath) {
        if ($Target -eq "all") { throw "-InstallPath cannot be combined with -Target all" }
        $destination = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($InstallPath))
        $parent = Split-Path -Parent $destination
    } else {
        $parent = Get-TargetParent $Kind
        $destination = Join-Path $parent "wechat-article-subscriber"
    }
    $temporary = Join-Path $parent ".wechat-article-subscriber.install.$([guid]::NewGuid().ToString('N'))"
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    New-Item -ItemType Directory -Path $temporary | Out-Null
    foreach ($fileName in @("SKILL.md", "requirements.txt")) {
        Copy-Item -LiteralPath (Join-Path $sourceDir $fileName) -Destination $temporary -Force
    }
    foreach ($directoryName in @("agents", "scripts", "references")) {
        New-Item -ItemType Directory -Path (Join-Path $temporary $directoryName) | Out-Null
    }
    foreach ($file in Get-ChildItem -LiteralPath (Join-Path $sourceDir "agents") -File | Where-Object { $_.Extension -in @(".yaml", ".yml") }) {
        Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $temporary "agents") -Force
    }
    foreach ($file in Get-ChildItem -LiteralPath (Join-Path $sourceDir "scripts") -File | Where-Object { $_.Extension -in @(".py", ".sh", ".ps1") }) {
        Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $temporary "scripts") -Force
    }
    foreach ($file in Get-ChildItem -LiteralPath (Join-Path $sourceDir "references") -File -Filter "*.md") {
        Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $temporary "references") -Force
    }
    $assetsPath = Join-Path $sourceDir "assets"
    if (Test-Path -LiteralPath $assetsPath) {
        Copy-Item -LiteralPath $assetsPath -Destination $temporary -Recurse -Force
    }
    return [pscustomobject]@{
        Kind = $Kind
        Destination = $destination
        Temporary = $temporary
        Backup = New-BackupPath $destination
        HadExisting = Test-Path -LiteralPath $destination
        Committed = $false
    }
}

$targets = if ($Target -eq "all") { @("agents", "codex", "claude", "copilot", "openclaw", "hermes") } else { @($Target) }
$prepared = @()
$venvStage = $null
$venvDir = $null
$venvBackup = $null
$venvHadExisting = $false
$venvCommitted = $false

try {
    foreach ($kind in $targets) { $prepared += Prepare-Skill $kind }

    if (-not $NoDeps) {
        $dataHome = Get-DataHome
        New-Item -ItemType Directory -Force -Path $dataHome | Out-Null
        $venvDir = Join-Path $dataHome "venv"
        $venvStage = Join-Path $dataHome ".venv.install.$([guid]::NewGuid().ToString('N'))"
        Invoke-Python @("-m", "venv", $venvStage) "Failed to create virtual environment"
        $venvPython = Join-Path $venvStage "Scripts\python.exe"
        & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $sourceDir "requirements.txt")
        if ($LASTEXITCODE -ne 0) { throw "Failed to install Python dependencies" }
        $venvHadExisting = Test-Path -LiteralPath $venvDir
        $venvBackup = New-BackupPath $venvDir
    }

    foreach ($item in $prepared) {
        if ($item.HadExisting) { Move-Item -LiteralPath $item.Destination -Destination $item.Backup }
        Move-Item -LiteralPath $item.Temporary -Destination $item.Destination
        $item.Committed = $true
    }

    if (-not $NoDeps) {
        if ($venvHadExisting) { Move-Item -LiteralPath $venvDir -Destination $venvBackup }
        Move-Item -LiteralPath $venvStage -Destination $venvDir
        $venvCommitted = $true
        Write-Host "Created isolated runtime at $venvDir"
    }
} catch {
    if ($venvCommitted -and (Test-Path -LiteralPath $venvDir)) {
        Remove-Item -LiteralPath $venvDir -Recurse -Force
    }
    if ($venvHadExisting -and $venvBackup -and (Test-Path -LiteralPath $venvBackup) -and -not (Test-Path -LiteralPath $venvDir)) {
        Move-Item -LiteralPath $venvBackup -Destination $venvDir
    }
    foreach ($item in @($prepared | Sort-Object { [array]::IndexOf($prepared, $_) } -Descending)) {
        if ($item.Committed -and (Test-Path -LiteralPath $item.Destination)) {
            Remove-Item -LiteralPath $item.Destination -Recurse -Force
        }
        if ($item.HadExisting -and (Test-Path -LiteralPath $item.Backup) -and -not (Test-Path -LiteralPath $item.Destination)) {
            Move-Item -LiteralPath $item.Backup -Destination $item.Destination
        }
        if (Test-Path -LiteralPath $item.Temporary) {
            Remove-Item -LiteralPath $item.Temporary -Recurse -Force
        }
    }
    if ($venvStage -and (Test-Path -LiteralPath $venvStage)) {
        Remove-Item -LiteralPath $venvStage -Recurse -Force
    }
    throw "Installation failed and previous installations were restored: $_"
}

foreach ($item in $prepared) {
    if ($item.HadExisting) { Write-Host "Backed up existing $($item.Kind) installation to $($item.Backup)" }
    Write-Host "Installed $($item.Kind) skill at $($item.Destination)"
}

if ($NoDeps) {
    Write-Host "Skipped dependency installation; commands other than setup require requests, beautifulsoup4, and curl_cffi in the selected Python runtime."
}
if (-not (Get-Command lark-cli -ErrorAction SilentlyContinue)) {
    Write-Host "Feishu sync is disabled until @larksuite/cli is installed and authenticated."
}
Write-Host "Installation complete. Restart or open your Agent, then say:"
Write-Host '  "配置微信公众号文章订阅"'
Write-Host "The Agent will guide configuration in dialogue; do not paste credentials into shell arguments."
