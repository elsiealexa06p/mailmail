"""Two-layer placeholder resolution.

Layer 1 (engine-internal): {pc}, {ac}, {light_bg}, {footer_bg}, {footer_text}, {font}
    — resolved at generation time from color/font config.

Layer 2 (mailer-facing): {Logo}, {Satz1}, {RedirectLink}, etc.
    — left as-is in output OR renamed/removed per config.
"""

import random
import re


def resolve_engine_placeholders(html: str, cfg: dict) -> str:
    """Replace engine-internal color/font placeholders."""
    colors = cfg.get("colors", {})

    def _pick(pool):
        if isinstance(pool, list):
            return random.choice(pool)
        return pool

    replacements = {
        "{pc}": _pick(colors.get("primary", ["#005eb8"])),
        "{ac}": _pick(colors.get("accent", ["#c0392b"])),
        "{light_bg}": _pick(colors.get("light_accent_bg", ["#eef4fb"])),
        "{footer_bg}": _pick(colors.get("footer_bg", ["#2c3e50"])),
        "{footer_text}": _pick(colors.get("footer_text", ["#cccccc"])),
        "{font}": random.choice(cfg.get("fonts", ["Arial, sans-serif"])),
    }

    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)

    return html


def apply_placeholder_mapping(html: str, cfg: dict) -> str:
    """Rename or remove mailer-facing placeholders per config.

    The config 'placeholders' dict maps logical names to output strings:
        Logo: "{CompanyLogo}"     — renames {Logo} to {CompanyLogo}
        Hinweis: ""               — removes {Hinweis} (replaces with empty string)

    Placeholders listed in 'disabled_placeholders' are already removed
    from the mapping by config.py, so they get replaced with "".
    """
    mapping = cfg.get("placeholders", {})

    # All known placeholder names (from defaults)
    all_names = [
        "Logo", "Satz1", "Satz2", "Hinweis", "FristText1", "FristText2",
        "RedirectLink", "Link", "Ende", "Footer1", "Footer2",
    ]

    for name in all_names:
        original = "{" + name + "}"
        if name in mapping:
            replacement = mapping[name]
            if replacement != original:
                html = html.replace(original, replacement)
        else:
            html = html.replace(original, "")

    return html
