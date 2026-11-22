# Compile the project-local Windows GUI launcher.
$ErrorActionPreference = 'Stop'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$source = Join-Path $PSScriptRoot 'WindowsLauncher.cs'
$output = Join-Path (Split-Path -Parent $PSScriptRoot) 'Aureus.exe'

& $compiler /nologo /target:winexe /codepage:65001 /reference:System.Windows.Forms.dll "/out:$output" $source
if ($LASTEXITCODE -ne 0) {
    throw "Launcher compilation failed (exit $LASTEXITCODE)."
}
