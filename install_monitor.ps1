param(
    [ValidateSet('Install', 'Uninstall', 'Start', 'Stop', 'Status', 'Run', 'WatchdogCheck')]
    [string]$Action = 'Install',

    [string]$TaskName = 'PDFScannerMonitor',

    [ValidateRange(1, 3600)]
    [int]$IntervalSeconds = 15,

    [ValidateRange(60, 86400)]
    [int]$HeartbeatTimeoutSeconds = 300,

    [string]$PythonPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProjectRoot = $PSScriptRoot
$ScriptPath = $MyInvocation.MyCommand.Path
$MainScript = Join-Path $ProjectRoot 'main.py'
$HeartbeatFile = Join-Path $ProjectRoot 'logs\.monitor_heartbeat.json'
$WatchdogTaskName = "$TaskName-Watchdog"

function Resolve-PythonExe {
    param([string]$PreferredPythonPath)

    if ($PreferredPythonPath) {
        if (Test-Path $PreferredPythonPath) {
            return (Resolve-Path $PreferredPythonPath).Path
        }
        throw "PythonPath informado nao existe: $PreferredPythonPath"
    }

    $candidates = @()
    $venvCandidate = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (Test-Path $venvCandidate) {
        $candidates += $venvCandidate
    }

    if ($env:VIRTUAL_ENV) {
        $activeVenvPython = Join-Path $env:VIRTUAL_ENV 'Scripts\python.exe'
        if (Test-Path $activeVenvPython) {
            $candidates += $activeVenvPython
        }
    }

    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }

    $pyLauncher = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        return $pyLauncher.Source
    }

    $pythonCommand = Get-Command 'python' -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        return $pythonCommand.Source
    }

    throw "Python nao encontrado. Informe -PythonPath ou instale Python/.venv e adicione ao PATH."
}

function Register-MonitorTasks {
    param([string]$ResolvedPythonExe)

    $pythonArguments = '"{0}" --watch --watch-interval {1}' -f $MainScript, $IntervalSeconds
    if ([System.IO.Path]::GetFileName($ResolvedPythonExe).Equals('py.exe', [System.StringComparison]::OrdinalIgnoreCase)) {
        $pythonArguments = '-3 ' + $pythonArguments
    }

    $monitorAction = New-ScheduledTaskAction -Execute $ResolvedPythonExe -Argument $pythonArguments
    $monitorTrigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Days 3650)

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $monitorAction `
        -Trigger $monitorTrigger `
        -Principal $principal `
        -Settings $settings `
        -Description "Mantem o PDF Scanner em execucao continua para monitorar a pasta 24/7. Python: $ResolvedPythonExe" | Out-Null

    $watchdogCommand = "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" -Action WatchdogCheck -TaskName `"$TaskName`" -HeartbeatTimeoutSeconds $HeartbeatTimeoutSeconds"
    $watchdogAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $watchdogCommand
    $watchdogTrigger = New-ScheduledTaskTrigger `
        -Once `
        -At ((Get-Date).AddMinutes(1)) `
        -RepetitionInterval (New-TimeSpan -Minutes 1) `
        -RepetitionDuration (New-TimeSpan -Days 3650)

    Register-ScheduledTask `
        -TaskName $WatchdogTaskName `
        -Action $watchdogAction `
        -Trigger $watchdogTrigger `
        -Principal $principal `
        -Settings $settings `
        -Description "Watchdog do PDF Scanner: reinicia monitor quando heartbeat fica vencido." | Out-Null
}

function Remove-TaskIfExists {
    param([string]$Name)

    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    }
}

function Read-HeartbeatAgeSeconds {
    if (-not (Test-Path $HeartbeatFile)) {
        return [double]::PositiveInfinity
    }

    try {
        $content = Get-Content -Path $HeartbeatFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($null -eq $content.updated_at_epoch) {
            return [double]::PositiveInfinity
        }
        return [math]::Max(0, ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$content.updated_at_epoch))
    }
    catch {
        return [double]::PositiveInfinity
    }
}

function Get-WatchdogConfiguredTimeoutSeconds {
    param([object]$WatchdogTask)

    if ($null -eq $WatchdogTask) {
        return $HeartbeatTimeoutSeconds
    }

    try {
        $actions = @($WatchdogTask.Actions)
        foreach ($action in $actions) {
            $arguments = [string]$action.Arguments
            if ($arguments -match '-HeartbeatTimeoutSeconds\s+(\d+)') {
                return [int]$matches[1]
            }
        }
    }
    catch {
        # Fallback para parametro atual quando nao for possivel inferir do task action.
    }

    return $HeartbeatTimeoutSeconds
}

if (-not (Test-Path $MainScript)) {
    throw "main.py nao encontrado em $ProjectRoot"
}

