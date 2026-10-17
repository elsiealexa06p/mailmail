"""Media Pool — konsolidierter Wizard für CID + Cloudinary + S3.

Statt in 3 Tabs jonglieren gibst du hier nur an:
  * welche Source-Logos
  * wie viele Varianten insgesamt
  * Verteilungs-Prozente (CID / Cloudinary / S3)
und der Wizard baut den Pool parallel. Beim nächsten Kampagnenstart
findet der Mailer alle drei Provider automatisch.

„Pool leeren" wischt die DB-Referenzen weg (Remote-Assets bei
Cloudinary/S3 bleiben liegen — Kosten quasi null, kann später im
alten Panel entsorgt werden).
"""
import io
import os
import random
import secrets
import threading
import mimetypes
import logging
from html import escape
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from ..jobs import job_manager

router = APIRouter()
logger = logging.getLogger("trans.pool")


def _tweaked_bytes(src_path: str, seed: int) -> bytes:
    """Bytes-jitter für eine Variante — visuell identisch, Byte-Hash
    unique. Fallback: raw bytes wenn Pillow fehlt/failt."""
    try:
        from PIL import Image
    except ImportError:
        return open(src_path, "rb").read()
    try:
        img = Image.open(src_path)
        fmt = (img.format or "PNG").upper()
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
        if fmt == "JPEG":
            img.convert("RGB").save(buf, "JPEG", quality=95, optimize=True)
        elif fmt == "WEBP":
            img.save(buf, "WEBP", quality=95)
        else:
            img.save(buf, "PNG", optimize=True)
        return buf.getvalue()
    except Exception as e:
        logger.warning("pool tweak failed: %s", e)
        return open(src_path, "rb").read()


def _resolve_logo_path(file_path: str) -> str:
    if not file_path:
        return ""
    if file_path.startswith("/static/"):
        return os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", file_path.lstrip("/")))
    return file_path


def _collect_sources(db, uid: int, source_mode: str, group_id: int,
                      logo_ids_csv: str) -> list:
    """Returns [(filename, abs_path), …] basierend auf Selektion."""
    out = []
    if source_mode == "group" and group_id:
        rows = db.get_logos_by_group(group_id)
    elif source_mode == "logos" and logo_ids_csv.strip():
        wanted = {int(x) for x in logo_ids_csv.split(",") if x.strip().isdigit()}
        rows = [l for l in db.get_logos(uid) if dict(l)["id"] in wanted]
    else:
        rows = db.get_logos(uid)
    for r in rows:
        d = dict(r)
        p = _resolve_logo_path(d.get("file_path", ""))
        if p and os.path.isfile(p):
            out.append((d.get("filename") or os.path.basename(p), p))
    return out


def _pool_stats(db, uid: int) -> dict:
    """Gesamtsummen für den Header-Badge."""
    from .logos import VARIANT_DIR
    cid_count = 0
    if os.path.isdir(VARIANT_DIR):
        for root, dirs, files in os.walk(VARIANT_DIR):
            cid_count += sum(1 for f in files if not f.startswith("."))
    cloud_count = sum(len(db.get_cloudinary_links(dict(u)["id"]))
                      for u in db.get_cloudinary_uploads(uid))
    s3_count = sum(len(db.get_s3_links(dict(u)["id"]))
                   for u in db.get_s3_uploads(uid))
    return {
        "cid": cid_count,
        "cloudinary": cloud_count,
        "s3": s3_count,
        "total": cid_count + cloud_count + s3_count,
    }


