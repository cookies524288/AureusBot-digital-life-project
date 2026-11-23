# Compile the project-local Windows GUI launcher.
param([string]$OutputPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'Aureus.exe'))

$ErrorActionPreference = 'Stop'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$source = Join-Path $PSScriptRoot 'WindowsLauncher.cs'
$window = Join-Path $PSScriptRoot 'LauncherWindow.cs'
$icon = Join-Path (Split-Path -Parent $PSScriptRoot) 'assets\logo\aureus-rounded.ico'

& $compiler /nologo /target:winexe /codepage:65001 /reference:System.Windows.Forms.dll /reference:System.Drawing.dll "/resource:$icon,Aureus.Tray.ico" "/win32icon:$icon" "/out:$OutputPath" $source $window
if ($LASTEXITCODE -ne 0) {
    throw "Launcher compilation failed (exit $LASTEXITCODE)."
}
