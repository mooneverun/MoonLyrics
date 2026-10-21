param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$moonOriginalPath = $env:PATH
Push-Location $PSScriptRoot
try {
    $moonPython = (Get-Command $Python -ErrorAction Stop).Source
    $env:PATH = @((Split-Path $moonPython), (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot) -join ';'
    $moonVersion = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'VERSION') -Raw).Trim()
    $moonVersionMatch = [regex]::Match($moonVersion, '^(\d+)\.(\d+)\.(\d+)(?:-test\.(\d+))?$')
    if (-not $moonVersionMatch.Success) { throw 'Invalid MoonLyrics version.' }
    $moonVersionParts = @(1..4 | ForEach-Object { [int]$moonVersionMatch.Groups[$_].Value })
    $moonVersionTuple = $moonVersionParts -join ', '
    $moonFileVersion = $moonVersionParts -join '.'
    $moonVersionFlags = if ($moonVersionMatch.Groups[4].Success) { 2 } else { 0 }
    New-Item -ItemType Directory -Path (Join-Path $PSScriptRoot 'build') -Force | Out-Null
    $moonVersionFile = Join-Path $PSScriptRoot 'build\version_info.txt'
    @"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($moonVersionTuple), prodvers=($moonVersionTuple), mask=0x3f,
                   flags=$moonVersionFlags, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('FileDescription', 'MoonLyrics desktop lyrics'),
    StringStruct('FileVersion', '$moonFileVersion'),
    StringStruct('InternalName', 'MoonLyrics'),
    StringStruct('OriginalFilename', 'MoonLyrics.exe'),
    StringStruct('ProductName', 'MoonLyrics'),
    StringStruct('ProductVersion', '$moonVersion')
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"@ | Set-Content -LiteralPath $moonVersionFile -Encoding utf8
    & $moonPython -m PyInstaller --noconfirm --clean --onefile --windowed --noupx `
        --version-file $moonVersionFile `
        --icon (Join-Path $PSScriptRoot 'assets\moonlyrics-icon.ico') `
        --add-data "$(Join-Path $PSScriptRoot 'assets\moonlyrics-icon.ico');assets" `
        --add-data "$(Join-Path $PSScriptRoot 'assets\moonlyrics-icon.png');assets" `
        --add-data "$(Join-Path $PSScriptRoot 'assets\settings-chevron.svg');assets" `
        --add-data "$(Join-Path $PSScriptRoot 'assets\settings-check.svg');assets" `
        --add-data "$(Join-Path $PSScriptRoot 'assets\NotoSansSC-Black.otf');assets" `
        --add-data "$(Join-Path $PSScriptRoot 'assets\NotoSansSC-OFL.txt');assets" `
        --add-data "$(Join-Path $PSScriptRoot 'VERSION');." `
        --name MoonLyrics --collect-data opencc --distpath (Join-Path $PSScriptRoot 'dist') `
        --workpath (Join-Path $PSScriptRoot 'build') --specpath (Join-Path $PSScriptRoot 'build') main.py
    if ($LASTEXITCODE -ne 0) { throw 'MoonLyrics build failed.' }
} finally {
    $env:PATH = $moonOriginalPath
    Pop-Location
}
