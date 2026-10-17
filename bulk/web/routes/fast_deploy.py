"""Fast Deploy — one-page wizard for full domain setup."""
import os
import re
import json
import shutil
import secrets
from html import escape
from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import List as TList

router = APIRouter()

HTML_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "bulk_html")


@router.get("/fast-deploy", response_class=HTMLResponse)
async def fast_deploy_page(request: Request):
    db = request.app.state.db
    brands = [dict(b) for b in db.get_brands()]
    domains = [dict(d) for d in db.get_domains()]
    cf_accounts = [dict(a) for a in db.get_cf_accounts()]
    templates = _template_list(db)
    return request.app.state.templates.TemplateResponse(request, "fast_deploy.html", {
        "active": "fast_deploy", "brands": brands, "domains": domains,
        "cf_accounts": cf_accounts, "templates": templates, "db": db,
    })


@router.post("/fast-deploy/dns", response_class=HTMLResponse)
async def deploy_dns(request: Request,
                     domain: str = Form(""),
                     ses_records: str = Form(""),
                     dmarc_policy: str = Form("none"),
                     cf_account_id: int = Form(0)):
    """Parse SES records + add to Cloudflare + create DMARC."""
    import requests as req_lib
    from .dns import _parse_ses_records, _cf_auth, _get_zone_id, CF_API

    if not domain.strip() or not ses_records.strip():
        return HTMLResponse('<div class="alert alert-warning">Domain and SES records required.</div>')

    headers, acct = _cf_auth(request.app.state.db, cf_account_id)
    if not headers:
        return HTMLResponse('<div class="alert alert-danger">No Cloudflare account configured.</div>')

    zone_id = _get_zone_id(headers, domain)
    if not zone_id:
        return HTMLResponse(f'<div class="alert alert-danger">Zone not found for {escape(domain)}. Add to Cloudflare first.</div>')

    records = _parse_ses_records(ses_records)
    results = []

    for r in records:
        body = {"type": r["type"], "name": r["name"], "content": r["content"],
                "ttl": r.get("ttl", 3600), "proxied": False}
        if r["type"] == "MX" and r.get("priority"):
            body["priority"] = r["priority"]
        try:
            resp = req_lib.post(f"{CF_API}/zones/{zone_id}/dns_records",
                                headers=headers, json=body, timeout=10)
            rj = resp.json()
            if rj.get("success"):
                results.append(f'<span style="color:var(--green)">&#10003;</span> {r["type"]} {escape(r["name"][:40])}')
            else:
                errs = rj.get("errors", [])
                msg = errs[0].get("message", "") if errs else "?"
                results.append(f'<span style="color:var(--red)">&#10007;</span> {r["type"]} {escape(r["name"][:40])}: {escape(msg[:60])}')
        except Exception as e:
            results.append(f'<span style="color:var(--red)">&#10007;</span> {r["type"]}: {escape(str(e)[:60])}')

    dmarc_val = f"v=DMARC1; p={dmarc_policy}"
    try:
        resp = req_lib.post(f"{CF_API}/zones/{zone_id}/dns_records",
                            headers=headers,
                            json={"type": "TXT", "name": f"_dmarc.{domain}",
                                  "content": dmarc_val, "ttl": 3600},
                            timeout=10)
        rj = resp.json()
        if rj.get("success"):
            results.append(f'<span style="color:var(--green)">&#10003;</span> DMARC: {escape(dmarc_val)}')
        else:
            errs = rj.get("errors", [])
            msg = errs[0].get("message", "") if errs else "?"
            results.append(f'<span style="color:var(--red)">&#10007;</span> DMARC: {escape(msg[:60])}')
    except Exception as e:
        results.append(f'<span style="color:var(--red)">&#10007;</span> DMARC: {escape(str(e)[:60])}')

    html = '<div style="font-size:13px;line-height:2">' + '<br>'.join(results) + '</div>'
    ok = sum(1 for r in results if "&#10003;" in r)
    html += f'<div class="alert alert-{"success" if ok else "warning"}" style="margin-top:8px">{ok}/{len(results)} records added.</div>'
    return HTMLResponse(html)


