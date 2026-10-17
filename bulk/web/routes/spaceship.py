"""Spaceship Registrar Integration — https://docs.spaceship.dev/

Auth: zwei Header — X-API-Key + X-API-Secret pro Account.
Primäre Use-Cases:
  * Account-Management (add/list/set-primary/delete)
  * Domain-Liste aus dem Spaceship-Account laden
  * Nameserver setzen (Cloudflare-Zone-Anlegen → NS bei Spaceship setzen)
  * Availability + Register (mit Fallbacks, weil Endpoint-Details je Version variieren)

Bewusste Retry-Semantik beim NS-Setzen wie im Bulk-Dynadot-Flow, weil
frisch registrierte Domains manchmal ein paar Minuten bis "settled" brauchen.
"""
import time
import json
import logging
import threading
from html import escape
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()
logger = logging.getLogger("bulk.spaceship")

SPACESHIP_BASE = "https://spaceship.dev/api/v1"


# ── Low-Level API Call ────────────────────────────────────

def _ss_headers(api_key: str, api_secret: str) -> dict:
    return {
        "X-API-Key": api_key,
        "X-API-Secret": api_secret,
        "Content-Type": "application/json",
    }


def _ss_call(api_key: str, api_secret: str, method: str, path: str,
              params: dict = None, body: dict = None,
              timeout: int = 20) -> dict:
    """Generischer Spaceship-API-Aufruf. Rückgabe: parsed JSON dict oder
    {'_error': msg, '_status': code}."""
    import requests
    if not api_key or not api_secret:
        return {"_error": "Kein Spaceship API-Key + Secret gesetzt"}
    url = f"{SPACESHIP_BASE}{path}"
    headers = _ss_headers(api_key, api_secret)
    try:
        r = requests.request(method.upper(), url, headers=headers,
                              params=params or {},
                              json=body, timeout=timeout)
        try:
            data = r.json() if r.text else {}
        except Exception:
            data = {"_raw": r.text[:500]}
        data["_status"] = r.status_code
        if r.status_code >= 400:
            data["_error"] = data.get("detail") or data.get("message") or f"HTTP {r.status_code}"
        return data
    except Exception as e:
        return {"_error": f"Spaceship-Request failed: {e}", "_status": 0}


def _ss_from_account_id(db, aid: int) -> tuple:
    """Nimmt account_id oder 0=primary → (api_key, api_secret, name)."""
    row = db.get_spaceship_account(aid) if aid else db.get_primary_spaceship_account()
    if not row:
        return "", "", ""
    r = dict(row)
    return r.get("api_key", ""), r.get("api_secret", ""), r.get("name", "")


# ── High-Level Convenience ────────────────────────────────

def ss_balance(api_key: str, api_secret: str) -> dict:
    return _ss_call(api_key, api_secret, "GET", "/account/balance")


def ss_list_domains(api_key: str, api_secret: str,
                     take: int = 100, skip: int = 0) -> dict:
    return _ss_call(api_key, api_secret, "GET", "/domains",
                     params={"take": take, "skip": skip})


def ss_get_domain(api_key: str, api_secret: str, domain: str) -> dict:
    return _ss_call(api_key, api_secret, "GET", f"/domains/{domain}")


def ss_check_availability(api_key: str, api_secret: str, domain: str) -> dict:
    """Availability-Check — mehrere mögliche Endpoints (docs sind unklar).
    Rückgabe: {available: bool|None, price: str, currency: str,
               premium: bool, raw: dict, error: str}."""
    tried = []
    endpoints = [
        ("GET", f"/domains/{domain}/registration/availability", None, None),
        ("GET", "/domains/availability", {"domainName": domain}, None),
        ("GET", "/domains/availability", {"domain": domain}, None),
        ("POST", "/domains/availability", None, {"domainNames": [domain]}),
    ]
    last_resp = {}
    for method, path, params, body in endpoints:
        resp = _ss_call(api_key, api_secret, method, path,
                        params=params, body=body)
        tried.append({"path": path, "status": resp.get("_status")})
        last_resp = resp
        if resp.get("_status") and resp["_status"] < 400:
            item = resp
            if isinstance(resp.get("items"), list) and resp["items"]:
                item = resp["items"][0]
            elif isinstance(resp.get("results"), list) and resp["results"]:
                item = resp["results"][0]
            avail_raw = (item.get("isAvailable") or item.get("available")
                         or item.get("availability"))
            if isinstance(avail_raw, str):
                available = avail_raw.lower() in ("true", "yes", "available", "1")
            elif isinstance(avail_raw, bool):
                available = avail_raw
            else:
                available = None
            price_obj = item.get("price") or item.get("premiumPrice") or {}
            if isinstance(price_obj, (int, float, str)):
                price_str = str(price_obj)
                currency = item.get("currency", "USD")
            elif isinstance(price_obj, dict):
                price_str = str(price_obj.get("amount") or price_obj.get("value") or "?")
                currency = price_obj.get("currency", item.get("currency", "USD"))
            else:
                price_str = ""
                currency = ""
            premium = bool(item.get("isPremium") or item.get("premium"))
            return {
                "available": available, "price": price_str,
                "currency": currency, "premium": premium,
                "raw": resp, "error": "",
            }
    return {
        "available": None, "price": "", "currency": "", "premium": False,
        "raw": last_resp,
        "error": last_resp.get("_error") or f"kein Endpoint hat funktioniert (tried: {tried})",
    }


