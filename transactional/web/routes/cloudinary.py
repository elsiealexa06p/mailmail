"""Transactional Cloudinary — upload a logo N times through a SOCKS proxy,
with per-upload pixel tweaks so each variant has a distinct hash. Same idea
as the bulk mailer version, but every request is routed through a proxy
picked from the user's trans_proxies configs (mandatory) so Cloudinary
doesn't see the panel's real egress IP.
"""
import os
import io
import time
import random
import secrets
import hashlib
import logging
import threading
from html import escape

from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..jobs import job_manager

logger = logging.getLogger("trans.cloudinary")
router = APIRouter()

UPLOAD_TMP = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "static", "uploads", "cloudinary_src"))
os.makedirs(UPLOAD_TMP, exist_ok=True)


# ── helpers ──────────────────────────────────────────────

def _tweaked_bytes(src_path: str, seed: int) -> bytes:
    """Nudge one pixel's R/G/B by ±1 so the file hash changes per variant
    while staying visually identical. Raw-bytes fallback on any Pillow error."""
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
        x = rng.randint(1, max(1, w - 2))
        y = rng.randint(1, max(1, h - 2))
        px = list(img.getpixel((x, y)))
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


def _proxy_lines(proxy_row: dict) -> list:
    """Return non-empty proxy lines from a trans_proxies row."""
    if not proxy_row:
        return []
    raw = (proxy_row.get("value") or "").strip()
    if not raw:
        return []
    if proxy_row.get("proxy_type") == "pool":
        return [ln.strip() for ln in raw.splitlines() if ln.strip()]
    return [raw.splitlines()[0].strip()] if raw else []


def _proxies_dict(line: str) -> dict:
    """Convert 'socks5://ip:port:user:pass' (or 'ip:port:user:pass') into
    the {'http','https'} requests proxies dict."""
    from urllib.parse import quote
    s = line.strip().replace("socks5://", "").replace("socks://", "")
    if not s:
        return {}
    if "@" in s:
        auth, hostpart = s.rsplit("@", 1)
        parts = hostpart.split(":")
        user, pwd = ("", "")
        if ":" in auth:
            user, pwd = auth.split(":", 1)
    else:
        parts = s.split(":")
        if len(parts) < 2:
            return {}
        user, pwd = ("", "")
        if len(parts) >= 4:
            user, pwd = parts[2], parts[3]
    host, port = parts[0], parts[1]
    auth = f"{quote(user, safe='')}:{quote(pwd, safe='')}@" if user else ""
    url = f"socks5://{auth}{host}:{port}"
    return {"http": url, "https": url}


def _cloudinary_upload(cloud_name: str, api_key: str, api_secret: str,
                       body: bytes, filename: str, public_id: str,
                       folder: str, proxies: dict) -> dict:
    """Signed upload to Cloudinary through the given SOCKS5 proxy dict.
    Returns parsed JSON or raises with the API error message."""
    import requests as req_lib

    timestamp = str(int(time.time()))
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
        proxies=proxies,
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
    uid = request.state.user["id"]
    cfg = db.get_config()
    proxies = [dict(p) for p in db.get_proxies(uid)]
    uploads = [dict(u) for u in db.get_cloudinary_uploads(uid)]
    for u in uploads:
        links = [dict(l) for l in db.get_cloudinary_links(u["id"])]
        u["links"] = links
        u["link_count"] = len(links)
        u["proxy_name"] = next(
            (p["name"] for p in proxies if p["id"] == u.get("proxy_id")), "")
    return request.app.state.templates.TemplateResponse(request, "cloudinary.html", {
        "active": "cloudinary",
        "cfg": {
            "cloud_name": cfg.get("cloudinary_cloud_name", ""),
            "api_key": cfg.get("cloudinary_api_key", ""),
            "api_secret": cfg.get("cloudinary_api_secret", ""),
        },
        "proxies": proxies,
        "uploads": uploads,
    })


@router.post("/cloudinary/config")
async def save_config(request: Request,
                       cloud_name: str = Form(""),
                       api_key: str = Form(""),
                       api_secret: str = Form("")):
    request.app.state.db.update_config(
        cloudinary_cloud_name=cloud_name.strip(),
        cloudinary_api_key=api_key.strip(),
        cloudinary_api_secret=api_secret.strip())
    return RedirectResponse("/cloudinary", status_code=303)


