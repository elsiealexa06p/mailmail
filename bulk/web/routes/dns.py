"""DNS Management — add records to Cloudflare, paste SES records, DMARC builder, mail routing."""
import re
import json
from html import escape
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()

CF_API = "https://api.cloudflare.com/client/v4"


def _cf_auth(db, cf_account_id: int = 0):
    if cf_account_id:
        row = db._conn().execute("SELECT * FROM cf_accounts WHERE id=?", (cf_account_id,)).fetchone()
        accounts = [row] if row else []
    else:
        accounts = db.get_cf_accounts()
    if not accounts:
        return None, None
    acct = dict(accounts[0])
    if acct.get("global_api_key") and acct.get("auth_email"):
        headers = {"X-Auth-Key": acct["global_api_key"],
                   "X-Auth-Email": acct["auth_email"],
                   "Content-Type": "application/json"}
    else:
        headers = {"Authorization": f"Bearer {acct.get('api_token', '')}",
                   "Content-Type": "application/json"}
    return headers, acct


def _get_zone_id(headers, domain):
    """Find zone ID for a domain."""
    import requests
    base = domain
    parts = domain.split(".")
    if len(parts) > 2:
        base = ".".join(parts[-2:])
    resp = requests.get(f"{CF_API}/zones", headers=headers,
                        params={"name": base}, timeout=10)
    if resp.status_code == 200:
        zones = resp.json().get("result", [])
        if zones:
            return zones[0]["id"]
    return None


def _parse_ses_records(text: str) -> list:
    """Parse SES-style DNS records from pasted text."""
    records = []
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.upper() in ("CNAME", "MX", "TXT", "A", "AAAA"):
            rtype = line.upper()
            if i + 2 < len(lines):
                name = lines[i + 1]
                value = lines[i + 2]
                priority = 0
                if rtype == "MX" and value and value[0].isdigit():
                    parts = value.split(None, 1)
                    if len(parts) == 2:
                        try:
                            priority = int(parts[0])
                        except ValueError:
                            pass
                        value = parts[1]
                if rtype == "TXT":
                    value = value.strip('"')
                records.append({
                    "type": rtype, "name": name, "content": value,
                    "priority": priority, "ttl": 3600,
                })
                i += 3
                continue
        i += 1
    return records


@router.get("/dns", response_class=HTMLResponse)
async def dns_page(request: Request):
    db = request.app.state.db
    cf_accounts = [dict(a) for a in db.get_cf_accounts()]
    return request.app.state.templates.TemplateResponse(request, "dns.html", {
        "active": "dns", "cf_accounts": cf_accounts, "db": db,
    })


@router.post("/dns/parse", response_class=HTMLResponse)
async def parse_records(request: Request, raw_text: str = Form("")):
    """Parse pasted SES records and show preview table."""
    records = _parse_ses_records(raw_text)
    if not records:
        return HTMLResponse('<div class="alert alert-warning">No records found. Check format.</div>')

    html = '<table><thead><tr><th>Type</th><th>Name</th><th>Content</th><th>Priority</th></tr></thead><tbody>'
    for i, r in enumerate(records):
        html += (f'<tr><td><span class="badge badge-info">{r["type"]}</span></td>'
                 f'<td style="font-family:monospace;font-size:12px">{escape(r["name"])}</td>'
                 f'<td style="font-family:monospace;font-size:12px;max-width:350px;overflow:hidden;text-overflow:ellipsis">{escape(r["content"])}</td>'
                 f'<td>{r["priority"] if r["priority"] else "—"}</td></tr>')
    html += '</tbody></table>'
    html += f'<input type="hidden" name="records_json" id="parsed-records" value=\'{json.dumps(records)}\'>'
    html += f'<p style="margin-top:8px;font-size:13px;color:var(--fg2)">{len(records)} records parsed.</p>'
    return HTMLResponse(html)


