"""Redirect Links — generate Google Share links, add, bulk-add, delete, clear."""
import random
import threading
import time
import logging
from html import escape
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from ..jobs import job_manager

logger = logging.getLogger("trans.redirects")
router = APIRouter()

AWS_REGIONS = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "af-south-1",
    "ap-east-1", "ap-south-1", "ap-south-2",
    "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
    "ap-southeast-1", "ap-southeast-2", "ap-southeast-3", "ap-southeast-4",
    "ca-central-1", "ca-west-1",
    "eu-central-1", "eu-central-2",
    "eu-west-1", "eu-west-2", "eu-west-3",
    "eu-north-1", "eu-south-1", "eu-south-2",
    "me-south-1", "me-central-1",
    "sa-east-1",
    "il-central-1",
]

# Subset used when picking a fresh region per-bucket. Restricted to
# regions with broad CDN propagation and zero opt-in friction — leaves
# out Brazil, Israel, Middle East, South Africa, opt-in APAC etc.
POPULAR_AWS_REGIONS = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "eu-west-1", "eu-west-2", "eu-west-3",
    "eu-central-1", "eu-north-1",
    "ca-central-1",
    "ap-northeast-1", "ap-southeast-1", "ap-southeast-2",
]

_gen_progress = {"running": False, "total": 0, "done": 0, "ok": 0, "errors": 0}
_s3_progress = {"running": False, "total": 0, "done": 0, "ok": 0, "errors": 0,
                "bucket": "", "stage": "", "region": "",
                "last_error": "", "aborted": False}

CONSECUTIVE_ERROR_LIMIT = 5


@router.get("/redirects", response_class=HTMLResponse)
async def redirects_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    redirects = [dict(r) for r in db.get_redirects(uid)]
    count = db.get_redirect_count(uid)
    redirect_pools = [dict(p, count=db.get_redirect_pool_count(p["id"])) for p in db.get_redirect_pools(uid)]
    cfg = db.get_config()
    s3_accounts = [dict(a) for a in db.get_s3_accounts(uid)]
    s3_configured = bool(s3_accounts) or bool(cfg.get("aws_access_key") and cfg.get("aws_secret_key"))
    proxies = [dict(p) for p in db.get_proxies(uid)]
    return request.app.state.templates.TemplateResponse(request, "redirects.html", {
        "active": "redirects", "redirects": redirects, "db": db,
        "redirect_count": count, "redirect_pools": redirect_pools,
        "gen_progress": _gen_progress, "s3_progress": _s3_progress,
        "s3_configured": s3_configured, "cfg": cfg,
        "aws_regions": AWS_REGIONS,
        "proxies": proxies,
        "s3_accounts": s3_accounts,
    })


def _resolve_proxy(db, cfg: dict) -> str:
    """Look up the AWS proxy value from trans_proxies via aws_proxy_id.
    Returns '' when none is selected or the entry no longer exists."""
    pid = int(cfg.get("aws_proxy_id", 0) or 0)
    if not pid:
        return ""
    row = db.get_proxy(pid)
    if not row:
        return ""
    return (dict(row).get("value") or "").strip()


def _resolve_proxy_by_id(db, proxy_id: int) -> str:
    if not proxy_id:
        return ""
    row = db.get_proxy(proxy_id)
    if not row:
        return ""
    return (dict(row).get("value") or "").strip()


def _resolve_proxy_list(db, proxy_id: int) -> list:
    """Nimmt eine Proxy-Row (single oder pool) und liefert eine Liste
    von Proxy-URLs die zum round-robin genutzt werden können.
    Format: host:port oder host:port:user:pass oder full URL."""
    if not proxy_id:
        return []
    row = db.get_proxy(proxy_id)
    if not row:
        return []
    value = (dict(row).get("value") or "").strip()
    if not value:
        return []
    return [ln.strip() for ln in value.splitlines() if ln.strip()]


def _resolve_s3_account(db, uid: int, account_id: int = 0,
                        region_override: str = "") -> dict:
    """Return {name, access_key, secret_key, region, bucket_prefix, proxy_val}.
    Picks the specified account or the primary if 0. Falls back to the
    legacy flat config if no accounts exist."""
    row = None
    if account_id:
        row = db.get_s3_account(account_id)
    if row is None:
        row = db.get_primary_s3_account(uid)
    if row is not None:
        r = dict(row)
        region = (region_override or r.get("region") or "eu-central-1").strip()
        return {
            "name": r.get("name", ""),
            "access_key": (r.get("access_key") or "").strip(),
            "secret_key": (r.get("secret_key") or "").strip(),
            "region": region or "eu-central-1",
            "bucket_prefix": (r.get("bucket_prefix") or "lk").strip() or "lk",
            "proxy_val": _resolve_proxy_by_id(db, r.get("proxy_id", 0) or 0),
        }
    # Legacy fall-through
    cfg = db.get_config()
    return {
        "name": "(legacy)",
        "access_key": (cfg.get("aws_access_key") or "").strip(),
        "secret_key": (cfg.get("aws_secret_key") or "").strip(),
        "region": (region_override or cfg.get("aws_region") or "eu-central-1").strip() or "eu-central-1",
        "bucket_prefix": (cfg.get("s3_bucket_prefix") or "lk").strip() or "lk",
        "proxy_val": _resolve_proxy(db, cfg),
    }


def _parse_s3_url(url: str) -> tuple:
    """Pull (region, bucket, key) out of a path-style S3 URL.
    Returns (None, None, None) when the URL doesn't look like one we
    generated."""
    import re
    m = re.match(r"^https://s3\.([a-z0-9-]+)\.amazonaws\.com/([a-z0-9.-]+)/(.+)$", url)
    if not m:
        return None, None, None
    return m.group(1), m.group(2), m.group(3)


@router.post("/redirects/append-ref-toggle")
async def toggle_append_ref(request: Request, append_ref: str = Form("")):
    db = request.app.state.db
    cfg = db.get_config()
    cfg["redirect_append_ref"] = bool(append_ref)
    db.save_config(cfg)
    return RedirectResponse("/redirects", status_code=303)


@router.post("/redirects/s3-accounts/add")
async def add_s3_account(request: Request,
                          name: str = Form(""),
                          access_key: str = Form(""),
                          secret_key: str = Form(""),
                          region: str = Form("eu-central-1"),
                          bucket_prefix: str = Form("lk"),
                          proxy_id: int = Form(0)):
    if not (name.strip() and access_key.strip() and secret_key.strip()):
        return RedirectResponse("/redirects", status_code=303)
    db = request.app.state.db
    uid = request.state.user["id"]
    db.add_s3_account(name.strip(), access_key.strip(), secret_key.strip(),
                      region.strip() or "eu-central-1",
                      bucket_prefix.strip() or "lk",
                      int(proxy_id or 0), uid)
    return RedirectResponse("/redirects", status_code=303)


@router.post("/redirects/s3-accounts/{aid}/update")
async def update_s3_account(request: Request, aid: int,
                             name: str = Form(""),
                             access_key: str = Form(""),
                             secret_key: str = Form(""),
                             region: str = Form("eu-central-1"),
                             bucket_prefix: str = Form("lk"),
                             proxy_id: int = Form(0)):
    fields = {}
    if name.strip():
        fields["name"] = name.strip()
    if access_key.strip():
        fields["access_key"] = access_key.strip()
    if secret_key.strip():
        fields["secret_key"] = secret_key.strip()
    if region.strip():
        fields["region"] = region.strip()
    fields["bucket_prefix"] = bucket_prefix.strip() or "lk"
    fields["proxy_id"] = int(proxy_id or 0)
    request.app.state.db.update_s3_account(aid, **fields)
    return RedirectResponse("/redirects", status_code=303)


@router.post("/redirects/s3-accounts/{aid}/set-primary")
async def set_primary_s3_account(request: Request, aid: int):
    request.app.state.db.set_primary_s3_account(aid)
    return RedirectResponse("/redirects", status_code=303)


@router.post("/redirects/s3-accounts/{aid}/delete")
async def delete_s3_account(request: Request, aid: int):
    request.app.state.db.delete_s3_account(aid)
    return RedirectResponse("/redirects", status_code=303)