def ss_register(api_key: str, api_secret: str, domain: str,
                  years: int = 1) -> dict:
    """Registrierung. Body-Format basiert auf Spaceship-Docs — falls
    API-Version es anders will, kommt Error mit Details zurück."""
    return _ss_call(api_key, api_secret, "POST",
                     f"/domains/{domain}/registration",
                     body={"years": years, "privacy": True, "autoRenew": False})


def ss_set_nameservers(api_key: str, api_secret: str, domain: str,
                        ns_list: list) -> dict:
    """Nameserver setzen — Custom-Provider mit Host-Liste.
    Antwort {ok: bool, msg: str, not_ready_yet: bool} für Retry-Loop."""
    if not ns_list or len(ns_list) < 2:
        return {"ok": False, "msg": "brauche mindestens 2 Nameserver",
                "not_ready_yet": False}
    body = {"provider": "custom", "hosts": ns_list[:6]}
    resp = _ss_call(api_key, api_secret, "PUT",
                    f"/domains/{domain}/nameservers", body=body)
    status = resp.get("_status", 0)
    err = resp.get("_error", "")
    err_lower = str(resp).lower()
    not_ready = ("pending" in err_lower or "not yet" in err_lower
                 or "not found" in err_lower or "processing" in err_lower)
    if 200 <= status < 300:
        return {"ok": True, "msg": "NS gesetzt", "not_ready_yet": False}
    if not_ready or status in (202, 404, 409):
        return {"ok": False, "msg": err or f"pending (HTTP {status})",
                "not_ready_yet": True}
    return {"ok": False, "msg": err or f"HTTP {status}", "not_ready_yet": False}


def ss_set_ns_with_retry(api_key: str, api_secret: str, domain: str,
                           ns_list: list, log_step=None) -> dict:
    """Retry-Loop wie im Dynadot-Flow:
      .de → 4 Versuche, 30s Delay
      andere → 2 Versuche, 10s Delay."""
    is_de = domain.lower().endswith(".de")
    max_retries = 4 if is_de else 2
    wait_s = 30 if is_de else 10
    last = {"ok": False, "msg": "kein Versuch"}
    for attempt in range(max_retries):
        if attempt > 0:
            if log_step:
                log_step(f"NS: warte {wait_s}s (Attempt {attempt+1}/{max_retries})",
                         True, "Spaceship braucht kurz bis der Domain-Datensatz settled ist")
            time.sleep(wait_s)
        r = ss_set_nameservers(api_key, api_secret, domain, ns_list)
        last = r
        if r["ok"] or not r.get("not_ready_yet"):
            return r
    return last


# ── Panel Routes ──────────────────────────────────────────

