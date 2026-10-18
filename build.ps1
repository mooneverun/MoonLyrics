param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$moonOriginalPath = $env:PATH
Push-Location $PSScriptRoot
try {
    $moonPython = (Get-Command $Python -ErrorAction Stop).Source
    $env:PATH = @((Split-Path $moonPython), (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot) -join ';'
    & $moonPython -m PyInstaller --noconfirm --clean --onefile --windowed --noupx `
        --icon (Join-Path $PSScriptRoot 'assets\moonlyrics-icon.ico') `
        --add-data "$(Join-Path $PSScriptRoot 'assets\moonlyrics-icon.ico');assets" `
        --add-data "$(Join-Path $PSScriptRoot 'assets\moonlyrics-icon.png');assets" `
        --add-data "$(Join-Path $PSScriptRoot 'assets\settings-chevron.svg');assets" `
        --add-data "$(Join-Path $PSScriptRoot 'assets\settings-check.svg');assets" `
        --name MoonLyrics --collect-data opencc --distpath (Join-Path $PSScriptRoot 'dist') `
        --workpath (Join-Path $PSScriptRoot 'build') --specpath (Join-Path $PSScriptRoot 'build') main.py
    if ($LASTEXITCODE -ne 0) { throw 'MoonLyrics build failed.' }
} finally {
    $env:PATH = $moonOriginalPath
    Pop-Location
}
