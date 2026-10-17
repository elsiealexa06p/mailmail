"""Cloudflare — Accounts, domain pulling, R2 storage, Unsub Worker deploy."""
import os
import json
import threading
import mimetypes
from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import List as TList

router = APIRouter()

_pull_results: dict = {}

WORKER_JS = os.path.join(os.path.dirname(__file__), "..", "..", "cloudflare", "unsubscribe-worker.js")


def _get_r2(db, cid: int):
    """Build R2Manager from a CF account row."""
    from bulk.mailer.r2_manager import R2Manager
    row = db._conn().execute("SELECT * FROM cf_accounts WHERE id=?", (cid,)).fetchone()
    if not row:
        return None, None
    acct = dict(row)
    return R2Manager(
        account_id=acct.get("account_id", ""),
        api_token=acct.get("api_token", ""),
        access_key_id=acct.get("r2_access_key", ""),
        secret_access_key=acct.get("r2_secret_key", ""),
        global_api_key=acct.get("global_api_key", ""),
        auth_email=acct.get("auth_email", ""),
    ), acct


def _cf_headers(acct: dict) -> dict:
    if acct.get("global_api_key") and acct.get("auth_email"):
        return {"X-Auth-Key": acct["global_api_key"],
                "X-Auth-Email": acct["auth_email"]}
    return {"Authorization": f"Bearer {acct.get('api_token', '')}"}


# ─── Main page ───────────────────────────────────────────

@router.get("/cloudflare", response_class=HTMLResponse)
async def cloudflare_page(request: Request):
    db = request.app.state.db
    tpl = request.app.state.templates
    accounts = []
    for a in db.get_cf_accounts():
        ad = dict(a)
        ad["domains"] = _pull_results.get(a["id"], [])
        ad["r2_enabled"] = bool(ad.get("account_id"))
        ad["buckets"] = []
        accounts.append(ad)
    return tpl.TemplateResponse(request, "cloudflare.html", {
        "active": "cloudflare", "accounts": accounts, "db": db,
    })


@router.get("/cloudflare/{cid}/r2/buckets", response_class=HTMLResponse)
async def r2_list_buckets(request: Request, cid: int):
    """HTMX: load bucket list — tries S3 API first, then CF REST API."""
    db = request.app.state.db
    buckets = []

    r2, acct = _get_r2(db, cid)
    if r2 and r2.enabled:
        buckets = r2.list_buckets()

    if not buckets and acct and acct.get("account_id"):
        import requests as req_lib
        headers = _cf_headers(acct)
        try:
            resp = req_lib.get(
                f"https://api.cloudflare.com/client/v4/accounts/{acct['account_id']}/r2/buckets",
                headers=headers, timeout=15)
            if resp.status_code == 200:
                for b in resp.json().get("result", {}).get("buckets", []):
                    buckets.append(b.get("name", ""))
                buckets = [b for b in buckets if b]
        except Exception:
            pass

    if not buckets:
        return HTMLResponse('<p style="color:var(--fg2);font-size:13px">No buckets found. Create one below or check credentials.</p>')

    html = ""
    for b in buckets:
        html += (f'<div style="display:flex;align-items:center;gap:8px;padding:8px 0;'
                 f'border-bottom:1px solid var(--border-light)">'
                 f'<span style="font-weight:500;flex:1">{b}</span>'
                 f'<button class="btn btn-secondary btn-xs" '
                 f'hx-get="/cloudflare/{cid}/r2/{b}/files" '
                 f'hx-target="#r2-files-{cid}" hx-swap="innerHTML">Browse</button>'
                 f'<form method="post" action="/cloudflare/{cid}/r2/enable-public" style="display:inline">'
                 f'<input type="hidden" name="bucket" value="{b}">'
                 f'<button class="btn btn-secondary btn-xs">Public</button></form></div>')
    return HTMLResponse(html)


# ─── Account CRUD ────────────────────────────────────────

