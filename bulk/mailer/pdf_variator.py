"""PDF Variator — erzeugt aus 1 Source-PDF N visuell identische, aber
byte-, hash-, fuzzy-hash- und pHash-mäßig einzigartige Varianten.

Ziel: Halon/Expurgate-Fuzzy-Matching umgehen für längere Bulk-Kampagnen.

Layer:
    A  Filename-Pool
    B  Metadaten (Title/Author/Producer/Creator/Subject/Keywords/Dates/ID/XMP)
    C  Struktur-Scrub (Outlines/Names/AcroForm/StructTree/Annots/JS raus)
    D  Image-Manipulation (Pixel-Nudge + JPEG-Reencode mit random Quality)
    E  Byte-Noise (Content-Stream Recompress + Object-Reorder + EOF-Padding)
    F  ToUnicode-CMap-Vergiftung (Text-Extraktion → Zeichensalat, visuell 1:1)
    G  Hidden-Text (weiß auf weiß, 3 Wörter auf letzter Seite)
"""
from __future__ import annotations

import io
import os
import re
import random
import secrets
import string
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger("bulk.pdf_variator")

try:
    import pikepdf
    from pikepdf import Pdf, Name, String, Array, Dictionary, Stream, PdfImage
except ImportError as e:
    pikepdf = None
    logger.warning("pikepdf nicht installiert: %s", e)

try:
    from PIL import Image
except ImportError:
    Image = None


# ── Default Pools (überschreibbar via PoolBag.from_dict) ──

FILENAME_POOL = [
    "Katalog", "Produktkatalog", "Gesamtkatalog", "Hauptkatalog",
    "Lieferprogramm", "Sortiment", "Sortimentsübersicht",
    "Sortimentskatalog", "Produktprogramm", "Produktübersicht",
    "Produktverzeichnis", "Modellübersicht", "Modellprogramm",
    "Modellkatalog", "Fahrzeugübersicht", "Fahrzeugkatalog",
    "Branchenkatalog", "Handelskatalog", "Großhandelskatalog",
    "Verkaufskatalog", "Bestellkatalog", "Broschüre",
    "Produktbroschüre", "Prospekt", "Kompaktkatalog", "Kurzkatalog",
    "Auswahlübersicht", "Preisliste", "Produktunterlagen",
    "Verkaufsunterlagen", "Vertriebsunterlagen", "Unterlagen",
    "Anhang", "Anlage", "Dokumentation",
]

PRODUCER_POOL = [
    "Microsoft® Word 2019", "Microsoft® Word 2021", "Microsoft® Word LTSC 2021",
    "LibreOffice 7.4", "LibreOffice 7.5", "LibreOffice 24.2",
    "Adobe Acrobat 23.6.0", "Adobe Acrobat Pro DC 24.001.20604",
    "Adobe PDF Library 15.0", "PDFCreator 4.4.0", "PDFCreator 5.0.0",
    "iText® 5.5.13.3 ©2000-2023 iText Group NV",
    "PDF-XChange Standard V10", "Foxit PhantomPDF 12.0",
    "PDF Producer 3.1", "PScript5.dll Version 5.2.2",
    "Prince 15.3 (www.princexml.com)", "wkhtmltopdf 0.12.6.1",
    "Nitro PDF Professional (10, 7, 1, 8)",
]

CREATOR_POOL = [
    "Microsoft® Word 2019", "Microsoft® Word 2021", "Microsoft® PowerPoint 2019",
    "Adobe InDesign 18.5 (Windows)", "Adobe InDesign 19.0 (Macintosh)",
    "CorelDRAW Graphics Suite 2023", "QuarkXPress 2022",
    "Scribus 1.5.6", "LaTeX pdfTeX-1.40.24",
    "LibreOffice 7.4", "LibreOffice 24.2",
    "Pages 12.2", "Canva", "Affinity Publisher 2.4.2",
]

AUTHOR_POOL = [
    "Max Müller", "Anna Schmidt", "Thomas Weber", "Lisa Fischer",
    "Michael Wagner", "Julia Becker", "Andreas Schulz", "Katrin Hoffmann",
    "Stefan Bauer", "Nicole Schäfer", "Markus Koch", "Petra Richter",
    "Christian Klein", "Sabine Wolf", "Daniel Neumann", "Melanie Zimmermann",
    "Frank Schwarz", "Susanne Braun", "Alexander Krüger", "Vanessa Hartmann",
    "Martin Lange", "Sandra Meyer", "Oliver Schmidt", "Claudia Werner",
]

