#Requires -Version 5.1
<#
.SYNOPSIS
Restarts the FN Scanner task when the process is gone or the heartbeat is stale.

The check is local. health.py sends a webhook only after a restart attempt,
and only when a health webhook is configured. A disabled task is left
disabled so maintenance stays stopped.
#>
param(
    [string]$InstallDir = "C:\fn-monitor",
    [string]$TaskName = "FN Scanner",
    [int]$StaleMinutes = 12
)

$ErrorActionPreference = "Stop"
$InstallDir = $InstallDir.TrimEnd("\")
if ($StaleMinutes -lt 1) {
    $StaleMinutes = 12
}
$LogPath = Join-Path $InstallDir "logs\watchdog.log"
$HeartbeatPath = Join-Path $InstallDir "scanner_heartbeat.json"
$WatchdogHeartbeatPath = Join-Path $InstallDir "watchdog_heartbeat.json"
$StatePath = Join-Path $InstallDir "watchdog_notify.json"
$Python = Join-Path $InstallDir ".venv\Scripts\python.exe"
$HealthPy = Join-Path $InstallDir "health.py"
$env:PYTHONIOENCODING = "utf-8"

function Write-WatchdogLog([string]$Message) {
    $dir = Split-Path -Parent $LogPath
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir | Out-Null
    }
    if ((Test-Path -LiteralPath $LogPath) -and ((Get-Item -LiteralPath $LogPath).Length -gt 1MB)) {
        Move-Item -LiteralPath $LogPath -Destination "$LogPath.prev" -Force
    }
    $safe = ($Message -replace "[\r\n]", " ")
    if ($safe.Length -gt 400) {
        $safe = $safe.Substring(0, 400)
    }
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $safe
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

function Invoke-HealthPy {
    param([string[]]$HealthArgs)
    if (-not (Test-Path -LiteralPath $Python) -or -not (Test-Path -LiteralPath $HealthPy)) {
        return @{ Code = 9; Text = "" }
    }
    $errFile = Join-Path $InstallDir "logs\health-last.err"
    $output = & $Python -u $HealthPy @HealthArgs 2> $errFile
    $code = $LASTEXITCODE
    if ($null -eq $code) {
        $code = 1
    }
    $text = ""
    if ($null -ne $output) {
        $text = ($output | Out-String).Trim()
    }
    if (Test-Path -LiteralPath $errFile) {
        $errText = Get-Content -LiteralPath $errFile -Raw -ErrorAction SilentlyContinue
        if ($errText -and $errText.Trim()) {
            $line = ($errText -replace "[\r\n]+", " ").Trim()
            if ($line.Length -gt 300) {
                $line = $line.Substring(0, 300)
            }
            Write-WatchdogLog "health.py: $line"
        }
    }
    return @{ Code = [int]$code; Text = $text }
}

function ConvertTo-DecisionObject([string]$Raw) {
    if ([string]::IsNullOrWhiteSpace($Raw)) {
        return $null
    }
    $start = $Raw.IndexOf("{")
    if ($start -lt 0) {
        return $null
    }
    $obj = $Raw.Substring($start) | ConvertFrom-Json
    $action = [string]$obj.action
    $known = @("ok", "starting", "disabled", "missing-task", "restart")
    if ($known -notcontains $action) {
        return $null
    }
    $problem = $false
    $flag = [string]$obj.heartbeat_problem
    if ($flag -eq "True" -or $flag -eq "true") {
        $problem = $true
    }
    $age = $null
    if ($null -ne $obj.heartbeat_age_seconds -and "$($obj.heartbeat_age_seconds)" -ne "") {
        $age = [string]$obj.heartbeat_age_seconds
    }
    $detail = ""
    if ($null -ne $obj.detail) {
        $detail = [string]$obj.detail
    }
    return @{
        action = $action
        reason = [string]$obj.reason
        heartbeat_problem = $problem
        heartbeat_age_seconds = $age
        detail = $detail
    }
}

# Fallback matches health.assess when health.py cannot start.
function Get-InlineDecision {
    param(
        $Heartbeat,
        [bool]$ProcessAlive,
        [bool]$TaskRunning,
        $RanSeconds,
        [double]$StaleSeconds
    )
    $age = $null
    if ($Heartbeat -and $null -ne $Heartbeat.unix) {
        $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$Heartbeat.unix
    }
    $fresh = ($null -ne $age) -and ($age -ge -120) -and ($age -le $StaleSeconds)
    $problem = -not $fresh
    $ageOut = $null
    if ($null -ne $age) {
        $ageOut = [string][int]$age
    }
    if ($ProcessAlive -and $fresh) {
        return @{ action = "ok"; reason = ""; heartbeat_problem = $false; heartbeat_age_seconds = $ageOut; detail = "process" }
    }
    if ($fresh -and $TaskRunning) {
        return @{ action = "ok"; reason = ""; heartbeat_problem = $false; heartbeat_age_seconds = $ageOut; detail = "task running" }
    }
    if ((-not $fresh) -and $TaskRunning -and $null -ne $RanSeconds) {
        $ran = [double]$RanSeconds
        if ($ran -ge 0 -and $ran -lt $StaleSeconds) {
            return @{ action = "starting"; reason = ""; heartbeat_problem = [bool]$problem; heartbeat_age_seconds = $ageOut; detail = "" }
        }
    }
    if ((-not $fresh) -and $null -eq $Heartbeat) {
        $reason = "heartbeat manjka"
    } elseif (-not $fresh) {
        $reason = "heartbeat je prestar"
    } else {
        $reason = "ni živega procesa"
    }
    return @{ action = "restart"; reason = $reason; heartbeat_problem = [bool]$problem; heartbeat_age_seconds = $ageOut; detail = "" }
}

function Format-AgeArg($Decision) {
    $age = $Decision.heartbeat_age_seconds
    if ($null -eq $age -or "$age" -eq "") {
        return @()
    }
    return @("--heartbeat-age", "$age")
}

try {
    $logDir = Split-Path -Parent $LogPath
    if (-not (Test-Path -LiteralPath $logDir)) {
        New-Item -ItemType Directory -Path $logDir | Out-Null
    }

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
    if ($hb -and $null -ne $hb.pid) {
        $candidate = Get-PidProcess ([int]$hb.pid)
        if (Test-ScannerProcess $candidate) {
            $proc = $candidate
        }
    }

    $ranSeconds = $null
    if ($task.State -eq "Running") {
        try {
            $info = Get-ScheduledTaskInfo -TaskName $TaskName
            $last = $info.LastRunTime
            if ($last -and $last.Year -ge 2000) {
                $span = (Get-Date) - $last
                if ($span.TotalSeconds -ge 0) {
                    $ranSeconds = [math]::Round($span.TotalSeconds, 0)
                }
            }
        } catch {
            $ranSeconds = $null
        }
    }

    $aliveFlag = "0"
    if ($null -ne $proc) {
        $aliveFlag = "1"
    }
    $runningFlag = "0"
    if ($task.State -eq "Running") {
        $runningFlag = "1"
    }
    $decideArgs = @(
        "decide",
        "--heartbeat", $HeartbeatPath,
        "--process-alive", $aliveFlag,
        "--task-exists", "1",
        "--task-disabled", "0",
        "--task-running", $runningFlag,
        "--stale-minutes", "$StaleMinutes"
    )
    if ($null -ne $ranSeconds) {
        $decideArgs += @("--task-ran-seconds", "$ranSeconds")
    }

    $decision = $null
    $pythonResult = Invoke-HealthPy -HealthArgs $decideArgs
    if ($pythonResult.Code -eq 0 -and $pythonResult.Text) {
        try {
            $decision = ConvertTo-DecisionObject $pythonResult.Text
        } catch {
            $decision = $null
        }
    }
    if (-not $decision) {
        Write-WatchdogLog "health.py ni vrnil odločitve. Lokalno pravilo."
        $taskRunning = $task.State -eq "Running"
        $decision = Get-InlineDecision -Heartbeat $hb -ProcessAlive ($null -ne $proc) -TaskRunning $taskRunning -RanSeconds $ranSeconds -StaleSeconds ($StaleMinutes * 60)
    }

    if ($decision.action -eq "ok") {
        Invoke-HealthPy -HealthArgs @("note-healthy", "--state", $StatePath) | Out-Null
        $detail = "task running"
        if ($proc) {
            $detail = "pid $($proc.Id)"
        }
        Write-WatchdogHeartbeat "ok" $detail
        exit 0
    }

    if ($decision.action -eq "starting") {
        Write-WatchdogHeartbeat "starting" ""
        exit 0
    }

    if ($decision.action -ne "restart") {
        Write-WatchdogHeartbeat $decision.action ""
        exit 0
    }

    $reason = (($decision.reason -replace "[\r\n]", " ") -replace '"', "")
    if (-not $reason) {
        $reason = "neznano"
    }
    if ($reason.Length -gt 300) {
        $reason = $reason.Substring(0, 300)
    }

    $restartOk = $false
    try {
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
        $restartOk = $true
        Write-WatchdogLog "Skener zagnan znova ($reason)."
        Write-WatchdogHeartbeat "restarted" $reason
    } catch {
        $restartOk = $false
        Write-WatchdogLog "Zagon ni uspel ($reason)."
        try {
            Write-WatchdogHeartbeat "restart-failed" $reason
        } catch {
        }
    }

    try {
        $okFlag = "0"
        if ($restartOk) {
            $okFlag = "1"
        }
        $problemFlag = "0"
        if ($decision.heartbeat_problem) {
            $problemFlag = "1"
        }
        $notifyArgs = @(
            "notify",
            "--restart-ok", $okFlag,
            "--heartbeat-problem", $problemFlag,
            "--reason=$reason",
            "--state", $StatePath,
            "--task", $TaskName
        ) + (Format-AgeArg $decision)
        $notified = Invoke-HealthPy -HealthArgs $notifyArgs
        if ($notified.Code -eq 0 -and $notified.Text) {
            $word = ($notified.Text -split "[\r\n]")[0].Trim()
            Write-WatchdogLog "Obvestilo: $word ($reason)."
        } else {
            Write-WatchdogLog "Obvestilo ni odšlo ($reason)."
        }
    } catch {
        Write-WatchdogLog "Obvestilo ni odšlo ($reason)."
    }
    exit 0
} catch {
    Write-WatchdogLog "Nadzor ni uspel: $($_.Exception.Message)"
    try {
        Write-WatchdogHeartbeat "error" $_.Exception.Message
    } catch {
    }
    exit 1
}
