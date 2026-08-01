param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot '..\.cortex\digests')
)

$ErrorActionPreference = 'Stop'
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $resolvedOutput -Force | Out-Null
$outputFile = Join-Path $resolvedOutput ((Get-Date -Format 'yyyy-MM-dd') + '.md')

& (Join-Path $PSScriptRoot 'cortex-portfolio.ps1') digest --output $outputFile
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Output "Daily digest ready: $outputFile"