TITLE_POOL = [
    "Katalog", "Produktkatalog", "Übersicht der Leistungen",
    "Produktübersicht", "Sortiment", "Angebotsunterlagen",
    "Vertriebsunterlagen", "Preisliste", "Produktdokumentation",
    "Leistungsverzeichnis", "Firmenpräsentation", "Broschüre",
    "Kundeninformation", "Vertragsunterlagen", "Detailinformationen",
]

SUBJECT_POOL = [
    "Produktkatalog", "Angebot", "Preisliste", "Übersicht", "Sortiment",
    "Firmeninformation", "Dokumentation", "Katalog", "Prospekt",
    "Präsentation", "Referenz", "Kundeninformation",
]

KEYWORDS_POOL = [
    "katalog", "produkte", "sortiment", "angebot", "preisliste",
    "informationen", "dokumentation", "übersicht", "prospekt",
    "service", "leistungen", "unternehmen", "qualität", "kunden",
    "referenz", "beratung", "vertrieb",
]

HIDDEN_TEXT_POOL = [
    "reference", "meta", "cat", "ref", "id", "seq", "batch", "run",
    "iter", "ver", "src", "gen", "cnt", "no", "tag", "loc",
]


@dataclass
class PoolBag:
    filename: list
    title: list
    producer: list
    creator: list
    author: list
    subject: list
    keywords: list

    @classmethod
    def default(cls) -> "PoolBag":
        return cls(
            filename=FILENAME_POOL[:], title=TITLE_POOL[:],
            producer=PRODUCER_POOL[:], creator=CREATOR_POOL[:],
            author=AUTHOR_POOL[:], subject=SUBJECT_POOL[:],
            keywords=KEYWORDS_POOL[:],
        )

    @classmethod
    def from_dict(cls, d: dict) -> "PoolBag":
        b = cls.default()
        for k in ("filename", "title", "producer", "creator", "author",
                  "subject", "keywords"):
            v = d.get(k)
            if v:
                if isinstance(v, str):
                    v = [x.strip() for x in re.split(r"[\n,;]", v) if x.strip()]
                if v:
                    setattr(b, k, v)
        return b


@dataclass
class LayerSet:
    filename: bool = True     # A — always on
    metadata: bool = True     # B — always on
    structure: bool = True    # C — always on
    image: bool = True        # D — default on
    byte_noise: bool = True   # E — default on
    cmap_poison: bool = False # F — default off (Empfänger-Copy-Paste-Risiko)
    hidden_text: bool = False # G — default off (experimentell)


# ── Filename builder ─────────────────────────────────────

_MONTHS_DE = ["Januar", "Februar", "März", "April", "Mai", "Juni",
              "Juli", "August", "September", "Oktober", "November", "Dezember"]

# Suffix-Typen: (Label, callable(rng, ref_date) -> str)
# ref_date ist ein datetime.date-Objekt (oder was mit .month/.year/.isocalendar).
SUFFIX_TYPES = {
    "none":       ("Kein Suffix (nur Basename)",       lambda r, d: ""),
    "month_de":   ("_Monat (deutsch, z.B. _September)", lambda r, d: f"_{_MONTHS_DE[d.month - 1]}"),
    "month_year": ("_Monat_Jahr (z.B. _September_2025)", lambda r, d: f"_{_MONTHS_DE[d.month - 1]}_{d.year}"),
    "mm":         ("_MM (Monatszahl, z.B. _09)",       lambda r, d: f"_{d.month:02d}"),
    "mm_yyyy":    ("_MM_YYYY (z.B. _09_2025)",          lambda r, d: f"_{d.month:02d}_{d.year}"),
    "yyyy":       ("_YYYY (nur Jahr, z.B. _2025)",     lambda r, d: f"_{d.year}"),
    "kw":         ("_KWnn (Kalenderwoche, z.B. _KW33)", lambda r, d: f"_KW{d.isocalendar()[1]:02d}"),
    "kw_yyyy":    ("_KWnn_YYYY (z.B. _KW33_2025)",      lambda r, d: f"_KW{d.isocalendar()[1]:02d}_{d.year}"),
    "v":          ("_vN (Version 1-12)",                lambda r, d: f"_v{r.randint(1, 12)}"),
    "rev":        ("_RevN (Revision 1-5)",              lambda r, d: f"_Rev{r.randint(1, 5)}"),
    "num":        ("_NNN (3-stellige Zufallsnummer)",   lambda r, d: f"_{r.randint(100, 999)}"),
}
DEFAULT_ACTIVE_SUFFIXES = ["none", "mm", "kw", "month_de"]