@router.post("/redirects/s3-config")
async def save_s3_config(request: Request,
                          aws_access_key: str = Form(""),
                          aws_secret_key: str = Form(""),
                          s3_bucket_prefix: str = Form("lk"),
                          aws_proxy_id: int = Form(0)):
    db = request.app.state.db
    cfg = db.get_config()
    cfg["aws_access_key"] = aws_access_key.strip()
    cfg["aws_secret_key"] = aws_secret_key.strip()
    cfg["s3_bucket_prefix"] = s3_bucket_prefix.strip() or "lk"
    cfg["aws_proxy_id"] = int(aws_proxy_id or 0)
    db.save_config(cfg)
    return RedirectResponse("/redirects", status_code=303)


@router.post("/redirects/s3-test", response_class=HTMLResponse)
async def test_s3_connection(request: Request,
                              s3_account_id: int = Form(0)):
    """Run a fast list_buckets to verify creds + proxy for the chosen
    (or primary) S3 account."""
    db = request.app.state.db
    uid = request.state.user["id"]
    acc = _resolve_s3_account(db, uid, int(s3_account_id or 0))
    if not acc["access_key"] or not acc["secret_key"]:
        return HTMLResponse('<div class="alert alert-warning">Missing credentials for this account.</div>')
    try:
        from mailer.s3_redirect import make_s3_client
        s3 = make_s3_client(acc["access_key"], acc["secret_key"],
                            acc["region"], proxy=acc["proxy_val"])
        resp = s3.list_buckets()
        n = len(resp.get("Buckets", []))
        proxy_str = f" via proxy <code>{escape(acc['proxy_val'])}</code>" if acc["proxy_val"] else ""
        label = f" [{escape(acc['name'])}]" if acc.get("name") else ""
        return HTMLResponse(
            f'<div class="alert alert-success">OK{label} — {n} bucket(s) visible '
            f'(region {escape(acc["region"])}){proxy_str}.</div>'
        )
    except Exception as e:
        return HTMLResponse(
            f'<div class="alert alert-danger">Connection failed: {escape(str(e)[:300])}</div>'
        )


@router.get("/redirects/pool/{pid}/export")
async def export_pool(request: Request, pid: int):
    """Export one redirect pool's links as plain .txt."""
    from fastapi.responses import Response
    db = request.app.state.db
    links = [dict(r)["short_url"] for r in db.get_redirects_by_pool(pid)]
    pool_name = "pool"
    for p in db.get_redirect_pools(request.state.user["id"]):
        if dict(p)["id"] == pid:
            pool_name = "".join(c if c.isalnum() or c in "-_" else "_"
                                for c in dict(p)["name"]).strip("_") or f"pool{pid}"
            break
    return Response(
        content="\n".join(links),
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename={pool_name}.txt'},
    )


@router.post("/redirects/add-pool")
async def add_redirect_pool(request: Request, name: str = Form("")):
    if name.strip():
        uid = request.state.user["id"]
        request.app.state.db.add_redirect_pool(name.strip(), uid)
    return RedirectResponse("/redirects", status_code=303)


@router.post("/redirects/pool/{pid}/delete")
async def delete_redirect_pool(request: Request, pid: int):
    request.app.state.db.delete_redirect_pool(pid)
    return RedirectResponse("/redirects", status_code=303)


GOOGLE_DOMAINS = [
    "google.ch", "google.fr", "google.nl", "google.be", "google.se",
    "google.dk", "google.fi", "google.hu", "google.no", "google.com",
]


def _rewrite_share_google(url: str) -> str:
    """Convert https://share.google/ID → https://www.google.XX/share.google?q=ID
    with a random domain from the pool."""
    prefix = "https://share.google/"
    if not url.startswith(prefix):
        return url
    link_id = url[len(prefix):].split("?")[0].split("#")[0]
    if not link_id:
        return url
    domain = random.choice(GOOGLE_DOMAINS)
    return f"https://www.{domain}/share.google?q={link_id}"


@router.post("/redirects/generate-multi", response_class=HTMLResponse)
async def generate_multi_targets(request: Request,
                                   targets: str = Form(""),
                                   count_per_target: int = Form(1),
                                   gen_threads: int = Form(3),
                                   pool_id: int = Form(0),
                                   google_rewrite: str = Form(""),
                                   gen_proxy_id: int = Form(0)):
    """Ein share.google-Link pro Target-URL (oder N pro Target).
    Textarea mit einer Target-URL pro Zeile. Antibot-Wrapping wird
    per-Target angewendet (jedes Target kriegt seinen eigenen Antibot-Token)."""
    lines = [ln.strip() for ln in targets.splitlines() if ln.strip()]
    valid_targets = [t for t in lines
                     if t.startswith("http://") or t.startswith("https://")]
    if not valid_targets:
        return HTMLResponse('<div class="alert alert-warning">Keine gültigen URLs (jede muss mit http:// oder https:// anfangen).</div>')
    # kein Target-Cap mehr — Textarea kann so viele Zeilen haben wie du willst
    count_per_target = max(1, int(count_per_target or 1))
    gen_threads = max(1, min(int(gen_threads or 3), 50))

    db = request.app.state.db
    gen_uid = request.state.user["id"]
    do_rewrite = bool(google_rewrite)

    cfg_snapshot = db.get_config()
    antibot_active = (cfg_snapshot.get("antibot_enabled")
                      and cfg_snapshot.get("antibot_base_url")
                      and cfg_snapshot.get("antibot_hmac_secret"))

    total = len(valid_targets) * count_per_target
    gen_proxies = _resolve_proxy_list(db, int(gen_proxy_id or 0))
    job = job_manager.create(
        "google_share_multi", gen_uid,
        f"google.share Multi: {len(valid_targets)} Targets × {count_per_target}",
        total=total, page_url="/redirects")

    def worker():
        try:
            from mailer.redirect_manager import RedirectManager
            from concurrent.futures import ThreadPoolExecutor, as_completed

            work_items = []
            for real_target in valid_targets:
                submit_target = real_target
                if antibot_active:
                    from .antibot_config import build_antibot_url
                    submit_target = build_antibot_url(
                        cfg_snapshot["antibot_base_url"],
                        cfg_snapshot["antibot_hmac_secret"],
                        real_target,
                        ttl_seconds=int(cfg_snapshot.get("antibot_token_ttl_hours", 168)) * 3600,
                    )
                for _ in range(count_per_target):
                    work_items.append((real_target, submit_target))

            def gen_one(item):
                real, submit = item
                proxy = random.choice(gen_proxies) if gen_proxies else ""
                url = RedirectManager._generate_one(submit, proxy=proxy)
                return (real, url)

            with ThreadPoolExecutor(max_workers=gen_threads) as executor:
                futures = [executor.submit(gen_one, w) for w in work_items]
                for f in as_completed(futures):
                    if job.cancelled():
                        for pending in futures:
                            pending.cancel()
                        job.log_line("cancel — pending futures gecancelled")
                        break
                    try:
                        real, url = f.result(timeout=15)
                        if url:
                            if do_rewrite:
                                url = _rewrite_share_google(url)
                            db.add_redirect(url, real, gen_uid, pool_id)
                            job.tick(ok=1)
                        else:
                            job.tick(err=1)
                    except Exception as e:
                        job.tick(err=1)
                        job.log_line(str(e)[:150])
            job.finish("done")
        except Exception as e:
            logger.error("Multi-target gen error: %s", e, exc_info=True)
            job.finish("error", str(e))

    threading.Thread(target=worker, daemon=True).start()
    label = " (google.xx format)" if do_rewrite else ""
    ab_label = " · via antibot" if antibot_active else ""
    return HTMLResponse(
        f'<div class="alert alert-info">Job #{job.id} gestartet: {total} Links '
        f'({len(valid_targets)} Targets × {count_per_target} pro Target){label}{ab_label}. '
        f'Live-Progress + Abbrechen im Job-Widget (unten rechts).</div>'
    )


