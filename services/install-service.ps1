<#
.SYNOPSIS
  Install/start the Telegram and/or Discord bot as Windows services via NSSM.

.DESCRIPTION
  Wraps `<bot>\.venv\Scripts\python.exe bot.py` as a Windows service with:
    - auto-start at boot
    - auto-restart on crash (5s delay)
    - stdout/stderr -> services\logs\<bot>.{out,err}.log (rotated at 10 MB)
    - service runs as the interactive user (NOT SYSTEM) so the claude CLI
      can find its auth at %USERPROFILE%\.claude\

  Run elevated (Administrator).

.PARAMETER Bot
  Which bot(s) to install: telegram, discord, or all (default).

.PARAMETER NssmPath
  Path to nssm.exe. Defaults to .\nssm.exe next to this script.
  Download: https://nssm.cc/download

.PARAMETER ServiceAccount
  PSCredential for the account to run the service as. If omitted, prompts.
  Username format: .\YourName  or  YourPC\YourName  or  DOMAIN\User

.EXAMPLE
  .\install-service.ps1
  .\install-service.ps1 -Bot telegram
  .\install-service.ps1 -NssmPath C:\tools\nssm.exe
#>
[CmdletBinding()]
param(
    [ValidateSet('telegram','discord','all')]
    [string]$Bot = 'all',
    [string]$NssmPath,
    [PSCredential]$ServiceAccount
)

$ErrorActionPreference = 'Stop'
$ScriptDir = $PSScriptRoot
$RepoRoot  = Split-Path -Parent $ScriptDir
$LogDir    = Join-Path $ScriptDir 'logs'

if (-not $NssmPath) { $NssmPath = Join-Path $ScriptDir 'nssm.exe' }
if (-not (Test-Path $NssmPath)) {
    throw "nssm.exe not found at '$NssmPath'. Download from https://nssm.cc/download (the win64 zip), then drop nssm.exe into '$ScriptDir' or pass -NssmPath."
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Must run as Administrator (services.msc registration requires elevation)." }

if (-not $ServiceAccount) {
    Write-Host "Service must run as the user that owns ~/.claude (claude CLI auth)." -ForegroundColor Yellow
    Write-Host "Enter Windows credentials for that account (e.g. .\YourName):" -ForegroundColor Yellow
    $ServiceAccount = Get-Credential -Message "Account to run the bot service as"
}
$svcUser = $ServiceAccount.UserName
$svcPass = $ServiceAccount.GetNetworkCredential().Password

# NSSM/Windows requires a qualified account name. Bare 'Empty' fails with
# "account name is invalid"; must be '.\Empty', 'COMPUTER\Empty', 'DOMAIN\User',
# or a UPN ('user@domain'). Normalise to '.\<name>' if unqualified.
if ($svcUser -notmatch '[\\@]') {
    $svcUser = ".\$svcUser"
    Write-Host "Using account '$svcUser' (local machine)" -ForegroundColor DarkGray
}

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

$serviceMap = @{
    telegram = @{
        Name        = 'ClaudeRemoteTelegram'
        DisplayName = 'Claude Remote - Telegram Bot'
        Description = 'Telegram bridge to local Claude Code CLI.'
    }
    discord  = @{
        Name        = 'ClaudeRemoteDiscord'
        DisplayName = 'Claude Remote - Discord Bot'
        Description = 'Discord bridge to local Claude Code CLI.'
    }
}

$bots = if ($Bot -eq 'all') { @('telegram','discord') } else { @($Bot) }

function Invoke-Nssm {
    param([Parameter(ValueFromRemainingArguments=$true)] [string[]]$Args)
    & $NssmPath @Args | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "nssm $($Args -join ' ') failed (exit $LASTEXITCODE)" }
}

foreach ($b in $bots) {
    $svc     = $serviceMap[$b]
    $name    = $svc.Name
    $botDir  = Join-Path $RepoRoot $b
    $python  = Join-Path $botDir '.venv\Scripts\python.exe'
    $envFile = Join-Path $botDir '.env'

    if (-not (Test-Path $python))  { Write-Warning "Skipping ${b}: $python not found. Run $botDir\start.ps1 once first."; continue }
    if (-not (Test-Path $envFile)) { Write-Warning "Skipping ${b}: $envFile not found."; continue }

    $stdoutLog = Join-Path $LogDir "$b.out.log"
    $stderrLog = Join-Path $LogDir "$b.err.log"

    if (Get-Service -Name $name -ErrorAction SilentlyContinue) {
        Write-Host "Service '$name' already exists; removing first..." -ForegroundColor Yellow
        & $NssmPath stop   $name confirm 2>$null | Out-Null
        & $NssmPath remove $name confirm 2>$null | Out-Null
    }

    Write-Host "Installing service '$name'..." -ForegroundColor Cyan
    Invoke-Nssm install $name $python bot.py
    Invoke-Nssm set $name AppDirectory          $botDir
    Invoke-Nssm set $name DisplayName           $svc.DisplayName
    Invoke-Nssm set $name Description           $svc.Description
    Invoke-Nssm set $name Start                 SERVICE_AUTO_START
    # Set ObjectName separately so the password never lands in an error message.
    & $NssmPath set $name ObjectName $svcUser $svcPass | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "nssm set $name ObjectName failed (exit $LASTEXITCODE) — check the account/password and that the user has 'Log on as a service' right." }
    Invoke-Nssm set $name AppStdout             $stdoutLog
    Invoke-Nssm set $name AppStderr             $stderrLog
    Invoke-Nssm set $name AppRotateFiles        1
    Invoke-Nssm set $name AppRotateOnline       1
    Invoke-Nssm set $name AppRotateBytes        10485760
    Invoke-Nssm set $name AppExit Default       Restart
    Invoke-Nssm set $name AppRestartDelay       5000
    Invoke-Nssm set $name AppStopMethodConsole  10000

    Write-Host "Starting service '$name'..." -ForegroundColor Green
    & $NssmPath start $name | Out-Null
    Start-Sleep -Seconds 2
    $status = (Get-Service -Name $name).Status
    Write-Host "  status: $status" -ForegroundColor Green
    Write-Host "  logs  : $stdoutLog" -ForegroundColor DarkGray
}

# Best-effort scrub of the password from memory
$svcPass = $null
$ServiceAccount = $null
[GC]::Collect()

Write-Host ""
Write-Host 'Done. Manage via: services.msc, or  Get-Service ClaudeRemote*' -ForegroundColor Cyan
