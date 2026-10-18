"""Platform-independent glass presets and settings."""
import math

PRESETS = {
    "clear": dict(label="清透", description="轻柔透镜感，保留更多背景细节。", blur=7, refraction=26),
    "soft": dict(label="柔光", description="均匀的轻磨砂，保留柔和的明暗层次。", blur=20, refraction=4),
    "rich": dict(label="凝润", description="细腻的浓磨砂，让繁杂背景更安静。", blur=38, refraction=2),
}
DEFAULTS = dict(preset="soft", blur=PRESETS["soft"]["blur"], refraction=PRESETS["soft"]["refraction"],
                transparency_level=1, width=583, height=80, fps=30, storefront="tr")
LIMITS = dict(transparency_level=(1, 10), width=(320, 960), height=(80, 240))


def normalized(values):
    result = dict(DEFAULTS)
    if not isinstance(values, dict):
        return result
    for key, (lo, hi) in LIMITS.items():
        value = values.get(key, result[key])
        if isinstance(value, (int, float)) and math.isfinite(value):
            result[key] = max(lo, min(hi, int(value)))
    if "transparency_level" not in values:
        previous = values.get("transparency")
        if isinstance(previous, (int, float)) and math.isfinite(previous):
            result["transparency_level"] = round(1 + (max(90, min(100, previous)) - 90) * .9)
    if values.get("fps") in (15, 30, 60):
        result["fps"] = int(values["fps"])
    if values.get("storefront") in ("tr", "cn", "hk", "tw", "us"):
        result["storefront"] = values["storefront"]
    preset = values.get("preset")
    if isinstance(preset, str) and preset in PRESETS:
        result["preset"] = preset
    for key in ("blur", "refraction"):
        result[key] = PRESETS[result["preset"]][key]
    return result


def transparency_amount(level):
    """Keep the former 90–100% endpoints while presenting a 1–10 scale."""
    return .9 + (level - 1) / 90


