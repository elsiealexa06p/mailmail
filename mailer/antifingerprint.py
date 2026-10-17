import re
import random
import string


_STYLE_ATTR_RE = re.compile(r'(style\s*=\s*")([^"]*?)(")', re.IGNORECASE)
_PX_PROP_RE = re.compile(
    r"((?:padding|margin)(?:-(?:top|right|bottom|left))?\s*:\s*)([^;\"]+)",
    re.IGNORECASE,
)
_PX_NUM_RE = re.compile(r"(\d+)(px)", re.IGNORECASE)
_TAG_WITH_STYLE_RE = re.compile(
    r"(<(?:div|p|table|td|span|a|hr)\b)([^>]*?)(style\s*=\s*\"[^\"]*?\")([^>]*?>)",
    re.IGNORECASE,
)
_ELIGIBLE_TAG_RE = re.compile(
    r"(<(?:div|p|table|td|span|a|hr)\b)([^>]*?>)",
    re.IGNORECASE,
)
_IMG_TAG_RE = re.compile(r"(<img\b)(\s[^>]*?)(/?>)", re.IGNORECASE)
_ATTR_RE = re.compile(r"""(\w[\w-]*)\s*=\s*(?:"[^"]*"|'[^']*'|\S+)""", re.IGNORECASE)

PREFIXES = [
    "content", "main", "info", "msg", "block", "section", "wrapper",
    "container", "box", "panel", "area", "module", "item", "group",
    "row", "cell", "txt", "hdr", "ftr", "notice",
]
SUFFIXES = [
    "primary", "inner", "top", "base", "core", "data", "body", "head",
    "sub", "alt", "new", "ext", "wrap", "layout", "frame", "view",
    "col", "set", "part", "line",
]
ADJ = ["light", "deep", "soft", "bold", "warm", "cool", "fresh", "clear",
        "plain", "rich", "bright", "dim", "neat", "smart"]
NOUN = ["row", "card", "head", "foot", "body", "item", "tile", "lane",
         "slot", "well", "pane", "rail", "edge", "frame", "wrap"]

CLASS_SCHEMES = (
    "prefix_suffix_num",   # block-inner-42  (legacy)
    "mc_hex6",             # mc-1a2b3c
    "e_hex4",              # e-9f4d
    "c_num",               # c103
    "underscore_pair",     # _light-row
    "noun_num",            # tile47
    "mixed_short",         # m_4_a1
    "camel",               # blockInner
)


def _gen_profile(enable_classes: bool,
                  pass_through_rate: float = 0.02,
                  light_touch_rate: float = 0.10) -> dict:
    """One profile per HTML — defines what transforms run, in what order,
    and at what intensity.

    Drei mögliche Intensitäts-Modi pro Mail:
      * `pass_through` (default 2%): keine Änderungen, HTML raus wie rein
      * `light_touch` (default 10%): nur Pixel-Jitter + Attr-Shuffle,
        keine Tag-Swaps oder Class-Injection — sieht "clean" aus
      * `full` (rest): alle Ops random gemischt

    Diese Verteilung macht die Ausgabe-Distribution breit genug dass
    Filter das Engine nicht per Batch-Fingerprint erkennen können."""
    roll = random.random()
    pass_through = roll < pass_through_rate
    light_touch = (not pass_through and
                    roll < pass_through_rate + light_touch_rate)
    swap_strategy = random.choice([
        "random", "random", "random",   # weighted: usually random
        "prefer_short",
        "prefer_long",
        "no_swap",
    ])
    return {
        "pass_through": pass_through,
        "light_touch":  light_touch,
        "swap_tags":      random.random() < 0.90,
        "swap_strategy":  swap_strategy,
        # Pixel jitter is cheap noise — always run, just vary intensity widely.
        "vary_pixels":    True,
        "jitter_prob":    random.uniform(0.15, 0.70),
        "jitter_delta":   random.randint(1, 4),
        # CSS/img property order is identifiable, so always shuffle but
        # at a variable per-attribute rate so the distribution isn't
        # "100% shuffled" any more.
        "shuffle_css":    True,
        "css_shuffle_rate": random.uniform(0.40, 1.00),
        "shuffle_img":    True,
        "img_shuffle_rate": random.uniform(0.40, 1.00),
        "inject_classes": enable_classes and random.random() < 0.90,
        "class_scheme":   random.choice(CLASS_SCHEMES),
        "inject_rate":    random.uniform(0.05, 0.90),
        "style_block":    random.choice(["multiline", "compact", "minified"]),
        # Dead-code Noise: HTML-Kommentare + invisible zero-width chars
        # gestreut ins Markup. Filter-Fingerprint über feste Struktur
        # wird dadurch verwässert.
        "noise_comments": random.random() < 0.85,
        "noise_comment_rate": random.uniform(0.02, 0.15),
        "noise_invisible": random.random() < 0.50,
        "noise_invisible_rate": random.uniform(0.005, 0.03),
        # Attribute-Order pro Element mischen — HTML-Rendering ist
        # order-invariant, aber Fingerprint-Extraktion oft nicht.
        "attr_shuffle":   random.random() < 0.80,
        "attr_shuffle_rate": random.uniform(0.30, 0.90),
    }


