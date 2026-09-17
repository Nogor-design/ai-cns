param(
    [string]$TaskName = 'Cortex Autopilot',

    [switch]$Install,

    [switch]$Uninstall
)

# Registers the unattended Cortex scheduler for the current user. It starts at
# logon and is re-tried every 10 minutes; the scheduler's database lease makes a
# second copy exit immediately, so the retry only matters after a crash.
# Dry run by default: nothing is registered without -Install.

$ErrorActionPreference = 'Stop'

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed scheduled task: $TaskName"
    exit 0
}

$wrapper = [System.IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot 'cortex-portfolio.ps1')
)
$logDir = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\.cortex\logs'))
$powershell = (Get-Command powershell.exe).Source
# Out-File, not *>>: PowerShell redirection writes UTF-16, which leaves the log
# full of null bytes and unreadable in most tools.
$command = "& '$wrapper' autopilot run *>&1 | Out-File -FilePath '$logDir\autopilot.log' -Append -Encoding utf8"
$arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$command`""

Write-Output "Task name : $TaskName"
Write-Output 'Triggers  : at logon, then every 10 minutes (a running copy keeps the lease)'
Write-Output "Executable: $powershell"
Write-Output "Arguments : $arguments"
Write-Output "Log       : $logDir\autopilot.log"
Write-Output 'Pause any time from the dashboard or with: cortex capacity pause'

if (-not $Install) {
    Write-Output 'Dry run only. Re-run with -Install to register the scheduled task.'
    exit 0
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 10)
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger @($logon, $repeat) `
    -Settings $settings `
    -Principal $principal `
    -Description 'Cortex unattended scheduler: read-only work within quota reserve; never pushes or touches main.' `
    -Force | Out-Null

Write-Output "Installed scheduled task: $TaskName"
