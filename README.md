# MoonLyrics

English · [简体中文](README.zh-CN.md)

Floating desktop lyrics for Apple Music on Windows, with smooth scrolling and liquid glass.

**v0.1.3** · **[Download](https://github.com/mooneverun/MoonLyrics/releases/latest)** · [Release notes](https://github.com/mooneverun/MoonLyrics/releases) · [Report an issue](https://github.com/mooneverun/MoonLyrics/issues)

## Features

- Follows the current song and playback position in Apple Music.
- Choose and order three active lyric sources from Kugou, LRCLIB, NetEase Cloud Music and QQ Music. Unselected sources remain in reserve. No extra login or API key.
- Handles regional track names and Chinese, English and Korean artist aliases; displays Chinese lyrics in Simplified Chinese.
- Smooth scrolling, faded context lines and horizontal scrolling for long lyrics. Includes font selection, bundled Noto Sans SC Black and timing adjustment.
- Previous, play/pause and next controls appear on hover, sharing the lyric bar's glass surface.
- Saves matched lyrics locally for repeat playback, with a cache-clearing option.
- Adjustable lyric bar size, with Day, Night or Follow system appearance shared by the lyrics and Settings. Follow system uses the Windows app theme; Settings stop sampling the desktop while hidden.
- Drag the lyric bar, use six position shortcuts, and right-click it to open Settings.
- English, 简体中文, 繁體中文, 日本語 and 한국어 interfaces; up to 120 FPS, with adaptive idle refresh and reuse of unchanged frames.

## Getting started

1. Download the Windows `.exe` from [Releases](https://github.com/mooneverun/MoonLyrics/releases/latest) and run it. Python is not required.
2. Open Apple Music for Windows and play a song. Lyrics are matched automatically.
3. Choose appearance, source priority or timing in Settings. Language, cache controls and runtime details are under **Status** at the bottom. Drag the lyric bar to move it and hover to use playback controls. Minimizing hides Settings from the taskbar; no tray icon is created. Right-click the lyric bar to open Settings again.

Requires 64-bit Windows 10 (2004+) or Windows 11 and OpenGL 3.3 support. Tested on Windows 11; Windows 10 has not been tested on a physical device. A macOS version is not available yet. The FPS setting is an upper limit, not a guaranteed frame rate.

The single-file EXE automatically chooses a writable extraction folder. If AppData or the system temporary folder is unavailable, it uses `MoonLyrics-data\runtime\` beside the EXE and removes the unpacked dependencies after exit. Only the EXE needs to be copied to another computer.

## Notes

- Initial lyric searches require an internet connection; cached matches can be read locally. Coverage and timing depend on third-party sources. MoonLyrics does not use Apple Music's subscription lyrics.
- Desktop images are processed locally and are never saved or uploaded. Lyric searches send song metadata to catalog and lyric services.
- Settings and logs: `%LOCALAPPDATA%\MoonLyrics\`. Lyrics: its `cache\lyrics\` subfolder, with automatic expiry and cleanup.
- If that data folder is not writable, a `MoonLyrics-data` folder beside the EXE stores the portable profile. Later launches continue using that profile.

<details>
<summary>Run from source or build an EXE</summary>

With Python 3.12, run these commands in the project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe main.py
.\build.ps1 -Python .\.venv\Scripts\python.exe
```

Output: `dist\MoonLyrics.exe`. The application version is recorded in [VERSION](VERSION).
For a portable folder that requires no temporary extraction at startup, add `-Portable` to the build command. Output: `dist\MoonLyrics\`.

</details>

## License

[GNU GPL v3.0](LICENSE). The bundled font has its own [SIL Open Font License](assets/NotoSansSC-OFL.txt).

Settings icons: [Lucide / ISC](assets/Lucide-LICENSE.txt).

The liquid glass renderer adapts [Glasswork](https://github.com/kubai087/glasswork) (`4f0b6744`, [MIT](assets/Glasswork-LICENSE.txt)), with neutral transmission and the continuous end profile from [Atrium](https://github.com/wynter-yaxley/atrium-desktop) (`2e51d036`, [MIT](assets/Atrium-LICENSE.txt)).