def _group_stats(db, uid: int, groups: list) -> dict:
    """Pro Group: {group_id: {"cid": N, "cloudinary": N, "s3": N, "total": N}}.
    Group-ID 0 = "global" (Uploads ohne explizite Group-Zuordnung)."""
    from .logos import _group_variant_dir, VARIANT_DIR
    cdn_by_group = db.get_cdn_stats_by_group(uid)
    out = {}
    # CID pro Group (Files im group_X/ Subdir)
    for g in groups:
        gid = g["id"]
        gdir = _group_variant_dir(gid)
        cid_n = 0
        if os.path.isdir(gdir):
            cid_n = sum(1 for f in os.listdir(gdir)
                         if os.path.isfile(os.path.join(gdir, f))
                         and not f.startswith("."))
        cdn = cdn_by_group.get(gid, {"cloudinary": 0, "s3": 0})
        out[gid] = {
            "cid": cid_n,
            "cloudinary": cdn["cloudinary"],
            "s3": cdn["s3"],
            "total": cid_n + cdn["cloudinary"] + cdn["s3"],
        }
    # Global (group_id=0): CID-Files direkt in VARIANT_DIR (nicht in Subdirs)
    global_cid = 0
    if os.path.isdir(VARIANT_DIR):
        global_cid = sum(1 for f in os.listdir(VARIANT_DIR)
                         if os.path.isfile(os.path.join(VARIANT_DIR, f))
                         and not f.startswith("."))
    global_cdn = cdn_by_group.get(0, {"cloudinary": 0, "s3": 0})
    out[0] = {
        "cid": global_cid,
        "cloudinary": global_cdn["cloudinary"],
        "s3": global_cdn["s3"],
        "total": global_cid + global_cdn["cloudinary"] + global_cdn["s3"],
    }
    return out


@router.get("/pool", response_class=HTMLResponse)
async def pool_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    cfg = db.get_config()
    logos = [dict(l) for l in db.get_logos(uid)]
    groups = [dict(g) for g in db.get_logo_groups(uid)]
    for g in groups:
        g["logo_count"] = len([l for l in logos if l.get("group_id") == g["id"]])
    proxies = [dict(p) for p in db.get_proxies(uid)]
    s3_accounts = [dict(a) for a in db.get_s3_accounts(uid)]
    stats = _pool_stats(db, uid)
    group_stats = _group_stats(db, uid, groups)
    cloudinary_ready = bool(cfg.get("cloudinary_cloud_name")
                             and cfg.get("cloudinary_api_key")
                             and cfg.get("cloudinary_api_secret"))
    return request.app.state.templates.TemplateResponse(request, "pool.html", {
        "active": "pool",
        "logos": logos,
        "groups": groups,
        "group_stats": group_stats,
        "proxies": proxies,
        "s3_accounts": s3_accounts,
        "stats": stats,
        "cloudinary_ready": cloudinary_ready,
    })


@router.post("/pool/clear", response_class=HTMLResponse)
async def pool_clear(request: Request,
                       group_id: int = Form(-1)):
    """Reißt den Media-Pool ab. group_id=-1 (Default) → ALLES weg;
    group_id=0 → nur globale/unassigned Uploads; group_id>0 → nur diese
    Group. Remote-Assets bleiben liegen."""
    db = request.app.state.db
    uid = request.state.user["id"]
    from .logos import VARIANT_DIR, _group_variant_dir
    import shutil

    only_group = None if int(group_id) < 0 else int(group_id)
    if only_group is None:
        scope_label = "kompletter Pool"
    elif only_group == 0:
        scope_label = "Pool global"
    else:
        scope_label = f"Pool Group #{only_group}"

    job = job_manager.create(
        "pool_clear", uid, f"Media-Pool leeren: {scope_label}",
        total=3, page_url="/pool")

    def worker():
        try:
            # 1) CID-Varianten
            if only_group is None:
                # alles: files direkt in VARIANT_DIR + alle group_X/
                if os.path.isdir(VARIANT_DIR):
                    for entry in os.listdir(VARIANT_DIR):
                        p = os.path.join(VARIANT_DIR, entry)
                        try:
                            if os.path.isfile(p):
                                os.unlink(p)
                            elif os.path.isdir(p):
                                shutil.rmtree(p)
                        except OSError as e:
                            job.log_line(f"cid: {entry} - {e}")
            elif only_group == 0:
                # nur die direkten Files in VARIANT_DIR (nicht group-Subdirs)
                if os.path.isdir(VARIANT_DIR):
                    for entry in os.listdir(VARIANT_DIR):
                        p = os.path.join(VARIANT_DIR, entry)
                        if os.path.isfile(p):
                            try:
                                os.unlink(p)
                            except OSError as e:
                                job.log_line(f"cid: {entry} - {e}")
            else:
                gdir = _group_variant_dir(only_group)
                if os.path.isdir(gdir):
                    try:
                        shutil.rmtree(gdir)
                        os.makedirs(gdir, exist_ok=True)
                    except OSError as e:
                        job.log_line(f"cid group {only_group}: {e}")
            job.tick(ok=1)
            job.log_line("CID-Varianten weg")

            # 2) Cloudinary DB
            n_cloud = db.delete_all_cloudinary_uploads(uid, group_id=only_group)
            job.tick(ok=1)
            job.log_line(f"Cloudinary DB: {n_cloud} uploads gelöscht")

            # 3) S3 DB
            n_s3 = db.delete_all_s3_uploads(uid, group_id=only_group)
            job.tick(ok=1)
            job.log_line(f"S3 DB: {n_s3} uploads gelöscht")

            job.finish("done")
        except Exception as e:
            job.finish("error", str(e))

    threading.Thread(target=worker, daemon=True).start()
    return HTMLResponse(
        f'<div class="alert alert-info">Job #{job.id} — {scope_label} wird geleert. '
        f'Progress im Widget rechts unten.</div>'
    )


