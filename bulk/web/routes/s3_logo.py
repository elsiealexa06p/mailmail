"""Bulk AWS S3 Logo Uploader.

Pro Upload-Batch wird ein brandneuer S3-Bucket angelegt (globally unique)
und darin N pixel-getweakte Varianten des Logos als public-read Objekte
abgelegt. Rückgabe: die direkten HTTPS-URLs zum Copy-Paste.

Warum eigener Bucket pro Batch?
  Spamfilter blacklisten regelmäßig komplette S3-Buckets wegen Abuse.
  Ein frischer Bucket pro Kampagne isoliert die Reputation.
"""
from __future__ import annotations

import io
import json
import logging
import os
import random
import secrets
import threading
import time
from html import escape

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

logger = logging.getLogger("bulk.s3_logo")
router = APIRouter()

UPLOAD_TMP = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "static", "uploads", "s3_src"))
os.makedirs(UPLOAD_TMP, exist_ok=True)

# Progress state (same shape as cloudinary.py — reuse pattern)
_progress: dict = {
    "running": False, "done": 0, "total": 0, "ok": 0, "errors": 0,
    "log": [], "upload_id": 0, "bucket": "",
}


# ── helpers ───────────────────────────────────────────────────────

def _tweaked_bytes(src_path: str, seed: int) -> tuple[bytes, str]:
    """Returns (image_bytes, content_type). Pixel-tweak falls möglich,
    sonst Rohdaten."""
    ct_map = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
    try:
        from PIL import Image
    except ImportError:
        return open(src_path, "rb").read(), "image/png"

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
            px[i] = max(0, min(255, int(px[i]) + rng.choice([-1, 1])))
        img.putpixel((x, y), tuple(px))

        buf = io.BytesIO()
        if original_format == "JPEG":
            img.convert("RGB").save(buf, "JPEG", quality=95, optimize=True)
            return buf.getvalue(), "image/jpeg"
        if original_format == "WEBP":
            img.save(buf, "WEBP", quality=95)
            return buf.getvalue(), "image/webp"
        img.save(buf, "PNG", optimize=True)
        return buf.getvalue(), "image/png"
    except Exception as e:
        logger.warning("Pixel tweak failed (%s) — using raw bytes", e)
        return open(src_path, "rb").read(), "image/png"


def _sanitize_prefix(raw: str) -> str:
    """Macht aus dem User-Prefix einen DNS-safe Bucket-Prefix."""
    s = (raw or "").lower()
    out = []
    for ch in s:
        if ch.isalnum():
            out.append(ch)
        elif ch in "-":
            out.append(ch)
    return "".join(out).strip("-")[:30] or "logo"


def _mk_bucket_name(prefix: str) -> str:
    """Baut einen DNS-safe, globally unique Bucket-Namen.
    Format: <prefix>-<ts>-<rand>  (max 63 chars, lowercase, [a-z0-9-])."""
    pfx = _sanitize_prefix(prefix)
    ts = str(int(time.time()))[-6:]
    rnd = secrets.token_hex(4)
    name = f"{pfx}-{ts}-{rnd}"
    if len(name) > 63:
        name = name[:63].rstrip("-")
    return name


def _public_url(bucket: str, region: str, key: str) -> str:
    """Return the virtual-hosted-style URL. us-east-1 lässt die Region weg,
    sonst wird sie Teil des Hosts. URL-safe weil wir keys sauber sanitizen."""
    if region == "us-east-1":
        return f"https://{bucket}.s3.amazonaws.com/{key}"
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def _mk_s3_client(cfg: dict):
    import boto3  # type: ignore
    from botocore.config import Config as BotoConfig  # type: ignore
    return boto3.client(
        "s3",
        aws_access_key_id=cfg["access_key"],
        aws_secret_access_key=cfg["secret_key"],
        region_name=cfg.get("region") or "us-east-1",
        config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}),
    )


def _create_public_bucket(client, bucket: str, region: str):
    """Legt den Bucket an, schaltet PublicAccessBlock aus und setzt
    eine GetObject-Bucket-Policy. Idempotent: wiederholter Lauf macht
    nichts kaputt."""
    kwargs = {"Bucket": bucket}
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    try:
        client.create_bucket(**kwargs)
    except client.exceptions.BucketAlreadyOwnedByYou:
        pass

    # ownership controls: BucketOwnerEnforced heißt ACLs sind aus, das will
    # AWS seit 2023 so haben — Public-Access regeln wir dann rein per Policy.
    try:
        client.put_bucket_ownership_controls(
            Bucket=bucket,
            OwnershipControls={"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
        )
    except Exception as e:
        logger.debug("ownership controls skipped: %s", e)

    client.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": False,
            "IgnorePublicAcls": False,
            "BlockPublicPolicy": False,
            "RestrictPublicBuckets": False,
        },
    )

    policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "PublicReadGetObject",
            "Effect": "Allow",
            "Principal": "*",
            "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{bucket}/*",
        }],
    })
    client.put_bucket_policy(Bucket=bucket, Policy=policy)