@router.post("/dns/apply", response_class=HTMLResponse)
async def apply_records(request: Request,
                        records_json: str = Form(""),
                        cf_account_id: int = Form(0),
                        zone_domain: str = Form("")):
    """Apply parsed DNS records to Cloudflare."""
    import requests as req_lib
    db = request.app.state.db
    headers, acct = _cf_auth(db, cf_account_id)
    if not headers:
        return HTMLResponse('<div class="alert alert-danger">No CF account found.</div>')

    try:
        records = json.loads(records_json)
    except (json.JSONDecodeError, TypeError):
        return HTMLResponse('<div class="alert alert-danger">Invalid records data.</div>')

    zone_id = _get_zone_id(headers, zone_domain) if zone_domain else None
    if not zone_id:
        return HTMLResponse(f'<div class="alert alert-danger">Zone not found for {escape(zone_domain)}. Add it to Cloudflare first.</div>')

    results = []
    for r in records:
        body = {
            "type": r["type"],
            "name": r["name"],
            "content": r["content"],
            "ttl": r.get("ttl", 3600),
            "proxied": False,
        }
        if r["type"] == "MX" and r.get("priority"):
            body["priority"] = r["priority"]

        try:
            resp = req_lib.post(f"{CF_API}/zones/{zone_id}/dns_records",
                                headers=headers, json=body, timeout=10)
            rj = resp.json()
            if rj.get("success"):
                results.append(f'<div style="color:var(--green);font-size:13px">&#10003; {r["type"]} {escape(r["name"])}</div>')
            else:
                errs = rj.get("errors", [])
                err_msg = errs[0].get("message", str(errs)) if errs else "Unknown"
                results.append(f'<div style="color:var(--red);font-size:13px">&#10007; {r["type"]} {escape(r["name"])}: {escape(err_msg)}</div>')
        except Exception as e:
            results.append(f'<div style="color:var(--red);font-size:13px">&#10007; {r["type"]} {escape(r["name"])}: {escape(str(e))}</div>')

    ok = sum(1 for r in results if "&#10003;" in r)
    fail = len(results) - ok
    header = f'<div style="margin-bottom:8px"><strong>{ok}</strong> added, <strong>{fail}</strong> failed</div>'
    return HTMLResponse(header + "".join(results))


@router.post("/dns/dmarc", response_class=HTMLResponse)
async def add_dmarc(request: Request,
                    domain: str = Form(""),
                    policy: str = Form("none"),
                    rua: str = Form(""),
                    ruf: str = Form(""),
                    pct: int = Form(100),
                    cf_account_id: int = Form(0)):
    """Add DMARC TXT record."""
    import requests as req_lib
    db = request.app.state.db
    headers, acct = _cf_auth(db, cf_account_id)
    if not headers or not domain:
        return HTMLResponse('<div class="alert alert-danger">Missing CF account or domain.</div>')

    parts = [f"v=DMARC1", f"p={policy}"]
    if pct != 100:
        parts.append(f"pct={pct}")
    if rua.strip():
        parts.append(f"rua=mailto:{rua.strip()}")
    if ruf.strip():
        parts.append(f"ruf=mailto:{ruf.strip()}")

    dmarc_value = "; ".join(parts)

    zone_id = _get_zone_id(headers, domain)
    if not zone_id:
        return HTMLResponse(f'<div class="alert alert-danger">Zone not found for {escape(domain)}.</div>')

    try:
        resp = req_lib.post(f"{CF_API}/zones/{zone_id}/dns_records",
                            headers=headers,
                            json={"type": "TXT", "name": f"_dmarc.{domain}",
                                  "content": dmarc_value, "ttl": 3600},
                            timeout=10)
        rj = resp.json()
        if rj.get("success"):
            return HTMLResponse(f'<div class="alert alert-success">DMARC added: <code>{escape(dmarc_value)}</code></div>')
        else:
            errs = rj.get("errors", [])
            err_msg = errs[0].get("message", str(errs)) if errs else "Unknown"
            return HTMLResponse(f'<div class="alert alert-danger">Failed: {escape(err_msg)}</div>')
    except Exception as e:
        return HTMLResponse(f'<div class="alert alert-danger">{escape(str(e))}</div>')


@router.post("/dns/zones", response_class=HTMLResponse)
async def list_zones(request: Request, cf_account_id: int = Form(0)):
    """HTMX: load zones for dropdown."""
    import requests as req_lib
    db = request.app.state.db
    headers, acct = _cf_auth(db, cf_account_id)
    if not headers:
        return HTMLResponse('<option value="">No CF account</option>')
    try:
        resp = req_lib.get(f"{CF_API}/zones", headers=headers,
                           params={"per_page": 100}, timeout=15)
        zones = resp.json().get("result", []) if resp.status_code == 200 else []
        opts = '<option value="">— Select domain —</option>'
        for z in zones:
            opts += f'<option value="{z["name"]}">{z["name"]} ({z.get("status","?")})</option>'
        return HTMLResponse(opts)
    except Exception as e:
        return HTMLResponse(f'<option value="">Error: {escape(str(e)[:50])}</option>')