@router.post("/redirects/generate", response_class=HTMLResponse)
async def generate_redirects(request: Request,
                              target_url: str = Form(""),
                              count: int = Form(100),
                              gen_threads: int = Form(3),
                              pool_id: int = Form(0),
                              google_rewrite: str = Form(""),
                              gen_proxy_id: int = Form(0)):
    target = target_url.strip()
    if not target:
        return HTMLResponse(
            '<div class="alert alert-warning">Enter a target URL.</div>'
        )
    count = max(1, int(count or 1))
    gen_threads = max(1, min(gen_threads, 50))
    db = request.app.state.db
    gen_uid = request.state.user["id"]
    do_rewrite = bool(google_rewrite)

    cfg_snapshot = db.get_config()
    antibot_active = (cfg_snapshot.get("antibot_enabled")
                      and cfg_snapshot.get("antibot_base_url")
                      and cfg_snapshot.get("antibot_hmac_secret"))
    submit_target = target
    if antibot_active:
        from .antibot_config import build_antibot_url
        submit_target = build_antibot_url(
            cfg_snapshot["antibot_base_url"],
            cfg_snapshot["antibot_hmac_secret"],
            target,
            ttl_seconds=int(cfg_snapshot.get("antibot_token_ttl_hours", 168)) * 3600,
        )

    gen_proxies = _resolve_proxy_list(db, int(gen_proxy_id or 0))
    job = job_manager.create(
        "google_share", gen_uid,
        f"google.share: {target[:60]} × {count}",
        total=count, page_url="/redirects")

    def worker():
        try:
            from mailer.redirect_manager import RedirectManager
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def gen_one(_):
                proxy = random.choice(gen_proxies) if gen_proxies else ""
                return RedirectManager._generate_one(submit_target, proxy=proxy)

            with ThreadPoolExecutor(max_workers=gen_threads) as executor:
                futures = [executor.submit(gen_one, i) for i in range(count)]
                for f in as_completed(futures):
                    if job.cancelled():
                        for pending in futures:
                            pending.cancel()
                        job.log_line("cancel — pending futures gecancelled")
                        break
                    try:
                        url = f.result(timeout=15)
                        if url:
                            if do_rewrite:
                                url = _rewrite_share_google(url)
                            db.add_redirect(url, target, gen_uid, pool_id)
                            job.tick(ok=1)
                        else:
                            job.tick(err=1)
                    except Exception as e:
                        job.tick(err=1)
                        job.log_line(str(e)[:150])
            job.finish("done")
        except Exception as e:
            logger.error("Redirect generation error: %s", e, exc_info=True)
            job.finish("error", str(e))

    threading.Thread(target=worker, daemon=True).start()
    fmt_label = " (google.xx format)" if do_rewrite else ""
    antibot_label = " · via antibot" if antibot_active else ""
    return HTMLResponse(
        f'<div class="alert alert-info">Job #{job.id} gestartet: {count} '
        f'google.share Links mit {gen_threads} threads{fmt_label}{antibot_label}. '
        f'Live-Progress + Abbrechen im Job-Widget (unten rechts).</div>'
    )


@router.post("/redirects/generate-goto-test", response_class=HTMLResponse)
async def generate_goto_test(request: Request, target_url: str = Form(""),
                               targets: str = Form(""),
                               gen_proxy_id: int = Form(0),
                               engine: str = Form("playwright")):
    """Diagnose-Endpoint: ruft _generate_one_goto[_pw] mit debug=True für
    EINE Target-URL und gibt zurück was Google wirklich antwortet.
    engine=playwright (default) oder engine=requests."""
    target = target_url.strip()
    if not target and targets:
        for ln in targets.splitlines():
            ln = ln.strip()
            if ln.startswith("http://") or ln.startswith("https://"):
                target = ln
                break
    if not target:
        return HTMLResponse('<div class="alert alert-warning">Target-URL fehlt — '
                             'oben in der Textarea eine URL eintragen (mit http:// '
                             'oder https://).</div>')
    from mailer.redirect_manager import RedirectManager
    db = request.app.state.db
    gen_proxies = _resolve_proxy_list(db, int(gen_proxy_id or 0))
    proxy = gen_proxies[0] if gen_proxies else ""
    fn = (RedirectManager._generate_one_goto_pw if engine == "playwright"
          else RedirectManager._generate_one_goto)
    # Playwright's sync-API bricht wenn wir im asyncio-Loop sind (was in
    # FastAPI-async-Endpoints der Fall ist). run_in_thread wrapt den Aufruf
    # in einen echten Thread ohne event loop.
    import asyncio as _asyncio
    result = await _asyncio.to_thread(fn, target, proxy=proxy, debug=True)
    if not isinstance(result, tuple):
        return HTMLResponse(
            f'<div class="alert alert-danger">Kein Debug-Tuple zurück — '
            f'Code nicht deployed? Antwort: {escape(str(result))[:200]}</div>')
    url, status, snippet = result
    color = "success" if url else "danger"
    out = [f'<div class="alert alert-{color}">',
           f'<strong>Engine:</strong> {escape(engine)}<br>',
           f'<strong>Status:</strong> {escape(status)}<br>']
    if url:
        out.append(f'<strong>Token:</strong> <code style="word-break:break-all">{escape(url)}</code>')
    else:
        out.append('<strong>Kein Token gefunden.</strong>')
    out.append('</div>')
    if snippet:
        out.append('<details style="margin-top:8px"><summary style="cursor:pointer">'
                    'HTML-Snippet (erste 400 chars)</summary>'
                    f'<pre style="white-space:pre-wrap;font-size:11px;'
                    f'background:#f7f7f7;padding:8px;max-height:300px;overflow:auto">'
                    f'{escape(snippet)}</pre></details>')
    return HTMLResponse("".join(out))


@router.post("/redirects/generate-goto", response_class=HTMLResponse)
async def generate_goto_redirects(request: Request,
                                    targets: str = Form(""),
                                    count_per_target: int = Form(50),
                                    gen_threads: int = Form(3),
                                    pool_id: int = Form(0),
                                    gen_proxy_id: int = Form(0),
                                    engine: str = Form("playwright")):
    """Google `/goto?url=<token>` — signed open-redirect via SERP-scraping."""
    lines = [ln.strip() for ln in targets.splitlines() if ln.strip()]
    valid = [t for t in lines
             if t.startswith("http://") or t.startswith("https://")]
    if not valid:
        return HTMLResponse('<div class="alert alert-warning">Keine gültigen URLs '
                             '(jede muss mit http:// oder https:// anfangen).</div>')

    count_per_target = max(1, min(int(count_per_target or 1), 5000))
    # Playwright is CPU/RAM heavy — limit threads bei playwright engine.
    max_threads = 4 if engine == "playwright" else 20
    gen_threads = max(1, min(int(gen_threads or 3), max_threads))
    db = request.app.state.db
    gen_uid = request.state.user["id"]

    cfg_snapshot = db.get_config()
    antibot_active = (cfg_snapshot.get("antibot_enabled")
                      and cfg_snapshot.get("antibot_base_url")
                      and cfg_snapshot.get("antibot_hmac_secret"))

    gen_proxies = _resolve_proxy_list(db, int(gen_proxy_id or 0))
    total = len(valid) * count_per_target
    job = job_manager.create(
        "google_goto", gen_uid,
        f"google /goto ({engine}): {len(valid)} × {count_per_target} = {total}",
        total=total, page_url="/redirects")

    def worker():
        try:
            from mailer.redirect_manager import RedirectManager
            from concurrent.futures import ThreadPoolExecutor, as_completed

            use_pw = engine == "playwright"
            # Playwright: einen shared browser für den ganzen Batch —
            # spart die ~2s Startup pro Link.
            pw_ctx = None
            shared_browser = None
            if use_pw:
                try:
                    from playwright.sync_api import sync_playwright
                    pw_ctx = sync_playwright().start()
                    shared_browser = pw_ctx.chromium.launch(
                        headless=True,
                        args=["--disable-blink-features=AutomationControlled"])
                    job.log_line("playwright browser launched (shared)")
                except Exception as e:
                    job.log_line(f"playwright launch fail — fallback requests: {e}")
                    use_pw = False
                    if pw_ctx:
                        try: pw_ctx.stop()
                        except Exception: pass
                    pw_ctx = None
                    shared_browser = None

            for real_target in valid:
                if job.cancelled():
                    break
                submit_target = real_target
                if antibot_active:
                    from .antibot_config import build_antibot_url
                    submit_target = build_antibot_url(
                        cfg_snapshot["antibot_base_url"],
                        cfg_snapshot["antibot_hmac_secret"],
                        real_target,
                        ttl_seconds=int(cfg_snapshot.get("antibot_token_ttl_hours", 168)) * 3600,
                    )
                job.log_line(f"→ Target: {real_target[:70]}")

                def gen_one(_):
                    proxy = random.choice(gen_proxies) if gen_proxies else ""
                    if use_pw:
                        return RedirectManager._generate_one_goto_pw(
                            submit_target, proxy=proxy, _browser=shared_browser)
                    return RedirectManager._generate_one_goto(
                        submit_target, proxy=proxy)

                with ThreadPoolExecutor(max_workers=gen_threads) as executor:
                    futures = [executor.submit(gen_one, i) for i in range(count_per_target)]
                    for f in as_completed(futures):
                        if job.cancelled():
                            for pending in futures:
                                pending.cancel()
                            break
                        try:
                            timeout = 45 if use_pw else 25
                            url = f.result(timeout=timeout)
                            if url:
                                db.add_redirect(url, real_target, gen_uid, pool_id)
                                job.tick(ok=1)
                            else:
                                job.tick(err=1)
                        except Exception as e:
                            job.tick(err=1)
                            job.log_line(str(e)[:150])
            if shared_browser:
                try: shared_browser.close()
                except Exception: pass
            if pw_ctx:
                try: pw_ctx.stop()
                except Exception: pass
            job.finish("done")
        except Exception as e:
            logger.error("Google /goto gen error: %s", e, exc_info=True)
            job.finish("error", str(e))

    threading.Thread(target=worker, daemon=True).start()
    ab_label = " · via antibot" if antibot_active else ""
    return HTMLResponse(
        f'<div class="alert alert-info">Job #{job.id} gestartet ({engine}): {total} '
        f'google.com/goto Links ({len(valid)} × {count_per_target}){ab_label}. '
        f'Live-Progress + Abbrechen im Widget rechts unten. '
        f'&#9888; TTL 1-3 Tage — kurz vor Send generieren.</div>'
    )