@router.post("/cloudinary/upload", response_class=HTMLResponse)
async def upload_logo(request: Request,
                      file: UploadFile = File(None),
                      count: int = Form(1),
                      base_name: str = Form(""),
                      folder: str = Form(""),
                      pixel_tweak: str = Form("1"),
                      proxy_id: int = Form(0)):
    db = request.app.state.db
    uid = request.state.user["id"]
    cfg = db.get_config()
    cloud_name = cfg.get("cloudinary_cloud_name", "")
    api_key = cfg.get("cloudinary_api_key", "")
    api_secret = cfg.get("cloudinary_api_secret", "")

    if not (cloud_name and api_key and api_secret):
        return HTMLResponse(
            '<div class="alert alert-danger">Cloudinary credentials missing — '
            'save them in the Config card first.</div>')
    if not file or not file.filename:
        return HTMLResponse('<div class="alert alert-warning">No file selected.</div>')
    if not proxy_id:
        return HTMLResponse(
            '<div class="alert alert-danger">A proxy is required — pick one '
            '(or create one on the Proxies page first).</div>')

    proxy_row = db.get_proxy(proxy_id)
    proxy_row = dict(proxy_row) if proxy_row else None
    if not proxy_row or proxy_row.get("user_id") not in (uid, 0):
        return HTMLResponse('<div class="alert alert-danger">Proxy not found.</div>')
    lines = _proxy_lines(proxy_row)
    if not lines:
        return HTMLResponse('<div class="alert alert-danger">Selected proxy is empty.</div>')

    count = max(1, min(int(count or 1), 200))
    base_name = "".join(c for c in (base_name or "").strip() if c.isalnum() or c in "-_") or "Logo"
    folder = "".join(c for c in (folder or "").strip() if c.isalnum() or c in "-_/") or ""
    tweak = bool(int(pixel_tweak or 0))

    safe_orig = "".join(c for c in file.filename if c.isalnum() or c in ".-_") or "upload.bin"
    src_path = os.path.join(UPLOAD_TMP, f"{int(time.time())}_{secrets.token_hex(4)}_{safe_orig}")
    raw = await file.read()
    with open(src_path, "wb") as fh:
        fh.write(raw)

    upload_id = db.add_cloudinary_upload(
        source_filename=file.filename, base_public_id=base_name,
        folder=folder, count=count, pixel_tweak=int(tweak),
        proxy_id=proxy_id, user_id=uid)

    original_filename = file.filename
    job = job_manager.create(
        "cloudinary_upload", uid,
        f"Cloudinary: {file.filename} × {count} via {proxy_row['name']}",
        total=count, page_url="/cloudinary")

    def worker():
        try:
            for i in range(count):
                if job.cancelled():
                    job.log_line(f"abgebrochen bei {i}/{count}")
                    break
                suffix = f"{i + 1}" if count > 1 else ""
                public_id = f"{base_name}{suffix}_{secrets.token_hex(3)}"
                body = _tweaked_bytes(src_path, seed=i) if tweak else open(src_path, "rb").read()
                proxies = _proxies_dict(lines[i % len(lines)])
                if not proxies:
                    job.tick(err=1)
                    job.log_line(f"#{i + 1}: invalid proxy line")
                    continue
                try:
                    resp = _cloudinary_upload(
                        cloud_name, api_key, api_secret,
                        body=body, filename=original_filename,
                        public_id=public_id, folder=folder,
                        proxies=proxies)
                    secure_url = resp.get("secure_url", "")
                    if secure_url:
                        db.add_cloudinary_link(upload_id, public_id, secure_url)
                        job.tick(ok=1)
                    else:
                        job.tick(err=1)
                        job.log_line(f"#{i + 1}: empty response")
                except Exception as e:
                    job.tick(err=1)
                    job.log_line(f"#{i + 1}: {str(e)[:200]}")
            job.finish("done")
        except Exception as e:
            job.finish("error", str(e))
        finally:
            try:
                os.unlink(src_path)
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True).start()
    return HTMLResponse(
        f'<div class="alert alert-info">Job #{job.id} gestartet: {count}× Cloudinary '
        f'via <code>{escape(proxy_row["name"])}</code>'
        f'{" mit pixel-tweak" if tweak else ""}. '
        f'Live-Status + Abbrechen im Job-Widget (unten rechts).</div>'
    )


@router.post("/cloudinary/upload/{uid_}/delete")
async def delete_upload(request: Request, uid_: int):
    uid = request.state.user["id"]
    request.app.state.db.delete_cloudinary_upload(uid_, uid)
    return RedirectResponse("/cloudinary", status_code=303)


@router.get("/cloudinary/upload/{uid_}/export.txt")
async def export_links(request: Request, uid_: int):
    """Plain-text export — one URL per line."""
    db = request.app.state.db
    uid = request.state.user["id"]
    if not db.get_cloudinary_upload(uid_, uid):
        return Response(content="", media_type="text/plain", status_code=404)
    links = db.get_cloudinary_links(uid_)
    body = "\n".join(dict(l)["secure_url"] for l in links)
    return Response(content=body, media_type="text/plain",
                    headers={"Content-Disposition": f'attachment; filename=cloudinary_{uid_}.txt'})
