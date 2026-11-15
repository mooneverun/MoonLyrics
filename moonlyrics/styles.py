"""Platform-independent glass material and settings."""
import math
from .i18n import LANGUAGES

PRESETS = {
    "rich": dict(label="玻璃外观", description="Glasswork 液态玻璃，采用日间或夜间外观。"),
}
APPEARANCE_MODES = {"day": "日间模式", "night": "夜间模式", "system": "跟随系统"}
LYRIC_SOURCES = {"kugou": "酷狗", "lrclib": "LRCLIB", "netease": "网易云", "qq": "QQ音乐"}
FPS_OPTIONS = (15, 30, 60, 120)
DEFAULTS = dict(appearance_mode="system", width=580, height=80, fps=30, storefront="tr", language="zh_CN", lyric_offset_ms=1000, lyric_timing_version=1,
                lyric_source_order=tuple(LYRIC_SOURCES)[:3], lyric_font="Noto Sans SC Black")
LIMITS = dict(width=(320, 960), height=(80, 240), lyric_offset_ms=(-3000, 3000))


def normalized_source_order(value):
    if (isinstance(value, (list, tuple)) and 3 <= len(value) <= len(LYRIC_SOURCES)
            and all(isinstance(item, str) and item in LYRIC_SOURCES for item in value)
            and len(set(value)) == len(value)):
        return tuple(value[:3])
    return tuple(LYRIC_SOURCES)[:3]


def normalized(values):
    result = dict(DEFAULTS)
    if not isinstance(values, dict):
        return result
    for key, (lo, hi) in LIMITS.items():
        value = values.get(key, result[key])
        if isinstance(value, (int, float)) and math.isfinite(value):
            result[key] = max(lo, min(hi, int(value)))
    # Upgrade the old zero default once; preserve manual values and an explicit new zero.
    if values.get("lyric_timing_version") != 1 and values.get("lyric_offset_ms") == 0:
        result["lyric_offset_ms"] = DEFAULTS["lyric_offset_ms"]
    mode = values.get("appearance_mode")
    if isinstance(mode, str) and mode in APPEARANCE_MODES:
        result["appearance_mode"] = mode
    result["width"] = (result["width"] + 5) // 10 * 10
    if values.get("fps") in FPS_OPTIONS:
        result["fps"] = int(values["fps"])
    if isinstance(values.get("language"), str) and values["language"] in LANGUAGES:
        result["language"] = values["language"]
    family = values.get("lyric_font")
    if isinstance(family, str) and 0 < len(family.strip()) <= 128 and family.isprintable():
        result["lyric_font"] = family.strip()
    if values.get("storefront") in ("tr", "cn", "hk", "tw", "us"):
        result["storefront"] = values["storefront"]
    result["lyric_source_order"] = normalized_source_order(values.get("lyric_source_order"))
    return result


def lyric_offset_seconds(settings):
    """Every provider uses the same calibrated playback offset."""
    return settings["lyric_offset_ms"] / 1000