def resolve_ref_date(month: str = "", year: str = "") -> date:
    """Auto → heute. Sonst month (1-12 or DE name) + year kombiniert.
    Fehlender Wert = current."""
    today = date.today()
    m = today.month
    y = today.year
    if month and month.lower() not in ("", "auto"):
        try:
            m = int(month)
        except ValueError:
            for i, name in enumerate(_MONTHS_DE, 1):
                if name.lower() == month.lower():
                    m = i
                    break
    if year and year.lower() not in ("", "auto"):
        try:
            y = int(year)
        except ValueError:
            pass
    m = max(1, min(m, 12))
    y = max(1980, min(y, 2099))
    try:
        return date(y, m, min(today.day, 28))
    except ValueError:
        return date(y, m, 1)


def build_filename(pool: list, rng: random.Random,
                    active_suffix_types: list, ref_date: date) -> str:
    """base_name + random pick aus aktiven Suffix-Typen + .pdf."""
    if not pool:
        pool = FILENAME_POOL
    if not active_suffix_types:
        active_suffix_types = ["none"]
    base = rng.choice(pool)
    stype = rng.choice(active_suffix_types)
    _, fn = SUFFIX_TYPES.get(stype, SUFFIX_TYPES["none"])
    suffix = fn(rng, ref_date)
    fname = base + suffix
    # umlauts → ascii fürs Dateisystem
    fname = (fname.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
                    .replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
                    .replace("ß", "ss"))
    return fname + ".pdf"


# ── Metadata rewrite ─────────────────────────────────────

def _pdf_date(dt: datetime) -> str:
    return f"D:{dt.strftime('%Y%m%d%H%M%S')}+00'00'"


def rewrite_metadata(pdf, pools: PoolBag, rng: random.Random):
    """Ersetzt Doc-Info + Trailer-ID + PDF-Version + XMP."""
    # Doc-Info
    di = pdf.docinfo
    for key in list(di.keys()):
        try:
            del di[key]
        except Exception:
            pass
    di[Name.Title] = String(rng.choice(pools.title))
    di[Name.Author] = String(rng.choice(pools.author))
    di[Name.Subject] = String(rng.choice(pools.subject))
    di[Name.Keywords] = String(", ".join(rng.sample(pools.keywords,
                                                     k=min(4, len(pools.keywords)))))
    di[Name.Creator] = String(rng.choice(pools.creator))
    di[Name.Producer] = String(rng.choice(pools.producer))
    base_dt = datetime.now() - timedelta(days=rng.randint(1, 90),
                                          hours=rng.randint(0, 23),
                                          minutes=rng.randint(0, 59))
    mod_dt = base_dt + timedelta(hours=rng.randint(1, 48))
    di[Name.CreationDate] = String(_pdf_date(base_dt))
    di[Name.ModDate] = String(_pdf_date(mod_dt))

    # Trailer ID
    try:
        pdf.trailer[Name.ID] = Array([
            String(secrets.token_bytes(16)),
            String(secrets.token_bytes(16)),
        ])
    except Exception:
        pass

    # PDF-Version rotieren (safe range: die Source-Version oder höher)
    try:
        src_ver = float(pdf.pdf_version) if pdf.pdf_version else 1.4
        candidates = [v for v in ("1.4", "1.5", "1.6", "1.7") if float(v) >= src_ver]
        if candidates:
            pdf.pdf_version = rng.choice(candidates)
    except Exception:
        pass

    # XMP komplett neu schreiben (fresh UUID, matched mit Doc-Info)
    try:
        with pdf.open_metadata() as meta:
            meta.load_from_docinfo(di)
            meta["xmp:CreatorTool"] = str(di.get(Name.Creator, ""))
            meta["xmp:CreateDate"] = base_dt.isoformat()
            meta["xmp:ModifyDate"] = mod_dt.isoformat()
            meta["xmpMM:DocumentID"] = f"uuid:{_random_uuid(rng)}"
            meta["xmpMM:InstanceID"] = f"uuid:{_random_uuid(rng)}"
    except Exception as e:
        logger.debug("XMP-Rewrite failed: %s", e)


