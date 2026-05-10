<#
.SYNOPSIS
  Stop and remove the NSSM-registered Telegram/Discord bot services.

.PARAMETER Bot
  Which bot(s) to remove: telegram, discord, or all (default).

.PARAMETER NssmPath
  Path to nssm.exe. Defaults to .\nssm.exe next to this script.

.EXAMPLE
  .\uninstall-service.ps1
  .\uninstall-service.ps1 -Bot discord
#>
[CmdletBinding()]
param(
    [ValidateSet('telegram','discord','all')]
    [string]$Bot = 'all',
    [string]$NssmPath
)

$ErrorActionPreference = 'Stop'
$ScriptDir = $PSScriptRoot

if (-not $NssmPath) { $NssmPath = Join-Path $ScriptDir 'nssm.exe' }
if (-not (Test-Path $NssmPath)) {
    throw "nssm.exe not found at '$NssmPath'."
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Must run as Administrator." }

$names = switch ($Bot) {
    'telegram' { @('ClaudeRemoteTelegram') }
    'discord'  { @('ClaudeRemoteDiscord') }
    'all'      { @('ClaudeRemoteTelegram','ClaudeRemoteDiscord') }
}

foreach ($name in $names) {
    if (-not (Get-Service -Name $name -ErrorAction SilentlyContinue)) {
        Write-Host "Service '$name' not installed. Skipping." -ForegroundColor Yellow
        continue
    }
    Write-Host "Stopping and removing '$name'..." -ForegroundColor Cyan
    & $NssmPath stop   $name confirm 2>$null | Out-Null
    & $NssmPath remove $name confirm | Out-Null
    Write-Host "  removed." -ForegroundColor Green
}
