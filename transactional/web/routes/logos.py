"""Logos — upload source logos, generate variants to separate dir, cleanup."""
import os
import time
import shutil
import threading
import logging
import secrets
from html import escape
from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import List as TList

logger = logging.getLogger("trans.logos")
router = APIRouter()

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "..", "static", "uploads", "logos")
VARIANT_DIR = os.path.join(os.path.dirname(__file__), "..", "static", "uploads", "logo_variants")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(VARIANT_DIR, exist_ok=True)

ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

_variant_progress = {"running": False, "done": 0, "total": 0, "count": 0, "error": ""}


def _group_variant_dir(group_id: int = 0) -> str:
    if group_id:
        d = os.path.join(VARIANT_DIR, f"group_{group_id}")
    else:
        d = VARIANT_DIR
    os.makedirs(d, exist_ok=True)
    return d


def _resolve_path(file_path: str) -> str:
    if file_path.startswith("/static/"):
        return os.path.normpath(os.path.join(
            os.path.dirname(__file__), "..", file_path.lstrip("/")))
    return file_path


def get_variant_count() -> int:
    try:
        count = 0
        for root, dirs, files in os.walk(VARIANT_DIR):
            count += sum(1 for f in files if not f.startswith("."))
        return count
    except OSError:
        return 0


def get_group_variant_count(group_id: int) -> int:
    d = _group_variant_dir(group_id)
    try:
        return sum(1 for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)))
    except OSError:
        return 0