def _random_uuid(rng: random.Random) -> str:
    h = "".join(rng.choice("0123456789abcdef") for _ in range(32))
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# ── Struktur-Scrub (Kimi-inspired) ───────────────────────

_ACTUALTEXT_RE = re.compile(rb"/ActualText\s*(\((?:[^()\\]|\\.)*\)|<[0-9A-Fa-f\s]*>)")


def scrub_structure(pdf):
    """Entfernt Outlines/Names/AcroForm/StructTree/Annots/JS/ActualText.
    Verhindert dass Filter über strukturelle Fingerprints scoren."""
    root = pdf.Root
    for key in ("/PieceInfo", "/Outlines", "/Names", "/OpenAction",
                "/AA", "/AcroForm", "/StructTreeRoot", "/MarkInfo",
                "/Perms", "/Collection", "/PageLabels", "/Threads"):
        if key in root:
            try:
                del root[key]
            except Exception:
                pass

    for page in pdf.pages:
        for k in ("/StructParents", "/AA", "/Annots", "/Tabs"):
            if k in page:
                try:
                    del page[k]
                except Exception:
                    pass
        contents = page.get("/Contents")
        if contents is None:
            continue
        streams = contents if isinstance(contents, Array) else [contents]
        for s in streams:
            try:
                data = s.read_bytes()
                data, n = _ACTUALTEXT_RE.subn(b"/ActualText ()", data)
                if n:
                    s.write(data)
            except Exception:
                continue


# ── Image manipulation ───────────────────────────────────

def _iter_image_objects(pdf):
    """Iteriert alle Bild-XObjects im ganzen Dokument."""
    seen = set()
    for page in pdf.pages:
        try:
            resources = page.get("/Resources")
            if not resources:
                continue
            xobjects = resources.get("/XObject")
            if not xobjects:
                continue
            for name, obj in xobjects.items():
                if id(obj) in seen:
                    continue
                seen.add(id(obj))
                try:
                    if obj.get("/Subtype") == Name("/Image"):
                        yield obj
                except Exception:
                    continue
        except Exception:
            continue


def tweak_images(pdf, rng: random.Random) -> int:
    """Für jedes Bild: 1 Pixel ±1 nudgen + (bei JPEG) mit random Quality re-encoden.
    Bricht bei individuellen Bildern still ab wenn's nicht funktioniert —
    im schlimmsten Fall bleibt das Bild unverändert, kein Layout-Break."""
    if Image is None:
        return 0
    n_touched = 0
    for img_obj in _iter_image_objects(pdf):
        try:
            pi = PdfImage(img_obj)
            pil_img = pi.as_pil_image()
            if pil_img.mode not in ("RGB", "RGBA", "L"):
                pil_img = pil_img.convert("RGB")
            w, h = pil_img.size
            if w < 3 or h < 3:
                continue
            # 1 Pixel nudge (nicht am Rand)
            x = rng.randint(1, w - 2)
            y = rng.randint(1, h - 2)
            px = list(pil_img.getpixel((x, y)))
            for i in range(min(3, len(px))):
                delta = rng.choice([-1, 1])
                px[i] = max(0, min(255, int(px[i]) + delta))
            pil_img.putpixel((x, y), tuple(px))

            # Re-encode als JPEG mit random Quality — nur wenn Original DCT war
            filters = img_obj.get("/Filter")
            was_jpeg = False
            if filters:
                fs = [str(f) for f in (filters if isinstance(filters, Array) else [filters])]
                was_jpeg = any("DCT" in f for f in fs)
            if was_jpeg:
                buf = io.BytesIO()
                if pil_img.mode == "RGBA":
                    pil_img = pil_img.convert("RGB")
                pil_img.save(buf, "JPEG", quality=rng.randint(85, 95), optimize=True)
                # Replace stream in place — behalte Dictionary, tausche Bytes
                img_obj.write(buf.getvalue(), filter=Name("/DCTDecode"))
                n_touched += 1
            # PNG/andere: nur Pixel-Nudge ohne Re-Encode ist riskant weil pikepdf
            # das Bild dann re-serialisiert. Skip für MVP.
        except Exception as e:
            logger.debug("Image-Tweak fail: %s", e)
            continue
    return n_touched


