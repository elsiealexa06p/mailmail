"""Config loader — reads YAML, merges with defaults, validates."""

import os
import yaml
from pathlib import Path
from .colors import lighten_color

_DEFAULTS = {
    "colors": {
        "primary": ["#005eb8", "#0066cc", "#1a73e8", "#2563eb", "#0052a3"],
        "accent": ["#c0392b", "#e74c3c", "#d63031", "#b71c1c"],
        "footer_bg": ["#2c3e50", "#1a1a2e", "#2d2d2d"],
        "footer_text": ["#cccccc", "#b0b0b0", "#999999"],
        "light_accent_bg": "auto",
    },
    "fonts": [
        "Arial, Helvetica, sans-serif",
        "'Segoe UI', Tahoma, Geneva, Verdana, sans-serif",
        "Calibri, 'Trebuchet MS', sans-serif",
        "'Helvetica Neue', Helvetica, Arial, sans-serif",
        "Verdana, Geneva, sans-serif",
    ],
    "placeholders": {
        "Logo": "{Logo}",
        "Satz1": "{Satz1}",
        "Satz2": "{Satz2}",
        "Hinweis": "{Hinweis}",
        "FristText1": "{FristText1}",
        "FristText2": "{FristText2}",
        "RedirectLink": "{RedirectLink}",
        "Link": "{Link}",
        "Ende": "{Ende}",
        "Footer1": "{Footer1}",
        "Footer2": "{Footer2}",
    },
    "disabled_placeholders": [],
    "blocks": {
        "logo": True,
        "referenz": True,
        "satz": True,
        "hinweis": True,
        "frist": True,
        "link": True,
        "gruss": True,
        "footer": True,
    },
    "constraints": [],
    "output": {
        "count": 50,
        "directory": "./output",
        "filename_pattern": "template_{n:04d}.html",
    },
    "layout": None,
    "lighten_amount": 0.85,
}


def load_config(path: str | Path | None = None) -> dict:
    """Load config from YAML file, merge with defaults."""
    cfg = _deep_copy(_DEFAULTS)

    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        cfg = _deep_merge(cfg, user)

    _resolve_light_bg(cfg)
    _resolve_disabled_placeholders(cfg)
    return cfg


def _resolve_light_bg(cfg: dict):
    """If light_accent_bg is 'auto', derive from first primary color."""
    colors = cfg.get("colors", {})
    light = colors.get("light_accent_bg", "auto")
    if light == "auto":
        primaries = colors.get("primary", _DEFAULTS["colors"]["primary"])
        base = primaries[0] if isinstance(primaries, list) else primaries
        amount = cfg.get("lighten_amount", 0.85)
        colors["light_accent_bg"] = [lighten_color(base, amount)]
    elif isinstance(light, str) and light.startswith("#"):
        colors["light_accent_bg"] = [light]


def _resolve_disabled_placeholders(cfg: dict):
    """Remove disabled placeholders from the mapping."""
    disabled = cfg.get("disabled_placeholders", [])
    ph = cfg.get("placeholders", {})
    for key in disabled:
        ph.pop(key, None)


def _deep_copy(d):
    import copy
    return copy.deepcopy(d)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result
