"""S3 als CDN-Quelle für Logo-URLs. Nutzt die bestehenden
trans_s3_accounts (die auch für Redirects verwendet werden) — dein
existierender IAM-Key funktioniert direkt hier weiter. Wir hängen nur
das buckets-Feld pro Account dran und bieten Auto-Bucket-Setup.

Multi-Bucket-Rotation: pro Upload wird ein Bucket round-robin gewählt →
Origin-Domain der URLs variiert (bucket-a.s3.eu-central-1... vs
bucket-b.s3.us-east-1...). Kombiniert mit Cloudinary im CDN-Pool ergibt
das maximale Origin-Diversität für Anti-Cluster."""
import io
import os
import secrets
import time
import threading
import logging
import random
import mimetypes
from html import escape

from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse

from ..jobs import job_manager

router = APIRouter()
logger = logging.getLogger("trans.s3")

UPLOAD_TMP = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "static", "uploads", "s3_src"))
os.makedirs(UPLOAD_TMP, exist_ok=True)


def _tweaked_bytes(src_path: str, seed: int) -> bytes:
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
        logger.warning("pixel tweak failed: %s", e)
        return open(src_path, "rb").read()


def _proxy_for_account(db, acc: dict) -> str:
    pid = int(acc.get("proxy_id") or 0)
    if not pid:
        return ""
    row = db.get_proxy(pid)
    if not row:
        return ""
    val = (dict(row).get("value") or "").strip()
    return val.splitlines()[0].strip() if val else ""


DEFAULT_REGIONS = [
    "eu-central-1", "eu-west-1", "eu-west-2", "eu-west-3", "eu-north-1",
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "ap-southeast-1", "ap-northeast-1",
]

# Pool an unauffälligen Bucket-Prefix-Wörtern. Aus einem Wort oder aus
# zwei kombiniert (z.B. "web-assets", "static-media") — sieht aus wie
# generische Company-CDN-Buckets. Kein "mailer" mehr im Namen.
_PREFIX_ADJ = [
    "web", "static", "public", "cdn", "img", "media", "assets",
    "content", "files", "storage", "uploads", "cache", "shared",
    "cloud", "prod", "app", "core", "edge", "digital", "resource",
    "site", "brand", "portal", "hub", "vault", "delivery", "pub",
]
_PREFIX_NOUN = [
    "assets", "media", "cdn", "store", "cache", "content", "img",
    "files", "static", "hub", "pool", "hosting", "data", "cloud",
    "vault", "share", "delivery", "pack",
]


def _random_bucket_name(prefix: str = "", region: str = "") -> str:
    """AWS-konformer Bucket-Name — nur a-z 0-9 -, 3-63 chars,
    startet/endet mit alphanum, unique via Random-Token.

    Wenn `prefix` gesetzt ist → fester Prefix + hex + stamp.
    Wenn `prefix` leer ist → 4 zufällige Naming-Strategien (25/25/25/25):
      1) generisches Substantiv:  media-a7f3-891234
      2) Adjektiv+Substantiv:     web-assets-a7f3-891234
      3) region-based (SDK-like): s3-eu-central-1-a7f3-891234
      4) reine hex-token:         a7f3b2c891234abc
    Ergebnis sieht in jedem Fall aus wie ein generischer AWS-Bucket, nie
    wie ein Mailer-Bucket. Wenn eine `region` reingegeben wird, ist die
    region-based Strategy verfügbar; sonst fällt Strategy 3 auf 1 zurück."""
    import secrets as _sec
    token = _sec.token_hex(6)
    stamp = str(int(time.time()))[-6:]

    if prefix and prefix.strip():
        pfx = "".join(c for c in prefix.lower()
                       if c.isalnum() or c == "-").strip("-")[:20] or "cdn"
        return f"{pfx}-{token}-{stamp}"

    strategy = _sec.randbelow(4)
    if strategy == 0:
        pfx = _sec.choice(_PREFIX_NOUN)
    elif strategy == 1:
        pfx = f"{_sec.choice(_PREFIX_ADJ)}-{_sec.choice(_PREFIX_NOUN)}"
    elif strategy == 2 and region:
        # AWS-Region reinbacken → sieht aus wie ein SDK-erzeugter Bucket
        pfx = f"s3-{region.lower()}"
    else:
        # Reine hex — keinerlei Wörter, wie ein UUID-Bucket
        # (kein separater Prefix, direkt token+stamp)
        return f"{token}{stamp}"
    return f"{pfx}-{token}-{stamp}"


