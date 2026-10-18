param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$moonOriginalPath = $env:PATH
Push-Location $PSScriptRoot
try {
    $moonPython = (Get-Command $Python -ErrorAction Stop).Source
    $env:PATH = @((Split-Path $moonPython), (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot) -join ';'
    & $moonPython -m PyInstaller --noconfirm --clean --onefile --windowed --noupx `
        --name MoonLyrics --distpath (Join-Path $PSScriptRoot 'dist') `
        --workpath (Join-Path $PSScriptRoot 'build') --specpath (Join-Path $PSScriptRoot 'build') main.py
    if ($LASTEXITCODE -ne 0) { throw 'MoonLyrics build failed.' }
} finally {
    $env:PATH = $moonOriginalPath
    Pop-Location
}