def clear_variants():
    """Löscht NUR Files direkt im globalen VARIANT_DIR — group_X/ subdirs
    bleiben unangetastet, damit Kampagnen mit logo_group_id nicht ihre
    Varianten verlieren."""
    try:
        for name in os.listdir(VARIANT_DIR):
            p = os.path.join(VARIANT_DIR, name)
            if os.path.isfile(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass
    except OSError:
        pass


@router.get("/logos", response_class=HTMLResponse)
async def logos_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    logos = [dict(l) for l in db.get_logos(uid)]
    import json as _json
    logo_groups = [dict(g) for g in db.get_logo_groups(uid)]
    for g in logo_groups:
        g["variant_count"] = get_group_variant_count(g["id"])
        try:
            g["cdn_count"] = len(_json.loads(g.get("cdn_urls_json") or "[]"))
        except Exception:
            g["cdn_count"] = 0
    return request.app.state.templates.TemplateResponse(request, "logos.html", {
        "active": "logos", "logos": logos, "logo_groups": logo_groups, "db": db,
        "variant_running": _variant_progress["running"],
        "variant_count": get_variant_count(),
    })


@router.post("/logos/add-group")
async def add_logo_group(request: Request, name: str = Form("")):
    if name.strip():
        uid = request.state.user["id"]
        request.app.state.db.add_logo_group(name.strip(), uid)
    return RedirectResponse("/logos", status_code=303)


@router.post("/logos/group/{gid}/delete")
async def delete_logo_group(request: Request, gid: int):
    request.app.state.db.delete_logo_group(gid)
    return RedirectResponse("/logos", status_code=303)


@router.post("/logos/upload")
async def upload_logos(request: Request):
    form = await request.form()
    files = form.getlist("files")
    group_id = int(form.get("group_id", 0) or 0)
    db = request.app.state.db
    uid = request.state.user['id']
    for f in files:
        if not hasattr(f, "read") or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_EXT:
            continue
        data = await f.read()
        if not data or len(data) > 5 * 1024 * 1024:
            continue
        safe = f"{int(time.time())}_{secrets.token_hex(4)}{ext}"
        dest = os.path.join(UPLOAD_DIR, safe)
        with open(dest, "wb") as fh:
            fh.write(data)
        db.add_logo(f.filename, f"/static/uploads/logos/{safe}", uid, group_id)
    return RedirectResponse("/logos", status_code=303)


@router.post("/logos/{lid}/delete")
async def delete_logo(request: Request, lid: int):
    db = request.app.state.db
    row = db._conn().execute("SELECT * FROM trans_logos WHERE id=?", (lid,)).fetchone()
    if row:
        abs_path = _resolve_path(dict(row).get("file_path", ""))
        if os.path.isfile(abs_path):
            try:
                os.unlink(abs_path)
            except OSError:
                pass
        db.delete_logo(lid)
    return RedirectResponse("/logos", status_code=303)


@router.post("/logos/generate-variants", response_class=HTMLResponse)
async def generate_variants(request: Request, variant_count: int = Form(25),
                             group_id: int = Form(0)):
    if _variant_progress["running"]:
        return HTMLResponse('<div class="alert alert-warning">Already running.</div>')

    db = request.app.state.db
    uid = request.state.user['id']
    if group_id:
        logos = [dict(l) for l in db.get_logos_by_group(group_id)]
    else:
        logos = [dict(l) for l in db.get_logos(uid)]
    if not logos:
        return HTMLResponse('<div class="alert alert-warning">No logos in this group.</div>')

    variant_dir = _group_variant_dir(group_id)
    if os.path.isdir(variant_dir):
        if group_id == 0:
            # NUR direkte Files löschen — sonst würden alle
            # group_X/ Subdirs mitgerissen (deren Varianten von anderen
            # Kampagnen aktiv genutzt werden).
            for name in os.listdir(variant_dir):
                p = os.path.join(variant_dir, name)
                if os.path.isfile(p):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
        else:
            shutil.rmtree(variant_dir)
            os.makedirs(variant_dir, exist_ok=True)
    else:
        os.makedirs(variant_dir, exist_ok=True)

    per_logo = max(1, variant_count // len(logos))
    _variant_progress.update(running=True, done=0, total=len(logos) * per_logo, count=0, error="")

    def worker():
        try:
            from PIL import Image, ImageEnhance
        except ImportError:
            _variant_progress["error"] = "Pillow not installed"
            _variant_progress["running"] = False
            return

        import random
        generated = 0
        try:
            for logo in logos:
                abs_path = _resolve_path(logo.get("file_path", ""))
                if not os.path.isfile(abs_path):
                    continue
                try:
                    img = Image.open(abs_path)
                    if img.mode == "P":
                        img = img.convert("RGBA")
                    elif img.mode not in ("RGB", "RGBA"):
                        img = img.convert("RGB")

                    ext = os.path.splitext(abs_path)[1].lower()
                    if ext not in (".png", ".jpg", ".jpeg"):
                        ext = ".png"
                    fmt = "PNG" if ext == ".png" else "JPEG"

                    for v in range(per_logo):
                        variant = img.copy()
                        variant = ImageEnhance.Brightness(variant).enhance(
                            1.0 + random.uniform(-0.05, 0.05))
                        variant = ImageEnhance.Color(variant).enhance(
                            1.0 + random.uniform(-0.08, 0.08))
                        variant = ImageEnhance.Contrast(variant).enhance(
                            1.0 + random.uniform(-0.03, 0.03))

                        pixels = variant.load()
                        w, h = variant.size
                        px = random.randint(0, w - 1)
                        py = random.randint(0, h - 1)
                        pixel = pixels[px, py]
                        if len(pixel) == 4:
                            p = list(pixel[:3])
                            p[random.randint(0, 2)] = min(255, max(0, p[random.randint(0, 2)] + random.choice([-2, -1, 1, 2])))
                            pixels[px, py] = (p[0], p[1], p[2], pixel[3])
                        elif len(pixel) == 3:
                            p = list(pixel)
                            p[random.randint(0, 2)] = min(255, max(0, p[random.randint(0, 2)] + random.choice([-2, -1, 1, 2])))
                            pixels[px, py] = (p[0], p[1], p[2])

                        if ext == ".png":
                            try:
                                from PIL import Image as PILImage
                                method = (PILImage.Quantize.FASTOCTREE if variant.mode == "RGBA"
                                          else PILImage.Quantize.MEDIANCUT)
                                variant = variant.quantize(colors=256, method=method)
                            except Exception:
                                pass

                        vname = f"v_{generated:04d}_{secrets.token_hex(3)}{ext}"
                        vpath = os.path.join(variant_dir, vname)
                        save_kw = {"optimize": True}
                        if fmt == "PNG":
                            save_kw["compress_level"] = 9
                        elif fmt == "JPEG":
                            save_kw["quality"] = 95
                        variant.save(vpath, format=fmt, **save_kw)
                        generated += 1
                        _variant_progress["done"] = generated
                except Exception as e:
                    logger.error("Variant error for %s: %s", logo["filename"], e)

            _variant_progress["count"] = generated
        except Exception as e:
            _variant_progress["error"] = str(e)[:200]
        finally:
            _variant_progress["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    group_label = f" (group {group_id})" if group_id else " (global)"
    return HTMLResponse(
        f'<div class="alert alert-info">Generating {per_logo} variants per logo ({len(logos)} logos){group_label}...</div>'
        f'<div hx-get="/logos/variant-status" hx-trigger="every 2s" hx-swap="innerHTML"></div>')


@router.get("/logos/variant-status", response_class=HTMLResponse)
async def variant_status(request: Request):
    p = _variant_progress
    if p["error"]:
        return HTMLResponse(f'<div class="alert alert-danger">Error: {escape(p["error"])}</div>')
    if not p["running"] and p["total"] == 0:
        return HTMLResponse("")
    done, total = p["done"], p["total"]
    pct = int(done / total * 100) if total > 0 else 0
    if p["running"]:
        return HTMLResponse(
            f'<div class="progress"><div class="progress-bar" style="width:{pct}%">{done}/{total}</div></div>')
    return HTMLResponse(
        f'<div class="alert alert-success">{p["count"]} variants generated in /logo_variants/. '
        f'<a href="/logos" style="color:var(--accent)">Reload</a></div>')


_cdn_progress = {"running": False, "done": 0, "total": 0, "ok": 0, "errors": 0, "log": []}


@router.post("/logos/upload-cloudinary", response_class=HTMLResponse)
async def upload_to_cloudinary(request: Request, group_id: int = Form(0)):
    """Pre-upload all variants of a group to Cloudinary, save URLs."""
    if _cdn_progress["running"]:
        return HTMLResponse('<div class="alert alert-warning">Upload already running.</div>')

    db = request.app.state.db
    cfg = db.get_config()
    cloud_name = cfg.get("cloudinary_cloud_name", "")
    api_key = cfg.get("cloudinary_api_key", "")
    api_secret = cfg.get("cloudinary_api_secret", "")
    if not cloud_name or not api_key or not api_secret:
        return HTMLResponse('<div class="alert alert-danger">Cloudinary credentials not configured in Config.</div>')

    d = _group_variant_dir(group_id)
    files = sorted([f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f))]) if os.path.isdir(d) else []
    if not files:
        return HTMLResponse('<div class="alert alert-warning">No variants to upload. Generate first.</div>')

    uid = request.state.user["id"]
    _cdn_progress.update(running=True, done=0, total=len(files), ok=0, errors=0, log=[])

    def worker():
        import requests as req_lib
        import hashlib, time as _time
        cdn_urls = []
        for i, fname in enumerate(files):
            fpath = os.path.join(d, fname)
            try:
                timestamp = str(int(_time.time()))
                params = f"folder=logos&public_id={os.path.splitext(fname)[0]}&timestamp={timestamp}{api_secret}"
                signature = hashlib.sha1(params.encode()).hexdigest()
                with open(fpath, "rb") as f:
                    resp = req_lib.post(
                        f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload",
                        data={"api_key": api_key, "timestamp": timestamp,
                              "signature": signature, "folder": "logos",
                              "public_id": os.path.splitext(fname)[0]},
                        files={"file": (fname, f)}, timeout=30)
                if resp.status_code == 200:
                    url = resp.json().get("secure_url", "")
                    if url:
                        cdn_urls.append(url)
                        _cdn_progress["ok"] += 1
                    else:
                        _cdn_progress["errors"] += 1
                        _cdn_progress["log"].append(f"{fname}: no URL in response")
                else:
                    _cdn_progress["errors"] += 1
                    try:
                        err_detail = resp.json().get("error", {}).get("message", resp.text[:150])
                    except Exception:
                        err_detail = f"HTTP {resp.status_code}"
                    _cdn_progress["log"].append(f"{fname}: {err_detail}")
            except Exception as e:
                _cdn_progress["errors"] += 1
                _cdn_progress["log"].append(f"{fname}: {str(e)[:100]}")
            _cdn_progress["done"] = i + 1

        if cdn_urls:
            import json
            c = db._conn()
            c.execute("UPDATE trans_logo_groups SET cdn_urls_json=? WHERE id=?",
                      (json.dumps(cdn_urls), group_id))
            c.commit()

        _cdn_progress["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return HTMLResponse(
        f'<div class="alert alert-info">Uploading {len(files)} variants to Cloudinary...</div>'
        f'<div hx-get="/logos/cdn-progress" hx-trigger="every 2s" hx-swap="innerHTML"></div>'
    )


@router.get("/logos/cdn-progress", response_class=HTMLResponse)
async def cdn_progress(request: Request):
    p = _cdn_progress
    if p["running"]:
        pct = int(p["done"] / p["total"] * 100) if p["total"] > 0 else 0
        return HTMLResponse(
            f'<div class="progress" style="margin-bottom:8px">'
            f'<div class="progress-bar" style="width:{pct}%">{p["done"]}/{p["total"]}</div></div>'
            f'<p style="font-size:12px;color:var(--fg2)">{p["ok"]} OK, {p["errors"]} errors</p>'
            f'<div hx-get="/logos/cdn-progress" hx-trigger="every 2s" hx-swap="outerHTML"></div>'
        )
    html = ""
    if p["ok"] > 0:
        html += f'<div class="alert alert-success">{p["ok"]} uploaded to Cloudinary. <a href="/logos" style="color:var(--accent)">Reload</a></div>'
    if p["log"]:
        html += '<div style="font-family:monospace;font-size:11px;max-height:200px;overflow-y:auto;background:#fff5f5;padding:8px;border-radius:4px;margin-top:8px">'
        for line in p["log"]:
            html += f'<div style="color:var(--red)">{escape(line)}</div>'
        html += '</div>'
    return HTMLResponse(html)


@router.get("/logos/export-variants")
async def export_variants(request: Request, group_id: int = 0):
    """Export logo variants as ZIP for uploading to CDN/domain."""
    import io
    import zipfile
    d = _group_variant_dir(group_id)
    if not os.path.isdir(d):
        return RedirectResponse("/logos", status_code=303)
    files = [f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f))]
    if not files:
        return RedirectResponse("/logos", status_code=303)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(files):
            zf.write(os.path.join(d, f), f)
    buf.seek(0)
    from fastapi.responses import Response
    label = f"group_{group_id}" if group_id else "global"
    return Response(content=buf.read(), media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=logo_variants_{label}.zip"})


@router.post("/logos/clear-variants", response_class=HTMLResponse)
async def clear_variant_files(request: Request, group_id: int = Form(0)):
    if group_id:
        d = _group_variant_dir(group_id)
        if os.path.isdir(d):
            shutil.rmtree(d)
            os.makedirs(d, exist_ok=True)
        return HTMLResponse(f'<div class="alert alert-success">Variants for group {group_id} cleared.</div>')
    clear_variants()
    return HTMLResponse('<div class="alert alert-success">All variants cleared.</div>')


@router.get("/logos/variants/list", response_class=HTMLResponse)
async def list_variant_files(request: Request, group_id: int = 0):
    """Render a thumbnail gallery of all variant files in a group (or global).
    Each tile is a direct download link to the static file."""
    d = _group_variant_dir(group_id)
    if not os.path.isdir(d):
        return HTMLResponse('<div class="form-help">No variants directory yet.</div>')

    files = sorted(
        f for f in os.listdir(d)
        if os.path.isfile(os.path.join(d, f)) and not f.startswith(".")
    )
    if not files:
        return HTMLResponse('<div class="form-help">No variants in this folder.</div>')

    rel = f"group_{group_id}" if group_id else ""
    label = f"group_{group_id}" if group_id else "global"
    tiles = []
    for f in files:
        url = f"/static/uploads/logo_variants/{rel}/{f}" if rel else f"/static/uploads/logo_variants/{f}"
        tiles.append(
            f'<a href="{url}" download="{escape(f)}" '
            f'style="display:flex;flex-direction:column;align-items:center;gap:4px;'
            f'border:1px solid var(--border-light);border-radius:var(--radius);'
            f'padding:6px;background:var(--bg);text-decoration:none;color:var(--fg)" '
            f'title="Download {escape(f)}">'
            f'<img src="{url}" style="max-width:100px;max-height:60px;object-fit:contain">'
            f'<span style="font-size:10px;color:var(--fg2);max-width:100px;'
            f'overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{escape(f)}</span>'
            f'</a>'
        )
    grid = (
        '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px">'
        + "".join(tiles) + "</div>"
    )
    return HTMLResponse(
        f'<div style="margin-top:8px">'
        f'<div style="font-size:12px;color:var(--fg2);margin-bottom:6px">'
        f'{len(files)} variant(s) in <code>{escape(label)}</code> — click any tile to download.</div>'
        f'{grid}</div>'
    )