class AntiFingerprintEngine:
    def __init__(self, enable_classes: bool = True,
                  pass_through_rate: float = 0.02,
                  light_touch_rate: float = 0.10):
        self._enable_classes = enable_classes
        self._pass_through_rate = pass_through_rate
        self._light_touch_rate = light_touch_rate

    def transform(self, html: str) -> str:
        profile = _gen_profile(self._enable_classes,
                                self._pass_through_rate,
                                self._light_touch_rate)
        if profile["pass_through"]:
            return html

        # Each op is a closure capturing the per-mail profile.
        ops = []
        if profile["light_touch"]:
            # Nur die subtileren Ops — kein Tag-Swap, keine Class-Inject,
            # keine Noise-Kommentare. Ergebnis sieht "sauber" aus.
            if profile["vary_pixels"]:
                ops.append(lambda h: _vary_pixels(h, profile["jitter_prob"] * 0.5,
                                                    profile["jitter_delta"]))
            if profile["attr_shuffle"]:
                ops.append(lambda h: _shuffle_element_attrs(h, profile["attr_shuffle_rate"] * 0.5))
            if profile["shuffle_css"]:
                ops.append(lambda h: _shuffle_css_properties(h, profile["css_shuffle_rate"] * 0.5))
        else:
            if profile["swap_tags"] and profile["swap_strategy"] != "no_swap":
                ops.append(lambda h: _swap_tags(h, profile["swap_strategy"]))
            if profile["vary_pixels"]:
                ops.append(lambda h: _vary_pixels(h, profile["jitter_prob"], profile["jitter_delta"]))
            if profile["shuffle_css"]:
                ops.append(lambda h: _shuffle_css_properties(h, profile["css_shuffle_rate"]))
            if profile["shuffle_img"]:
                ops.append(lambda h: _shuffle_image_attributes(h, profile["img_shuffle_rate"]))
            if profile["inject_classes"]:
                ops.append(lambda h: _inject_classes(h, profile["class_scheme"],
                                                      profile["inject_rate"],
                                                      profile["style_block"]))
            if profile["noise_comments"]:
                ops.append(lambda h: _inject_noise_comments(h, profile["noise_comment_rate"]))
            if profile["noise_invisible"]:
                ops.append(lambda h: _inject_invisible_chars(h, profile["noise_invisible_rate"]))
            if profile["attr_shuffle"]:
                ops.append(lambda h: _shuffle_element_attrs(h, profile["attr_shuffle_rate"]))

        random.shuffle(ops)
        for op in ops:
            html = op(html)
        return html


def _swap_tags(html: str, strategy: str = "random") -> str:
    pairs = [("strong", "b"), ("em", "i")]
    for tag_a, tag_b in pairs:
        html = _swap_one_pair(html, tag_a, tag_b, strategy)
    return html


def _swap_one_pair(html: str, tag_a: str, tag_b: str, strategy: str) -> str:
    tokens = re.split(
        rf"(</?(?:{tag_a}|{tag_b})\b[^>]*>)", html, flags=re.IGNORECASE
    )
    stack: list = []
    out = []
    for token in tokens:
        m = re.match(rf"<(/?)(?:{tag_a}|{tag_b})\b", token, re.IGNORECASE)
        if not m:
            out.append(token)
            continue
        is_close = m.group(1) == "/"
        if not is_close:
            lower = token.lower()
            currently_a = tag_a in lower
            if strategy == "prefer_short":
                # short = b/i (tag_b). Swap if currently long.
                do_swap = currently_a
            elif strategy == "prefer_long":
                # long = strong/em (tag_a). Swap if currently short.
                do_swap = not currently_a
            else:  # random
                do_swap = random.random() < 0.5
            stack.append(do_swap)
            if do_swap:
                token = _flip_tag(token, tag_a, tag_b)
        else:
            do_swap = stack.pop() if stack else False
            if do_swap:
                token = _flip_tag(token, tag_a, tag_b)
        out.append(token)
    return "".join(out)


