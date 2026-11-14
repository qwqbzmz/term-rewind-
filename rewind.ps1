$python = Join-Path $PSScriptRoot 'runtime\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
}
if (-not $python) {
    Write-Error 'Python 3.10+ is required. Install Python and add it to PATH.'
    exit 1
}
& $python (Join-Path $PSScriptRoot 'rewind.py') @args
exit $LASTEXITCODE
