param(
    [int]$Port = 8765,
    [switch]$NoOpen,
    [switch]$Rebuild
)

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$dashboardRoot = Join-Path $repoRoot 'dashboard'
$distIndex = Join-Path $dashboardRoot 'dist\index.html'

if ($Rebuild -or -not (Test-Path -LiteralPath $distIndex)) {
    if (-not (Test-Path -LiteralPath (Join-Path $dashboardRoot 'node_modules'))) {
        & npm --prefix $dashboardRoot install
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    & npm --prefix $dashboardRoot run build
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$args = @('serve', '--port', $Port)
if ($NoOpen) { $args += '--no-open' }
& (Join-Path $PSScriptRoot 'cortex-portfolio.ps1') @args
exit $LASTEXITCODE