def _empty_bucket(client, bucket: str):
    """Löscht alle Objekte im Bucket. Für delete_mode='full'."""
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            client.delete_objects(Bucket=bucket, Delete={"Objects": keys})


# ── routes ───────────────────────────────────────────────────────

@router.get("/s3-logo", response_class=HTMLResponse)
async def s3_logo_page(request: Request):
    db = request.app.state.db
    cfg = db.get_s3_config()
    uploads = [dict(u) for u in db.get_s3_uploads()]
    for u in uploads:
        links = [dict(l) for l in db.get_s3_links(u["id"])]
        u["links"] = links
        u["link_count"] = len(links)
    return request.app.state.templates.TemplateResponse(request, "s3_logo.html", {
        "active": "s3_logo",
        "cfg": cfg,
        "uploads": uploads,
        "progress": _progress,
    })


@router.post("/s3-logo/config")
async def save_config(request: Request,
                      access_key: str = Form(""),
                      secret_key: str = Form(""),
                      region: str = Form("us-east-1"),
                      bucket_prefix: str = Form("")):
    db = request.app.state.db
    db.save_s3_config(
        access_key.strip(), secret_key.strip(),
        (region or "us-east-1").strip(),
        _sanitize_prefix(bucket_prefix))
    return RedirectResponse("/s3-logo", status_code=303)


@router.post("/s3-logo/test", response_class=HTMLResponse)
async def test_creds(request: Request):
    """Smoke-test: aws sts style — list_buckets reicht als Berechtigungs-
    probe (erfordert s3:ListAllMyBuckets)."""
    db = request.app.state.db
    cfg = db.get_s3_config()
    if not (cfg["access_key"] and cfg["secret_key"]):
        return HTMLResponse('<span class="v-block">Keine Credentials gespeichert</span>')
    try:
        client = _mk_s3_client(cfg)
        resp = client.list_buckets()
        n = len(resp.get("Buckets", []))
        return HTMLResponse(
            f'<span class="v-allow">&#10003; Verbindung ok · {n} vorhandene Bucket(s) im Account</span>')
    except Exception as e:
        return HTMLResponse(
            f'<span class="v-block">Fehler: {escape(str(e)[:200])}</span>')


@router.post("/s3-logo/upload", response_class=HTMLResponse)
async def upload_logo(request: Request,
                      file: UploadFile = File(None),
                      count: int = Form(1),
                      base_name: str = Form(""),
                      bucket_prefix_override: str = Form(""),
                      pixel_tweak: str = Form("1")):
    db = request.app.state.db
    cfg = db.get_s3_config()
    if not (cfg["access_key"] and cfg["secret_key"]):
        return HTMLResponse(
            '<div class="alert alert-danger">AWS Credentials fehlen — '
            'bitte oben im Config-Card speichern.</div>')
    if _progress["running"]:
        return HTMLResponse(
            '<div class="alert alert-warning">Ein anderer Upload läuft noch.</div>')
    if not file or not file.filename:
        return HTMLResponse(
            '<div class="alert alert-warning">Keine Datei ausgewählt.</div>')

    try:
        import boto3  # noqa: F401
    except ImportError:
        return HTMLResponse(
            '<div class="alert alert-danger">boto3 nicht installiert. '
            '<code>pip install boto3</code> im venv ausführen.</div>')

    count = max(1, min(int(count or 1), 200))
    base_name_clean = "".join(c for c in (base_name or "").strip()
                              if c.isalnum() or c in "-_") or "logo"
    tweak = bool(int(pixel_tweak or 0))
    region = (cfg.get("region") or "us-east-1").strip() or "us-east-1"
    prefix = _sanitize_prefix(bucket_prefix_override or cfg.get("bucket_prefix") or base_name_clean)
    bucket = _mk_bucket_name(prefix)

    # Source persistent speichern (Worker läuft im Thread, FastAPI Stream
    # wäre dort nicht mehr verfügbar).
    safe_orig = "".join(c for c in file.filename if c.isalnum() or c in ".-_") or "upload.bin"
    src_path = os.path.join(UPLOAD_TMP, f"{int(time.time())}_{secrets.token_hex(4)}_{safe_orig}")
    raw = await file.read()
    with open(src_path, "wb") as fh:
        fh.write(raw)

    upload_id = db.add_s3_upload(
        source_filename=file.filename, base_name=base_name_clean,
        bucket_name=bucket, region=region,
        count=count, pixel_tweak=int(tweak))

    _progress.update(running=True, done=0, total=count, ok=0, errors=0,
                     log=[], upload_id=upload_id, bucket=bucket)

    def worker():
        try:
            client = _mk_s3_client(cfg)
            try:
                _create_public_bucket(client, bucket, region)
            except Exception as e:
                _progress["errors"] += 1
                _progress["log"].append(f"bucket create: {str(e)[:300]}")
                _progress["done"] = count
                return

            ext_map = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
            for i in range(count):
                suffix = f"{i + 1}" if count > 1 else ""
                tag = secrets.token_hex(3)
                body, ct = (_tweaked_bytes(src_path, seed=i)
                            if tweak else
                            (open(src_path, "rb").read(), "image/png"))
                ext = ext_map.get(ct, ".png")
                key = f"{base_name_clean}{suffix}_{tag}{ext}"
                try:
                    client.put_object(
                        Bucket=bucket, Key=key, Body=body,
                        ContentType=ct,
                        CacheControl="public, max-age=31536000",
                    )
                    url = _public_url(bucket, region, key)
                    db.add_s3_link(upload_id, key, url)
                    _progress["ok"] += 1
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
        f'<div class="alert alert-info">Erstelle Bucket <code>{escape(bucket)}</code> '
        f'und lade {count}× hoch{" mit Pixel-Tweak" if tweak else ""}…</div>'
        f'<div hx-get="/s3-logo/progress" hx-trigger="every 1s" hx-swap="outerHTML"></div>'
    )