# ── Byte-Level noise ─────────────────────────────────────

def add_eof_padding(pdf_bytes: bytes, rng: random.Random) -> bytes:
    """Random 8-32 Byte Padding NACH %%EOF — von PDF-Spec toleriert,
    Fuzzy-Hasher kriegen anderen Input."""
    n = rng.randint(8, 32)
    pad = bytes(rng.randint(0, 255) for _ in range(n))
    # Zwischen 2 Newlines packen damit's als "garbage after EOF" harmlos bleibt
    return pdf_bytes + b"\n%" + pad.hex().encode() + b"\n"


# ── ToUnicode CMap poison (Kimi-Port) ────────────────────

_HEX_RE = re.compile(r"<([0-9A-Fa-f]+)>")
_POOLS = [(0x41, 0x5A), (0x61, 0x7A), (0xC0, 0xFF), (0x100, 0x17F),
          (0x391, 0x3C9), (0x410, 0x44F)]


def _rand_cp(rng):
    a, b = rng.choice(_POOLS)
    return rng.randint(a, b)


def _rand_hex(n_units, rng):
    return "".join(f"{_rand_cp(rng):04X}" for _ in range(n_units))


def _replace_last(line, old, new):
    idx = line.rfind(old)
    return line[:idx] + new + line[idx + len(old):] if idx >= 0 else line