@router.post("/redirects/generate-s3", response_class=HTMLResponse)
async def generate_s3_redirects(request: Request,
                                 target_url: str = Form(""),
                                 count: int = Form(50),
                                 tag: str = Form(""),
                                 region: str = Form("random"),
                                 pool_id: int = Form(0),
                                 bot_filter: str = Form(""),
                                 unique_bucket: str = Form(""),
                                 bucket_strategy: str = Form(""),
                                 shuffle_pool_size: int = Form(5),
                                 s3_account_id: int = Form(0)):
    target = target_url.strip()
    if not target:
        return HTMLResponse('<div class="alert alert-warning">Enter a target URL.</div>')
    if not (target.startswith("http://") or target.startswith("https://")):
        return HTMLResponse('<div class="alert alert-warning">Target URL must start with http:// or https://</div>')
    db = request.app.state.db
    uid = request.state.user["id"]
    region_override = (region or "").strip().lower()
    region_was_random = region_override in ("random", "")
    if region_was_random:
        chosen_region = random.choice(POPULAR_AWS_REGIONS)
    elif region_override not in AWS_REGIONS:
        return HTMLResponse(f'<div class="alert alert-warning">Unknown region: {escape(region_override)}</div>')
    else:
        chosen_region = region_override

    acc = _resolve_s3_account(db, uid, int(s3_account_id or 0),
                               region_override=chosen_region)
    access_key = acc["access_key"]
    secret_key = acc["secret_key"]
    bucket_prefix = acc["bucket_prefix"]
    proxy = acc["proxy_val"]
    region = acc["region"]
    account_label = acc.get("name", "")

    if not access_key or not secret_key:
        return HTMLResponse(
            '<div class="alert alert-warning">AWS credentials missing for the selected account. '
            'Add an S3 account in the S3 Accounts box below.</div>'
        )

    count = max(1, min(count, 5000))
    gen_uid = uid

    use_bot_filter = bool(bot_filter)
    # Bucket-Strategie:
    #   shared   — ein Bucket für alle Links (schnell, wenig Diversität)
    #   unique   — jeder Link kriegt eigenen Bucket (AWS erlaubt 10k/account seit Nov 2024)
    #   shuffled — Pool von N Buckets in N random Regions, pro Link random gepickt
    strat = (bucket_strategy or "").strip().lower()
    # Backwards-Compat: alte "unique_bucket" checkbox überschreibt wenn strat leer
    if not strat:
        strat = "unique" if unique_bucket else "shared"
    if strat not in ("shared", "unique", "shuffled"):
        strat = "shared"
    per_link_bucket = strat == "unique"
    shuffled = strat == "shuffled"
    region_per_bucket = per_link_bucket and region_was_random
    pool_n = max(1, min(int(shuffle_pool_size or 5), 500)) if shuffled else 0
    logger.info("S3 gen: account=%s strategy=%s region=%s count=%d target=%s pool_n=%d",
                account_label or "(primary)", strat, region, count, target[:80], pool_n)
    label_suffix = ""
    if shuffled:
        label_suffix = f" (shuffled {pool_n})"
    elif per_link_bucket:
        label_suffix = "/multi"
    job = job_manager.create(
        "s3_redirect_gen", gen_uid,
        f"S3 Redirects: {target[:50]} × {count} ({region}{label_suffix})",
        total=count, page_url="/redirects")

    def worker():
        consecutive = 0
        try:
            from mailer.s3_redirect import _new_bucket_name, make_s3_client, create_public_bucket
            from mailer.s3_redirect import _redirect_html, _random_suffix

            s3_clients = {}
            def _client_for(r: str):
                if r not in s3_clients:
                    s3_clients[r] = make_s3_client(
                        access_key, secret_key, r, proxy=proxy)
                return s3_clients[r]

            body = _redirect_html(target, bot_filter=use_bot_filter).encode("utf-8")

            def _spawn_bucket(r: str):
                cli = _client_for(r)
                b = _new_bucket_name(region=r)
                for attempt in range(3):
                    try:
                        create_public_bucket(cli, b, r)
                        return b
                    except cli.exceptions.BucketAlreadyOwnedByYou:
                        return b
                    except cli.exceptions.BucketAlreadyExists:
                        b = _new_bucket_name(region=r)
                    except Exception as e:
                        if attempt == 2:
                            raise
                        logger.warning("Bucket creation retry %d: %s", attempt + 1, e)
                        b = _new_bucket_name(region=r)
                return b

            shared_bucket = None
            shuffle_pool = []   # [(bucket, region), …] für strat=shuffled
            if shuffled:
                pool_regions = (random.sample(
                    POPULAR_AWS_REGIONS,
                    min(pool_n, len(POPULAR_AWS_REGIONS)))
                    if region_was_random else [region] * pool_n)
                while len(pool_regions) < pool_n:
                    pool_regions.append(random.choice(POPULAR_AWS_REGIONS))
                # Parallel bucket-setup — sequential wäre bei 500 Buckets
                # ~17 min bevor der erste Upload passiert. Mit 10 Workern
                # parallel schafft man 500 in ~1-2 min. AWS-Rate-Limit
                # für CreateBucket ist 100 req/s per account, weit weg.
                from concurrent.futures import ThreadPoolExecutor as _TPE
                job.log_line(f"pool setup: {pool_n} buckets parallel (10 workers)…")
                def _one_pool_bucket(r):
                    try:
                        return (r, _spawn_bucket(r), None)
                    except Exception as e:
                        return (r, None, str(e)[:180])
                results = []
                with _TPE(max_workers=10) as tpe:
                    for i, res in enumerate(
                            tpe.map(_one_pool_bucket, pool_regions[:pool_n]), 1):
                        results.append(res)
                        if i % 10 == 0 or i == pool_n:
                            ok_cnt = sum(1 for _r, b, _e in results if b)
                            job.log_line(f"pool {i}/{pool_n} — {ok_cnt} ok, {i - ok_cnt} fail")
                        if job.cancelled():
                            break
                for r, b, err in results:
                    if b:
                        shuffle_pool.append((b, r))
                    else:
                        job.log_line(f"pool bucket in {r} failed: {err}")
                job.log_line(f"pool ready: {len(shuffle_pool)}/{pool_n} buckets usable")
                if not shuffle_pool:
                    job.finish("error", "Kein einziger Bucket im Pool erstellbar")
                    return
            elif not per_link_bucket:
                shared_bucket = _spawn_bucket(region)
                job.log_line(f"shared bucket: {shared_bucket}")

            for i in range(count):
                if job.cancelled():
                    job.log_line(f"cancel bei {i}/{count}")
                    break
                if consecutive >= CONSECUTIVE_ERROR_LIMIT:
                    job.log_line(f"abort — {consecutive} consecutive errors")
                    job.finish("error", f"aborted nach {consecutive} consecutive errors")
                    return

                if shuffled:
                    bucket, this_region = random.choice(shuffle_pool)
                elif per_link_bucket:
                    this_region = (random.choice(POPULAR_AWS_REGIONS)
                                    if region_per_bucket else region)
                    try:
                        bucket = _spawn_bucket(this_region)
                    except Exception as e:
                        consecutive += 1
                        job.tick(err=1)
                        job.log_line(f"bucket {i+1} ({this_region}): {str(e)[:200]}")
                        continue
                else:
                    bucket = shared_bucket
                    this_region = region
                key = _random_suffix(10)
                try:
                    cli = _client_for(this_region)
                    cli.put_object(
                        Bucket=bucket, Key=key, Body=body,
                        ContentType="text/html; charset=utf-8",
                        CacheControl="no-cache",
                    )
                    url = f"https://s3.{this_region}.amazonaws.com/{bucket}/{key}"
                    db.add_redirect(url, target, gen_uid, pool_id)
                    job.tick(ok=1)
                    consecutive = 0
                except Exception as e:
                    consecutive += 1
                    job.tick(err=1)
                    job.log_line(f"upload {i+1}: {str(e)[:200]}")
                    logger.warning("S3 upload failed (%d/%d): %s", i + 1, count, e)
            job.finish("done")
        except Exception as e:
            logger.error("S3 generation error: %s", e, exc_info=True)
            job.finish("error", str(e))

    threading.Thread(target=worker, daemon=True).start()
    return HTMLResponse(
        f'<div class="alert alert-info">Job #{job.id} gestartet: {count} S3 Redirects. '
        f'Live-Progress + Abbrechen im Job-Widget (unten rechts).</div>'
    )