@router.get("/s3-logo/progress", response_class=HTMLResponse)
async def progress(request: Request):
    p = _progress
    if p["running"]:
        pct = int(p["done"] / p["total"] * 100) if p["total"] else 0
        return HTMLResponse(
            f'<div hx-get="/s3-logo/progress" hx-trigger="every 1s" hx-swap="outerHTML">'
            f'<div class="progress" style="margin-bottom:6px">'
            f'<div class="progress-bar" style="width:{pct}%">{p["done"]}/{p["total"]}</div></div>'
            f'<p style="font-size:12px;color:var(--fg2)">'
            f'Bucket <code>{escape(p["bucket"])}</code> · '
            f'{p["ok"]} ok, {p["errors"]} Fehler</p></div>'
        )
    if p["ok"] > 0 or p["errors"] > 0:
        err_html = ""
        if p["log"]:
            err_html = (
                '<details style="margin-top:6px">'
                '<summary style="font-size:12px;color:var(--red);cursor:pointer">'
                'View errors</summary>'
                '<pre style="font-size:11px;background:#fdf0f0;padding:8px;'
                'border-radius:4px;white-space:pre-wrap">'
                + escape("\n".join(p["log"])) + "</pre></details>"
            )
        return HTMLResponse(
            f'<div><div class="alert alert-success">Done — {p["ok"]} uploaded, '
            f'{p["errors"]} Fehler. Bucket: <code>{escape(p["bucket"])}</code>. '
            f'<a href="/s3-logo" style="color:var(--accent)">Reload</a></div>{err_html}</div>'
        )
    return HTMLResponse("")


@router.post("/s3-logo/upload/{uid}/delete")
async def delete_upload(request: Request, uid: int,
                         full: str = Form("0")):
    """full=1 → löscht auch den S3-Bucket (vorher leeren)."""
    db = request.app.state.db
    row = db.get_s3_upload(uid)
    if not row:
        return RedirectResponse("/s3-logo", status_code=303)
    row = dict(row)
    cfg = db.get_s3_config()
    if full == "1" and cfg.get("access_key"):
        try:
            client = _mk_s3_client(cfg)
            _empty_bucket(client, row["bucket_name"])
            client.delete_bucket(Bucket=row["bucket_name"])
        except Exception as e:
            logger.warning("S3 bucket delete failed: %s", e)
    db.delete_s3_upload(uid)
    return RedirectResponse("/s3-logo", status_code=303)


@router.get("/s3-logo/upload/{uid}/export.txt")
async def export_links(request: Request, uid: int):
    db = request.app.state.db
    links = db.get_s3_links(uid)
    body = "\n".join(dict(l)["public_url"] for l in links)
    return Response(
        content=body, media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename=s3_{uid}.txt'})