@router.get("/s3-logos", response_class=HTMLResponse)
async def s3_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    accounts = [dict(a) for a in db.get_s3_accounts(uid)]
    source_logos = [dict(l) for l in db.get_logos(uid)]
    logo_groups = [dict(g) for g in db.get_logo_groups(uid)]
    # Für jede Gruppe die enthaltenen Logos zählen
    for g in logo_groups:
        g["logo_count"] = len([l for l in source_logos if l.get("group_id") == g["id"]])
    uploads = []
    for u in db.get_s3_uploads(uid):
        ud = dict(u)
        ud["links"] = [dict(l) for l in db.get_s3_links(u["id"])]
        uploads.append(ud)
    return request.app.state.templates.TemplateResponse(request, "s3_cdn.html", {
        "active": "s3_logos",
        "accounts": accounts,
        "source_logos": source_logos,
        "logo_groups": logo_groups,
        "regions": DEFAULT_REGIONS,
        "uploads": uploads,
        "pool_size": len(db.get_all_cdn_urls(uid)),
    })


@router.post("/s3-logos/accounts/{aid}/setup-bucket", response_class=HTMLResponse)
async def setup_bucket(request: Request, aid: int,
                        bucket: str = Form(""),
                        region: str = Form("eu-central-1"),
                        add_to_pool: int = Form(1)):
    """Full-Auto: Bucket erstellen (falls fehlt), Public-Access-Config
    setzen, Public-Read Policy anhängen. Danach zur Bucket-Liste
    des Accounts hinzufügen."""
    db = request.app.state.db
    uid = request.state.user["id"]
    row = db.get_s3_account(aid)
    if not row:
        return HTMLResponse('<div class="alert alert-danger">Account nicht gefunden.</div>')
    row = dict(row)
    if row.get("user_id", 0) not in (uid, 0):
        return HTMLResponse('<div class="alert alert-danger">Kein Zugriff auf diesen Account.</div>')
    bucket = "".join(c for c in bucket.strip().lower()
                       if c.isalnum() or c in ".-")
    region = region.strip() or "eu-central-1"
    if not bucket or len(bucket) < 3:
        return HTMLResponse('<div class="alert alert-warning">Bucket-Name '
                             'zu kurz (min. 3 Zeichen, nur a-z 0-9 . -).</div>')
    from mailer.s3_uploader import s3_setup_bucket, s3_ping
    proxy = _proxy_for_account(db, row)
    r = s3_setup_bucket(row["access_key"], row["secret_key"], region,
                         bucket, proxy=proxy)
    if not r.get("ok"):
        return HTMLResponse(
            f'<div class="alert alert-danger">Setup fehlgeschlagen: '
            f'{escape(r.get("error", ""))}</div>'
        )
    p = s3_ping(row["access_key"], row["secret_key"], region, bucket, proxy=proxy)
    if not p.get("ok"):
        return HTMLResponse(
            f'<div class="alert alert-warning">Setup lief, aber Test-PUT '
            f'schlug fehl: {escape(p.get("error", ""))}</div>'
        )
    if add_to_pool:
        current = (row.get("buckets") or "").strip()
        new_entry = f"{bucket}:{region}"
        entries = set()
        for e in current.replace("\n", ",").split(","):
            e = e.strip()
            if e:
                entries.add(e)
        if new_entry not in entries:
            entries.add(new_entry)
            new_buckets = "\n".join(sorted(entries))
            db.update_s3_account_buckets(aid, new_buckets, uid)
    steps_html = "".join(
        f'<li>{s["step"]}: {escape(str(s["result"]))}</li>'
        for s in r.get("steps", [])
    )
    return HTMLResponse(
        f'<div class="alert alert-success">✓ Bucket <code>{escape(bucket)}</code> '
        f'in <code>{escape(region)}</code> ist ready.</div>'
        f'<ul style="font-size:11px">{steps_html}</ul>'
        + ('<div class="form-help">Zur Bucket-Liste hinzugefügt — Reload für neue Übersicht.</div>'
           if add_to_pool else "")
    )


