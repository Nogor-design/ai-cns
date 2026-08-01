param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^([01]\d|2[0-3]):[0-5]\d$')]
    [string]$At,

    [string]$TaskName = 'Cortex Daily Digest',

    [switch]$Install
)

$ErrorActionPreference = 'Stop'
$digestScript = [System.IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot 'write-daily-digest.ps1')
)
$powershell = (Get-Command powershell.exe).Source
$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$digestScript`""

Write-Output "Task name : $TaskName"
Write-Output "Schedule  : daily at $At"
Write-Output "Executable: $powershell"
Write-Output "Arguments : $arguments"

if (-not $Install) {
    Write-Output 'Dry run only. Re-run with -Install to register the scheduled task.'
    exit 0
}

$action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description 'Writes the local Cortex portfolio digest. No AI worker is launched.' `
    -Force | Out-Null

Write-Output "Installed scheduled task: $TaskName"