@router.get("/spaceship", response_class=HTMLResponse)
async def spaceship_page(request: Request):
    db = request.app.state.db
    accounts = [dict(a) for a in db.get_spaceship_accounts()]
    primary = db.get_primary_spaceship_account()
    domains = []
    balance = ""
    if primary:
        pd = dict(primary)
        bal = ss_balance(pd["api_key"], pd["api_secret"])
        if bal.get("_error"):
            balance = f"— ({bal['_error']})"
        else:
            balance = str(bal.get("balance") or bal.get("value") or bal)[:60]
        dlist = ss_list_domains(pd["api_key"], pd["api_secret"], take=100)
        if not dlist.get("_error"):
            # Antwort ist meist {items: [...], total: N} oder direkt list
            items = dlist.get("items") or dlist.get("data") or []
            if isinstance(items, list):
                domains = items[:100]
    cf_accounts = [dict(a) for a in db.get_cf_accounts()]
    return request.app.state.templates.TemplateResponse(request, "spaceship.html", {
        "active": "spaceship",
        "accounts": accounts,
        "primary": primary and dict(primary),
        "domains": domains,
        "balance": balance,
        "cf_accounts": cf_accounts,
    })


@router.post("/spaceship/accounts/add")
async def add_account(request: Request,
                       name: str = Form(""),
                       api_key: str = Form(""),
                       api_secret: str = Form("")):
    db = request.app.state.db
    if name.strip() and api_key.strip() and api_secret.strip():
        db.add_spaceship_account(name.strip(), api_key.strip(), api_secret.strip())
    return RedirectResponse("/spaceship", status_code=303)


@router.post("/spaceship/accounts/{aid}/update")
async def update_account(request: Request, aid: int,
                          name: str = Form(""),
                          api_key: str = Form(""),
                          api_secret: str = Form("")):
    db = request.app.state.db
    fields = {}
    if name.strip():
        fields["name"] = name.strip()
    if api_key.strip():
        fields["api_key"] = api_key.strip()
    if api_secret.strip():
        fields["api_secret"] = api_secret.strip()
    db.update_spaceship_account(aid, **fields)
    return RedirectResponse("/spaceship", status_code=303)


@router.post("/spaceship/accounts/{aid}/set-primary")
async def set_primary(request: Request, aid: int):
    request.app.state.db.set_primary_spaceship_account(aid)
    return RedirectResponse("/spaceship", status_code=303)


@router.post("/spaceship/accounts/{aid}/delete")
async def delete_account(request: Request, aid: int):
    request.app.state.db.delete_spaceship_account(aid)
    return RedirectResponse("/spaceship", status_code=303)


@router.post("/spaceship/test", response_class=HTMLResponse)
async def test_account(request: Request, account_id: int = Form(0)):
    db = request.app.state.db
    key, secret, name = _ss_from_account_id(db, account_id)
    if not key or not secret:
        return HTMLResponse('<span style="color:var(--red)">Kein Account oder API-Key fehlt</span>')
    bal = ss_balance(key, secret)
    if bal.get("_error"):
        return HTMLResponse(
            f'<span style="color:var(--red)">✗ {escape(bal["_error"])} (Status {bal.get("_status")})</span>'
        )
    return HTMLResponse(
        f'<span style="color:var(--green)">✓ Auth OK — Balance: '
        f'{escape(str(bal.get("balance") or bal.get("value") or bal)[:80])}</span>'
    )


@router.post("/spaceship/set-ns", response_class=HTMLResponse)
async def set_ns_route(request: Request, domain: str = Form(""),
                        nameservers: str = Form(""),
                        account_id: int = Form(0)):
    """Manuelles NS-Setzen — Domain + Nameserver-Liste (eine pro Zeile).
    Nutzt Retry-Loop."""
    db = request.app.state.db
    key, secret, name = _ss_from_account_id(db, account_id)
    if not key or not secret:
        return HTMLResponse('<span style="color:var(--red)">Kein Account gesetzt</span>')
    domain = domain.strip().lower()
    ns = [n.strip() for n in nameservers.replace(",", "\n").splitlines() if n.strip()]
    if not domain or len(ns) < 2:
        return HTMLResponse('<span style="color:var(--red)">Domain + mind. 2 NS pflicht</span>')
    logs = []
    res = ss_set_ns_with_retry(
        key, secret, domain, ns,
        log_step=lambda label, ok, detail: logs.append((label, ok, detail)))
    log_html = "".join(
        f'<li>{"✓" if ok else "…"} {escape(label)}'
        f'{": " + escape(detail) if detail else ""}</li>'
        for label, ok, detail in logs
    )
    if res["ok"]:
        return HTMLResponse(
            f'<div style="color:var(--green)">✓ NS gesetzt für <code>{escape(domain)}</code>: '
            f'{escape(", ".join(ns))}</div>'
            f'<ul style="font-size:11px;color:var(--fg2)">{log_html}</ul>'
        )
    return HTMLResponse(
        f'<div style="color:var(--red)">✗ {escape(res["msg"])}</div>'
        f'<ul style="font-size:11px;color:var(--fg2)">{log_html}</ul>'
    )