@router.post("/redirects/generate-s3-multi", response_class=HTMLResponse)
async def generate_s3_multi_redirects(request: Request,
                                        targets: str = Form(""),
                                        count_per_target: int = Form(50),
                                        tag: str = Form(""),
                                        region: str = Form("random"),
                                        pool_id: int = Form(0),
                                        bot_filter: str = Form(""),
                                        unique_bucket: str = Form(""),
                                        bucket_strategy: str = Form(""),
                                        shuffle_pool_size: int = Form(5),
                                        s3_account_id: int = Form(0)):
    """Wie generate_s3_redirects, aber für mehrere Targets in einer
    Textarea. Jedes Target kriegt N Links. Buckets werden über alle
    Targets geteilt (bzw. per-link neue wenn unique_bucket gesetzt) —
    spart AWS-Setup-Zeit. Alles in einem Job im Widget rechts unten."""
    lines = [ln.strip() for ln in targets.splitlines() if ln.strip()]
    valid = [t for t in lines
             if t.startswith("http://") or t.startswith("https://")]
    if not valid:
        return HTMLResponse('<div class="alert alert-warning">Keine gültigen URLs '
                             '(jede muss mit http:// oder https:// anfangen).</div>')

    db = request.app.state.db
    uid = request.state.user["id"]
    count_per_target = max(1, min(int(count_per_target or 1), 5000))
    region_override = (region or "").strip().lower()
    region_was_random = region_override in ("random", "")
    if region_was_random:
        chosen_region = random.choice(POPULAR_AWS_REGIONS)
    elif region_override not in AWS_REGIONS:
        return HTMLResponse(f'<div class="alert alert-warning">Unknown region: {escape(region_override)}</div>')
    else:
        chosen_region = region_override

    acc = _resolve_s3_account(db, uid, int(s3_account_id or 0),
                                region_override=chosen_region)
    access_key = acc["access_key"]
    secret_key = acc["secret_key"]
    bucket_prefix = acc["bucket_prefix"]
    proxy = acc["proxy_val"]
    region = acc["region"]
    if not access_key or not secret_key:
        return HTMLResponse(
            '<div class="alert alert-warning">AWS credentials fehlen — '
            'unter „S3 Accounts" anlegen.</div>')

    use_bot_filter = bool(bot_filter)
    strat = (bucket_strategy or "").strip().lower()
    if not strat:
        strat = "unique" if unique_bucket else "shared"
    if strat not in ("shared", "unique", "shuffled"):
        strat = "shared"
    per_link_bucket = strat == "unique"
    shuffled = strat == "shuffled"
    region_per_bucket = per_link_bucket and region_was_random
    pool_n = max(1, min(int(shuffle_pool_size or 5), 500)) if shuffled else 0
    total = len(valid) * count_per_target
    gen_uid = uid
    label_suffix = ""
    if shuffled:
        label_suffix = f" — shuffled pool of {pool_n}"
    elif per_link_bucket:
        label_suffix = " — unique/link"
    job = job_manager.create(
        "s3_redirect_multi", gen_uid,
        f"S3 Multi: {len(valid)} Targets × {count_per_target} = {total} Links{label_suffix}",
        total=total, page_url="/redirects")

    def worker():
        consecutive = 0
        try:
            from mailer.s3_redirect import (_new_bucket_name, make_s3_client,
                                              create_public_bucket, _redirect_html,
                                              _random_suffix)
            s3_clients = {}
            def _client_for(r: str):
                if r not in s3_clients:
                    s3_clients[r] = make_s3_client(
                        access_key, secret_key, r, proxy=proxy)
                return s3_clients[r]

            def _spawn_bucket(r: str):
                cli = _client_for(r)
                b = _new_bucket_name(region=r)
                for attempt in range(3):
                    try:
                        create_public_bucket(cli, b, r)
                        return b
                    except cli.exceptions.BucketAlreadyOwnedByYou:
                        return b
                    except cli.exceptions.BucketAlreadyExists:
                        b = _new_bucket_name(region=r)
                    except Exception as e:
                        if attempt == 2:
                            raise
                        b = _new_bucket_name(region=r)
                return b

            # Shared bucket über ALLE Targets (spart Setup-Zeit).
            # Wenn per_link_bucket=1 → jeder Link kriegt eigenen Bucket.
            # Wenn shuffled → Pool von N Buckets, pro Link random gepickt.
            shared_bucket = None
            shuffle_pool = []
            if shuffled:
                pool_regions = (random.sample(
                    POPULAR_AWS_REGIONS,
                    min(pool_n, len(POPULAR_AWS_REGIONS)))
                    if region_was_random else [region] * pool_n)
                while len(pool_regions) < pool_n:
                    pool_regions.append(random.choice(POPULAR_AWS_REGIONS))
                from concurrent.futures import ThreadPoolExecutor as _TPE
                job.log_line(f"pool setup: {pool_n} buckets parallel (10 workers)…")
                def _one_pool_bucket(r):
                    try:
                        return (r, _spawn_bucket(r), None)
                    except Exception as e:
                        return (r, None, str(e)[:180])
                results = []
                with _TPE(max_workers=10) as tpe:
                    for i, res in enumerate(
                            tpe.map(_one_pool_bucket, pool_regions[:pool_n]), 1):
                        results.append(res)
                        if i % 10 == 0 or i == pool_n:
                            ok_cnt = sum(1 for _r, b, _e in results if b)
                            job.log_line(f"pool {i}/{pool_n} — {ok_cnt} ok, {i - ok_cnt} fail")
                        if job.cancelled():
                            break
                for r, b, err in results:
                    if b:
                        shuffle_pool.append((b, r))
                    else:
                        job.log_line(f"pool bucket in {r} failed: {err}")
                job.log_line(f"pool ready: {len(shuffle_pool)}/{pool_n} buckets usable")
                if not shuffle_pool:
                    job.finish("error", "Kein einziger Bucket im Pool erstellbar")
                    return
            elif not per_link_bucket:
                shared_bucket = _spawn_bucket(region)
                job.log_line(f"shared bucket über alle Targets: {shared_bucket}")

            for t_idx, target in enumerate(valid, start=1):
                if job.cancelled():
                    job.log_line(f"cancel nach Target {t_idx-1}/{len(valid)}")
                    break
                body = _redirect_html(target, bot_filter=use_bot_filter).encode("utf-8")
                job.log_line(f"→ Target {t_idx}/{len(valid)}: {target[:60]}")

                for i in range(count_per_target):
                    if job.cancelled():
                        break
                    if consecutive >= CONSECUTIVE_ERROR_LIMIT:
                        job.log_line(f"abort — {consecutive} consecutive errors")
                        job.finish("error", f"aborted bei Target {t_idx} nach {consecutive} errors")
                        return

                    if shuffled:
                        bucket, this_region = random.choice(shuffle_pool)
                    elif per_link_bucket:
                        this_region = (random.choice(POPULAR_AWS_REGIONS)
                                        if region_per_bucket else region)
                        try:
                            bucket = _spawn_bucket(this_region)
                        except Exception as e:
                            consecutive += 1
                            job.tick(err=1)
                            job.log_line(f"bucket for T{t_idx}#{i+1}: {str(e)[:150]}")
                            continue
                    else:
                        bucket = shared_bucket
                        this_region = region
                    key = _random_suffix(10)
                    try:
                        cli = _client_for(this_region)
                        cli.put_object(
                            Bucket=bucket, Key=key, Body=body,
                            ContentType="text/html; charset=utf-8",
                            CacheControl="no-cache",
                        )
                        url = f"https://s3.{this_region}.amazonaws.com/{bucket}/{key}"
                        db.add_redirect(url, target, gen_uid, pool_id)
                        job.tick(ok=1)
                        consecutive = 0
                    except Exception as e:
                        consecutive += 1
                        job.tick(err=1)
                        job.log_line(f"T{t_idx}#{i+1}: {str(e)[:150]}")
            job.finish("done")
        except Exception as e:
            logger.error("S3 multi-target gen error: %s", e, exc_info=True)
            job.finish("error", str(e))

    threading.Thread(target=worker, daemon=True).start()
    bucket_mode = ({"shuffled": f"Shuffled Pool von {pool_n} Buckets",
                     "unique": "eigener Bucket pro Link",
                     "shared": "ein Shared-Bucket"}.get(strat, "ein Shared-Bucket"))
    return HTMLResponse(
        f'<div class="alert alert-info">Job #{job.id} gestartet: {len(valid)} Targets '
        f'× {count_per_target} = {total} S3 Redirects ({bucket_mode}). '
        f'Live-Progress + Abbrechen im Widget rechts unten.</div>'
    )