switch ($Action) {
    'Install' {
        $pythonExe = Resolve-PythonExe -PreferredPythonPath $PythonPath
        Remove-TaskIfExists -Name $TaskName
        Remove-TaskIfExists -Name $WatchdogTaskName
        Register-MonitorTasks -ResolvedPythonExe $pythonExe
        Start-ScheduledTask -TaskName $TaskName
        Start-ScheduledTask -TaskName $WatchdogTaskName
        Write-Host "Tarefa instalada e iniciada: $TaskName"
        Write-Host "Watchdog instalado e iniciado: $WatchdogTaskName (timeout heartbeat: ${HeartbeatTimeoutSeconds}s)"
        Write-Host "Python configurado: $pythonExe"
    }

    'Uninstall' {
        Remove-TaskIfExists -Name $TaskName
        Remove-TaskIfExists -Name $WatchdogTaskName
        Write-Host "Tarefas removidas: $TaskName e $WatchdogTaskName"
    }

    'Start' {
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Start-ScheduledTask -TaskName $TaskName
            Write-Host "Tarefa iniciada: $TaskName"
        }
        if (Get-ScheduledTask -TaskName $WatchdogTaskName -ErrorAction SilentlyContinue) {
            Start-ScheduledTask -TaskName $WatchdogTaskName
            Write-Host "Watchdog iniciado: $WatchdogTaskName"
        }
    }

    'Stop' {
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $TaskName
            Write-Host "Tarefa parada: $TaskName"
        }
        if (Get-ScheduledTask -TaskName $WatchdogTaskName -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $WatchdogTaskName
            Write-Host "Watchdog parado: $WatchdogTaskName"
        }
    }

    'Status' {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        $watchdog = Get-ScheduledTask -TaskName $WatchdogTaskName -ErrorAction SilentlyContinue
        $configuredHeartbeatTimeoutSeconds = Get-WatchdogConfiguredTimeoutSeconds -WatchdogTask $watchdog

        if ($null -eq $task -and $null -eq $watchdog) {
            Write-Host "Nenhuma tarefa encontrada: $TaskName / $WatchdogTaskName"
            exit 1
        }

        if ($task) {
            $info = Get-ScheduledTaskInfo -TaskName $TaskName
            [pscustomobject]@{
                TaskName    = $TaskName
                State       = $task.State
                LastRunTime = $info.LastRunTime
                NextRunTime = $info.NextRunTime
                LastResult  = $info.LastTaskResult
            } | Format-List
        }

        if ($watchdog) {
            $watchdogInfo = Get-ScheduledTaskInfo -TaskName $WatchdogTaskName
            [pscustomobject]@{
                TaskName    = $WatchdogTaskName
                State       = $watchdog.State
                LastRunTime = $watchdogInfo.LastRunTime
                NextRunTime = $watchdogInfo.NextRunTime
                LastResult  = $watchdogInfo.LastTaskResult
            } | Format-List
        }

        $ageSeconds = Read-HeartbeatAgeSeconds
        if ([double]::IsPositiveInfinity($ageSeconds)) {
            Write-Host "Heartbeat: ausente ou invalido ($HeartbeatFile)"
        }
        else {
            Write-Host "Heartbeat age: $([int]$ageSeconds)s (timeout configurado: ${configuredHeartbeatTimeoutSeconds}s)"
        }
    }

    'Run' {
        $pythonExe = Resolve-PythonExe -PreferredPythonPath $PythonPath
        if ([System.IO.Path]::GetFileName($pythonExe).Equals('py.exe', [System.StringComparison]::OrdinalIgnoreCase)) {
            & $pythonExe -3 $MainScript --watch --watch-interval $IntervalSeconds
        }
        else {
            & $pythonExe $MainScript --watch --watch-interval $IntervalSeconds
        }
    }

    'WatchdogCheck' {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            Write-Host "Tarefa alvo nao encontrada para watchdog: $TaskName"
            exit 1
        }

        $ageSeconds = Read-HeartbeatAgeSeconds
        $isStale = [double]::IsPositiveInfinity($ageSeconds) -or ($ageSeconds -gt $HeartbeatTimeoutSeconds)
        $isStopped = $task.State -ne 'Running'

        if ($isStale -or $isStopped) {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            Start-ScheduledTask -TaskName $TaskName
            if ([double]::IsPositiveInfinity($ageSeconds)) {
                Write-Host "Watchdog: monitor reiniciado por heartbeat ausente/invalido."
            }
            else {
                Write-Host "Watchdog: monitor reiniciado por heartbeat vencido (${ageSeconds}s > ${HeartbeatTimeoutSeconds}s) ou tarefa parada."
            }
        }
    }
}