@router.post("/spaceship/search", response_class=HTMLResponse)
async def search_domains(request: Request, query: str = Form(""),
                          account_id: int = Form(0)):
    """Bulk-Availability-Check. Registrieren via API bräuchte Contact-Profile,
    daher zeigen wir für „available" nur einen Deep-Link zur Spaceship-Cart."""
    db = request.app.state.db
    key, secret, name = _ss_from_account_id(db, account_id)
    if not key or not secret:
        return HTMLResponse('<div class="alert alert-danger">Kein Spaceship-Account gesetzt.</div>')
    domains_raw = [d.strip().lower() for d in query.replace(",", "\n").splitlines() if d.strip()]
    if not domains_raw:
        return HTMLResponse('<div class="alert alert-warning">Mindestens eine Domain eingeben.</div>')

    # Bereits im Account? → dann Set-NS-Button, kein Buy-Link
    owned = set()
    try:
        dlist = ss_list_domains(key, secret, take=500)
        if not dlist.get("_error"):
            items = dlist.get("items") or dlist.get("data") or []
            for it in items:
                nm = (it.get("name") or it.get("domain") or "").lower()
                if nm:
                    owned.add(nm)
    except Exception as e:
        logger.warning("owned-lookup failed: %s", e)

    rows = ""
    for d in domains_raw[:40]:
        did = d.replace(".", "-")
        if d in owned:
            rows += (
                f'<tr>'
                f'<td style="font-family:monospace">{escape(d)}</td>'
                f'<td><span class="badge badge-info">In deinem Account</span></td>'
                f'<td>—</td>'
                f'<td>'
                f'<button class="btn btn-primary btn-xs" '
                f'hx-post="/spaceship/quick-cf" '
                f'hx-vals=\'{{"domain":"{escape(d)}"}}\' '
                f'hx-include="#ss-cf-account,#ss-search-account" '
                f'hx-target="#ss-search-result-{did}" hx-swap="innerHTML">'
                f'→ CF-Zone + NS setzen</button>'
                f'<div id="ss-search-result-{did}" style="margin-top:4px;font-size:11px"></div>'
                f'</td>'
                f'</tr>'
            )
            continue
        r = ss_check_availability(key, secret, d)
        if r["available"] is True:
            badge = '<span class="badge badge-running">Verfügbar</span>'
            if r["premium"]:
                badge = '<span class="badge badge-warning">Premium</span>'
            price = f'{r["price"]} {r["currency"]}' if r["price"] else '—'
            buy_url = f'https://www.spaceship.com/domain-search/?query={d}'
            action = (
                f'<a href="{escape(buy_url)}" target="_blank" rel="noopener" '
                f'class="btn btn-primary btn-xs">→ Bei Spaceship kaufen</a>'
                f'<span class="muted" style="margin-left:6px;font-size:11px">'
                f'nach Kauf zurück → „In deinem Account" → CF setzen</span>'
            )
        elif r["available"] is False:
            badge = '<span class="badge badge-failed">Vergeben</span>'
            price = "—"
            action = '<span class="muted" style="font-size:11px">—</span>'
        else:
            badge = '<span class="badge badge-warning">?</span>'
            price = "—"
            action = (
                f'<span class="muted" style="font-size:11px">'
                f'{escape(r["error"][:80])}</span>'
            )
        rows += (
            f'<tr>'
            f'<td style="font-family:monospace">{escape(d)}</td>'
            f'<td>{badge}</td>'
            f'<td style="font-size:12px">{escape(price)}</td>'
            f'<td>{action}</td>'
            f'</tr>'
        )

    return HTMLResponse(
        '<table style="font-size:12px"><thead><tr>'
        '<th>Domain</th><th>Status</th><th>Preis</th><th></th>'
        '</tr></thead><tbody>' + rows + '</tbody></table>'
        '<p class="muted" style="font-size:11px;margin-top:6px">'
        'Register-via-API braucht Contact-Profile in Spaceship — daher '
        'externer Buy-Link.</p>'
    )