@router.post("/s3-logos/accounts/{aid}/test-buckets", response_class=HTMLResponse)
async def test_buckets(request: Request, aid: int):
    db = request.app.state.db
    row = db.get_s3_account(aid)
    if not row:
        return HTMLResponse('<span style="color:var(--red)">Account nicht gefunden</span>')
    row = dict(row)
    from mailer.s3_uploader import parse_buckets_field, s3_ping
    buckets = parse_buckets_field(row.get("buckets", ""))
    if not buckets:
        return HTMLResponse('<span style="color:var(--fg2);font-size:12px">Keine Buckets konfiguriert — nutze „Bucket auto-anlegen" oben.</span>')
    proxy = _proxy_for_account(db, row)
    results = []
    for b, r in buckets:
        p = s3_ping(row["access_key"], row["secret_key"], r, b, proxy=proxy)
        if p.get("ok"):
            results.append(f'<div style="color:var(--green);font-size:12px">✓ <code>{escape(b)}</code> ({escape(r)})</div>')
        else:
            results.append(f'<div style="color:var(--red);font-size:12px">✗ <code>{escape(b)}</code>: {escape(p.get("error", "")[:120])}</div>')
    return HTMLResponse("".join(results))


@router.post("/s3-logos/upload", response_class=HTMLResponse)
async def upload_logo(request: Request,
                        account_id: int = Form(0),
                        file: UploadFile = File(None),
                        count: int = Form(10),
                        base_name: str = Form(""),
                        pixel_tweak: str = Form("1")):
    db = request.app.state.db
    uid = request.state.user["id"]
    if not file or not file.filename:
        return HTMLResponse('<div class="alert alert-warning">Keine Datei gewählt.</div>')
    acc = db.get_s3_account(account_id)
    if not acc:
        return HTMLResponse('<div class="alert alert-danger">S3-Account nicht gefunden.</div>')
    acc = dict(acc)
    from mailer.s3_uploader import parse_buckets_field, s3_upload_object
    buckets = parse_buckets_field(acc.get("buckets", ""))
    if not buckets:
        return HTMLResponse('<div class="alert alert-danger">Account hat keine Buckets konfiguriert. Nutze „Bucket auto-anlegen" oder trag welche unter /redirects manuell ein.</div>')

    count = max(1, min(int(count or 1), 500))
    base_name = "".join(c for c in (base_name or "").strip()
                          if c.isalnum() or c in "-_") or "logo"
    tweak = bool(int(pixel_tweak or 0))

    safe = "".join(c for c in file.filename if c.isalnum() or c in ".-_") or "upload.bin"
    src_path = os.path.join(UPLOAD_TMP, f"{int(time.time())}_{secrets.token_hex(4)}_{safe}")
    raw = await file.read()
    with open(src_path, "wb") as fh:
        fh.write(raw)

    ext = os.path.splitext(safe)[1].lower() or ".png"
    content_type = mimetypes.guess_type(safe)[0] or "image/png"
    upload_id = db.add_s3_upload(account_id, file.filename, uid)

    proxy = _proxy_for_account(db, acc)
    job = job_manager.create(
        "s3_upload", uid,
        f"S3 Upload: {file.filename} × {count} → {len(buckets)} bucket(s)",
        total=count, page_url="/s3-logos")

    def worker():
        try:
            for i in range(count):
                if job.cancelled():
                    job.log_line(f"abgebrochen nach {i}/{count}")
                    break
                bucket, region = buckets[i % len(buckets)]
                key = f"{base_name}/{secrets.token_hex(6)}{ext}"
                try:
                    body = _tweaked_bytes(src_path, seed=i) if tweak else raw
                    url = s3_upload_object(
                        acc["access_key"], acc["secret_key"], region,
                        bucket, key, body, content_type=content_type,
                        public=True, proxy=proxy, timeout=45)
                    db.add_s3_link(upload_id, url, bucket, key)
                    job.tick(ok=1)
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
        f'<div class="alert alert-info">Job #{job.id} gestartet: {count} Varianten '
        f'verteilt über {len(buckets)} Bucket(s). Progress + Abbrechen im Job-Widget '
        f'unten rechts (folgt dir überall hin).</div>'
    )


