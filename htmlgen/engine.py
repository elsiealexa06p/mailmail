"""Core engine — assemble layout + blocks into finished HTML templates."""

import random
from pathlib import Path
from .config import load_config
from .constraints import parse_tags, check_constraints
from .placeholders import resolve_engine_placeholders, apply_placeholder_mapping

_BASE = Path(__file__).parent
MAX_RETRIES = 200
BLOCK_NAMES = ["logo", "referenz", "satz", "hinweis", "frist", "link", "gruss", "footer"]

# A theme constrains the *color personality* of a generated template so
# the footer, frist and link blocks don't drift into three competing
# colour regions. Each entry lists, per block, which variant tags are
# allowed; an empty list means "no constraint".
#
# - clean_light: white-card classic. No red accent in Frist, no dark or
#   coloured Footer, soft Link styles. The look the old engine had.
# - dark_footer: contrast look. Allows the red accent Frist *because*
#   the dark footer balances it; pill/outline Links.
# - primary_band: a band of {pc} somewhere in the layout — Footer keeps
#   to a light or primary-bordered style, Frist stays on the primary
#   side (no red), Link mirrors.
# Per-theme rules for footer / frist / link block selection.
# "allow" is a positive filter (variant must have at least one matching tag),
# "forbid" is a hard negative filter (variant must NOT have any of these).
# forbid wins over allow.
_THEMES = {
    "clean_light": {
        # Calm white-card look. Zurückhaltend — plain Sätze, einfache
        # Grüße, kein has_bg/colored auf mittleren Blocks, kein dark
        # Footer. Header/Logo dezent (border-bottom eher als thick).
        "footer":   {"allow": set(),                     "forbid": {"dark", "colored"}},
        "frist":    {"allow": {"colored", "grey"},       "forbid": {"accent", "yellow"}},
        "link":     {"allow": set(),                     "forbid": {"pill"}},
        "satz":     {"allow": set(),                     "forbid": {"grouped", "has_bg", "darker"}},
        "gruss":    {"allow": {"simple", "em", "bold-name"}, "forbid": {"centered"}},
        "hinweis":  {"allow": {"italic", "border-left"}, "forbid": {"has_bg"}},
        "logo":     {"allow": set(),                     "forbid": {"border-bottom-thick", "border-left-thick"}},
        "referenz": {"allow": {"plain", "bold-label", "letterspaced"}, "forbid": {"has_bg", "colored", "badge"}},
    },
    "dark_footer": {
        # Contrast look. Darker footer paired with the red accent Frist —
        # der Mittelteil darf reichhaltiger sein (grouped, has_bg), Grüße
        # zentriert erlaubt, Referenz auch als Badge.
        "footer":   {"allow": {"dark"},                  "forbid": set()},
        "frist":    {"allow": {"accent", "colored"},     "forbid": {"yellow"}},
        "link":     {"allow": {"pill", "outline"},       "forbid": set()},
        "satz":     {"allow": {"plain", "spaced", "grouped", "border-left"}, "forbid": set()},
        "gruss":    {"allow": set(),                     "forbid": set()},
        "hinweis":  {"allow": {"has_bg", "border-left"}, "forbid": set()},
        "logo":     {"allow": set(),                     "forbid": set()},
        "referenz": {"allow": set(),                     "forbid": set()},
    },
    "primary_band": {
        # Layouts that already paint a {pc} band. Alles bleibt auf der
        # primary-Seite — kein red, kein dark, mittlerer Reichtum.
        "footer":   {"allow": set(),                     "forbid": {"dark"}},
        "frist":    {"allow": {"colored", "grey"},       "forbid": {"accent", "yellow"}},
        "link":     {"allow": set(),                     "forbid": {"pill"}},
        "satz":     {"allow": {"plain", "spaced", "border-left"}, "forbid": {"darker"}},
        "gruss":    {"allow": {"simple", "em", "bold-name", "split"}, "forbid": set()},
        "hinweis":  {"allow": {"border-left", "italic"}, "forbid": {"has_bg"}},
        "logo":     {"allow": set(),                     "forbid": set()},
        "referenz": {"allow": {"plain", "bold-label", "letterspaced", "colored"}, "forbid": {"has_bg"}},
    },
}


def _load_variants(block_name: str, base_dir: Path | None = None) -> list[dict]:
    """Load all HTML variant files for a block."""
    block_dir = (base_dir or _BASE) / "blocks" / block_name
    if not block_dir.is_dir():
        return []

    variants = []
    for f in sorted(block_dir.glob("*.html")):
        content = f.read_text(encoding="utf-8")
        tags = parse_tags(content)
        variants.append({
            "variant": f.stem,
            "tags": tags,
            "html": content,
        })
    return variants


def _load_layouts(base_dir: Path | None = None) -> list[dict]:
    """Load all layout templates."""
    layout_dir = (base_dir or _BASE) / "layouts"
    if not layout_dir.is_dir():
        return []

    layouts = []
    for f in sorted(layout_dir.glob("*.html")):
        content = f.read_text(encoding="utf-8")
        layouts.append({
            "name": f.stem,
            "tags": parse_tags(content),
            "html": content,
        })
    return layouts