@router.post("/dns/mail-routing/enable", response_class=HTMLResponse)
async def enable_mail_routing(request: Request,
                              zone_domain: str = Form(""),
                              cf_account_id: int = Form(0)):
    """Enable Cloudflare Email Routing for a zone."""
    import requests as req_lib
    db = request.app.state.db
    headers, acct = _cf_auth(db, cf_account_id)
    if not headers or not zone_domain:
        return HTMLResponse('<div class="alert alert-danger">Missing CF account or domain.</div>')

    zone_id = _get_zone_id(headers, zone_domain)
    if not zone_id:
        return HTMLResponse(f'<div class="alert alert-danger">Zone not found for {escape(zone_domain)}.</div>')

    results = []

    try:
        resp = req_lib.post(f"{CF_API}/zones/{zone_id}/email/routing/enable",
                            headers=headers, timeout=10)
        rj = resp.json()
        if rj.get("success"):
            results.append(f'<div style="color:var(--green);font-size:13px">&#10003; Email Routing enabled</div>')
        else:
            errs = rj.get("errors", [])
            msg = errs[0].get("message", str(errs)) if errs else "Unknown"
            results.append(f'<div style="color:var(--fg2);font-size:13px">Routing status: {escape(msg)}</div>')
    except Exception as e:
        results.append(f'<div style="color:var(--red);font-size:13px">Error: {escape(str(e))}</div>')

    try:
        resp = req_lib.get(f"{CF_API}/zones/{zone_id}/email/routing/dns",
                           headers=headers, timeout=10)
        rj = resp.json()
        if rj.get("success"):
            dns_records = rj.get("result", [])
            if dns_records:
                results.append(f'<div style="margin-top:8px;font-size:13px"><strong>{len(dns_records)} DNS records required:</strong></div>')
                for rec in dns_records:
                    rtype = rec.get("type", "")
                    name = rec.get("name", "")
                    content = rec.get("content", "")
                    priority = rec.get("priority", "")
                    pri_str = f" (priority: {priority})" if priority else ""
                    results.append(f'<div style="font-family:monospace;font-size:12px;padding:2px 0">'
                                   f'<span class="badge badge-info" style="min-width:45px;text-align:center">{rtype}</span> '
                                   f'{escape(name)} → {escape(content)}{pri_str}</div>')
    except Exception:
        pass

    return HTMLResponse("".join(results))


@router.post("/dns/mail-routing/rules", response_class=HTMLResponse)
async def list_routing_rules(request: Request,
                             zone_domain: str = Form(""),
                             cf_account_id: int = Form(0)):
    """List existing email routing rules."""
    import requests as req_lib
    db = request.app.state.db
    headers, acct = _cf_auth(db, cf_account_id)
    if not headers or not zone_domain:
        return HTMLResponse('<div class="alert alert-danger">Missing parameters.</div>')

    zone_id = _get_zone_id(headers, zone_domain)
    if not zone_id:
        return HTMLResponse(f'<div class="alert alert-danger">Zone not found.</div>')

    try:
        resp = req_lib.get(f"{CF_API}/zones/{zone_id}/email/routing/rules",
                           headers=headers, timeout=10)
        rj = resp.json()
        rules = rj.get("result", []) if rj.get("success") else []

        if not rules:
            return HTMLResponse('<p style="color:var(--fg2);font-size:13px">No routing rules yet.</p>')

        html = '<table><thead><tr><th>From</th><th>Action</th><th>Destination</th><th>Enabled</th></tr></thead><tbody>'
        for rule in rules:
            matchers = rule.get("matchers", [])
            actions = rule.get("actions", [])
            from_addr = matchers[0].get("value", "*") if matchers else "catch-all"
            action_type = actions[0].get("type", "?") if actions else "?"
            dest = actions[0].get("value", ["?"])[0] if actions and actions[0].get("value") else "—"
            enabled = rule.get("enabled", False)
            badge = "badge-running" if enabled else "badge-draft"
            html += (f'<tr><td style="font-family:monospace;font-size:12px">{escape(str(from_addr))}</td>'
                     f'<td>{escape(action_type)}</td>'
                     f'<td style="font-family:monospace;font-size:12px">{escape(str(dest))}</td>'
                     f'<td><span class="badge {badge}">{"On" if enabled else "Off"}</span></td></tr>')
        html += '</tbody></table>'
        return HTMLResponse(html)
    except Exception as e:
        return HTMLResponse(f'<div class="alert alert-danger">{escape(str(e))}</div>')