@router.post("/s3-logos/auto-batch", response_class=HTMLResponse)
async def auto_batch(request: Request,
                       account_id: int = Form(0),
                       bucket_count: int = Form(3),
                       regions_csv: str = Form(""),
                       source_mode: str = Form("group"),
                       group_id: int = Form(0),
                       logo_ids: str = Form(""),
                       variants_per_logo: int = Form(50),
                       bucket_prefix: str = Form(""),
                       pixel_tweak: str = Form("1")):
    """Full-Auto-Batch:
      1) N random Buckets in ausgewählten Regionen anlegen + konfigurieren
      2) Buckets an Account.buckets anhängen
      3) Für jedes Source-Logo Y Varianten hochladen, verteilt auf alle
         neuen Buckets
    Ein Klick, alles fertig. Progress-Anzeige."""
    db = request.app.state.db
    uid = request.state.user["id"]
    acc = db.get_s3_account(account_id)
    if not acc:
        return HTMLResponse('<div class="alert alert-danger">S3-Account nicht gefunden.</div>')
    acc = dict(acc)

    # Regionen parsen
    picked_regions = [r.strip() for r in regions_csv.split(",") if r.strip()]
    if not picked_regions:
        picked_regions = DEFAULT_REGIONS[:5]
    bucket_count = max(1, min(int(bucket_count or 1), 30))
    variants_per_logo = max(1, min(int(variants_per_logo or 1), 500))
    tweak = bool(int(pixel_tweak or 0))

    # Source-Logos einsammeln
    source_paths = []
    if source_mode == "group" and group_id:
        for l in db.get_logos_by_group(group_id):
            ld = dict(l)
            p = _resolve_logo_path(ld.get("file_path", ""))
            if p and os.path.isfile(p):
                source_paths.append((ld.get("filename") or os.path.basename(p), p))
    elif source_mode == "logos" and logo_ids.strip():
        wanted = {int(x) for x in logo_ids.split(",") if x.strip().isdigit()}
        for l in db.get_logos(uid):
            ld = dict(l)
            if ld["id"] in wanted:
                p = _resolve_logo_path(ld.get("file_path", ""))
                if p and os.path.isfile(p):
                    source_paths.append((ld.get("filename") or os.path.basename(p), p))
    elif source_mode == "all":
        for l in db.get_logos(uid):
            ld = dict(l)
            p = _resolve_logo_path(ld.get("file_path", ""))
            if p and os.path.isfile(p):
                source_paths.append((ld.get("filename") or os.path.basename(p), p))
    if not source_paths:
        return HTMLResponse('<div class="alert alert-warning">Keine Source-Logos gefunden — lade welche in <a href="/logos" style="color:var(--accent)">/logos</a> hoch.</div>')

    total_uploads = bucket_count + len(source_paths) * variants_per_logo

    from mailer.s3_uploader import (s3_setup_bucket, s3_upload_object,
                                      parse_buckets_field)
    proxy = _proxy_for_account(db, acc)
    job = job_manager.create(
        "s3_auto_batch", uid,
        f"S3 Auto-Batch: {bucket_count} buckets × {len(source_paths)} logos × {variants_per_logo}",
        total=total_uploads, page_url="/s3-logos")

    def worker():
        try:
            # 1) Buckets erzeugen — random name + round-robin region
            new_buckets = []   # [(bucket, region)]
            for i in range(bucket_count):
                if job.cancelled():
                    job.log_line("abgebrochen vor bucket-setup")
                    break
                region = picked_regions[i % len(picked_regions)]
                name = _random_bucket_name(bucket_prefix, region=region)
                r = s3_setup_bucket(acc["access_key"], acc["secret_key"],
                                     region, name, proxy=proxy)
                if r.get("ok"):
                    new_buckets.append((name, region))
                    job.tick(ok=1)
                    job.log_line(f"✓ bucket {name} ({region})")
                else:
                    job.tick(err=1)
                    job.log_line(f"✗ bucket {name}: {r.get('error', '')[:120]}")

            if not new_buckets:
                job.log_line("Keine Buckets konnten angelegt werden — abbruch.")
                job.finish("error", "Keine Buckets angelegt")
                return

            # Buckets in Account-Config eintragen
            existing = (acc.get("buckets") or "").strip()
            existing_set = set()
            for e in existing.replace("\n", ",").split(","):
                e = e.strip()
                if e:
                    existing_set.add(e)
            for b, r in new_buckets:
                existing_set.add(f"{b}:{r}")
            db.update_s3_account_buckets(account_id, "\n".join(sorted(existing_set)), uid)

            # 2) Für jedes Source-Logo Varianten hochladen
            for src_name, src_path in source_paths:
                if job.cancelled():
                    break
                safe_base = "".join(c for c in os.path.splitext(src_name)[0]
                                      if c.isalnum() or c in "-_") or "logo"
                ext = os.path.splitext(src_name)[1].lower() or ".png"
                ctype = mimetypes.guess_type(src_name)[0] or "image/png"
                upload_id = db.add_s3_upload(account_id, src_name, uid)
                for i in range(variants_per_logo):
                    if job.cancelled():
                        job.log_line(f"abgebrochen bei {src_name}#{i}")
                        break
                    bucket, region = new_buckets[i % len(new_buckets)]
                    key = f"{safe_base}/{secrets.token_hex(6)}{ext}"
                    try:
                        body = _tweaked_bytes(src_path, seed=i) if tweak else open(src_path, "rb").read()
                        url = s3_upload_object(
                            acc["access_key"], acc["secret_key"], region,
                            bucket, key, body, content_type=ctype,
                            public=True, proxy=proxy, timeout=45)
                        db.add_s3_link(upload_id, url, bucket, key)
                        job.tick(ok=1)
                    except Exception as e:
                        job.tick(err=1)
                        job.log_line(f"✗ {src_name}#{i+1}: {str(e)[:150]}")
            job.finish("done")
        except Exception as e:
            job.finish("error", str(e))

    threading.Thread(target=worker, daemon=True).start()
    return HTMLResponse(
        f'<div class="alert alert-info">'
        f'Job #{job.id} gestartet: Auto-Batch mit {bucket_count} Buckets '
        f'× {len(source_paths)} Logo(s) × {variants_per_logo} Varianten = '
        f'{total_uploads} Ops. Live-Progress + Abbrechen im Job-Widget (unten rechts).</div>'
    )


