"""Bulk Cloudinary — upload a logo N times with per-upload pixel tweaks
so each variant has a distinct hash. Designed for redirect / hosted-logo
use where you want N different URLs that all visually show the same logo
but cannot be lumped together by a perceptual-hash filter.
"""
import os
import io
import json
import time
import shutil
import random
import secrets
import hashlib
import logging
import threading
from html import escape

from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response

logger = logging.getLogger("bulk.cloudinary")
router = APIRouter()

UPLOAD_TMP = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "static", "uploads", "cloudinary_src"
))
os.makedirs(UPLOAD_TMP, exist_ok=True)

# In-process progress per running upload job
_progress: dict = {"running": False, "done": 0, "total": 0, "ok": 0,
                    "errors": 0, "log": [], "upload_id": 0}


# ── helpers ──────────────────────────────────────────────

def _tweaked_bytes(src_path: str, seed: int) -> bytes:
    """Return the image bytes with exactly one pixel's R/G/B nudged by ±1
    so the file hash changes per variant while staying visually identical.
    Falls back to raw bytes on any Pillow error so the upload never blocks
    on imaging trouble."""
    try:
        from PIL import Image
    except ImportError:
        return open(src_path, "rb").read()

    try:
        img = Image.open(src_path)
        original_format = (img.format or "PNG").upper()
        if img.mode == "P":
            img = img.convert("RGBA")
        elif img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")

        rng = random.Random(seed)
        w, h = img.size
        # Pick a pixel that's not a corner — corners are often clipped
        # by viewers / responsive resizers.
        x = rng.randint(1, max(1, w - 2))
        y = rng.randint(1, max(1, h - 2))
        px = list(img.getpixel((x, y)))
        # Nudge each channel by ±1, clamped 0..255.
        for i in range(min(3, len(px))):
            delta = rng.choice([-1, 1])
            px[i] = max(0, min(255, int(px[i]) + delta))
        img.putpixel((x, y), tuple(px))

        buf = io.BytesIO()
        if original_format == "JPEG":
            img.convert("RGB").save(buf, "JPEG", quality=95, optimize=True)
        elif original_format == "WEBP":
            img.save(buf, "WEBP", quality=95)
        else:
            img.save(buf, "PNG", optimize=True)
        return buf.getvalue()
    except Exception as e:
        logger.warning("Pixel tweak failed (%s) — sending raw bytes", e)
        return open(src_path, "rb").read()


def _cloudinary_upload(cloud_name: str, api_key: str, api_secret: str,
                        body: bytes, filename: str, public_id: str,
                        folder: str = "") -> dict:
    """Signed upload to Cloudinary. Returns parsed JSON (with secure_url)
    or raises with the API error message."""
    import requests as req_lib

    timestamp = str(int(time.time()))
    # Params that participate in the signature, sorted alphabetically:
    params_to_sign = {}
    if folder:
        params_to_sign["folder"] = folder
    if public_id:
        params_to_sign["public_id"] = public_id
    params_to_sign["timestamp"] = timestamp
    sign_str = "&".join(f"{k}={v}" for k, v in sorted(params_to_sign.items())) + api_secret
    signature = hashlib.sha1(sign_str.encode("utf-8")).hexdigest()

    data = {"api_key": api_key, "timestamp": timestamp, "signature": signature}
    if folder:
        data["folder"] = folder
    if public_id:
        data["public_id"] = public_id

    resp = req_lib.post(
        f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload",
        data=data,
        files={"file": (filename, body)},
        timeout=60,
    )
    if resp.status_code != 200:
        try:
            msg = resp.json().get("error", {}).get("message", resp.text[:300])
        except Exception:
            msg = f"HTTP {resp.status_code}: {resp.text[:300]}"
        raise RuntimeError(f"Cloudinary {resp.status_code}: {msg}")
    return resp.json()


# ── routes ───────────────────────────────────────────────

@router.get("/cloudinary", response_class=HTMLResponse)
async def cloudinary_page(request: Request):
    db = request.app.state.db
    cfg = db.get_cloudinary_config()
    uploads = [dict(u) for u in db.get_cloudinary_uploads()]
    # Attach link count + a preview slice for each upload
    for u in uploads:
        links = [dict(l) for l in db.get_cloudinary_links(u["id"])]
        u["links"] = links
        u["link_count"] = len(links)
    return request.app.state.templates.TemplateResponse(request, "cloudinary.html", {
        "active": "cloudinary",
        "cfg": cfg,
        "uploads": uploads,
        "progress": _progress,
    })


@router.post("/cloudinary/config")
async def save_config(request: Request,
                       cloud_name: str = Form(""),
                       api_key: str = Form(""),
                       api_secret: str = Form("")):
    db = request.app.state.db
    db.save_cloudinary_config(cloud_name.strip(), api_key.strip(), api_secret.strip())
    return RedirectResponse("/cloudinary", status_code=303)