def _scramble_cmap(data: bytes, rng: random.Random) -> bytes:
    text = data.decode("latin-1", "replace")
    out, state = [], None
    for line in text.split("\n"):
        low = line.strip().lower()
        if "beginbfchar" in low:
            state = "bfchar"
        elif "beginbfrange" in low:
            state = "bfrange"
        elif "endbfchar" in low or "endbfrange" in low:
            state = None
        elif state == "bfchar":
            toks = _HEX_RE.findall(line)
            if len(toks) >= 2:
                dst = toks[-1]
                line = _replace_last(line, "<" + dst + ">",
                                     "<" + _rand_hex(len(dst) // 4 or 1, rng) + ">")
        elif state == "bfrange":
            if "[" in line:
                head, rest = line.split("[", 1)
                for t in _HEX_RE.findall(rest):
                    rest = rest.replace("<" + t + ">",
                                        "<" + _rand_hex(len(t) // 4 or 1, rng) + ">", 1)
                line = head + "[" + rest
            else:
                toks = _HEX_RE.findall(line)
                if len(toks) >= 3:
                    dst = toks[-1]
                    line = _replace_last(line, "<" + dst + ">",
                                         "<" + _rand_hex(len(dst) // 4 or 1, rng) + ">")
        out.append(line)
    return "\n".join(out).encode("latin-1")


def poison_cmaps(pdf, rng: random.Random) -> int:
    """ToUnicode-CMaps aller Fonts scramblen. Text visuell 1:1, Extraktion
    liefert falschen Zeichensalz. Empfänger-Copy-Paste kaputt — nur nutzen
    wenn du sicher bist dass niemand Text aus dem PDF kopiert!"""
    n = 0
    for obj in list(pdf.objects):
        try:
            if obj.get("/Type") == Name("/Font") and "/ToUnicode" in obj:
                raw = obj["/ToUnicode"].read_bytes()
                obj["/ToUnicode"] = Stream(pdf, _scramble_cmap(raw, rng))
                n += 1
        except Exception:
            continue
    return n


# ── Hidden text (weiß auf weiß auf letzter Seite) ────────

def inject_hidden_text(pdf, rng: random.Random) -> bool:
    """Klebt 2-3 unsichtbare weiße Wörter auf die letzte Seite via
    Standard-Helvetica (kein Font-Embedding nötig)."""
    try:
        page = pdf.pages[-1]
        resources = page.get("/Resources")
        if resources is None:
            page["/Resources"] = Dictionary()
            resources = page["/Resources"]
        fonts = resources.get("/Font")
        if fonts is None:
            resources["/Font"] = Dictionary()
            fonts = resources["/Font"]
        # AbF = "antibot font" — eindeutiger Name, unwahrscheinlich Konflikt
        if "/AbF" not in fonts:
            font_obj = pdf.make_indirect(Dictionary({
                "/Type": Name("/Font"),
                "/Subtype": Name("/Type1"),
                "/BaseFont": Name("/Helvetica"),
            }))
            fonts["/AbF"] = font_obj

        words = " ".join(rng.choice(HIDDEN_TEXT_POOL)
                         for _ in range(rng.randint(2, 4)))
        # ASCII-only für simple Tj-Encoding
        words = "".join(c for c in words if 32 <= ord(c) < 127)
        x = rng.randint(20, 200)
        y = rng.randint(20, 100)
        payload = (f"q 1 1 1 rg 1 1 1 RG BT /AbF 6 Tf {x} {y} Td "
                   f"({words}) Tj ET Q\n").encode("latin-1")
        new_stream = Stream(pdf, payload)
        cur = page.get("/Contents")
        if cur is None:
            page["/Contents"] = new_stream
        elif isinstance(cur, Array):
            cur.append(new_stream)
        else:
            page["/Contents"] = Array([cur, new_stream])
        return True
    except Exception as e:
        logger.debug("Hidden-text fail: %s", e)
        return False


# ── Main variator ────────────────────────────────────────

class PDFVariator:
    def __init__(self, source_bytes: bytes, layers: LayerSet = None,
                 pools: PoolBag = None, active_suffix_types: list = None,
                 ref_date: date = None):
        if pikepdf is None:
            raise RuntimeError("pikepdf nicht installiert — bitte requirements.txt aktualisieren.")
        self.source = source_bytes
        self.layers = layers or LayerSet()
        self.pools = pools or PoolBag.default()
        self.active_suffix_types = active_suffix_types or DEFAULT_ACTIVE_SUFFIXES
        self.ref_date = ref_date or date.today()

    def make_variant(self, seed: int) -> tuple[str, bytes]:
        """Erzeugt eine einzige Variante. Rückgabe: (filename, pdf_bytes)."""
        rng = random.Random(seed if seed >= 0 else secrets.randbits(64))
        pdf = Pdf.open(io.BytesIO(self.source))

        try:
            if self.layers.metadata:
                rewrite_metadata(pdf, self.pools, rng)
            if self.layers.structure:
                scrub_structure(pdf)
            if self.layers.image:
                tweak_images(pdf, rng)
            if self.layers.cmap_poison:
                poison_cmaps(pdf, rng)
            if self.layers.hidden_text:
                inject_hidden_text(pdf, rng)

            buf = io.BytesIO()
            # Object-Reorder + recompression per pikepdf save-Options
            save_kwargs = {"linearize": False}
            if self.layers.byte_noise:
                save_kwargs["object_stream_mode"] = pikepdf.ObjectStreamMode.generate \
                    if rng.random() < 0.5 else pikepdf.ObjectStreamMode.disable
                save_kwargs["recompress_flate"] = True
                save_kwargs["compress_streams"] = True
            pdf.save(buf, **save_kwargs)
            data = buf.getvalue()
        finally:
            pdf.close()

        if self.layers.byte_noise:
            data = add_eof_padding(data, rng)

        filename = build_filename(
            self.pools.filename, rng,
            self.active_suffix_types, self.ref_date,
        ) if self.layers.filename else f"variant_{seed}.pdf"
        return filename, data

    def compare_variants(self, count: int = 3) -> list:
        """Test-Report: N Varianten + Hashes zurück."""
        out = []
        src_md5 = hashlib.md5(self.source).hexdigest()
        src_sha = hashlib.sha256(self.source).hexdigest()
        out.append({
            "which": "source", "filename": "(source)",
            "size": len(self.source),
            "md5": src_md5, "sha256": src_sha,
        })
        for i in range(count):
            fname, data = self.make_variant(seed=i)
            out.append({
                "which": f"v{i+1}", "filename": fname,
                "size": len(data),
                "md5": hashlib.md5(data).hexdigest(),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
        return out