def _flip_tag(token: str, tag_a: str, tag_b: str) -> str:
    lo = token.lower()
    if tag_a in lo:
        return re.sub(tag_a, tag_b, token, count=1, flags=re.IGNORECASE)
    return re.sub(tag_b, tag_a, token, count=1, flags=re.IGNORECASE)


def _vary_pixels(html: str, jitter_prob: float = 0.30, jitter_delta: int = 2) -> str:
    def _vary_num(m: re.Match) -> str:
        val = int(m.group(1))
        if val <= 4:
            return m.group(0)
        if random.random() < jitter_prob:
            offset = random.randint(1, jitter_delta) * random.choice([-1, 1])
            val = max(0, val + offset)
        return f"{val}px"

    def _vary_prop(match: re.Match) -> str:
        return match.group(1) + _PX_NUM_RE.sub(_vary_num, match.group(2))

    def _vary_style(match: re.Match) -> str:
        prefix, css, suffix = match.group(1), match.group(2), match.group(3)
        return prefix + _PX_PROP_RE.sub(_vary_prop, css) + suffix

    return _STYLE_ATTR_RE.sub(_vary_style, html)


def _shuffle_css_properties(html: str, rate: float = 1.0) -> str:
    def _shuffle_one(match: re.Match) -> str:
        prefix, css, suffix = match.group(1), match.group(2), match.group(3)
        if random.random() > rate:
            return match.group(0)
        parts = [p.strip() for p in css.split(";") if p.strip()]
        if len(parts) > 1:
            random.shuffle(parts)
        return prefix + ";".join(parts) + ";" + suffix

    return _STYLE_ATTR_RE.sub(_shuffle_one, html)


def _shuffle_image_attributes(html: str, rate: float = 1.0) -> str:
    def _shuffle_one(match: re.Match) -> str:
        if random.random() > rate:
            return match.group(0)
        tag_open = match.group(1)
        attrs_str = match.group(2)
        tag_close = match.group(3)
        attr_spans = list(_ATTR_RE.finditer(attrs_str))
        if len(attr_spans) <= 1:
            return match.group(0)
        attr_strings = [attrs_str[m.start():m.end()] for m in attr_spans]
        random.shuffle(attr_strings)
        return tag_open + " " + " ".join(attr_strings) + " " + tag_close

    return _IMG_TAG_RE.sub(_shuffle_one, html)


def _inject_classes(html: str, scheme: str, inject_rate: float,
                     style_block: str) -> str:
    style_to_class: dict = {}
    injections: list = []

    def _maybe_inject(match: re.Match) -> str:
        full = match.group(0)
        if re.search(r'\bclass\s*=', full, re.IGNORECASE):
            return full
        style_match = re.search(r'style\s*=\s*"([^"]*?)"', full, re.IGNORECASE)
        if not style_match:
            return full
        if random.random() > inject_rate:
            return full
        style_val = style_match.group(1).strip()
        if not style_val:
            return full

        sorted_key = ";".join(sorted(p.strip() for p in style_val.split(";") if p.strip()))
        if sorted_key in style_to_class:
            cls_name = style_to_class[sorted_key]
        else:
            cls_name = _make_class_name(scheme)
            style_to_class[sorted_key] = cls_name
            injections.append((cls_name, style_val))

        insert_pos = match.end(1)
        offset = insert_pos - match.start()
        before = full[:offset]
        after = full[offset:]
        return before + f' class="{cls_name}"' + after

    html = _ELIGIBLE_TAG_RE.sub(_maybe_inject, html)

    if not injections:
        return html

    if style_block == "minified":
        body = "".join(f".{c}{{{css}}}" for c, css in injections)
        block = f"<style>{body}</style>"
    elif style_block == "compact":
        body = " ".join(f".{c} {{ {css} }}" for c, css in injections)
        block = f"<style>{body}</style>"
    else:  # multiline (legacy)
        body = "\n".join(f"  .{c} {{ {css} }}" for c, css in injections)
        block = f"\n<style>\n{body}\n</style>\n"

    meta_match = re.search(
        r'(<meta\s+charset\s*=\s*"[^"]*"\s*/?>)', html, re.IGNORECASE
    )
    if meta_match:
        pos = meta_match.end()
        return html[:pos] + block + html[pos:]
    head_close = re.search(r"</head>", html, re.IGNORECASE)
    if head_close:
        return html[: head_close.start()] + block + html[head_close.start():]
    return block + html


_NOISE_COMMENT_PHRASES = [
    "layout", "wrapper", "start", "end", "section", "block", "container",
    "outer", "inner", "row", "col", "cta", "footer", "header", "top",
    "hero", "banner", "spacer", "divider", "content", "main", "region",
    "grid", "cell", "item", "list", "aside", "panel", "unit", "module",
]

