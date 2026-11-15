"""Embed the verified one-folder bundle in a Windows single-file launcher."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import zipfile


def build(folder, output, work, version, icon):
    folder, output, work, icon = (Path(p).resolve() for p in (folder, output, work, icon))
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:-test\.(\d+))?', version)
    if not match:
        raise ValueError('Invalid application version')
    if not (folder / 'MoonLyrics.exe').is_file() or not (folder / '_internal' / 'VERSION').is_file():
        raise ValueError('Build the one-folder application first')
    compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    if not compiler.is_file():
        raise RuntimeError('The Windows .NET Framework C# compiler is unavailable')
    work.mkdir(parents=True, exist_ok=True)
    payload = work / 'application.zip'
    with zipfile.ZipFile(payload, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(folder.rglob('*')):
            if path.is_file() and 'MoonLyrics-data' not in path.relative_to(folder).parts:
                archive.write(path, path.relative_to(folder).as_posix())
    numeric = '.'.join(str(int(value or 0)) for value in match.groups())
    metadata = work / 'version.cs'
    metadata.write_text(
        'using System.Reflection;\n'
        '[assembly: AssemblyTitle("MoonLyrics")]\n'
        '[assembly: AssemblyProduct("MoonLyrics")]\n'
        '[assembly: AssemblyDescription("MoonLyrics desktop lyrics")]\n'
        f'[assembly: AssemblyFileVersion("{numeric}")]\n'
        f'[assembly: AssemblyVersion("{numeric}")]\n'
        f'[assembly: AssemblyInformationalVersion("{version}")]\n', encoding='utf-8')
    source = Path(__file__).resolve().parent
    environment = dict(os.environ, TEMP=str(work), TMP=str(work))
    subprocess.run([
        str(compiler), '/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/codepage:65001',
        '/reference:System.IO.Compression.dll', '/reference:System.Windows.Forms.dll',
        '/win32icon:' + str(icon), '/win32manifest:' + str(source / 'launcher.manifest'),
        '/resource:' + str(payload) + ',MoonLyrics.Payload', '/out:' + str(output),
        str(source / 'windows_launcher.cs'), str(metadata),
    ], check=True, env=environment)
    print(f'Single-file MoonLyrics {version}: {output}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('folder', 'output', 'work', 'version', 'icon'):
        parser.add_argument('--' + name, required=True)
    build(**vars(parser.parse_args()))