@router.post("/pool/build", response_class=HTMLResponse)
async def pool_build(request: Request,
                       source_mode: str = Form("all"),
                       group_id: int = Form(0),
                       logo_ids: str = Form(""),
                       total_variants: int = Form(100),
                       pct_cid: int = Form(34),
                       pct_cloudinary: int = Form(33),
                       pct_s3: int = Form(33),
                       cloudinary_proxy_id: int = Form(0),
                       s3_account_id: int = Form(0),
                       target_group_id: int = Form(0)):
    """Baut in einem Rutsch: X CID + Y Cloudinary + Z S3-Varianten aus den
    gewählten Sources. Parallel via ThreadPoolExecutor(3). Ein Job."""
    db = request.app.state.db
    uid = request.state.user["id"]
    cfg = db.get_config()

    sources = _collect_sources(db, uid, source_mode, group_id, logo_ids)
    if not sources:
        return HTMLResponse('<div class="alert alert-warning">Keine Source-Logos '
                             'gefunden — leg welche unter /logos an.</div>')

    # UX-Fix: wenn Source aus einer Group kommt aber Target nicht explizit
    # gesetzt ist, geht der Ziel-Ordner automatisch in dieselbe Group.
    # Sonst hat man den klassischen Fall "Source=Pool2, Target leer=global,
    # Kampagne mit logo_group_id=2 findet 0 Varianten und der User denkt
    # 'er hat wieder den ersten Pool genommen'."
    if source_mode == "group" and group_id and not target_group_id:
        target_group_id = group_id
        logger.info("pool_build: target_group_id auto-set auf %d (source group)",
                     group_id)

    total_variants = max(1, min(int(total_variants or 1), 5000))
    pct_cid = max(0, min(100, int(pct_cid or 0)))
    pct_cloudinary = max(0, min(100, int(pct_cloudinary or 0)))
    pct_s3 = max(0, min(100, int(pct_s3 or 0)))
    tot_pct = pct_cid + pct_cloudinary + pct_s3
    if tot_pct <= 0:
        return HTMLResponse('<div class="alert alert-warning">Verteilung ist 0/0/0 — '
                             'setz mindestens einen Provider auf > 0%.</div>')

    n_cid = total_variants * pct_cid // tot_pct
    n_cloud = total_variants * pct_cloudinary // tot_pct
    n_s3 = total_variants - n_cid - n_cloud   # Rest zu S3
    if pct_s3 == 0:
        # User will kein S3 — Rest auf Cloudinary oder CID zurück
        n_cloud += n_s3
        n_s3 = 0
    if pct_cloudinary == 0 and n_cloud > 0:
        n_cid += n_cloud
        n_cloud = 0

    # Provider-Readiness
    cloud_name = cfg.get("cloudinary_cloud_name", "")
    cloud_key = cfg.get("cloudinary_api_key", "")
    cloud_sec = cfg.get("cloudinary_api_secret", "")
    cloud_ready = bool(cloud_name and cloud_key and cloud_sec)
    if n_cloud > 0 and not cloud_ready:
        return HTMLResponse('<div class="alert alert-danger">Cloudinary-Credentials '
                             'fehlen — trag sie unter /cloudinary ein oder setz '
                             'Cloudinary-% auf 0.</div>')
    if n_cloud > 0 and not cloudinary_proxy_id:
        return HTMLResponse('<div class="alert alert-danger">Cloudinary braucht '
                             'einen Proxy — wähl einen aus oder setz Cloudinary-% '
                             'auf 0.</div>')

    s3_acc = None
    s3_buckets = []
    s3_proxy = ""
    if n_s3 > 0:
        if not s3_account_id:
            return HTMLResponse('<div class="alert alert-danger">S3 braucht '
                                 'einen Account — wähl einen aus oder setz '
                                 'S3-% auf 0.</div>')
        row = db.get_s3_account(s3_account_id)
        if not row:
            return HTMLResponse('<div class="alert alert-danger">S3-Account '
                                 'nicht gefunden.</div>')
        s3_acc = dict(row)
        from mailer.s3_uploader import parse_buckets_field
        s3_buckets = parse_buckets_field(s3_acc.get("buckets", ""))
        if not s3_buckets:
            return HTMLResponse('<div class="alert alert-danger">S3-Account hat '
                                 'keine Buckets — leg welche unter /s3-logos an.</div>')
        # Proxy vom Account
        pid = int(s3_acc.get("proxy_id") or 0)
        if pid:
            prow = db.get_proxy(pid)
            if prow:
                val = (dict(prow).get("value") or "").strip()
                s3_proxy = val.splitlines()[0].strip() if val else ""

    # Cloudinary Proxy-Lines
    cloud_lines = []
    if n_cloud > 0:
        prow = db.get_proxy(cloudinary_proxy_id)
        if not prow:
            return HTMLResponse('<div class="alert alert-danger">Cloudinary-Proxy '
                                 'nicht gefunden.</div>')
        pd = dict(prow)
        raw = (pd.get("value") or "").strip()
        if pd.get("proxy_type") == "pool":
            cloud_lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        elif raw:
            cloud_lines = [raw.splitlines()[0].strip()]
        if not cloud_lines:
            return HTMLResponse('<div class="alert alert-danger">Cloudinary-Proxy '
                                 'ist leer.</div>')

    # CID target dir
    from .logos import _group_variant_dir
    cid_dir = _group_variant_dir(target_group_id)

    job = job_manager.create(
        "pool_build", uid,
        f"Pool bauen: {n_cid} CID + {n_cloud} Cloudinary + {n_s3} S3 "
        f"({len(sources)} Source-Logo(s))",
        total=n_cid + n_cloud + n_s3, page_url="/pool")

    def _cid_worker():
        if n_cid <= 0:
            return
        per_src = max(1, n_cid // len(sources))
        made = 0
        for src_name, src_path in sources:
            if job.cancelled() or made >= n_cid:
                break
            ext = os.path.splitext(src_path)[1].lower() or ".png"
            for i in range(per_src):
                if job.cancelled() or made >= n_cid:
                    break
                out = os.path.join(cid_dir,
                                    f"pool_{secrets.token_hex(4)}{ext}")
                try:
                    body = _tweaked_bytes(src_path,
                                           seed=random.randint(0, 9999999))
                    with open(out, "wb") as fh:
                        fh.write(body)
                    job.tick(ok=1)
                    made += 1
                except Exception as e:
                    job.tick(err=1)
                    job.log_line(f"cid #{made+1}: {e}")

    def _proxies_dict(line: str) -> dict:
        from .cloudinary import _proxies_dict as _pd
        return _pd(line)

    def _cloud_worker():
        if n_cloud <= 0:
            return
        try:
            from .cloudinary import _cloudinary_upload
        except Exception as e:
            job.log_line(f"cloudinary import failed: {e}")
            job.tick(err=n_cloud, done_delta=n_cloud)
            return
        per_src = max(1, n_cloud // len(sources))
        made = 0
        for src_name, src_path in sources:
            if job.cancelled() or made >= n_cloud:
                break
            base = "".join(c for c in os.path.splitext(src_name)[0]
                            if c.isalnum() or c in "-_") or "logo"
            up_id = db.add_cloudinary_upload(
                source_filename=src_name, base_public_id=base,
                folder="pool", count=per_src, pixel_tweak=1,
                proxy_id=cloudinary_proxy_id, user_id=uid,
                group_id=target_group_id)
            for i in range(per_src):
                if job.cancelled() or made >= n_cloud:
                    break
                public_id = f"{base}_{secrets.token_hex(3)}"
                proxies = _proxies_dict(cloud_lines[made % len(cloud_lines)])
                try:
                    body = _tweaked_bytes(src_path,
                                           seed=random.randint(0, 9999999))
                    resp = _cloudinary_upload(
                        cloud_name, cloud_key, cloud_sec,
                        body=body, filename=src_name,
                        public_id=public_id, folder="pool", proxies=proxies)
                    url = resp.get("secure_url", "")
                    if url:
                        db.add_cloudinary_link(up_id, public_id, url)
                        job.tick(ok=1)
                    else:
                        job.tick(err=1)
                        job.log_line(f"cloud #{made+1}: empty response")
                    made += 1
                except Exception as e:
                    job.tick(err=1)
                    job.log_line(f"cloud #{made+1}: {str(e)[:150]}")
                    made += 1

    def _s3_worker():
        if n_s3 <= 0:
            return
        try:
            from mailer.s3_uploader import s3_upload_object
        except Exception as e:
            job.log_line(f"s3 import failed: {e}")
            job.tick(err=n_s3, done_delta=n_s3)
            return
        per_src = max(1, n_s3 // len(sources))
        made = 0
        for src_name, src_path in sources:
            if job.cancelled() or made >= n_s3:
                break
            base = "".join(c for c in os.path.splitext(src_name)[0]
                            if c.isalnum() or c in "-_") or "logo"
            ext = os.path.splitext(src_name)[1].lower() or ".png"
            ctype = mimetypes.guess_type(src_name)[0] or "image/png"
            up_id = db.add_s3_upload(s3_account_id, src_name, uid,
                                       group_id=target_group_id)
            for i in range(per_src):
                if job.cancelled() or made >= n_s3:
                    break
                bucket, region = s3_buckets[made % len(s3_buckets)]
                key = f"pool/{base}/{secrets.token_hex(6)}{ext}"
                try:
                    body = _tweaked_bytes(src_path,
                                           seed=random.randint(0, 9999999))
                    url = s3_upload_object(
                        s3_acc["access_key"], s3_acc["secret_key"], region,
                        bucket, key, body, content_type=ctype,
                        public=True, proxy=s3_proxy, timeout=45)
                    db.add_s3_link(up_id, url, bucket, key)
                    job.tick(ok=1)
                    made += 1
                except Exception as e:
                    job.tick(err=1)
                    job.log_line(f"s3 #{made+1}: {str(e)[:150]}")
                    made += 1

    def worker():
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                futs = [pool.submit(_cid_worker),
                        pool.submit(_cloud_worker),
                        pool.submit(_s3_worker)]
                for f in futs:
                    try:
                        f.result()
                    except Exception as e:
                        job.log_line(f"sub-worker crashed: {e}")
            job.finish("done")
        except Exception as e:
            job.finish("error", str(e))

    threading.Thread(target=worker, daemon=True).start()
    return HTMLResponse(
        f'<div class="alert alert-info">Job #{job.id} gestartet — '
        f'{n_cid} CID + {n_cloud} Cloudinary + {n_s3} S3 aus {len(sources)} '
        f'Source-Logo(s). Live-Progress + Abbrechen im Widget rechts unten.</div>'
    )
