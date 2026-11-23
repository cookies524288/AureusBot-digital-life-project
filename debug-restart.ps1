$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Write-Error '缺少项目虚拟环境，请先在项目根目录运行 uv sync。'
    exit 1
}

Push-Location -LiteralPath $PSScriptRoot
try {
    & $python -m aureus.cli.debug_restart
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