def _resolve_logo_path(file_path: str) -> str:
    """Wandelt /static/... in absoluten Filesystem-Pfad."""
    if not file_path:
        return ""
    if file_path.startswith("/static/"):
        return os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", file_path.lstrip("/")))
    return file_path


@router.post("/s3-logos/uploads/{upload_id}/delete")
async def delete_upload(request: Request, upload_id: int):
    db = request.app.state.db
    uid = request.state.user["id"]
    db.delete_s3_upload(upload_id, uid)
    return RedirectResponse("/s3-logos", status_code=303)


@router.post("/s3-logos/accounts/{aid}/delete-bucket", response_class=HTMLResponse)
async def delete_bucket(request: Request, aid: int,
                          bucket: str = Form(""),
                          region: str = Form("us-east-1")):
    """Leert Bucket (alle Objekte weg) und löscht ihn dann bei AWS.
    Danach aus Account.buckets rauswerfen und alle trans_s3_links, die auf
    diesen Bucket zeigen, verwaisen lassen (bleiben als Historie im
    upload-Datensatz, sind aber tote URLs — der User wirft die eh via
    Upload-Delete raus)."""
    db = request.app.state.db
    uid = request.state.user["id"]
    row = db.get_s3_account(aid)
    if not row:
        return HTMLResponse('<span style="color:var(--red)">Account nicht gefunden</span>')
    row = dict(row)
    if row.get("user_id", 0) not in (uid, 0):
        return HTMLResponse('<span style="color:var(--red)">Kein Zugriff auf diesen Account.</span>')
    bucket = "".join(c for c in bucket.strip().lower()
                       if c.isalnum() or c in ".-")
    region = region.strip() or "us-east-1"
    if not bucket:
        return HTMLResponse('<span style="color:var(--red)">Bucket-Name fehlt.</span>')
    from mailer.s3_uploader import s3_empty_and_delete_bucket
    proxy = _proxy_for_account(db, row)
    r = s3_empty_and_delete_bucket(row["access_key"], row["secret_key"],
                                     region, bucket, proxy=proxy, timeout=90)
    if not r.get("ok"):
        return HTMLResponse(
            f'<div class="alert alert-danger" style="margin:6px 0">'
            f'Löschen fehlgeschlagen: {escape(r.get("error", ""))} '
            f'(vorher {r.get("deleted", 0)} Objekte gelöscht)</div>'
        )
    # Aus Bucket-Liste des Accounts entfernen
    current = (row.get("buckets") or "").strip()
    target = f"{bucket}:{region}"
    kept = []
    for e in current.replace("\n", ",").split(","):
        e = e.strip()
        if e and e != target and e != bucket:
            kept.append(e)
    db.update_s3_account_buckets(aid, "\n".join(sorted(set(kept))), uid)
    note = r.get("note", "")
    detail = f" (Objekte {r.get('deleted', 0)})"
    if note:
        detail += f" · {note}"
    return HTMLResponse(
        f'<div class="alert alert-success" style="margin:6px 0">'
        f'✓ Bucket <code>{escape(bucket)}</code> ({escape(region)}) '
        f'gelöscht{escape(detail)}. '
        f'<a href="/s3-logos" style="color:var(--accent)">Reload</a></div>'
    )