@router.get("/redirects/s3-status", response_class=HTMLResponse)
async def s3_gen_status(request: Request):
    p = _s3_progress
    bucket_html = ''
    if p["bucket"] or p.get("region"):
        region_str = f' <span style="margin-left:6px">Region: <code>{escape(p.get("region", ""))}</code></span>' if p.get("region") else ''
        bucket_str = f'Bucket: <code>{escape(p["bucket"])}</code>' if p["bucket"] else ''
        bucket_html = f'<div style="font-size:11px;color:var(--fg2);margin-bottom:6px">{bucket_str}{region_str}</div>'

    last_err_html = ''
    if p.get("last_error"):
        last_err_html = (
            f'<div style="margin-top:6px;padding:6px;border:1px solid var(--red);'
            f'border-radius:var(--radius);background:#ffeded;color:#a00;'
            f'font-family:monospace;font-size:11px;white-space:pre-wrap;max-height:120px;overflow:auto">'
            f'<strong>Last error:</strong> {escape(p["last_error"])}</div>'
        )

    if p["running"]:
        done = p["done"]
        total = p["total"] or 1
        pct = int(done / total * 100)
        return HTMLResponse(
            f'{bucket_html}'
            f'<div style="font-size:12px;margin-bottom:4px">Stage: {escape(p["stage"])}</div>'
            f'<div class="progress" style="margin-bottom:8px">'
            f'<div class="progress-bar" style="width:{pct}%">{done}/{p["total"]}</div></div>'
            f'<p style="font-size:12px;color:var(--fg2)">'
            f'Uploaded: {p["ok"]} OK, {p["errors"]} errors</p>'
            f'{last_err_html}'
        )

    if p.get("aborted"):
        return HTMLResponse(
            f'{bucket_html}'
            f'<div class="alert alert-danger">'
            f'Aborted: {escape(p["stage"])}<br>'
            f'<strong>{p["ok"]} OK</strong>, {p["errors"]} errors before stopping.'
            f'</div>{last_err_html}'
        )
    if p["stage"].startswith("error:"):
        return HTMLResponse(
            f'{bucket_html}'
            f'<div class="alert alert-danger">{escape(p["stage"])}</div>'
            f'{last_err_html}'
        )
    if p["ok"] > 0:
        return HTMLResponse(
            f'{bucket_html}'
            f'<div class="alert alert-success">Done! {p["ok"]} S3 redirect links generated. '
            f'<a href="/redirects" style="color:var(--accent)">Reload page</a></div>'
            f'{last_err_html if p["errors"] > 0 else ""}'
        )
    return HTMLResponse("")


def _re_upload_redirect(link_row: dict, new_target: str, bot_filter: bool,
                         access: str, secret: str, proxy: str,
                         client_cache: dict = None) -> tuple:
    """Re-PUT the HTML for one stored redirect link with a new destination
    using the given AWS credentials. client_cache maps region → boto client
    so a bulk pass doesn't rebuild one per object.
    Returns (ok: bool, error_msg: str)."""
    from mailer.s3_redirect import make_s3_client, _redirect_html
    region, bucket, key = _parse_s3_url(link_row.get("short_url", ""))
    if not bucket or not key or not region:
        return False, "URL not parseable as S3"
    if not access or not secret:
        return False, "AWS credentials missing"
    if client_cache is None:
        client_cache = {}
    if region not in client_cache:
        client_cache[region] = make_s3_client(access, secret, region, proxy=proxy)
    s3 = client_cache[region]
    body = _redirect_html(new_target, bot_filter=bot_filter).encode("utf-8")
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=body,
                      ContentType="text/html; charset=utf-8",
                      CacheControl="no-cache")
        return True, ""
    except Exception as e:
        return False, str(e)[:200]


@router.post("/redirects/{rid}/update-target", response_class=HTMLResponse)
async def update_link_target(request: Request, rid: int,
                              new_target: str = Form(""),
                              bot_filter: str = Form(""),
                              s3_account_id: int = Form(0)):
    new_target = new_target.strip()
    if not new_target:
        return HTMLResponse('<span style="color:var(--red)">Empty target</span>')
    if not (new_target.startswith("http://") or new_target.startswith("https://")):
        return HTMLResponse('<span style="color:var(--red)">Target must start with http:// or https://</span>')
    db = request.app.state.db
    uid = request.state.user["id"]
    row = db._conn().execute("SELECT * FROM trans_redirect_links WHERE id=?", (rid,)).fetchone()
    if not row:
        return HTMLResponse('<span style="color:var(--red)">Not found</span>')
    acc = _resolve_s3_account(db, uid, int(s3_account_id or 0))
    ok, err = _re_upload_redirect(dict(row), new_target, bool(bot_filter),
                                    acc["access_key"], acc["secret_key"],
                                    acc["proxy_val"])
    if not ok:
        return HTMLResponse(f'<span style="color:var(--red)">Failed: {escape(err)}</span>')
    db._conn().execute("UPDATE trans_redirect_links SET target_url=? WHERE id=?",
                        (new_target, rid))
    db._conn().commit()
    return HTMLResponse(f'<span class="badge badge-running">Updated</span>')