@router.post("/cloudflare/add-account")
async def add_account(request: Request,
                      name: str = Form(""),
                      auth_type: str = Form("token"),
                      api_token: str = Form(""),
                      global_api_key: str = Form(""),
                      auth_email: str = Form(""),
                      account_id: str = Form(""),
                      r2_access_key: str = Form(""),
                      r2_secret_key: str = Form("")):
    if not name.strip():
        return RedirectResponse("/cloudflare", status_code=303)
    request.app.state.db.add_cf_account(
        name=name.strip(), auth_type=auth_type,
        api_token=api_token.strip(), global_api_key=global_api_key.strip(),
        auth_email=auth_email.strip(), account_id=account_id.strip(),
        r2_access_key=r2_access_key.strip(), r2_secret_key=r2_secret_key.strip(),
    )
    return RedirectResponse("/cloudflare", status_code=303)


@router.post("/cloudflare/{cid}/delete")
async def delete_account(request: Request, cid: int):
    request.app.state.db.delete_cf_account(cid)
    _pull_results.pop(cid, None)
    return RedirectResponse("/cloudflare", status_code=303)


# ─── Domain pull ──────────────────────────────────────────

def _pull_domains_worker(db, cid: int):
    import requests as req_lib
    acct = db._conn().execute("SELECT * FROM cf_accounts WHERE id=?", (cid,)).fetchone()
    if not acct:
        _pull_results[cid] = [{"name": "(account not found)", "status": "", "in_brands": False}]
        return
    acct = dict(acct)
    headers = _cf_headers(acct)
    try:
        resp = req_lib.get("https://api.cloudflare.com/client/v4/zones",
                           headers=headers, params={"per_page": 50}, timeout=15)
        zones = resp.json().get("result", []) if resp.status_code == 200 else []
    except Exception:
        zones = []
    brand_domains = {d["domain"] for d in db.get_domains()}
    _pull_results[cid] = [
        {"name": z.get("name", ""), "status": z.get("status", ""),
         "in_brands": z.get("name", "") in brand_domains}
        for z in zones
    ]


@router.post("/cloudflare/{cid}/pull-domains", response_class=HTMLResponse)
async def pull_domains(request: Request, cid: int):
    db = request.app.state.db
    _pull_results[cid] = [{"name": "Pulling...", "status": "loading", "in_brands": False}]
    t = threading.Thread(target=_pull_domains_worker, args=(db, cid), daemon=True)
    t.start()
    t.join(timeout=20)
    domains = _pull_results.get(cid, [])
    rows = ""
    for d in domains:
        brand_badge = '<span class="badge badge-running">Brands</span>' if d.get("in_brands") else ""
        rows += f'<tr><td>{d["name"]}</td><td>{d.get("status","")}</td><td>{brand_badge}</td></tr>'
    return HTMLResponse(
        f'<table><thead><tr><th>Domain</th><th>Status</th><th>In Brands</th></tr></thead>'
        f'<tbody>{rows}</tbody></table>'
        f'<p style="font-size:12px;color:var(--fg2);margin-top:8px">{len(domains)} domains found</p>'
    )


# ─── R2 Storage ───────────────────────────────────────────

@router.post("/cloudflare/{cid}/r2/create-bucket")
async def r2_create_bucket(request: Request, cid: int,
                           bucket_name: str = Form("")):
    if not bucket_name.strip():
        return RedirectResponse("/cloudflare", status_code=303)
    r2, _ = _get_r2(request.app.state.db, cid)
    if r2 and r2.enabled:
        r2.create_bucket(bucket_name.strip())
    return RedirectResponse("/cloudflare", status_code=303)


@router.post("/cloudflare/{cid}/r2/enable-public")
async def r2_enable_public(request: Request, cid: int,
                           bucket: str = Form("")):
    r2, _ = _get_r2(request.app.state.db, cid)
    if r2 and bucket:
        r2.enable_public_access(bucket)
    return RedirectResponse("/cloudflare", status_code=303)


@router.post("/cloudflare/{cid}/r2/add-domain")
async def r2_add_domain(request: Request, cid: int,
                        bucket: str = Form(""),
                        domain: str = Form("")):
    r2, _ = _get_r2(request.app.state.db, cid)
    if r2 and bucket and domain.strip():
        r2.add_custom_domain(bucket, domain.strip())
    return RedirectResponse("/cloudflare", status_code=303)


