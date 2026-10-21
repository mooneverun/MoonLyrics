# MoonLyrics

English · [简体中文](README.zh-CN.md)

Floating desktop lyrics for Apple Music on Windows, with a frosted glass background and smooth scrolling.

**[Download](https://github.com/mooneverun/MoonLyrics/releases/latest)** · [Release notes](https://github.com/mooneverun/MoonLyrics/releases/tag/v0.1.2) · [Report an issue](https://github.com/mooneverun/MoonLyrics/issues)

## Features

- Follows the current song and playback position in Apple Music.
- Matches lyrics from Kugou, LRCLIB, NetEase Cloud Music and QQ Music, with adjustable source priority and no extra login or API key.
- Handles regional track names and Chinese, English and Korean artist aliases; displays Chinese lyrics in Simplified Chinese.
- Smooth scrolling, faded context lines and horizontal scrolling for long lyrics. Includes font selection, bundled Noto Sans SC Black and timing adjustment.
- Previous, play/pause and next controls appear on hover when unlocked, sharing the lyric bar's glass surface.
- Saves matched lyrics locally for repeat playback, with a cache-clearing option.
- Adjustable size and shared transparency for the lyric bar and settings panel.
- Drag, lock, click-through, six position shortcuts and a system tray icon.
- English, 简体中文, 繁體中文, 日本語 and 한국어 interfaces; refresh limit up to 120 FPS.

## Getting started

1. Download the Windows `.exe` from [Releases](https://github.com/mooneverun/MoonLyrics/releases/latest) and run it. Python is not required.
2. Open Apple Music for Windows and play a song. Lyrics are matched automatically.
3. Adjust transparency, source priority or timing in Settings. Unlock the lyric bar to drag it or use playback controls. Click “−” to minimize to the tray; click the moon icon to restore Settings.

Requires 64-bit Windows 10 (2004+) or Windows 11 and OpenGL 3.3 support. Tested on Windows 11; Windows 10 has not been tested on a physical device. A macOS version is not available yet. The FPS setting is an upper limit, not a guaranteed frame rate.

## Notes

- Initial lyric searches require an internet connection; cached matches can be read locally. Coverage and timing depend on third-party sources. MoonLyrics does not use Apple Music's subscription lyrics.
- Desktop images are processed locally and are never saved or uploaded. Lyric searches send song metadata to catalog and lyric services.
- Settings and logs: `%LOCALAPPDATA%\MoonLyrics\`. Lyrics: its `cache\lyrics\` subfolder, with automatic expiry and cleanup.

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

</details>

## License

[GNU GPL v3.0](LICENSE). The bundled font has its own [SIL Open Font License](assets/NotoSansSC-OFL.txt).