@router.post("/spaceship/quick-cf", response_class=HTMLResponse)
async def spaceship_quick_cf(request: Request, domain: str = Form(""),
                              cf_account_id: int = Form(0),
                              account_id: int = Form(0)):
    """One-Click für Domains die schon im Account sind: CF-Zone anlegen
    (falls nicht da) → NS bei Spaceship setzen. Ruft intern set_ns_from_cf."""
    return await set_ns_from_cf(request, domain=domain,
                                 cf_account_id=cf_account_id,
                                 account_id=account_id)


@router.post("/spaceship/set-ns-cf", response_class=HTMLResponse)
async def set_ns_from_cf(request: Request, domain: str = Form(""),
                          cf_account_id: int = Form(0),
                          account_id: int = Form(0)):
    """Auto-Flow: CF-Zone anlegen (falls nicht schon da) → NS von CF holen
    → bei Spaceship setzen (mit Retry). Braucht CF-Account im Panel."""
    db = request.app.state.db
    key, secret, ss_name = _ss_from_account_id(db, account_id)
    if not key or not secret:
        return HTMLResponse('<div class="alert alert-danger">Kein Spaceship-Account gesetzt.</div>')
    domain = domain.strip().lower()
    if not domain or "." not in domain:
        return HTMLResponse('<div class="alert alert-warning">Ungültige Domain.</div>')

    # CF-Auth aus dem Dynadot-Modul wiederverwenden
    from .dynadot import _cf_headers_for
    headers, cf_account = _cf_headers_for(db, cf_account_id)
    if not headers:
        return HTMLResponse('<div class="alert alert-danger">Kein Cloudflare-Account im Panel — erst dort hinterlegen.</div>')

    import requests as _req
    # Zone da?
    zone_id = None
    ns_list = []
    try:
        r = _req.get("https://api.cloudflare.com/client/v4/zones",
                     headers=headers, params={"name": domain}, timeout=15)
        data = r.json()
        if data.get("success") and data.get("result"):
            zone_id = data["result"][0]["id"]
            ns_list = data["result"][0].get("name_servers", [])
    except Exception as e:
        return HTMLResponse(f'<div class="alert alert-danger">CF-Zone-Lookup failed: {escape(str(e))}</div>')

    ns_hint = ""
    if not zone_id:
        # Zone neu anlegen
        try:
            body = {"name": domain, "type": "full"}
            if cf_account:
                body["account"] = {"id": cf_account}
            r = _req.post("https://api.cloudflare.com/client/v4/zones",
                          headers=headers, json=body, timeout=20)
            data = r.json()
            if not data.get("success"):
                errs = "; ".join(e.get("message", "") for e in data.get("errors", []))
                return HTMLResponse(f'<div class="alert alert-danger">CF-Zone anlegen: {escape(errs)}</div>')
            zone_id = data["result"]["id"]
            ns_list = data["result"].get("name_servers", [])
            ns_hint = f"Neue CF-Zone angelegt (ID {zone_id[:12]}…)"
        except Exception as e:
            return HTMLResponse(f'<div class="alert alert-danger">CF Zone add failed: {escape(str(e))}</div>')

    if not ns_list:
        return HTMLResponse('<div class="alert alert-warning">CF hat keine Nameserver geliefert.</div>')

    # NS bei Spaceship setzen mit Retry
    logs = []
    if ns_hint:
        logs.append((ns_hint, True, ""))
    res = ss_set_ns_with_retry(
        key, secret, domain, ns_list,
        log_step=lambda label, ok, detail: logs.append((label, ok, detail)))
    log_html = "".join(
        f'<li>{"✓" if ok else "…"} <strong>{escape(label)}</strong>'
        f'{": " + escape(detail) if detail else ""}</li>'
        for label, ok, detail in logs
    )
    if res["ok"]:
        return HTMLResponse(
            f'<div class="alert alert-success">✓ <code>{escape(domain)}</code> zeigt jetzt auf Cloudflare</div>'
            f'<p class="muted" style="font-size:12px">NS: {escape(", ".join(ns_list))}</p>'
            f'<ul style="font-size:12px">{log_html}</ul>'
        )
    return HTMLResponse(
        f'<div class="alert alert-warning">NS-Set fehlgeschlagen: {escape(res["msg"])}</div>'
        f'<p class="muted" style="font-size:12px">CF-NS zum manuellen Setzen: <code>{escape(", ".join(ns_list))}</code></p>'
        f'<ul style="font-size:12px">{log_html}</ul>'
    )
