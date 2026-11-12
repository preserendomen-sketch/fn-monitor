#Requires -Version 5.1
<#
.SYNOPSIS
Restarts the FN Scanner task when the process is gone or the heartbeat is stale.

Does not call the webhook. A disabled task is left disabled so maintenance
stays stopped.
#>
param(
    [string]$InstallDir = "C:\fn-monitor",
    [string]$TaskName = "FN Scanner",
    [int]$StaleMinutes = 12
)

$ErrorActionPreference = "Stop"
$InstallDir = $InstallDir.TrimEnd("\")
$LogPath = Join-Path $InstallDir "logs\watchdog.log"
$HeartbeatPath = Join-Path $InstallDir "scanner_heartbeat.json"
$WatchdogHeartbeatPath = Join-Path $InstallDir "watchdog_heartbeat.json"

function Write-WatchdogLog([string]$Message) {
    $dir = Split-Path -Parent $LogPath
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir | Out-Null
    }
    if ((Test-Path -LiteralPath $LogPath) -and ((Get-Item -LiteralPath $LogPath).Length -gt 1MB)) {
        Move-Item -LiteralPath $LogPath -Destination "$LogPath.prev" -Force
    }
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $LogPath -Value $line -Encoding ascii
}

function Write-WatchdogHeartbeat([string]$Action, [string]$Detail) {
    $unix = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $utc = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    $safeAction = ($Action -replace '"', "")
    $safeDetail = (($Detail -replace '"', "") -replace "[\r\n]", " ")
    $json = @"
{
  "utc": "$utc",
  "unix": $unix,
  "action": "$safeAction",
  "detail": "$safeDetail",
  "task": "$TaskName"
}
"@
    $tmp = "$WatchdogHeartbeatPath.tmp"
    Set-Content -LiteralPath $tmp -Value $json -Encoding ascii
    Move-Item -LiteralPath $tmp -Destination $WatchdogHeartbeatPath -Force
}

function Read-ScannerHeartbeat {
    if (-not (Test-Path -LiteralPath $HeartbeatPath)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $HeartbeatPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Get-PidProcess([int]$ProcessId) {
    if ($ProcessId -le 0) {
        return $null
    }
    try {
        return Get-Process -Id $ProcessId -ErrorAction Stop
    } catch {
        return $null
    }
}

function Test-ScannerProcess($proc) {
    if (-not $proc) {
        return $false
    }
    if ($proc.ProcessName -notmatch "^(?i)pythonw?$") {
        return $false
    }
    $cmd = ""
    try {
        $cim = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $($proc.Id)" -ErrorAction Stop
        $cmd = [string]$cim.CommandLine
    } catch {
        $cmd = ""
    }
    if (-not $cmd) {
        return $true
    }
    if ($cmd -match "(?i)--backfill" -or $cmd -match "(?i)--once") {
        return $false
    }
    return $cmd -match "(?i)scanner\.py"
}

try {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-WatchdogLog "Opravilo '$TaskName' ne obstaja. Najprej zaženi install-fn-scanner.ps1."
        Write-WatchdogHeartbeat "missing-task" $TaskName
        exit 1
    }

    if ($task.State -eq "Disabled") {
        Write-WatchdogHeartbeat "disabled" ""
        exit 0
    }

    $hb = Read-ScannerHeartbeat
    $proc = $null
    $ageSec = $null
    if ($hb -and $null -ne $hb.pid) {
        $candidate = Get-PidProcess ([int]$hb.pid)
        if (Test-ScannerProcess $candidate) {
            $proc = $candidate
        }
    }
    if ($hb -and $null -ne $hb.unix) {
        $ageSec = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$hb.unix
    }
    $limit = $StaleMinutes * 60
    $fresh = ($null -ne $ageSec) -and ($ageSec -ge -120) -and ($ageSec -le $limit)

    if ($proc -and $fresh) {
        Write-WatchdogHeartbeat "ok" "pid $($proc.Id)"
        exit 0
    }

    # A fresh heartbeat while the task is running means the service is up,
    # even if this account cannot see the process id.
    if ($fresh -and $task.State -eq "Running") {
        Write-WatchdogHeartbeat "ok" "task running"
        exit 0
    }

    # First cycle can take a couple of minutes before the heartbeat exists.
    if (-not $fresh -and $task.State -eq "Running") {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        $last = $info.LastRunTime
        if ($last -and $last.Year -ge 2000) {
            $ranFor = (Get-Date) - $last
            if ($ranFor.TotalSeconds -ge 0 -and $ranFor.TotalMinutes -lt $StaleMinutes) {
                Write-WatchdogHeartbeat "starting" ""
                exit 0
            }
        }
    }

    $reason = "ni živega procesa"
    if ($proc -and -not $fresh) {
        $reason = "heartbeat je prestar"
    } elseif (-not $fresh -and $hb) {
        $reason = "heartbeat je prestar"
    }

    if ($task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $TaskName
        Start-Sleep -Seconds 3
    }
    if ($proc) {
        try {
            Stop-Process -Id $proc.Id -Force -ErrorAction Stop
        } catch {
        }
        Start-Sleep -Seconds 1
    }
    Start-ScheduledTask -TaskName $TaskName
    Write-WatchdogLog "Skener zagnan znova ($reason)."
    Write-WatchdogHeartbeat "restarted" $reason
    exit 0
} catch {
    Write-WatchdogLog "Nadzor ni uspel: $($_.Exception.Message)"
    try {
        Write-WatchdogHeartbeat "error" $_.Exception.Message
    } catch {
    }
    exit 1
}