@router.post("/cloudinary/upload", response_class=HTMLResponse)
async def upload_logo(request: Request,
                       file: UploadFile = File(None),
                       count: int = Form(1),
                       base_name: str = Form(""),
                       folder: str = Form(""),
                       pixel_tweak: str = Form("1")):
    db = request.app.state.db
    cfg = db.get_cloudinary_config()
    if not (cfg["cloud_name"] and cfg["api_key"] and cfg["api_secret"]):
        return HTMLResponse(
            '<div class="alert alert-danger">Cloudinary credentials missing — '
            'save them in the Config card first.</div>')
    if _progress["running"]:
        return HTMLResponse('<div class="alert alert-warning">Another upload is still running.</div>')
    if not file or not file.filename:
        return HTMLResponse('<div class="alert alert-warning">No file selected.</div>')

    count = max(1, min(int(count or 1), 200))
    base_name = "".join(c for c in (base_name or "").strip() if c.isalnum() or c in "-_") or "Logo"
    folder = "".join(c for c in (folder or "").strip() if c.isalnum() or c in "-_/") or ""
    tweak = bool(int(pixel_tweak or 0))

    # Persist the uploaded source file so the background worker can read it
    safe_orig = "".join(c for c in file.filename if c.isalnum() or c in ".-_") or "upload.bin"
    src_path = os.path.join(UPLOAD_TMP, f"{int(time.time())}_{secrets.token_hex(4)}_{safe_orig}")
    raw = await file.read()
    with open(src_path, "wb") as fh:
        fh.write(raw)

    upload_id = db.add_cloudinary_upload(
        source_filename=file.filename, base_public_id=base_name,
        folder=folder, count=count, pixel_tweak=int(tweak))

    _progress.update(running=True, done=0, total=count, ok=0, errors=0,
                     log=[], upload_id=upload_id)

    cloud_name = cfg["cloud_name"]
    api_key = cfg["api_key"]
    api_secret = cfg["api_secret"]

    def worker():
        try:
            for i in range(count):
                suffix = f"{i + 1}" if count > 1 else ""
                # Add a random tag so two batches with the same base_name
                # don't collide on Cloudinary's public_id namespace.
                public_id = f"{base_name}{suffix}_{secrets.token_hex(3)}"
                body = _tweaked_bytes(src_path, seed=i) if tweak else open(src_path, "rb").read()
                try:
                    resp = _cloudinary_upload(
                        cloud_name, api_key, api_secret,
                        body=body, filename=file.filename,
                        public_id=public_id, folder=folder)
                    secure_url = resp.get("secure_url", "")
                    if secure_url:
                        db.add_cloudinary_link(upload_id, public_id, secure_url)
                        _progress["ok"] += 1
                    else:
                        _progress["errors"] += 1
                        _progress["log"].append(f"#{i + 1}: empty response")
                except Exception as e:
                    _progress["errors"] += 1
                    _progress["log"].append(f"#{i + 1}: {str(e)[:200]}")
                _progress["done"] = i + 1
        finally:
            _progress["running"] = False
            try:
                os.unlink(src_path)
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True).start()
    return HTMLResponse(
        f'<div class="alert alert-info">Uploading {count}× to Cloudinary'
        f'{" with per-upload pixel tweak" if tweak else ""}…</div>'
        f'<div hx-get="/cloudinary/progress" hx-trigger="every 1s" hx-swap="outerHTML"></div>'
    )


@router.get("/cloudinary/progress", response_class=HTMLResponse)
async def progress(request: Request):
    p = _progress
    if p["running"]:
        pct = int(p["done"] / p["total"] * 100) if p["total"] else 0
        return HTMLResponse(
            f'<div hx-get="/cloudinary/progress" hx-trigger="every 1s" hx-swap="outerHTML">'
            f'<div class="progress" style="margin-bottom:6px">'
            f'<div class="progress-bar" style="width:{pct}%">{p["done"]}/{p["total"]}</div></div>'
            f'<p style="font-size:12px;color:var(--fg2)">'
            f'{p["ok"]} uploaded, {p["errors"]} errors</p></div>'
        )
    if p["ok"] > 0 or p["errors"] > 0:
        err_html = ''
        if p["log"]:
            err_html = '<details style="margin-top:6px"><summary style="font-size:12px;color:var(--red);cursor:pointer">View errors</summary><pre style="font-size:11px;background:#fdf0f0;padding:8px;border-radius:4px;white-space:pre-wrap">' + escape("\n".join(p["log"])) + '</pre></details>'
        return HTMLResponse(
            f'<div><div class="alert alert-success">Done — {p["ok"]} uploaded, {p["errors"]} errors. '
            f'<a href="/cloudinary" style="color:var(--accent)">Reload</a></div>{err_html}</div>'
        )
    return HTMLResponse("")


@router.post("/cloudinary/upload/{uid}/delete")
async def delete_upload(request: Request, uid: int):
    request.app.state.db.delete_cloudinary_upload(uid)
    return RedirectResponse("/cloudinary", status_code=303)


@router.get("/cloudinary/upload/{uid}/export.txt")
async def export_links(request: Request, uid: int):
    """Plain-text export of all URLs for one upload batch (one per line)."""
    db = request.app.state.db
    links = db.get_cloudinary_links(uid)
    body = "\n".join(dict(l)["secure_url"] for l in links)
    return Response(content=body, media_type="text/plain",
                    headers={"Content-Disposition": f'attachment; filename=cloudinary_{uid}.txt'})
