#Requires -Version 5.1
<#
.SYNOPSIS
Registers FN Scanner and FN Scanner Watchdog to run without an interactive logon.

The password is passed only to Register-ScheduledTask. It is not written to a
file, the task XML in git, or the log. config.json, scanner_state.json, and
.venv are not read or written. A scanner that is already running is stopped
and started again so it loads the new code.
#>
param(
    [string]$InstallDir = "",
    [string]$TaskName = "FN Scanner",
    [string]$WatchdogTaskName = "FN Scanner Watchdog",
    [string]$User = "",
    [securestring]$Password,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

function ConvertTo-XmlText([string]$Value) {
    return ($Value -replace "&", "&amp;" -replace "<", "&lt;" -replace ">", "&gt;")
}

function Stop-FnTaskIfRunning([string]$Name) {
    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (-not $existing) {
        return
    }
    if ($existing.State -eq "Running") {
        Stop-ScheduledTask -TaskName $Name
    }
    $deadline = (Get-Date).AddSeconds(30)
    do {
        $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        if (-not $existing -or $existing.State -ne "Running") {
            return
        }
        if ((Get-Date) -ge $deadline) {
            break
        }
        Start-Sleep -Seconds 1
    } while ($true)
    Write-Error "Opravilo '$Name' se ni ustavilo. Nova koda se ne naloži, dokler ga ne ustaviš in znova zaženeš."
}

function ConvertFrom-SecureStringPlain([securestring]$Secure) {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Register-FnTask([string]$XmlPath, [string]$Name, [string]$RunAs, [string]$PlainPassword) {
    $raw = Get-Content -LiteralPath $XmlPath -Raw -Encoding UTF8
    $raw = $raw.Replace("__INSTALL_DIR__", (ConvertTo-XmlText $InstallDir))
    $raw = $raw.Replace("__RUN_AS__", (ConvertTo-XmlText $RunAs))
    if ($raw -match "__INSTALL_DIR__|__RUN_AS__") {
        throw "V $XmlPath so ostali neznani nadomestni znaki."
    }
    Register-ScheduledTask -TaskName $Name -Xml $raw -User $RunAs -Password $PlainPassword -Force | Out-Null
    Enable-ScheduledTask -TaskName $Name | Out-Null
}

if ($Uninstall) {
    foreach ($name in @($TaskName, $WatchdogTaskName)) {
        $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "Odstranjeno: $name"
        } else {
            Write-Host "Ni opravila: $name"
        }
    }
    return
}

if (-not $InstallDir) {
    $sibling = Split-Path -Parent $PSScriptRoot
    if (Test-Path -LiteralPath (Join-Path $sibling "scanner.py")) {
        $InstallDir = $sibling
    } else {
        $InstallDir = "C:\fn-monitor"
    }
}
$InstallDir = $InstallDir.TrimEnd("\")

if (-not $User) {
    $User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
}

$python = Join-Path $InstallDir ".venv\Scripts\python.exe"
$scanner = Join-Path $InstallDir "scanner.py"
$health = Join-Path $InstallDir "health.py"
$starter = Join-Path $InstallDir "windows\start-scanner.cmd"
$watchdog = Join-Path $InstallDir "windows\watchdog.ps1"
$scannerXml = Join-Path $PSScriptRoot "FN-Scanner.xml"
$watchdogXml = Join-Path $PSScriptRoot "FN-Scanner-Watchdog.xml"

foreach ($path in @($python, $scanner, $health, $starter, $watchdog, $scannerXml, $watchdogXml)) {
    if (-not (Test-Path -LiteralPath $path)) {
        Write-Error "Manjka: $path"
        exit 1
    }
}

$logs = Join-Path $InstallDir "logs"
if (-not (Test-Path -LiteralPath $logs)) {
    New-Item -ItemType Directory -Path $logs | Out-Null
}

if (-not $Password) {
    $Password = Read-Host -AsSecureString -Prompt "Geslo za $User (opravilo teče tudi brez prijave)"
}
$plain = ConvertFrom-SecureStringPlain $Password
if (-not $plain) {
    Write-Error "Geslo je prazno. Brez gesla opravilo ne teče, ko nihče ni prijavljen."
    exit 1
}

try {
    Register-FnTask $scannerXml $TaskName $User $plain
    Register-FnTask $watchdogXml $WatchdogTaskName $User $plain
} finally {
    $plain = $null
}

Stop-FnTaskIfRunning $WatchdogTaskName
Stop-FnTaskIfRunning $TaskName
Start-ScheduledTask -TaskName $TaskName
Start-ScheduledTask -TaskName $WatchdogTaskName

Write-Host "Python: $python"
Write-Host "Mapa: $InstallDir"
Write-Host "Opravilo '$TaskName' teče ob vklopu, tudi brez prijave."
Write-Host "Prejšnje opravilo z istim imenom (samo ob prijavi) je zamenjano."
Write-Host "Nadzor: $WatchdogTaskName vsako minuto (lokalno)."
Write-Host "config.json, scanner_state.json in .venv ne prepisuj."
Write-Host "Če je skener že tekel, je ustavljen in znova zagnan, da prebere novo kodo."
Write-Host "Zdravstveni webhook je izklopljen, dokler nista nastavljena FN_HEALTH_WEBHOOK_URL in FN_HEALTH_WEBHOOK_KEY."
Write-Host "Srčni utrip: $(Join-Path $InstallDir 'scanner_heartbeat.json')"
Write-Host "Dnevnik: $(Join-Path $InstallDir 'logs\scanner.out.log')"