@router.get("/cloudflare/{cid}/r2/{bucket}/files", response_class=HTMLResponse)
async def r2_list_files(request: Request, cid: int, bucket: str,
                        prefix: str = ""):
    r2, _ = _get_r2(request.app.state.db, cid)
    if not r2 or not r2.enabled:
        return HTMLResponse("<p>R2 not configured</p>")
    objects = r2.list_objects(bucket, prefix)
    if not objects:
        return HTMLResponse('<p style="color:var(--fg2);font-size:12px">No files in bucket.</p>')
    rows = ""
    for o in objects:
        size_kb = o["size"] / 1024
        rows += (f'<tr><td style="font-size:12px">{o["key"]}</td>'
                 f'<td style="font-size:12px">{size_kb:.1f} KB</td>'
                 f'<td><form method="post" action="/cloudflare/{cid}/r2/{bucket}/delete" style="display:inline">'
                 f'<input type="hidden" name="key" value="{o["key"]}">'
                 f'<button class="btn btn-danger btn-sm" style="padding:1px 6px;font-size:10px"'
                 f' onclick="return confirm(\'Delete {o["key"]}?\')">✕</button></form></td></tr>')
    return HTMLResponse(
        f'<table><thead><tr><th>Key</th><th>Size</th><th></th></tr></thead>'
        f'<tbody>{rows}</tbody></table>'
        f'<p style="font-size:11px;color:var(--fg2);margin-top:6px">{len(objects)} objects</p>'
    )


@router.post("/cloudflare/{cid}/r2/{bucket}/upload")
async def r2_upload_files(request: Request, cid: int, bucket: str,
                          prefix: str = Form(""),
                          files: TList[UploadFile] = File(...)):
    r2, _ = _get_r2(request.app.state.db, cid)
    results = []
    if r2 and r2.enabled:
        for f in files:
            data = await f.read()
            key = prefix + f.filename
            ct = mimetypes.guess_type(f.filename)[0] or "application/octet-stream"
            url = r2.upload_bytes(bucket, key, data, ct)
            results.append({"key": key, "url": url, "ok": url is not None})
    ok = sum(1 for r in results if r["ok"])
    fail = len(results) - ok
    lines = ""
    for r in results:
        if r["ok"]:
            lines += f'<div style="font-size:12px;color:var(--green)">✓ {r["key"]} → {r["url"]}</div>'
        else:
            lines += f'<div style="font-size:12px;color:var(--red)">✗ {r["key"]} — upload failed</div>'
    return HTMLResponse(
        f'<div style="margin-bottom:8px"><strong>{ok}</strong> uploaded, <strong>{fail}</strong> failed</div>{lines}'
    )


@router.post("/cloudflare/{cid}/r2/{bucket}/delete")
async def r2_delete_file(request: Request, cid: int, bucket: str,
                         key: str = Form("")):
    r2, _ = _get_r2(request.app.state.db, cid)
    if r2 and key:
        r2.delete_object(bucket, key)
    return RedirectResponse("/cloudflare", status_code=303)


# ─── Unsub Worker Deploy ─────────────────────────────────