@router.post("/fast-deploy/email-routing", response_class=HTMLResponse)
async def deploy_email_routing(request: Request,
                                domain: str = Form(""),
                                from_address: str = Form(""),
                                destination: str = Form(""),
                                cf_account_id: int = Form(0)):
    """Enable email routing + create forwarding rule."""
    import requests as req_lib
    from .dns import _cf_auth, _get_zone_id, CF_API

    headers, acct = _cf_auth(request.app.state.db, cf_account_id)
    if not headers or not domain:
        return HTMLResponse('<div class="alert alert-danger">Missing parameters.</div>')

    zone_id = _get_zone_id(headers, domain)
    if not zone_id:
        return HTMLResponse(f'<div class="alert alert-danger">Zone not found for {escape(domain)}.</div>')

    results = []

    try:
        resp = req_lib.post(f"{CF_API}/zones/{zone_id}/email/routing/enable",
                            headers=headers, timeout=10)
        results.append('<span style="color:var(--green)">&#10003;</span> Email Routing enabled')
    except Exception as e:
        results.append(f'<span style="color:var(--fg2)">&#8226;</span> Routing: {escape(str(e)[:60])}')

    if from_address.strip() and destination.strip():
        full_from = from_address.strip()
        if "@" not in full_from:
            full_from = f"{full_from}@{domain}"
        body = {
            "matchers": [{"type": "literal", "field": "to", "value": full_from}],
            "actions": [{"type": "forward", "value": [destination.strip()]}],
            "enabled": True, "name": f"Route {full_from}"
        }
        try:
            resp = req_lib.post(f"{CF_API}/zones/{zone_id}/email/routing/rules",
                                headers=headers, json=body, timeout=10)
            rj = resp.json()
            if rj.get("success"):
                results.append(f'<span style="color:var(--green)">&#10003;</span> {escape(full_from)} → {escape(destination.strip())}')
            else:
                errs = rj.get("errors", [])
                msg = errs[0].get("message", "") if errs else "?"
                results.append(f'<span style="color:var(--red)">&#10007;</span> Rule: {escape(msg[:80])}')
        except Exception as e:
            results.append(f'<span style="color:var(--red)">&#10007;</span> Rule: {escape(str(e)[:60])}')

    return HTMLResponse('<div style="font-size:13px;line-height:2">' + '<br>'.join(results) + '</div>')


@router.post("/fast-deploy/logos", response_class=HTMLResponse)
async def deploy_logos(request: Request):
    """Upload logos to R2 bucket."""
    form = await request.form()
    cf_account_id = int(form.get("cf_account_id", 0))
    bucket = form.get("bucket", "")
    domain = form.get("logo_domain", "")
    files = form.getlist("logo_files")

    if not bucket or not files:
        return HTMLResponse('<div class="alert alert-warning">Select bucket and upload files.</div>')

    db = request.app.state.db
    from .cloudflare import _get_r2
    r2, acct = _get_r2(db, cf_account_id)
    if not r2:
        return HTMLResponse('<div class="alert alert-danger">R2 not configured.</div>')

    uploaded_urls = []
    for f in files:
        if not hasattr(f, "read"):
            continue
        content = await f.read()
        filename = f.filename.replace(" ", "_")
        try:
            r2.upload_bytes(bucket, filename, content,
                           content_type=f.content_type or "image/png")
            url = f"https://{domain}/{filename}" if domain else filename
            uploaded_urls.append(url)
        except Exception as e:
            uploaded_urls.append(f"ERROR: {e}")

    ok = sum(1 for u in uploaded_urls if not u.startswith("ERROR"))
    html = f'<div class="alert alert-success">{ok} logos uploaded.</div>'
    html += '<div style="font-size:12px;font-family:monospace;max-height:150px;overflow-y:auto">'
    for u in uploaded_urls:
        color = "var(--green)" if not u.startswith("ERROR") else "var(--red)"
        html += f'<div style="color:{color}">{escape(u)}</div>'
    html += '</div>'
    html += f'<input type="hidden" id="uploaded-logo-urls" value=\'{json.dumps([u for u in uploaded_urls if not u.startswith("ERROR")])}\'>'
    return HTMLResponse(html)


@router.post("/fast-deploy/clone-template", response_class=HTMLResponse)
async def clone_template(request: Request,
                         source_template_id: int = Form(0),
                         new_name: str = Form(""),
                         old_domain: str = Form(""),
                         new_domain: str = Form("")):
    """Clone template, replace domain in all logo URLs."""
    if not source_template_id or not new_name.strip():
        return HTMLResponse('<div class="alert alert-warning">Select source template and enter new name.</div>')

    db = request.app.state.db
    row = db._conn().execute("SELECT * FROM message_templates WHERE id=?",
                              (source_template_id,)).fetchone()
    if not row:
        return HTMLResponse('<div class="alert alert-danger">Source template not found.</div>')

    row = dict(row)
    old_files = json.loads(row.get("html_files_json") or "[]")
    if not old_files:
        return HTMLResponse('<div class="alert alert-warning">Source template has no HTML files.</div>')

    os.makedirs(HTML_DIR, exist_ok=True)

    new_tid = db.add_template(
        name=new_name.strip(),
        subject_macro=row.get("subject_macro", ""),
        html_files_json="[]",
        html_rotate_every=row.get("html_rotate_every", 0),
        pdf_path=row.get("pdf_path", ""),
        pdf_macro_enabled=row.get("pdf_macro_enabled", 0),
    )

    new_files = []
    replaced = 0
    for old_path in old_files:
        if not os.path.isfile(old_path):
            continue
        with open(old_path, "r", encoding="utf-8", errors="replace") as f:
            html = f.read()

        if old_domain.strip() and new_domain.strip():
            count = html.count(old_domain.strip())
            html = html.replace(old_domain.strip(), new_domain.strip())
            replaced += count

        new_filename = f"{new_tid}_{os.path.basename(old_path)}"
        new_path = os.path.join(HTML_DIR, new_filename)
        with open(new_path, "w", encoding="utf-8") as f:
            f.write(html)
        new_files.append(os.path.abspath(new_path))

    c = db._conn()
    c.execute("UPDATE message_templates SET html_files_json=? WHERE id=?",
              (json.dumps(new_files, ensure_ascii=False), new_tid))
    c.commit()

    return HTMLResponse(
        f'<div class="alert alert-success">Template "{escape(new_name)}" created with {len(new_files)} HTMLs. '
        f'{replaced} domain replacements ({escape(old_domain)} → {escape(new_domain)}).</div>'
    )