@router.post("/s3-logos/accounts/{aid}/delete-all-buckets", response_class=HTMLResponse)
async def delete_all_buckets(request: Request, aid: int):
    """Nuke: alle konfigurierten Buckets dieses Accounts leeren + droppen.
    Läuft im Background-Thread (kann bei 30 Buckets × hunderten Objekten
    ein paar Minuten dauern). Status via /s3-logos/progress."""
    db = request.app.state.db
    uid = request.state.user["id"]
    row = db.get_s3_account(aid)
    if not row:
        return HTMLResponse('<div class="alert alert-danger">Account nicht gefunden.</div>')
    row = dict(row)
    if row.get("user_id", 0) not in (uid, 0):
        return HTMLResponse('<div class="alert alert-danger">Kein Zugriff auf diesen Account.</div>')
    from mailer.s3_uploader import parse_buckets_field, s3_empty_and_delete_bucket
    buckets = parse_buckets_field(row.get("buckets", ""))
    if not buckets:
        return HTMLResponse('<div class="alert alert-info">Keine Buckets — nichts zu tun.</div>')
    proxy = _proxy_for_account(db, row)
    job = job_manager.create(
        "s3_delete_all", uid,
        f"S3 Delete-All: {len(buckets)} bucket(s) (Account #{aid})",
        total=len(buckets), page_url="/s3-logos")

    def worker():
        try:
            kept = []
            for b, r in buckets:
                if job.cancelled():
                    job.log_line(f"abgebrochen — {len(kept)} Buckets nicht bearbeitet")
                    kept.extend(f"{bb}:{rr}" for bb, rr in buckets[len(kept):])
                    break
                try:
                    res = s3_empty_and_delete_bucket(
                        row["access_key"], row["secret_key"], r, b,
                        proxy=proxy, timeout=120)
                    if res.get("ok"):
                        job.tick(ok=1)
                        note = f" ({res.get('note')})" if res.get("note") else ""
                        job.log_line(f"✓ {b} ({r}) — {res.get('deleted', 0)} Objekte{note}")
                    else:
                        kept.append(f"{b}:{r}")
                        job.tick(err=1)
                        job.log_line(f"✗ {b} ({r}): {res.get('error', '')[:150]}")
                except Exception as e:
                    kept.append(f"{b}:{r}")
                    job.tick(err=1)
                    job.log_line(f"✗ {b} ({r}): {str(e)[:150]}")
            db.update_s3_account_buckets(aid, "\n".join(sorted(set(kept))), uid)
            job.finish("done")
        except Exception as e:
            job.finish("error", str(e))

    threading.Thread(target=worker, daemon=True).start()
    return HTMLResponse(
        f'<div class="alert alert-warning">Job #{job.id} gestartet: lösche {len(buckets)} Bucket(s). '
        f'Live-Status + Abbrechen im Job-Widget (unten rechts).</div>'
    )