# Zero-Width Chars die von allen gängigen Renderern gerendert werden
# ohne Text zu verändern. ZWSP + WJ sind safest, VS16 nur nach Emoji.
_INVISIBLE_CHARS = ("​", "⁠", "‌", "‍")


def _inject_noise_comments(html: str, rate: float = 0.1) -> str:
    """HTML-Kommentare an zufälligen Positionen zwischen Block-Tags
    einstreuen. rate = Wahrscheinlichkeit pro Insert-Kandidat. Alle
    Kommentare sind Standard-HTML → wird von jedem Renderer ignoriert
    und stört keinen Client (Outlook/Gmail rendern sie einfach nicht).
    """
    if rate <= 0:
        return html

    def _make_comment() -> str:
        style = random.randint(0, 3)
        phrase = random.choice(_NOISE_COMMENT_PHRASES)
        if style == 0:
            return f"<!-- {phrase} -->"
        if style == 1:
            return f"<!--{phrase}-->"
        if style == 2:
            return f"<!-- /{phrase} -->"
        return f"<!-- {phrase} {random.randint(1, 999)} -->"

    def _sub(match: re.Match) -> str:
        if random.random() < rate:
            return match.group(0) + _make_comment()
        return match.group(0)

    return re.sub(
        r"</(?:div|p|table|tr|td|section|article|header|footer|nav|main|"
        r"aside|ul|ol|li|blockquote)>",
        _sub, html, flags=re.IGNORECASE)


def _inject_invisible_chars(html: str, rate: float = 0.01) -> str:
    """Zero-width chars in Text-Nodes streuen. Verändern die sichtbare
    Darstellung nicht, aber Tokenizer/Hash-Fingerprint schon."""
    if rate <= 0:
        return html
    result = []
    in_tag = False
    for ch in html:
        result.append(ch)
        if ch == "<":
            in_tag = True
        elif ch == ">":
            in_tag = False
        elif not in_tag and ch.isalnum() and random.random() < rate:
            result.append(random.choice(_INVISIBLE_CHARS))
    return "".join(result)


def _shuffle_element_attrs(html: str, rate: float = 0.5) -> str:
    """Attribut-Reihenfolge in Block/Inline-Tags mischen. HTML-Rendering
    ist order-invariant, aber viele Fingerprint-Signaturen extrahieren
    Attribut-Reihenfolge. Wir touchen NUR div/span/a/p/table/td/tr —
    weniger risky als komplexere Tags wie <input> mit form-state."""
    if rate <= 0:
        return html
    tag_re = re.compile(
        r"<(div|span|a|p|table|td|tr|section|article|header|footer)"
        r"\b((?:\s+[a-zA-Z_:][a-zA-Z0-9_:\-]*(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))?)+)"
        r"(\s*/?)>",
        re.IGNORECASE,
    )
    attr_re = re.compile(
        r"\s+([a-zA-Z_:][a-zA-Z0-9_:\-]*)(?:\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+))?"
    )

    def _shuffle_one(match: re.Match) -> str:
        if random.random() > rate:
            return match.group(0)
        tag = match.group(1)
        attrs_str = match.group(2)
        trailing = match.group(3) or ""
        attrs = attr_re.findall(attrs_str)
        if len(attrs) < 2:
            return match.group(0)
        random.shuffle(attrs)
        new_attrs = "".join(
            f' {name}={val}' if val else f' {name}' for name, val in attrs
        )
        return f"<{tag}{new_attrs}{trailing}>"

    return tag_re.sub(_shuffle_one, html)


def _make_class_name(scheme: str = "prefix_suffix_num") -> str:
    """Random class name in one of several real-world ESP styles."""
    if scheme == "mc_hex6":
        return "mc-" + "".join(random.choices("0123456789abcdef", k=6))
    if scheme == "e_hex4":
        return "e-" + "".join(random.choices("0123456789abcdef", k=4))
    if scheme == "c_num":
        return "c" + str(random.randint(100, 999))
    if scheme == "underscore_pair":
        return f"_{random.choice(ADJ)}-{random.choice(NOUN)}"
    if scheme == "noun_num":
        return f"{random.choice(NOUN)}{random.randint(1, 99)}"
    if scheme == "mixed_short":
        h = "".join(random.choices("0123456789abcdef", k=2))
        return f"m_{random.randint(1, 9)}_{h}"
    if scheme == "camel":
        return random.choice(PREFIXES) + random.choice(SUFFIXES).capitalize()
    # default — legacy scheme, kept as one of many options
    return (
        f"{random.choice(PREFIXES)}-"
        f"{random.choice(SUFFIXES)}-"
        f"{random.randint(10, 99)}"
    )
