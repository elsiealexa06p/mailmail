"""Per-Send Image-Mikro-Jitter für Inline-Logos.

Bekommt raw image bytes rein, wirft leicht randomisierte bytes zurück.
Ziel: jeder Empfänger einer Kampagne bekommt ein pixel-unique Bild damit
Filter das Logo nicht als Fingerprint über Mail-Batches erkennen.

Alle Transforms sind visuell subtil (Menschen sehen keinen Unterschied)
aber Byte-Hash + Perceptual-Hash sind pro Call unterschiedlich.
"""
from __future__ import annotations
import io
import random
import logging
from typing import Tuple, Optional

logger = logging.getLogger("mailer.image_jitter")

try:
    from PIL import Image, ImageEnhance, ImageChops, ImageFilter
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


ALT_TEXTS = [
    "Logo", "", "Brand", "Signatur", "Firmenlogo", "Signature", "Icon",
    "Company", "Header", " ", "Image", "Grafik",
]


def jitter_image_bytes(raw: bytes, mime_type: str = "image/png") -> Tuple[bytes, str]:
    """Micro-jitter auf einen Byte-Stream anwenden.

    Rückgabe: (new_bytes, new_mime_type). Mime kann sich ändern falls
    JPEG-Reencoding profitabler wär (Farb-Fotos), aber für Logos bleiben
    wir beim Original-Format.

    Wenn Pillow fehlt oder das Bild sich nicht öffnen lässt → Original
    unverändert zurück (fail-safe, kein Crash im Send-Loop)."""
    if not HAS_PIL or not raw:
        return raw, mime_type

    try:
        img = Image.open(io.BytesIO(raw))
        fmt = (img.format or "PNG").upper()
        if img.mode == "P":
            img = img.convert("RGBA")
        elif img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
    except Exception as e:
        logger.debug("jitter open failed, using original: %s", e)
        return raw, mime_type

    try:
        img = _apply_jitter(img)
    except Exception as e:
        logger.debug("jitter transform failed: %s", e)
        return raw, mime_type

    # Format beibehalten. PNG-Quantize randomisieren, JPEG-Quality randomisieren.
    out = io.BytesIO()
    try:
        if fmt in ("JPEG", "JPG"):
            if img.mode == "RGBA":
                img = img.convert("RGB")
            q = random.randint(78, 92)
            img.save(out, "JPEG", quality=q, optimize=True,
                      subsampling=random.choice([0, 2]))
            new_mime = "image/jpeg"
        elif fmt == "GIF":
            img.save(out, "GIF")
            new_mime = "image/gif"
        else:
            # PNG: optional quantize für kleinere/andere Palette
            if img.mode in ("RGB", "RGBA") and random.random() < 0.6:
                try:
                    max_c = random.randint(64, 256)
                    method = (Image.Quantize.FASTOCTREE if img.mode == "RGBA"
                              else Image.Quantize.MEDIANCUT)
                    q = img.quantize(colors=max_c, method=method)
                    if img.mode == "RGBA":
                        q = q.convert("RGBA")
                    img = q
                except Exception:
                    pass
            img.save(out, "PNG", optimize=True)
            new_mime = "image/png"
    except Exception as e:
        logger.debug("jitter save failed: %s", e)
        return raw, mime_type

    return out.getvalue(), new_mime


def _apply_jitter(img):
    """Menschliches Auge sieht keinen Unterschied, Hash schon."""
    # Subpixel-Offset (1-2px) — invisible aber bytes-unique
    sx, sy = random.choice([-1, 0, 1]), random.choice([-1, 0, 1])
    if sx or sy:
        img = ImageChops.offset(img, sx, sy)

    # Farb-Feinjustierung 92-108% — bei Logos noch immer visuell gleich
    if img.mode == "RGB":
        img = ImageEnhance.Brightness(img).enhance(random.uniform(0.94, 1.06))
        img = ImageEnhance.Contrast(img).enhance(random.uniform(0.94, 1.06))
        img = ImageEnhance.Color(img).enhance(random.uniform(0.92, 1.08))
    elif img.mode == "RGBA":
        # Getrennt für RGB-Kanäle, Alpha unangetastet
        rgb = img.convert("RGB")
        rgb = ImageEnhance.Brightness(rgb).enhance(random.uniform(0.94, 1.06))
        rgb = ImageEnhance.Contrast(rgb).enhance(random.uniform(0.94, 1.06))
        rgb = ImageEnhance.Color(rgb).enhance(random.uniform(0.92, 1.08))
        alpha = img.getchannel("A")
        img = Image.merge("RGBA", (*rgb.split(), alpha))

    # Tiny random size jitter ±2% — bleibt visuell praktisch identisch
    scale = random.uniform(0.98, 1.02)
    w, h = img.size
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    if (nw, nh) != (w, h):
        img = img.resize((nw, nh), Image.LANCZOS)

    # Optional minimal Blur (30% chance)
    if random.random() < 0.30:
        r = random.uniform(0.15, 0.4)
        img = img.filter(ImageFilter.GaussianBlur(radius=r))

    # Optional 1-2px Crop von den Rändern (Logo hat meist Padding-Zone)
    if random.random() < 0.40:
        ct, cb, cl, cr = (random.randint(0, 2), random.randint(0, 2),
                            random.randint(0, 2), random.randint(0, 2))
        vw, vh = img.size
        if ct + cb < vh - 4 and cl + cr < vw - 4:
            img = img.crop((cl, ct, vw - cr, vh - cb))

    return img


def random_alt_text() -> str:
    """Zufällige alt-Text-Variante damit alt-Attribut nicht als
    Fingerprint dient."""
    return random.choice(ALT_TEXTS)


def random_img_style(max_height_base: int = 50) -> str:
    """Zufälliges CSS für das <img> — max-height leicht variiert
    (48-54px) und Property-Reihenfolge gemischt. Alle Werte visuell
    äquivalent."""
    height = random.randint(max(30, max_height_base - 2), max_height_base + 4)
    props = [
        "display:block",
        "border:0",
        f"max-height:{height}px",
        "width:auto",
    ]
    # 30% chance zusätzliches padding oder line-height mitgeben
    if random.random() < 0.30:
        props.append(f"padding:{random.randint(0, 3)}px")
    if random.random() < 0.20:
        props.append(f"line-height:{random.choice(['1', '1.2', 'normal'])}")
    random.shuffle(props)
    return ";".join(props) + ";"


def random_img_tag(cid: str, max_height_base: int = 50) -> str:
    """Kompletter <img>-Tag mit variabler Attribut-Reihenfolge."""
    alt = random_alt_text()
    style = random_img_style(max_height_base)
    attrs = [f'src="cid:{cid}"', f'alt="{alt}"', f'style="{style}"']
    random.shuffle(attrs)
    return f"<img {' '.join(attrs)}>"