@router.post("/s3-logos/accounts/{aid}/delete-empty-buckets", response_class=HTMLResponse)
async def delete_empty_buckets(request: Request, aid: int):
    """Bulk-Cleanup: alle Buckets des Accounts durchgehen, wenn leer →
    droppen. Rechnet auch mit Buckets die schon manuell gelöscht wurden."""
    db = request.app.state.db
    uid = request.state.user["id"]
    row = db.get_s3_account(aid)
    if not row:
        return HTMLResponse('<span style="color:var(--red)">Account nicht gefunden</span>')
    row = dict(row)
    if row.get("user_id", 0) not in (uid, 0):
        return HTMLResponse('<span style="color:var(--red)">Kein Zugriff auf diesen Account.</span>')
    from mailer.s3_uploader import (parse_buckets_field, s3_list_objects,
                                      s3_delete_bucket, S3Error)
    buckets = parse_buckets_field(row.get("buckets", ""))
    if not buckets:
        return HTMLResponse('<span style="color:var(--fg2);font-size:12px">Keine Buckets konfiguriert.</span>')
    proxy = _proxy_for_account(db, row)
    kept = []
    lines = []
    for b, r in buckets:
        try:
            keys, _ = s3_list_objects(row["access_key"], row["secret_key"],
                                        r, b, proxy=proxy, timeout=30)
        except S3Error as e:
            if e.status == 404 or "NoSuchBucket" in str(e):
                lines.append(f'<div style="color:var(--fg2);font-size:12px">– <code>{escape(b)}</code>: existiert nicht mehr, aus Liste entfernt.</div>')
                continue
            kept.append(f"{b}:{r}")
            lines.append(f'<div style="color:var(--red);font-size:12px">✗ <code>{escape(b)}</code>: {escape(str(e)[:120])}</div>')
            continue
        if keys:
            kept.append(f"{b}:{r}")
            lines.append(f'<div style="color:var(--fg2);font-size:12px">– <code>{escape(b)}</code>: {len(keys)}+ Objekte — behalten.</div>')
            continue
        try:
            s3_delete_bucket(row["access_key"], row["secret_key"], r, b,
                              proxy=proxy, timeout=30)
            lines.append(f'<div style="color:var(--green);font-size:12px">✓ <code>{escape(b)}</code> gelöscht.</div>')
        except S3Error as e:
            kept.append(f"{b}:{r}")
            lines.append(f'<div style="color:var(--red);font-size:12px">✗ <code>{escape(b)}</code> delete: {escape(str(e)[:120])}</div>')
    db.update_s3_account_buckets(aid, "\n".join(sorted(set(kept))), uid)
    return HTMLResponse("".join(lines) or '<span style="color:var(--fg2);font-size:12px">Nichts zu tun.</span>')
