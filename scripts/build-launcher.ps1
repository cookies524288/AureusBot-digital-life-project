# Compile the project-local Windows GUI launcher.
$ErrorActionPreference = 'Stop'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$source = Join-Path $PSScriptRoot 'WindowsLauncher.cs'
$output = Join-Path (Split-Path -Parent $PSScriptRoot) 'Aureus.exe'
$icon = Join-Path (Split-Path -Parent $PSScriptRoot) 'assets\logo\aureus.ico'

$trayIcon = Join-Path (Split-Path -Parent $PSScriptRoot) 'assets\logo\tray-no-ring-pixel.ico'
& $compiler /nologo /target:winexe /codepage:65001 /reference:System.Windows.Forms.dll /reference:System.Drawing.dll "/resource:$trayIcon,Aureus.Tray.ico" "/win32icon:$icon" "/out:$output" $source
if ($LASTEXITCODE -ne 0) {
    throw "Launcher compilation failed (exit $LASTEXITCODE)."
}
