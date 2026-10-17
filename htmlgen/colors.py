"""Color utilities — lighten/darken hex colors, auto-derive palettes."""


def lighten_color(hex_color: str, amount: float = 0.85) -> str:
    """Lighten a hex color by blending it toward white.

    amount=0.0 returns the original, amount=1.0 returns pure white.
    Default 0.85 gives a subtle background tint.
    """
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    r = int(r + (255 - r) * amount)
    g = int(g + (255 - g) * amount)
    b = int(b + (255 - b) * amount)
    return f"#{r:02x}{g:02x}{b:02x}"


def darken_color(hex_color: str, amount: float = 0.3) -> str:
    """Darken a hex color by blending it toward black."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    r = int(r * (1 - amount))
    g = int(g * (1 - amount))
    b = int(b * (1 - amount))
    return f"#{r:02x}{g:02x}{b:02x}"