@router.post("/cloudflare/{cid}/deploy-worker", response_class=HTMLResponse)
async def deploy_unsub_worker(request: Request, cid: int,
                              worker_name: str = Form("unsub-worker"),
                              kv_namespace: str = Form("unsubscribes"),
                              route_pattern: str = Form(""),
                              zone_id: str = Form("")):
    """Deploy the unsubscribe worker to Cloudflare."""
    import requests as req_lib
    db = request.app.state.db
    _, acct = _get_r2(db, cid)
    if not acct:
        return HTMLResponse('<div style="color:var(--red)">Account not found</div>')

    headers = _cf_headers(acct)
    account_id = acct.get("account_id", "")
    log_lines = []

    # Step 1: Create KV namespace
    log_lines.append("Creating KV namespace...")
    try:
        resp = req_lib.post(
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces",
            headers={**headers, "Content-Type": "application/json"},
            json={"title": kv_namespace}, timeout=15)
        rj = resp.json()
        if resp.status_code in (200, 409) or rj.get("success"):
            kv_id = rj.get("result", {}).get("id", "")
            log_lines.append(f"✓ KV namespace ready (ID: {kv_id[:12]}...)")
        else:
            # Try to find existing
            resp2 = req_lib.get(
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces",
                headers=headers, timeout=15)
            kv_id = ""
            for ns in resp2.json().get("result", []):
                if ns.get("title") == kv_namespace:
                    kv_id = ns["id"]
                    break
            if kv_id:
                log_lines.append(f"✓ Existing KV namespace found (ID: {kv_id[:12]}...)")
            else:
                log_lines.append(f"✗ KV create failed: {rj}")
                return HTMLResponse(_format_log(log_lines))
    except Exception as e:
        log_lines.append(f"✗ KV error: {e}")
        return HTMLResponse(_format_log(log_lines))

    # Step 2: Upload worker
    log_lines.append("Uploading worker script...")
    try:
        if not os.path.isfile(WORKER_JS):
            log_lines.append(f"✗ Worker file not found: {WORKER_JS}")
            return HTMLResponse(_format_log(log_lines))

        with open(WORKER_JS, "r") as f:
            worker_code = f.read()

        metadata = {
            "main_module": "worker.mjs",
            "compatibility_date": "2026-04-01",
            "bindings": [
                {"type": "kv_namespace", "name": "UNSUB_KV", "namespace_id": kv_id}
            ]
        }

        resp = req_lib.put(
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/scripts/{worker_name}",
            headers=headers,
            files={
                "metadata": ("metadata.json", json.dumps(metadata), "application/json"),
                "worker.mjs": ("worker.mjs", worker_code, "application/javascript+module"),
            },
            timeout=30)
        if resp.status_code == 200:
            log_lines.append("✓ Worker uploaded")
        else:
            log_lines.append(f"✗ Worker upload failed ({resp.status_code}): {resp.text[:200]}")
            return HTMLResponse(_format_log(log_lines))
    except Exception as e:
        log_lines.append(f"✗ Upload error: {e}")
        return HTMLResponse(_format_log(log_lines))

    # Step 3: Add route
    if route_pattern and zone_id:
        log_lines.append(f"Adding route: {route_pattern}...")
        try:
            resp = req_lib.post(
                f"https://api.cloudflare.com/client/v4/zones/{zone_id}/workers/routes",
                headers={**headers, "Content-Type": "application/json"},
                json={"pattern": route_pattern, "script": worker_name},
                timeout=15)
            if resp.status_code in (200, 201):
                log_lines.append(f"✓ Route added: {route_pattern}")
            else:
                log_lines.append(f"⚠ Route may already exist ({resp.status_code})")
        except Exception as e:
            log_lines.append(f"⚠ Route error: {e}")

    log_lines.append("✓ Deployment complete!")
    return HTMLResponse(_format_log(log_lines))


@router.post("/cloudflare/{cid}/check-worker", response_class=HTMLResponse)
async def check_worker_status(request: Request, cid: int,
                              worker_name: str = Form("unsub-worker")):
    import requests as req_lib
    db = request.app.state.db
    _, acct = _get_r2(db, cid)
    if not acct:
        return HTMLResponse('<span style="color:var(--red)">Account not found</span>')
    headers = _cf_headers(acct)
    account_id = acct.get("account_id", "")
    try:
        resp = req_lib.get(
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/scripts/{worker_name}",
            headers=headers, timeout=10)
        if resp.status_code == 200:
            return HTMLResponse('<span style="color:var(--green)">✓ Worker deployed and active</span>')
        else:
            return HTMLResponse('<span style="color:var(--fg2)">✗ Worker not found</span>')
    except Exception as e:
        return HTMLResponse(f'<span style="color:var(--red)">Error: {e}</span>')


@router.get("/cloudflare/{cid}/zones", response_class=HTMLResponse)
async def get_zones(request: Request, cid: int):
    """HTMX endpoint: fetch zones for the zone dropdown."""
    r2, _ = _get_r2(request.app.state.db, cid)
    if not r2:
        return HTMLResponse('<option value="">No account</option>')
    zones = r2.list_zones()
    opts = '<option value="">— Select Zone —</option>'
    for z in zones:
        opts += f'<option value="{z["id"]}">{z["name"]}</option>'
    return HTMLResponse(opts)


def _format_log(lines: list) -> str:
    html = '<div style="font-family:monospace;font-size:12px;background:var(--bg);padding:10px;border-radius:var(--radius)">'
    for line in lines:
        color = "var(--green)" if line.startswith("✓") else "var(--red)" if line.startswith("✗") else "var(--fg2)" if line.startswith("⚠") else "var(--fg)"
        html += f'<div style="color:{color};padding:2px 0">{line}</div>'
    html += '</div>'
    return html