@router.post("/fast-deploy/unsub-worker", response_class=HTMLResponse)
async def deploy_unsub(request: Request,
                       domain: str = Form(""),
                       cf_account_id: int = Form(0),
                       worker_name: str = Form("unsub-worker")):
    """Deploy unsub worker for domain."""
    import requests as req_lib
    from .cloudflare import _cf_headers, _get_r2, WORKER_JS
    from .dns import _get_zone_id, CF_API

    db = request.app.state.db
    _, acct = _get_r2(db, cf_account_id)
    if not acct:
        return HTMLResponse('<div class="alert alert-danger">No CF account.</div>')

    headers = _cf_headers(acct)
    account_id = acct.get("account_id", "")
    results = []

    kv_id = ""
    try:
        resp = req_lib.post(
            f"{CF_API}/accounts/{account_id}/storage/kv/namespaces",
            headers={**headers, "Content-Type": "application/json"},
            json={"title": "unsubscribes"}, timeout=15)
        rj = resp.json()
        if rj.get("success"):
            kv_id = rj.get("result", {}).get("id", "")
        if not kv_id:
            resp2 = req_lib.get(
                f"{CF_API}/accounts/{account_id}/storage/kv/namespaces",
                headers=headers, timeout=15)
            for ns in resp2.json().get("result", []):
                if ns.get("title") == "unsubscribes":
                    kv_id = ns["id"]
                    break
        if kv_id:
            results.append(f'<span style="color:var(--green)">&#10003;</span> KV namespace ready')
        else:
            results.append(f'<span style="color:var(--red)">&#10007;</span> KV namespace failed')
            return HTMLResponse('<div style="font-size:13px;line-height:2">' + '<br>'.join(results) + '</div>')
    except Exception as e:
        results.append(f'<span style="color:var(--red)">&#10007;</span> KV: {escape(str(e)[:60])}')
        return HTMLResponse('<div style="font-size:13px;line-height:2">' + '<br>'.join(results) + '</div>')

    try:
        if not os.path.isfile(WORKER_JS):
            results.append(f'<span style="color:var(--red)">&#10007;</span> Worker file not found')
            return HTMLResponse('<div style="font-size:13px;line-height:2">' + '<br>'.join(results) + '</div>')

        with open(WORKER_JS, "r") as f:
            worker_code = f.read()

        metadata = {
            "main_module": "worker.mjs",
            "compatibility_date": "2026-04-01",
            "bindings": [{"type": "kv_namespace", "name": "UNSUB_KV", "namespace_id": kv_id}]
        }
        resp = req_lib.put(
            f"{CF_API}/accounts/{account_id}/workers/scripts/{worker_name}",
            headers=headers,
            files={
                "metadata": ("metadata.json", json.dumps(metadata), "application/json"),
                "worker.mjs": ("worker.mjs", worker_code, "application/javascript+module"),
            }, timeout=30)
        if resp.status_code == 200:
            results.append(f'<span style="color:var(--green)">&#10003;</span> Worker uploaded')
        else:
            results.append(f'<span style="color:var(--red)">&#10007;</span> Upload failed: {resp.status_code}')
    except Exception as e:
        results.append(f'<span style="color:var(--red)">&#10007;</span> Worker: {escape(str(e)[:60])}')

    unsub_domain = f"unsub.{domain}" if domain else ""
    if domain:
        zone_id = None
        try:
            from .dns import _cf_auth
            h2, _ = _cf_auth(db, cf_account_id)
            zone_id = _get_zone_id(h2, domain) if h2 else None
        except Exception:
            pass
        if zone_id:
            route_pattern = f"{unsub_domain}/*"
            try:
                resp = req_lib.post(
                    f"{CF_API}/zones/{zone_id}/workers/routes",
                    headers={**headers, "Content-Type": "application/json"},
                    json={"pattern": route_pattern, "script": worker_name},
                    timeout=15)
                if resp.status_code in (200, 201):
                    results.append(f'<span style="color:var(--green)">&#10003;</span> Route: {escape(route_pattern)}')
                else:
                    results.append(f'<span style="color:var(--fg2)">&#8226;</span> Route may exist already')
            except Exception as e:
                results.append(f'<span style="color:var(--red)">&#10007;</span> Route: {escape(str(e)[:60])}')

    results.append(f'<span style="color:var(--green)">&#10003;</span> Deployment complete')
    return HTMLResponse('<div style="font-size:13px;line-height:2">' + '<br>'.join(results) + '</div>')


def _template_list(db):
    templates = []
    for t in db.get_templates():
        td = dict(t)
        td["html_files"] = json.loads(td.get("html_files_json") or "[]")
        templates.append(td)
    return templates
