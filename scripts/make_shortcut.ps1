# Creates the Desktop shortcut. Uses pythonw.exe so no console window appears.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    $pythonw = Join-Path $root ".venv\Scripts\python.exe"
}
if (-not (Test-Path $pythonw)) {
    throw "No interpreter found in .venv. Run: py -m venv .venv"
}

$icon = Join-Path $root "assets\murmur.ico"
$lnk  = Join-Path $env:USERPROFILE "Desktop\Murmur.lnk"

$ws = New-Object -ComObject WScript.Shell
$s  = $ws.CreateShortcut($lnk)
$s.TargetPath       = $pythonw
$s.Arguments        = "-m murmur"
$s.WorkingDirectory = $root
$s.Description      = "Murmur - hold Ctrl+Win to dictate, Ctrl+Win+Space hands-free"
if (Test-Path $icon) { $s.IconLocation = $icon }
$s.Save()

Write-Host "Created $lnk"
Write-Host "  target: $pythonw -m murmur"