def _load_all(base_dir: Path | None = None) -> tuple[dict, list]:
    """Load all block variants and layouts once from disk."""
    base = base_dir or _BASE
    block_variants = {}
    for name in BLOCK_NAMES:
        block_variants[name] = _load_variants(name, base)
    layouts = _load_layouts(base)
    return block_variants, layouts


def _pick_theme(layout: dict) -> str:
    """Choose a color theme. Layouts tagged with `band` (the ones that
    already paint a {pc} region themselves) are locked to primary_band
    so we don't add yet another competing colour region. Others get a
    weighted random pick that mostly stays clean."""
    layout_tags = layout.get("tags", set())
    if "band" in layout_tags:
        return "primary_band"
    if "dark_friendly" in layout_tags:
        # Layouts that visually carry a dark footer well.
        return random.choices(
            ["clean_light", "dark_footer"], weights=[1, 1])[0]
    # Default mix — favour the calm look.
    return random.choices(
        ["clean_light", "dark_footer", "primary_band"],
        weights=[6, 2, 2])[0]


def _filter_by_theme(variants: list, theme: str, block_name: str) -> list:
    """Return variants compatible with the theme for this block.
    Forbid wins over allow; if `allow` is empty there is no positive
    filter (any non-forbidden variant is fine)."""
    rule = _THEMES.get(theme, {}).get(block_name)
    if not rule:
        return variants
    allow, forbid = rule.get("allow", set()), rule.get("forbid", set())

    def ok(v: dict) -> bool:
        tags = v["tags"]
        if forbid and tags & forbid:
            return False
        if allow and not (tags & allow):
            return False
        return True

    keep = [v for v in variants if ok(v)]
    return keep or variants  # fall back rather than fail to generate


def _pick_blocks(block_variants: dict, disabled: set, constraints: list,
                  theme: str = "") -> dict:
    """Pick one variant per enabled block, respecting constraints
    and (if given) the theme's per-block tag filter."""
    for _ in range(MAX_RETRIES):
        selection = {}
        for block_name, variants in block_variants.items():
            if block_name in disabled or not variants:
                continue
            pool = _filter_by_theme(variants, theme, block_name) if theme else variants
            v = random.choice(pool)
            selection[block_name] = v

        if check_constraints(selection, constraints):
            return selection

    return selection


def _assemble(cfg: dict, layout: dict, selection: dict) -> str:
    """Assemble a single template from layout + block selection."""
    html = layout["html"]
    for block_name in BLOCK_NAMES:
        placeholder = "{BLOCK_" + block_name.upper() + "}"
        if block_name in selection:
            block_html = selection[block_name]["html"]
            first_line = block_html.split("\n", 1)[0]
            if first_line.strip().startswith("<!--") and "tags:" in first_line.lower():
                block_html = block_html.split("\n", 1)[1] if "\n" in block_html else ""
            html = html.replace(placeholder, block_html)
        else:
            html = html.replace(placeholder, "")

    html = resolve_engine_placeholders(html, cfg)
    html = apply_placeholder_mapping(html, cfg)
    return html


def generate_one(cfg: dict, base_dir: Path | None = None,
                 _cache: tuple | None = None) -> str:
    """Generate a single HTML template string.

    Pass _cache=(block_variants, layouts) to skip disk reads.
    """
    if _cache:
        block_variants, layouts = _cache
    else:
        block_variants, layouts = _load_all(base_dir)

    chosen_layout_name = cfg.get("layout")
    if chosen_layout_name:
        filtered = [l for l in layouts if l["name"] == chosen_layout_name]
        pick_from = filtered or layouts
    else:
        pick_from = layouts

    if not pick_from:
        raise FileNotFoundError("No layouts found")

    layout = random.choice(pick_from)
    blocks_cfg = cfg.get("blocks", {})
    disabled = {name for name, enabled in blocks_cfg.items() if not enabled}
    constraints = cfg.get("constraints", [])

    # Choose a color theme for this template so the footer/frist/link
    # blocks land in one coherent palette instead of three competing
    # colour regions. Can be disabled per config for the old behaviour.
    theme = "" if cfg.get("disable_theme") else _pick_theme(layout)

    selection = _pick_blocks(block_variants, disabled, constraints, theme=theme)
    return _assemble(cfg, layout, selection)


def generate(config_path: str | Path | None = None,
             count: int | None = None,
             output_dir: str | Path | None = None,
             base_dir: Path | None = None) -> list[Path]:
    """Generate multiple HTML templates and write them to disk."""
    cfg = load_config(config_path)

    count = count or cfg.get("output", {}).get("count", 50)
    output_dir = Path(output_dir or cfg.get("output", {}).get("directory", "./output"))
    pattern = cfg.get("output", {}).get("filename_pattern", "template_{n:04d}.html")

    output_dir.mkdir(parents=True, exist_ok=True)

    cache = _load_all(base_dir)

    written = []
    for i in range(1, count + 1):
        html = generate_one(cfg, base_dir, _cache=cache)
        filename = pattern.format(n=i)
        path = output_dir / filename
        path.write_text(html, encoding="utf-8")
        written.append(path)

    return written