_pool_update_progress = {"running": False, "total": 0, "done": 0,
                          "ok": 0, "errors": 0, "pool_id": 0}


@router.post("/redirects/pool/{pid}/update-targets", response_class=HTMLResponse)
async def update_pool_targets(request: Request, pid: int,
                               new_target: str = Form(""),
                               bot_filter: str = Form(""),
                               s3_account_id: int = Form(0)):
    new_target = new_target.strip()
    if not new_target:
        return HTMLResponse('<div class="alert alert-warning">Empty target.</div>')
    if not (new_target.startswith("http://") or new_target.startswith("https://")):
        return HTMLResponse('<div class="alert alert-warning">Target must start with http:// or https://</div>')
    if _pool_update_progress["running"]:
        return HTMLResponse('<div class="alert alert-warning">Pool update already running.</div>')

    db = request.app.state.db
    uid = request.state.user["id"]
    acc = _resolve_s3_account(db, uid, int(s3_account_id or 0))
    if not acc["access_key"] or not acc["secret_key"]:
        return HTMLResponse('<div class="alert alert-warning">AWS credentials missing.</div>')
    use_bot_filter = bool(bot_filter)
    rows = [dict(r) for r in db.get_redirects_by_pool(pid)]
    if not rows:
        return HTMLResponse('<div class="alert alert-warning">Pool is empty.</div>')

    _pool_update_progress.update(running=True, total=len(rows), done=0,
                                  ok=0, errors=0, pool_id=pid)

    def worker():
        try:
            client_cache = {}
            for r in rows:
                ok, err = _re_upload_redirect(r, new_target, use_bot_filter,
                                                acc["access_key"], acc["secret_key"],
                                                acc["proxy_val"], client_cache)
                if ok:
                    db._conn().execute("UPDATE trans_redirect_links SET target_url=? WHERE id=?",
                                        (new_target, r["id"]))
                    db._conn().commit()
                    _pool_update_progress["ok"] += 1
                else:
                    _pool_update_progress["errors"] += 1
                    logger.warning("Pool target update failed for %d: %s", r["id"], err)
                _pool_update_progress["done"] += 1
        finally:
            _pool_update_progress["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return HTMLResponse(
        f'<div class="alert alert-info">Updating {len(rows)} links in pool to {escape(new_target)}…</div>'
        f'<div hx-get="/redirects/pool/{pid}/update-status" hx-trigger="every 2s" hx-swap="outerHTML"></div>'
    )


@router.get("/redirects/pool/{pid}/update-status", response_class=HTMLResponse)
async def pool_update_status(request: Request, pid: int):
    p = _pool_update_progress
    if p["pool_id"] != pid:
        return HTMLResponse("")
    if p["running"]:
        pct = int(p["done"] / p["total"] * 100) if p["total"] else 0
        return HTMLResponse(
            f'<div hx-get="/redirects/pool/{pid}/update-status" hx-trigger="every 2s" hx-swap="outerHTML">'
            f'<div class="progress" style="margin-bottom:6px">'
            f'<div class="progress-bar" style="width:{pct}%">{p["done"]}/{p["total"]}</div></div>'
            f'<p style="font-size:12px;color:var(--fg2)">Re-uploading objects…</p></div>'
        )
    if p["ok"] > 0 or p["errors"] > 0:
        return HTMLResponse(
            f'<div class="alert alert-success">Done. {p["ok"]} updated, {p["errors"]} errors. '
            f'<a href="/redirects" style="color:var(--accent)">Reload</a></div>'
        )
    return HTMLResponse("")


_global_update_progress = {"running": False, "total": 0, "done": 0,
                            "ok": 0, "errors": 0, "skipped": 0}


