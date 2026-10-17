"""PDF Variator — Web-UI + Streaming-ZIP-Download.

Keine Server-seitige Speicherung: Source-PDF landet nur im RAM, jede
Variante wird generiert → in ZIP-Stream gepusht → RAM freigegeben.
Zu jedem Zeitpunkt max ~2 Varianten im Speicher.
"""
import io
import struct
import time
import zlib
import logging
from html import escape
from typing import Optional

from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse, PlainTextResponse


# ── True-Streaming-ZIP-Writer ───────────────────────────────────────
# zipfile.ZipFile braucht seekable file objects — beim Truncate-Buffer
# Ansatz landen falsche Central-Directory-Offsets im Output → Archiv-
# Fehler beim Öffnen. Deshalb ZIP hand-rolled, format ist simpel genug.

_LOCAL_HDR_SIG = b"PK\x03\x04"
_CENTRAL_DIR_SIG = b"PK\x01\x02"
_END_OF_CD_SIG = b"PK\x05\x06"


def _dos_datetime(ts=None) -> tuple:
    t = time.localtime(ts)
    dos_time = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
    dos_date = ((max(t.tm_year, 1980) - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
    return dos_time, dos_date


class StreamingZipWriter:
    """Kein seek() nötig — jedes add_file yields die vollständigen Bytes
    (local header + data). finalize() yields Central-Directory + EOCD.
    Alle Files STORED (unkomprimiert) — PDFs sind eh intern schon
    komprimiert."""

    def __init__(self):
        self._entries = []  # list of dicts für CD-Bau
        self._offset = 0     # aktuelle Byte-Position im Stream

    def add_file(self, filename: str, data: bytes) -> bytes:
        fname_bytes = filename.encode("utf-8")
        flags = 0x0800 if any(b > 0x7F for b in fname_bytes) else 0
        crc = zlib.crc32(data) & 0xFFFFFFFF
        size = len(data)
        dt, dd = _dos_datetime()

        local_hdr = struct.pack(
            "<4sHHHHHIIIHH",
            _LOCAL_HDR_SIG,
            20,     # version needed to extract (2.0 = STORED)
            flags,
            0,      # compression method (STORED)
            dt, dd,
            crc,
            size,   # compressed size
            size,   # uncompressed size
            len(fname_bytes),
            0,      # extra field length
        )
        self._entries.append({
            "filename": fname_bytes, "crc": crc, "size": size,
            "dos_time": dt, "dos_date": dd, "offset": self._offset,
            "flags": flags,
        })
        chunk = local_hdr + fname_bytes + data
        self._offset += len(chunk)
        return chunk

    def finalize(self) -> bytes:
        cd_offset = self._offset
        cd_bytes = b""
        for e in self._entries:
            cd_entry = struct.pack(
                "<4sHHHHHHIIIHHHHHII",
                _CENTRAL_DIR_SIG,
                20,             # version made by
                20,             # version needed
                e["flags"],
                0,              # method (STORED)
                e["dos_time"], e["dos_date"],
                e["crc"],
                e["size"],      # compressed
                e["size"],      # uncompressed
                len(e["filename"]),
                0,              # extra len
                0,              # comment len
                0,              # disk number start
                0,              # internal attrs
                0,              # external attrs
                e["offset"],    # local header offset
            )
            cd_bytes += cd_entry + e["filename"]

        eocd = struct.pack(
            "<4sHHHHIIH",
            _END_OF_CD_SIG,
            0, 0,               # disk numbers
            len(self._entries), len(self._entries),
            len(cd_bytes), cd_offset,
            0,                  # comment length
        )
        return cd_bytes + eocd

logger = logging.getLogger("bulk.pdf_variator")
router = APIRouter()


def _parse_layers(**kw) -> "LayerSet":
    from bulk.mailer.pdf_variator import LayerSet
    ls = LayerSet()
    # A/B/C sind immer an — keine Toggles hier
    ls.image = bool(kw.get("layer_image"))
    ls.byte_noise = bool(kw.get("layer_byte_noise"))
    ls.cmap_poison = bool(kw.get("layer_cmap_poison"))
    ls.hidden_text = bool(kw.get("layer_hidden_text"))
    return ls


def _parse_pools(**kw) -> "PoolBag":
    from bulk.mailer.pdf_variator import PoolBag
    return PoolBag.from_dict({
        "filename": kw.get("pool_filename", ""),
        "producer": kw.get("pool_producer", ""),
        "creator": kw.get("pool_creator", ""),
        "author": kw.get("pool_author", ""),
        "title": kw.get("pool_title", ""),
        "subject": kw.get("pool_subject", ""),
        "keywords": kw.get("pool_keywords", ""),
    })


_POOL_KEYS = ("filename", "title", "producer", "creator",
              "author", "subject", "keywords")


def _parse_suffixes(form_getter) -> list:
    """Sammelt aktive Suffix-Typen aus dem Formular. Wenn nichts checked
    ist, fallback auf 'none' (nur Basename) damit's nie leer bleibt."""
    from bulk.mailer.pdf_variator import SUFFIX_TYPES
    active = [k for k in SUFFIX_TYPES if form_getter(f"suffix_{k}")]
    return active or ["none"]


def _resolve_pools(db) -> dict:
    """Liefert für jeden Pool den gespeicherten Inhalt, oder — falls
    nichts gespeichert wurde — den Default aus dem pdf_variator-Modul."""
    from bulk.mailer.pdf_variator import (FILENAME_POOL, PRODUCER_POOL,
                                            CREATOR_POOL, AUTHOR_POOL,
                                            TITLE_POOL, SUBJECT_POOL,
                                            KEYWORDS_POOL)
    defaults = {
        "filename": "\n".join(FILENAME_POOL),
        "title":    "\n".join(TITLE_POOL),
        "producer": "\n".join(PRODUCER_POOL),
        "creator":  "\n".join(CREATOR_POOL),
        "author":   "\n".join(AUTHOR_POOL),
        "subject":  "\n".join(SUBJECT_POOL),
        "keywords": ", ".join(KEYWORDS_POOL),
    }
    saved = db.get_variator_pools() if hasattr(db, "get_variator_pools") else {}
    return {k: (saved.get(k) or defaults[k]) for k in _POOL_KEYS}


@router.get("/pdf-variator", response_class=HTMLResponse)
async def pdf_variator_page(request: Request):
    try:
        import pikepdf  # noqa
        pikepdf_ok = True
        pike_ver = pikepdf.__version__
    except ImportError:
        pikepdf_ok = False
        pike_ver = None

    from bulk.mailer.pdf_variator import SUFFIX_TYPES, DEFAULT_ACTIVE_SUFFIXES, _MONTHS_DE
    from datetime import date
    today = date.today()

    db = request.app.state.db
    pools = _resolve_pools(db)
    saved_flag = "1" if db.get_variator_pools() else ""

    ctx = {
        "active": "pdf_variator",
        "pikepdf_ok": pikepdf_ok, "pike_ver": pike_ver,
        "pools_saved": saved_flag,
        "suffix_types": [(k, label) for k, (label, _) in SUFFIX_TYPES.items()],
        "default_active_suffixes": DEFAULT_ACTIVE_SUFFIXES,
        "months_de": list(enumerate(_MONTHS_DE, 1)),
        "current_month": today.month,
        "current_month_de": _MONTHS_DE[today.month - 1],
        "current_year": today.year,
        "current_kw": today.isocalendar()[1],
    }
    for k in _POOL_KEYS:
        ctx[f"pool_{k}"] = pools[k]
    return request.app.state.templates.TemplateResponse(request, "pdf_variator.html", ctx)


@router.post("/pdf-variator/pools/save", response_class=HTMLResponse)
async def pools_save(request: Request,
                      pool_filename: str = Form(""),
                      pool_producer: str = Form(""),
                      pool_creator: str = Form(""),
                      pool_author: str = Form(""),
                      pool_title: str = Form(""),
                      pool_subject: str = Form(""),
                      pool_keywords: str = Form("")):
    db = request.app.state.db
    to_save = {
        "filename": pool_filename.strip(),
        "producer": pool_producer.strip(),
        "creator":  pool_creator.strip(),
        "author":   pool_author.strip(),
        "title":    pool_title.strip(),
        "subject":  pool_subject.strip(),
        "keywords": pool_keywords.strip(),
    }
    db.save_variator_pools(to_save)
    return HTMLResponse(
        '<span style="color:var(--green);font-size:12px">✓ Pools gespeichert. '
        'Werden beim nächsten Reload automatisch geladen.</span>'
    )


@router.post("/pdf-variator/pools/reset", response_class=HTMLResponse)
async def pools_reset(request: Request):
    db = request.app.state.db
    db.reset_variator_pools()
    return HTMLResponse(
        '<span style="color:var(--green);font-size:12px">✓ Auf Defaults zurückgesetzt. '
        '<a href="/pdf-variator">Reload</a> um die Textareas neu zu befüllen.</span>'
    )


@router.post("/pdf-variator/test-report", response_class=HTMLResponse)
async def test_report(request: Request):
    try:
        from bulk.mailer.pdf_variator import PDFVariator, resolve_ref_date
    except Exception as e:
        return HTMLResponse(f'<div class="alert alert-danger">pikepdf fehlt: {escape(str(e))}</div>')

    form = await request.form()
    source = form.get("source")
    if source is None or not hasattr(source, "read"):
        return HTMLResponse('<div class="alert alert-warning">PDF fehlt.</div>')
    src = await source.read()
    if not src or len(src) > 50 * 1024 * 1024:
        return HTMLResponse('<div class="alert alert-warning">PDF fehlt oder > 50 MB.</div>')
    if not src.startswith(b"%PDF"):
        return HTMLResponse('<div class="alert alert-warning">Kein %PDF-Header.</div>')

    layers = _parse_layers(
        layer_image=form.get("layer_image", ""),
        layer_byte_noise=form.get("layer_byte_noise", ""),
        layer_cmap_poison=form.get("layer_cmap_poison", ""),
        layer_hidden_text=form.get("layer_hidden_text", ""),
    )
    pools = _parse_pools(
        pool_filename=form.get("pool_filename", ""),
        pool_producer=form.get("pool_producer", ""),
        pool_creator=form.get("pool_creator", ""),
        pool_author=form.get("pool_author", ""),
        pool_title=form.get("pool_title", ""),
        pool_subject=form.get("pool_subject", ""),
        pool_keywords=form.get("pool_keywords", ""),
    )
    active_suffixes = _parse_suffixes(form.get)
    ref_date = resolve_ref_date(form.get("ref_month", ""), form.get("ref_year", ""))

    try:
        v = PDFVariator(src, layers=layers, pools=pools,
                         active_suffix_types=active_suffixes, ref_date=ref_date)
        rows = v.compare_variants(3)
    except Exception as e:
        logger.exception("test-report crashed")
        return HTMLResponse(f'<div class="alert alert-danger">Fehler: {escape(str(e))}</div>')

    rows_html = []
    for r in rows:
        rows_html.append(
            f'<tr>'
            f'<td>{escape(r["which"])}</td>'
            f'<td style="font-family:monospace;font-size:11px">{escape(r["filename"])}</td>'
            f'<td>{r["size"]:,}</td>'
            f'<td style="font-family:monospace;font-size:10px">{escape(r["md5"])}</td>'
            f'<td style="font-family:monospace;font-size:10px">{escape(r["sha256"][:24])}…</td>'
            f'</tr>'
        )
    all_hashes = [r["md5"] for r in rows[1:]]  # nur die Varianten
    all_unique = len(set(all_hashes)) == len(all_hashes)
    verdict_html = (
        '<div class="alert alert-success">✓ Alle 3 Varianten haben unterschiedliche MD5s.</div>'
        if all_unique else
        '<div class="alert alert-danger">✗ Duplikate! Layer nachziehen (Image + Byte-Noise aktivieren).</div>'
    )
    return HTMLResponse(
        verdict_html +
        '<table style="font-size:11px"><thead>'
        '<tr><th>#</th><th>Filename</th><th>Bytes</th><th>MD5</th><th>SHA256 (Kurz)</th></tr>'
        '</thead><tbody>' + "".join(rows_html) + '</tbody></table>'
        '<p class="muted" style="margin-top:6px">'
        'Wenn die MD5s der 3 Varianten schon so weit auseinander sind → auch '
        'ssdeep/tlsh/pHash werden es. Kein Grund vorher mit größeren Batches zu testen.'
        '</p>'
    )


@router.post("/pdf-variator/generate")
async def generate(request: Request):
    try:
        from bulk.mailer.pdf_variator import PDFVariator, resolve_ref_date
    except Exception as e:
        return PlainTextResponse(f"pikepdf fehlt: {e}", status_code=500)

    form = await request.form()
    source = form.get("source")
    if source is None or not hasattr(source, "read"):
        return PlainTextResponse("PDF fehlt", status_code=400)
    src = await source.read()
    if not src or len(src) > 50 * 1024 * 1024:
        return PlainTextResponse("PDF fehlt oder > 50 MB", status_code=400)
    if not src.startswith(b"%PDF"):
        return PlainTextResponse("Kein %PDF-Header", status_code=400)

    count = max(1, min(int(form.get("count") or 100), 1000))
    layers = _parse_layers(
        layer_image=form.get("layer_image", ""),
        layer_byte_noise=form.get("layer_byte_noise", ""),
        layer_cmap_poison=form.get("layer_cmap_poison", ""),
        layer_hidden_text=form.get("layer_hidden_text", ""),
    )
    pools = _parse_pools(
        pool_filename=form.get("pool_filename", ""),
        pool_producer=form.get("pool_producer", ""),
        pool_creator=form.get("pool_creator", ""),
        pool_author=form.get("pool_author", ""),
        pool_title=form.get("pool_title", ""),
        pool_subject=form.get("pool_subject", ""),
        pool_keywords=form.get("pool_keywords", ""),
    )
    active_suffixes = _parse_suffixes(form.get)
    ref_date = resolve_ref_date(form.get("ref_month", ""), form.get("ref_year", ""))
    variator = PDFVariator(src, layers=layers, pools=pools,
                            active_suffix_types=active_suffixes, ref_date=ref_date)

    # Filename-Kollisionen aktiv vermeiden — pro ZIP kein Duplikat
    used_names = set()

    def _generate_stream():
        """Yield chunks eines echten Streaming-ZIPs. Peak-RAM = 1 Variante."""
        zw = StreamingZipWriter()
        for i in range(count):
            try:
                fname, pdf_bytes = variator.make_variant(seed=i)
            except Exception as e:
                logger.warning("Variant %d failed: %s", i, e)
                continue
            base_fname = fname
            n = 1
            while fname in used_names:
                stem, ext = base_fname.rsplit(".", 1)
                fname = f"{stem}_{n}.{ext}"
                n += 1
            used_names.add(fname)
            yield zw.add_file(fname, pdf_bytes)
        yield zw.finalize()

    return StreamingResponse(
        _generate_stream(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename=pdf_variants_{count}.zip'},
    )
