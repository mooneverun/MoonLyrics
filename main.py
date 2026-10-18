"""Launch MoonLyrics with the available native platform backend."""
import sys

if __name__ == "__main__":
    if sys.platform != "win32":
        raise SystemExit("MoonLyrics 目前支持 Windows；macOS 原生版本尚在规划中。")
    from moonlyrics.windows import main
    raise SystemExit(main())
