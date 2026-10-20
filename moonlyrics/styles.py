"""Platform-independent glass material and settings."""
import math
from .i18n import LANGUAGES

PRESETS = {
    "rich": dict(label="凝润", description="细腻的浓磨砂，让繁杂背景更安静。", blur=38, refraction=2),
}
LYRIC_SOURCES = {"kugou": "酷狗", "lrclib": "LRCLIB", "netease": "网易云", "qq": "QQ音乐"}
FPS_OPTIONS = (15, 30, 60, 120)
DEFAULTS = dict(preset="rich", blur=PRESETS["rich"]["blur"], refraction=PRESETS["rich"]["refraction"],
                transparency_level=1, width=580, height=80, fps=30, storefront="tr", language="zh_CN", lyric_offset_ms=1000, lyric_timing_version=1,
                lyric_source_order=tuple(LYRIC_SOURCES))
LIMITS = dict(transparency_level=(1, 10), width=(320, 960), height=(80, 240), lyric_offset_ms=(-3000, 3000))


def normalized_source_order(value):
    if (isinstance(value, (list, tuple)) and len(value) == len(LYRIC_SOURCES)
            and all(isinstance(item, str) for item in value) and set(value) == set(LYRIC_SOURCES)):
        return tuple(value)
    return tuple(LYRIC_SOURCES)


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
    if "transparency_level" not in values:
        previous = values.get("transparency")
        if isinstance(previous, (int, float)) and math.isfinite(previous):
            result["transparency_level"] = round(1 + (max(90, min(100, previous)) - 90) * .9)
    result["width"] = (result["width"] + 5) // 10 * 10
    if values.get("fps") in FPS_OPTIONS:
        result["fps"] = int(values["fps"])
    if isinstance(values.get("language"), str) and values["language"] in LANGUAGES:
        result["language"] = values["language"]
    if values.get("storefront") in ("tr", "cn", "hk", "tw", "us"):
        result["storefront"] = values["storefront"]
    result["lyric_source_order"] = normalized_source_order(values.get("lyric_source_order"))
    # Old selectable styles migrate to the single approved rich material.
    for key in ("blur", "refraction"):
        result[key] = PRESETS[result["preset"]][key]
    return result


def transparency_amount(level):
    """Keep the former 90–100% endpoints while presenting a 1–10 scale."""
    return .9 + (level - 1) / 90


def lyric_offset_seconds(settings):
    """Every provider uses the same calibrated playback offset."""
    return settings["lyric_offset_ms"] / 1000


