param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CortexArgs
)

$env:CORTEX_DB = [System.IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot '..\.cortex\portfolio.db')
)
if (Test-Path -LiteralPath 'E:\') {
    $env:CORTEX_WORK_ROOT = 'E:\AI-Worktrees'
}

& cortex @CortexArgs
exit $LASTEXITCODE