@router.post("/dns/mail-routing/add-rule", response_class=HTMLResponse)
async def add_routing_rule(request: Request,
                           zone_domain: str = Form(""),
                           cf_account_id: int = Form(0),
                           from_address: str = Form(""),
                           destination: str = Form(""),
                           catch_all: int = Form(0)):
    """Add email routing rule (specific address or catch-all)."""
    import requests as req_lib
    db = request.app.state.db
    headers, acct = _cf_auth(db, cf_account_id)
    if not headers or not zone_domain:
        return HTMLResponse('<div class="alert alert-danger">Missing parameters.</div>')

    zone_id = _get_zone_id(headers, zone_domain)
    if not zone_id:
        return HTMLResponse(f'<div class="alert alert-danger">Zone not found.</div>')

    if catch_all:
        body = {
            "matchers": [{"type": "all"}],
            "actions": [{"type": "forward", "value": [destination.strip()]}],
            "enabled": True, "name": "Catch-all"
        }
        url = f"{CF_API}/zones/{zone_id}/email/routing/rules/catch_all"
        try:
            resp = req_lib.put(url, headers=headers, json=body, timeout=10)
            rj = resp.json()
            if rj.get("success"):
                return HTMLResponse(f'<div class="alert alert-success">Catch-all → {escape(destination)} set!</div>')
            else:
                errs = rj.get("errors", [])
                return HTMLResponse(f'<div class="alert alert-danger">{escape(str(errs))}</div>')
        except Exception as e:
            return HTMLResponse(f'<div class="alert alert-danger">{escape(str(e))}</div>')
    else:
        body = {
            "matchers": [{"type": "literal", "field": "to", "value": from_address.strip()}],
            "actions": [{"type": "forward", "value": [destination.strip()]}],
            "enabled": True, "name": f"Route {from_address}"
        }
        try:
            resp = req_lib.post(f"{CF_API}/zones/{zone_id}/email/routing/rules",
                                headers=headers, json=body, timeout=10)
            rj = resp.json()
            if rj.get("success"):
                return HTMLResponse(f'<div class="alert alert-success">{escape(from_address)} → {escape(destination)} added!</div>')
            else:
                errs = rj.get("errors", [])
                err_msg = errs[0].get("message", str(errs)) if errs else "Unknown"
                return HTMLResponse(f'<div class="alert alert-danger">{escape(err_msg)}</div>')
        except Exception as e:
            return HTMLResponse(f'<div class="alert alert-danger">{escape(str(e))}</div>')


@router.post("/dns/mail-routing/destinations", response_class=HTMLResponse)
async def list_destinations(request: Request, cf_account_id: int = Form(0)):
    """List verified email routing destinations."""
    import requests as req_lib
    db = request.app.state.db
    headers, acct = _cf_auth(db, cf_account_id)
    if not headers:
        return HTMLResponse('<option value="">No CF account</option>')

    account_id = acct.get("account_id", "")
    try:
        resp = req_lib.get(f"{CF_API}/accounts/{account_id}/email/routing/addresses",
                           headers=headers, timeout=10)
        rj = resp.json()
        addrs = rj.get("result", []) if rj.get("success") else []
        if not addrs:
            return HTMLResponse('<option value="">No destinations found</option>')
        opts = '<option value="">— Select —</option>'
        for a in addrs:
            email = a.get("email", "")
            verified = a.get("verified", "")
            tag = " (unverified)" if not verified else ""
            opts += f'<option value="{email}">{email}{tag}</option>'
        return HTMLResponse(opts)
    except Exception as e:
        return HTMLResponse(f'<option value="">Error</option>')