@router.post("/redirects/update-all-targets", response_class=HTMLResponse)
async def update_all_targets(request: Request,
                              new_target: str = Form(""),
                              bot_filter: str = Form(""),
                              s3_account_id: int = Form(0),
                              scope: str = Form("all")):
    """Re-PUT the redirect HTML for every S3-backed link the user owns to
    point at `new_target`. Non-S3 links (Google Share, manually pasted URLs)
    are silently skipped — they can't be repointed remotely.

    scope=all         → every user link
    scope=pooled      → only links assigned to any pool
    scope=unassigned  → only links with pool_id=0
    """
    new_target = new_target.strip()
    if not new_target:
        return HTMLResponse('<div class="alert alert-warning">Empty target.</div>')
    if not (new_target.startswith("http://") or new_target.startswith("https://")):
        return HTMLResponse('<div class="alert alert-warning">Target must start with http:// or https://</div>')
    if _global_update_progress["running"]:
        return HTMLResponse('<div class="alert alert-warning">Global update already running.</div>')

    db = request.app.state.db
    uid = request.state.user["id"]
    acc = _resolve_s3_account(db, uid, int(s3_account_id or 0))
    if not acc["access_key"] or not acc["secret_key"]:
        return HTMLResponse(
            '<div class="alert alert-warning">AWS credentials missing for the selected S3 account.</div>')

    use_bot_filter = bool(bot_filter)
    if scope == "pooled":
        where = "user_id=? AND pool_id IS NOT NULL AND pool_id!=0"
    elif scope == "unassigned":
        where = "user_id=? AND (pool_id IS NULL OR pool_id=0)"
    else:
        where = "user_id=?"
    rows = [dict(r) for r in db._conn().execute(
        f"SELECT * FROM trans_redirect_links WHERE {where}", (uid,)).fetchall()]
    # Skip anything that isn't a path-style S3 URL
    s3_rows = [r for r in rows if _parse_s3_url(r.get("short_url", ""))[0]]
    skipped_upfront = len(rows) - len(s3_rows)
    if not s3_rows:
        return HTMLResponse(
            f'<div class="alert alert-warning">No S3-backed links found in this scope '
            f'({len(rows)} total, {skipped_upfront} non-S3 skipped).</div>')

    _global_update_progress.update(running=True, total=len(s3_rows), done=0,
                                    ok=0, errors=0, skipped=skipped_upfront)

    def worker():
        try:
            client_cache = {}
            for r in s3_rows:
                ok, err = _re_upload_redirect(r, new_target, use_bot_filter,
                                                acc["access_key"], acc["secret_key"],
                                                acc["proxy_val"], client_cache)
                if ok:
                    db._conn().execute(
                        "UPDATE trans_redirect_links SET target_url=? WHERE id=?",
                        (new_target, r["id"]))
                    db._conn().commit()
                    _global_update_progress["ok"] += 1
                else:
                    _global_update_progress["errors"] += 1
                    logger.warning("Global target update failed for %d: %s",
                                    r["id"], err)
                _global_update_progress["done"] += 1
        finally:
            _global_update_progress["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    proxy_str = f" via <code>{escape(acc['proxy_val'])}</code>" if acc["proxy_val"] else ""
    return HTMLResponse(
        f'<div class="alert alert-info">Re-pointing {len(s3_rows)} S3 link(s) '
        f'({skipped_upfront} non-S3 skipped) to <code>{escape(new_target)}</code>'
        f'{proxy_str}…</div>'
        f'<div hx-get="/redirects/update-all-status" hx-trigger="every 2s" hx-swap="outerHTML"></div>'
    )


@router.get("/redirects/update-all-status", response_class=HTMLResponse)
async def global_update_status(request: Request):
    p = _global_update_progress
    if p["running"]:
        pct = int(p["done"] / p["total"] * 100) if p["total"] else 0
        return HTMLResponse(
            f'<div hx-get="/redirects/update-all-status" hx-trigger="every 2s" hx-swap="outerHTML">'
            f'<div class="progress" style="margin-bottom:6px">'
            f'<div class="progress-bar" style="width:{pct}%">{p["done"]}/{p["total"]}</div></div>'
            f'<p style="font-size:12px;color:var(--fg2)">'
            f'{p["ok"]} re-uploaded, {p["errors"]} errors</p></div>'
        )
    if p["ok"] > 0 or p["errors"] > 0:
        skipped_note = (f' · {p["skipped"]} non-S3 skipped'
                        if p.get("skipped") else '')
        return HTMLResponse(
            f'<div class="alert alert-success">Done — {p["ok"]} updated, '
            f'{p["errors"]} errors{skipped_note}. '
            f'<a href="/redirects" style="color:var(--accent)">Reload</a></div>'
        )
    return HTMLResponse("")


@router.post("/redirects/add")
async def add_redirect(request: Request, short_url: str = Form(""),
                       pool_id: int = Form(0)):
    url = short_url.strip()
    if url:
        uid = request.state.user['id']
        request.app.state.db.add_redirect(url, '', uid, pool_id)
    return RedirectResponse("/redirects", status_code=303)


@router.post("/redirects/bulk-add", response_class=HTMLResponse)
async def bulk_add(request: Request, urls: str = Form(""),
                     pool_id: int = Form(0)):
    db = request.app.state.db
    uid = request.state.user["id"]
    pool_id = max(0, int(pool_id or 0))
    # Verifiziere dass der Pool wirklich dem User gehört (sonst könnte
    # jemand mit gültiger Session in fremde Pools schreiben).
    if pool_id:
        pools = {int(dict(p)["id"]) for p in db.get_redirect_pools(uid)}
        if pool_id not in pools:
            return HTMLResponse(
                '<div class="alert alert-danger">Pool nicht gefunden oder '
                'gehört nicht dir.</div>'
            )
    added = 0
    for line in urls.splitlines():
        url = line.strip()
        if url and (url.startswith("http://") or url.startswith("https://")):
            db.add_redirect(url, '', uid, pool_id)
            added += 1
    if added:
        pool_note = f" in Pool #{pool_id}" if pool_id else " (kein Pool)"
        return HTMLResponse(
            f'<div class="alert alert-success">{added} redirect link(s) added'
            f'{pool_note}. '
            f'<a href="/redirects" style="color:var(--accent)">Reload page</a></div>'
        )
    return HTMLResponse(
        '<div class="alert alert-warning">No valid URLs found. '
        'Each line must start with http:// or https://</div>'
    )


@router.post("/redirects/{rid}/delete")
async def delete_redirect(request: Request, rid: int):
    request.app.state.db.delete_redirect(rid)
    return HTMLResponse("", status_code=200)


@router.post("/redirects/{rid}/set-pool", response_class=HTMLResponse)
async def set_redirect_pool(request: Request, rid: int,
                             pool_id: int = Form(0)):
    db = request.app.state.db
    uid = request.state.user["id"]
    db._conn().execute(
        "UPDATE trans_redirect_links SET pool_id=? WHERE id=?",
        (int(pool_id or 0), rid),
    )
    db._conn().commit()

    # Re-render the cell with the updated dropdown so the change sticks
    # visibly and a follow-up change re-submits cleanly.
    pools = [dict(p) for p in db.get_redirect_pools(uid)]
    cur = int(pool_id or 0)
    options = [f'<option value="0"{" selected" if not cur else ""}>— No Pool</option>']
    for p in pools:
        sel = " selected" if p["id"] == cur else ""
        options.append(f'<option value="{p["id"]}"{sel}>{escape(p["name"])}</option>')
    return HTMLResponse(
        f'<select hx-post="/redirects/{rid}/set-pool" '
        f'hx-target="#pool-cell-{rid}" hx-swap="innerHTML" hx-trigger="change" '
        f'name="pool_id" style="margin:0;padding:2px 4px;font-size:11px;max-width:160px">'
        + "".join(options) +
        '</select>'
    )


@router.post("/redirects/move-latest", response_class=HTMLResponse)
async def move_latest_to_pool(request: Request,
                               count: int = Form(100),
                               pool_id: int = Form(0)):
    """Move the N most recent unassigned (pool_id=0) links to a pool."""
    db = request.app.state.db
    uid = request.state.user["id"]
    count = max(1, min(int(count or 100), 50000))
    if not pool_id:
        return HTMLResponse('<span style="color:var(--red)">Pick a pool.</span>')
    rows = db._conn().execute(
        "SELECT id FROM trans_redirect_links "
        "WHERE user_id=? AND (pool_id IS NULL OR pool_id=0) "
        "ORDER BY id DESC LIMIT ?",
        (uid, count)).fetchall()
    if not rows:
        return HTMLResponse('<span style="color:var(--fg2)">No unassigned links found.</span>')
    ids = [r[0] for r in rows]
    ph = ",".join("?" for _ in ids)
    db._conn().execute(
        f"UPDATE trans_redirect_links SET pool_id=? WHERE id IN ({ph})",
        [pool_id, *ids])
    db._conn().commit()
    pool_row = db._conn().execute(
        "SELECT name FROM trans_redirect_pools WHERE id=?", (pool_id,)).fetchone()
    pname = dict(pool_row)["name"] if pool_row else str(pool_id)
    return HTMLResponse(
        f'<span style="color:var(--green)">'
        f'Moved {len(ids)} link(s) &rarr; <strong>{escape(pname)}</strong>. '
        f'<a href="/redirects" style="color:var(--accent)">Reload</a></span>'
    )


@router.post("/redirects/bulk-assign-pool", response_class=HTMLResponse)
async def bulk_assign_pool(request: Request):
    db = request.app.state.db
    form = await request.form()
    pool_id = int(form.get("pool_id", 0) or 0)
    raw_ids = form.getlist("link_ids[]") or form.getlist("link_ids")
    ids = []
    for v in raw_ids:
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            pass
    if not ids:
        return HTMLResponse('<span style="color:var(--red)">No links selected.</span>')
    placeholders = ",".join("?" for _ in ids)
    db._conn().execute(
        f"UPDATE trans_redirect_links SET pool_id=? WHERE id IN ({placeholders})",
        [pool_id, *ids],
    )
    db._conn().commit()
    target = "No Pool"
    if pool_id:
        row = db._conn().execute(
            "SELECT name FROM trans_redirect_pools WHERE id=?", (pool_id,)).fetchone()
        if row:
            target = dict(row)["name"]
    return HTMLResponse(
        f'<span style="color:var(--green)">'
        f'Moved {len(ids)} link(s) &rarr; <strong>{escape(target)}</strong>. '
        f'<a href="/redirects" style="color:var(--accent)">Reload</a></span>'
    )


@router.get("/redirects/export")
async def export_redirects(request: Request):
    """Export all redirect URLs as .txt download."""
    db = request.app.state.db
    uid = request.state.user['id']
    redirects = db.get_redirects(uid)
    lines = "\n".join(dict(r)["short_url"] for r in redirects)
    from fastapi.responses import Response
    return Response(content=lines, media_type="text/plain",
                    headers={"Content-Disposition": "attachment; filename=redirects.txt"})


@router.post("/redirects/clear")
async def clear_redirects(request: Request):
    uid = request.state.user['id']
    request.app.state.db.clear_redirects(uid)
    return RedirectResponse("/redirects", status_code=303)


@router.get("/redirects/status", response_class=HTMLResponse)
async def gen_status(request: Request):
    p = _gen_progress
    if p["running"]:
        done = p["done"]
        total = p["total"]
        pct = int(done / total * 100) if total > 0 else 0
        return HTMLResponse(
            f'<div class="progress" style="margin-bottom:8px">'
            f'<div class="progress-bar" style="width:{pct}%">{done}/{total}</div></div>'
            f'<p style="font-size:12px;color:var(--fg2)">'
            f'Generated: {p["ok"]} OK, {p["errors"]} errors</p>'
        )
    if p["ok"] > 0:
        return HTMLResponse(
            f'<div class="alert alert-success">Done! {p["ok"]} redirect links generated. '
            f'<a href="/redirects" style="color:var(--accent)">Reload page</a></div>'
        )
    if p["errors"] > 0:
        return HTMLResponse(
            '<div class="alert alert-danger">Generation failed. '
            'Check logs for details.</div>'
        )
    return HTMLResponse("")
