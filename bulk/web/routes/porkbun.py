"""Porkbun Registrar Integration — https://porkbun.com/api/json/v3/documentation

Auth: apikey + secretapikey pro Request-Body (nicht Header).
Alle Requests sind POST mit JSON. Endpoints:
  /ping                           auth-test, returns your IP
  /domain/listAll?start=N         list domains (max 1000 per call, paginated)
  /domain/updateNs/{domain}       set nameservers (body: {ns: [...]})
  /domain/getNs/{domain}          get current NS
  /domain/checkDomain/{domain}    availability check

Wie Spaceship mit Retry-Semantik beim NS-Setzen für frisch registrierte Domains.
"""
import time
import threading
import logging
from html import escape
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()
logger = logging.getLogger("bulk.porkbun")

PORKBUN_BASE = "https://api.porkbun.com/api/json/v3"

# Live-Log für Buy-Flow (analog Dynadot _buy_progress)
_pb_progress = {"running": False, "log": [], "domain": "", "done": False}


# ── Low-Level API Call ────────────────────────────────────

def _normalize_proxy(raw: str):
    """Nimmt einen Proxy-String und liefert ein requests-proxies dict.
    Akzeptiert host:port, host:port:user:pass, scheme://…, scheme://host:port:user:pass.
    Default-Scheme socks5h (DNS auch durch proxy). Return None wenn leer/kaputt.

    Wichtig: socks5 (ohne h) → wird auf socks5h upgraded. Viele Proxy-Provider
    haben Ziel-Whitelists auf Hostname-Ebene — lokales DNS liefert die IP und
    der Proxy blockt (Reply 0x05 Connection refused). Remote DNS umgeht das.
    Wer WIRKLICH lokales DNS will kann socks5-local:// als Scheme nutzen."""
    s = (raw or "").strip()
    if not s:
        return None
    scheme = "socks5h"
    rest = s
    if "://" in s:
        scheme, rest = s.split("://", 1)
    # socks5 → socks5h upgrade. socks5-local behält lokales DNS.
    if scheme == "socks5":
        scheme = "socks5h"
    elif scheme == "socks5-local":
        scheme = "socks5"
    if "@" in rest:
        url = f"{scheme}://{rest}"
        return {"http": url, "https": url}
    parts = rest.split(":")
    if len(parts) == 2:
        url = f"{scheme}://{parts[0]}:{parts[1]}"
    elif len(parts) == 4:
        url = f"{scheme}://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    else:
        return None
    return {"http": url, "https": url}


def _pb_call(api_key: str, api_secret: str, path: str,
              body: dict = None, timeout: int = 20,
              proxy: str = "") -> dict:
    """Alle Porkbun-Calls sind POST mit apikey+secretapikey im Body.
    Optional per SOCKS5/HTTP-Proxy — für Anti-Fingerprinting oder wenn
    der Registrar auf Hetzner-IPs blockt."""
    import requests
    if not api_key or not api_secret:
        return {"_error": "Kein Porkbun API-Key + Secret gesetzt", "status": "ERROR"}
    payload = {"apikey": api_key, "secretapikey": api_secret}
    if body:
        payload.update(body)
    kwargs = {"json": payload, "timeout": timeout}
    px = _normalize_proxy(proxy)
    if px:
        kwargs["proxies"] = px
    try:
        r = requests.post(f"{PORKBUN_BASE}{path}", **kwargs)
        try:
            data = r.json() if r.text else {}
        except Exception:
            data = {"_raw": r.text[:500]}
        data.setdefault("_status", r.status_code)
        if data.get("status") == "ERROR" and "_error" not in data:
            data["_error"] = data.get("message", "unknown error")
        return data
    except Exception as e:
        return {"_error": f"Porkbun-Request failed: {e}", "_status": 0, "status": "ERROR"}


def _pb_from_account_id(db, aid: int) -> tuple:
    """Returns (api_key, api_secret, name, proxy)."""
    row = db.get_porkbun_account(aid) if aid else db.get_primary_porkbun_account()
    if not row:
        return "", "", "", ""
    r = dict(row)
    return (r.get("api_key", ""), r.get("api_secret", ""),
            r.get("name", ""), r.get("proxy", "") or "")


# ── High-Level Convenience ────────────────────────────────

def pb_ping(api_key: str, api_secret: str, proxy: str = "") -> dict:
    """Auth-Test — returns your IP address on success."""
    return _pb_call(api_key, api_secret, "/ping", proxy=proxy)


def pb_list_domains(api_key: str, api_secret: str, start: int = 0,
                     proxy: str = "") -> dict:
    """List domains — bis 1000 pro Call, paginierbar via start=N."""
    return _pb_call(api_key, api_secret, "/domain/listAll",
                     body={"start": start, "includeLabels": "yes"},
                     proxy=proxy)


def pb_get_ns(api_key: str, api_secret: str, domain: str,
               proxy: str = "") -> dict:
    return _pb_call(api_key, api_secret, f"/domain/getNs/{domain}",
                     proxy=proxy)


def _is_rate_limit(msg: str) -> bool:
    """Porkbun-Text: '1 out of 1 checks within 10 seconds used.' etc."""
    m = (msg or "").lower()
    return ("checks within" in m or "check within" in m
            or "10 seconds" in m or "rate limit" in m or "too many" in m)


def pb_check_availability(api_key: str, api_secret: str, domain: str,
                            wait_on_rate_limit: bool = False,
                            max_retries: int = 3,
                            log_step=None,
                            proxy: str = "") -> dict:
    """Rückgabe: {available, price, cents, currency, premium, raw,
                  error, rate_limited}.
    Porkbun erlaubt checkDomain nur 1×/10s. Wenn `wait_on_rate_limit=True`,
    warten wir 12s und retryen bis zu `max_retries`× ."""
    resp = {}
    for attempt in range(max_retries):
        resp = _pb_call(api_key, api_secret, f"/domain/checkDomain/{domain}",
                         proxy=proxy)
        if resp.get("status") == "SUCCESS":
            break
        err_msg = resp.get("message") or resp.get("_error") or ""
        rl = _is_rate_limit(err_msg)
        if rl and wait_on_rate_limit and attempt < max_retries - 1:
            if log_step:
                log_step(f"Rate-Limit hit — warte 12s (Attempt {attempt+2}/{max_retries})")
            time.sleep(12)
            continue
        # kein Success, kein Retry mehr → return mit Flag
        return {
            "available": None, "price": "", "cents": 0, "currency": "USD",
            "premium": False, "raw": resp,
            "error": err_msg or "unknown",
            "rate_limited": rl,
        }

    r = resp.get("response") or {}
    avail_raw = r.get("isAvailable") if "isAvailable" in r else (r.get("avail") or r.get("available"))
    if isinstance(avail_raw, str):
        available = avail_raw.lower() in ("yes", "true", "available", "1")
    elif isinstance(avail_raw, bool):
        available = avail_raw
    else:
        available = None
    # Bevorzugt Promo-Preis, sonst regulär
    price_str = str(r.get("price") or r.get("regularPrice") or r.get("regular_price") or "")
    try:
        cents = int(round(float(price_str) * 100)) if price_str else 0
    except ValueError:
        cents = 0
    return {
        "available": available, "price": price_str, "cents": cents,
        "currency": "USD",
        "premium": bool(r.get("isPremium") or r.get("premium")),
        "raw": resp, "error": "", "rate_limited": False,
    }


def pb_register_domain(api_key: str, api_secret: str, domain: str,
                        cost_cents: int, proxy: str = "") -> dict:
    """POST /domain/create/{domain} — Body {cost, agreeToTerms:"yes"}.
    Cost in Cents, muss zum Availability-Preis passen sonst reject.
    Nutzt die Default-Kontakte am Account (im Porkbun-Dashboard einmal setzen)."""
    if cost_cents <= 0:
        return {"ok": False, "msg": "cost=0 — Availability-Check zuerst nötig",
                "raw": {}}
    resp = _pb_call(api_key, api_secret, f"/domain/create/{domain}",
                    body={"cost": cost_cents, "agreeToTerms": "yes"},
                    timeout=45, proxy=proxy)
    status = (resp.get("status") or "").upper()
    if status == "SUCCESS":
        return {"ok": True, "msg": "registriert",
                "order_id": resp.get("orderId") or resp.get("order_id"),
                "balance": resp.get("balance"),
                "raw": resp}
    return {"ok": False,
            "msg": resp.get("message") or resp.get("_error") or f"HTTP {resp.get('_status')}",
            "raw": resp}


def pb_get_pricing(api_key: str, api_secret: str, proxy: str = "") -> dict:
    return _pb_call(api_key, api_secret, "/pricing/get", proxy=proxy)


def pb_set_nameservers(api_key: str, api_secret: str, domain: str,
                         ns_list: list, proxy: str = "") -> dict:
    """NS setzen — Body {ns: [...]}. Returns {ok, msg, not_ready_yet}."""
    if not ns_list or len(ns_list) < 2:
        return {"ok": False, "msg": "brauche mindestens 2 Nameserver",
                "not_ready_yet": False}
    resp = _pb_call(api_key, api_secret, f"/domain/updateNs/{domain}",
                    body={"ns": ns_list[:8]}, proxy=proxy)
    status = (resp.get("status") or "").upper()
    msg = resp.get("message") or resp.get("_error") or ""
    msg_lower = msg.lower()
    not_ready = ("not currently registered" in msg_lower
                 or "pending" in msg_lower
                 or "waiting" in msg_lower
                 or "not found" in msg_lower)
    if status == "SUCCESS":
        return {"ok": True, "msg": "NS gesetzt", "not_ready_yet": False}
    if not_ready:
        return {"ok": False, "msg": msg or "pending", "not_ready_yet": True}
    return {"ok": False, "msg": msg or f"HTTP {resp.get('_status')}",
            "not_ready_yet": False}


def pb_set_ns_with_retry(api_key: str, api_secret: str, domain: str,
                           ns_list: list, log_step=None,
                           proxy: str = "") -> dict:
    is_de = domain.lower().endswith(".de")
    max_retries = 4 if is_de else 2
    wait_s = 30 if is_de else 10
    last = {"ok": False, "msg": "kein Versuch"}
    for attempt in range(max_retries):
        if attempt > 0:
            if log_step:
                log_step(f"NS: warte {wait_s}s (Attempt {attempt+1}/{max_retries})",
                         True, "Porkbun braucht kurz bis der Domain-Datensatz settled ist")
            time.sleep(wait_s)
        r = pb_set_nameservers(api_key, api_secret, domain, ns_list, proxy=proxy)
        last = r
        if r["ok"] or not r.get("not_ready_yet"):
            return r
    return last


# ── Panel Routes ──────────────────────────────────────────

@router.get("/porkbun", response_class=HTMLResponse)
async def porkbun_page(request: Request):
    db = request.app.state.db
    accounts = [dict(a) for a in db.get_porkbun_accounts()]
    primary = db.get_primary_porkbun_account()
    domains = []
    ping_ip = ""
    if primary:
        pd = dict(primary)
        _px = pd.get("proxy", "") or ""
        p = pb_ping(pd["api_key"], pd["api_secret"], proxy=_px)
        if p.get("_error"):
            ping_ip = f"— ({p['_error']})"
        else:
            ping_ip = p.get("yourIp") or p.get("your_ip") or "OK"
        dlist = pb_list_domains(pd["api_key"], pd["api_secret"], proxy=_px)
        if dlist.get("status") == "SUCCESS":
            domains = dlist.get("domains") or []
    cf_accounts = [dict(a) for a in db.get_cf_accounts()]
    return request.app.state.templates.TemplateResponse(request, "porkbun.html", {
        "active": "porkbun",
        "accounts": accounts,
        "primary": primary and dict(primary),
        "domains": domains[:200],
        "ping_ip": ping_ip,
        "cf_accounts": cf_accounts,
    })


@router.post("/porkbun/accounts/add")
async def add_account(request: Request,
                       name: str = Form(""),
                       api_key: str = Form(""),
                       api_secret: str = Form(""),
                       proxy: str = Form("")):
    db = request.app.state.db
    if name.strip() and api_key.strip() and api_secret.strip():
        aid = db.add_porkbun_account(name.strip(), api_key.strip(), api_secret.strip())
        if proxy.strip():
            db.update_porkbun_account(aid, proxy=proxy.strip())
    return RedirectResponse("/porkbun", status_code=303)


@router.post("/porkbun/accounts/{aid}/update")
async def update_account(request: Request, aid: int,
                          name: str = Form(""),
                          api_key: str = Form(""),
                          api_secret: str = Form(""),
                          proxy: str = Form("__unchanged__")):
    db = request.app.state.db
    fields = {}
    if name.strip():
        fields["name"] = name.strip()
    if api_key.strip():
        fields["api_key"] = api_key.strip()
    if api_secret.strip():
        fields["api_secret"] = api_secret.strip()
    # Proxy explizit setzbar (auch leer = "kein proxy" möglich).
    # Sentinel "__unchanged__" damit ein fehlendes Form-Feld nicht die
    # bestehenden Proxy-Einstellungen zurücksetzt.
    if proxy != "__unchanged__":
        fields["proxy"] = proxy.strip()
    db.update_porkbun_account(aid, **fields)
    return RedirectResponse("/porkbun", status_code=303)


@router.post("/porkbun/accounts/{aid}/set-primary")
async def set_primary(request: Request, aid: int):
    request.app.state.db.set_primary_porkbun_account(aid)
    return RedirectResponse("/porkbun", status_code=303)


@router.post("/porkbun/accounts/{aid}/delete")
async def delete_account(request: Request, aid: int):
    request.app.state.db.delete_porkbun_account(aid)
    return RedirectResponse("/porkbun", status_code=303)


@router.post("/porkbun/test", response_class=HTMLResponse)
async def test_account(request: Request, account_id: int = Form(0)):
    db = request.app.state.db
    key, secret, name, proxy = _pb_from_account_id(db, account_id)
    if not key or not secret:
        return HTMLResponse('<span style="color:var(--red)">Kein Account oder API-Key fehlt</span>')
    p = pb_ping(key, secret, proxy=proxy)
    if p.get("_error"):
        return HTMLResponse(
            f'<span style="color:var(--red)">✗ {escape(p["_error"])}'
            f'{" [via proxy]" if proxy else ""}</span>'
        )
    return HTMLResponse(
        f'<span style="color:var(--green)">✓ Auth OK — dein IP bei Porkbun: '
        f'<code>{escape(str(p.get("yourIp", "?")))}</code>'
        f'{" [via proxy]" if proxy else ""}</span>'
    )


@router.post("/porkbun/set-ns", response_class=HTMLResponse)
async def set_ns_route(request: Request, domain: str = Form(""),
                        nameservers: str = Form(""),
                        account_id: int = Form(0)):
    db = request.app.state.db
    key, secret, name, proxy = _pb_from_account_id(db, account_id)
    if not key or not secret:
        return HTMLResponse('<span style="color:var(--red)">Kein Account gesetzt</span>')
    domain = domain.strip().lower()
    ns = [n.strip() for n in nameservers.replace(",", "\n").splitlines() if n.strip()]
    if not domain or len(ns) < 2:
        return HTMLResponse('<span style="color:var(--red)">Domain + mind. 2 NS pflicht</span>')
    logs = []
    res = pb_set_ns_with_retry(
        key, secret, domain, ns,
        log_step=lambda label, ok, detail: logs.append((label, ok, detail)),
        proxy=proxy)
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


@router.post("/porkbun/search", response_class=HTMLResponse)
async def search_domains(request: Request, query: str = Form(""),
                          account_id: int = Form(0)):
    """Bulk-Availability. Porkbun bietet KEIN API-Register — daher für
    verfügbare Domains ein Deep-Link zu Porkbun-Cart."""
    db = request.app.state.db
    key, secret, name, proxy = _pb_from_account_id(db, account_id)
    if not key or not secret:
        return HTMLResponse('<div class="alert alert-danger">Kein Porkbun-Account gesetzt.</div>')
    domains_raw = [d.strip().lower() for d in query.replace(",", "\n").splitlines() if d.strip()]
    if not domains_raw:
        return HTMLResponse('<div class="alert alert-warning">Mindestens eine Domain eingeben.</div>')

    # Domains im Account? (aus /domain/listAll)
    owned = set()
    try:
        dl = pb_list_domains(key, secret, proxy=proxy)
        if dl.get("status") == "SUCCESS":
            for it in dl.get("domains") or []:
                nm = (it.get("domain") or it.get("name") or "").lower()
                if nm:
                    owned.add(nm)
    except Exception as e:
        logger.warning("owned-lookup failed: %s", e)

    rows = ""
    for idx, d in enumerate(domains_raw[:15]):
        did = d.replace(".", "-")
        if d in owned:
            rows += (
                f'<tr>'
                f'<td style="font-family:monospace">{escape(d)}</td>'
                f'<td><span class="badge badge-info">In deinem Account</span></td>'
                f'<td>—</td>'
                f'<td>'
                f'<button class="btn btn-primary btn-xs" '
                f'hx-post="/porkbun/quick-cf" '
                f'hx-vals=\'{{"domain":"{escape(d)}"}}\' '
                f'hx-include="#pb-cf-account,#pb-search-account" '
                f'hx-target="#pb-search-result-{did}" hx-swap="innerHTML">'
                f'→ CF-Zone + NS setzen</button>'
                f'<div id="pb-search-result-{did}" style="margin-top:4px;font-size:11px"></div>'
                f'</td>'
                f'</tr>'
            )
            continue
        # Search-Row: warten wenn Rate-Limit (Porkbun: 1/10s)
        if idx > 0:
            time.sleep(11)
        r = pb_check_availability(key, secret, d, wait_on_rate_limit=True,
                                    max_retries=2, proxy=proxy)
        if r["available"] is True:
            badge = '<span class="badge badge-running">Verfügbar</span>'
            if r["premium"]:
                badge = '<span class="badge badge-warning">Premium</span>'
            price = f'{r["price"]} {r["currency"]}' if r["price"] else '—'
            confirm_txt = f"{d} für {price} kaufen und automatisch mit Cloudflare verbinden?"
            action = (
                f'<button class="btn btn-primary btn-xs" '
                f'hx-post="/porkbun/buy" '
                f'hx-vals=\'{{"domain":"{escape(d)}","cost_cents":"{r["cents"]}"}}\' '
                f'hx-include="#pb-search-account,#pb-cf-account" '
                f'hx-target="#pb-buy-result" hx-swap="innerHTML" '
                f'hx-confirm="{escape(confirm_txt)}">'
                f'→ Kaufen + CF anschließen</button>'
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
        'Porkbun rate-limitet Availability-Checks auf <strong>1 pro 10 Sekunden</strong> — '
        'wir warten dazwischen, deshalb dauert Bulk-Suche mit N Domains ca. N×11s. '
        'Cap: 15 pro Batch. Beim „Kaufen" schickt der Button den Preis aus dieser '
        'Suche mit → kein zweiter Check nötig, kein Rate-Limit-Konflikt.</p>'
    )


def _do_buy_pb(db, api_key, api_secret, domain, cf_account_id, log,
                cost_cents: int = 0, proxy: str = ""):
    """Register → CF-Zone → NS bei Porkbun setzen (mit Retry). Analog
    Dynadot _do_buy. Wenn `cost_cents>0` übergeben (z.B. vom Search-Result),
    sparen wir uns den zweiten Availability-Call (Rate-Limit!) und
    registrieren direkt. `proxy` (SOCKS5/HTTP) optional — der Registrar-
    Traffic läuft dann darüber."""
    if cost_cents > 0:
        log.append(f"Preis aus Suche übernommen: {cost_cents/100:.2f} USD "
                    f"({cost_cents} cents) — kein zweiter Availability-Check.")
        av_cents = cost_cents
    else:
        log.append(f"Availability-Check für {domain}… (retry falls Rate-Limit)")
        av = pb_check_availability(api_key, api_secret, domain,
                                    wait_on_rate_limit=True, max_retries=3,
                                    log_step=lambda m: log.append(m),
                                    proxy=proxy)
        if av.get("rate_limited"):
            log.append("Rate-Limit hält an — abbruch.")
            return
        if av["available"] is not True:
            log.append(f"Nicht verfügbar ({av.get('error') or 'occupied'}) — abbruch.")
            return
        if av["cents"] <= 0:
            log.append("Kein Preis vom Availability-Check — abbruch.")
            return
        log.append(f"Verfügbar: {av['price']} USD ({av['cents']} cents).")
        if av["premium"]:
            log.append("⚠ Premium-Domain! Weiter mit angegebenem Preis.")
        av_cents = av["cents"]

    log.append(f"Register {domain} über /domain/create/{domain}…"
                + (f" [via proxy]" if proxy else ""))
    reg = pb_register_domain(api_key, api_secret, domain, av_cents, proxy=proxy)
    if not reg["ok"]:
        if _is_rate_limit(reg.get("msg", "")):
            log.append("Register Rate-Limit — warte 12s und retry.")
            time.sleep(12)
            reg = pb_register_domain(api_key, api_secret, domain, av_cents,
                                       proxy=proxy)
        if not reg["ok"]:
            log.append(f"Register fehlgeschlagen: {reg['msg']}")
            # Volle Porkbun-Response ins Log — "Unable to register domain"
            # allein hilft nicht, aber im raw stehen oft die echten Gründe
            # (fehlende Contact-Info, Whois-Verify, no buy permission, …)
            import json as _json
            try:
                raw_str = _json.dumps(reg.get("raw", {}), ensure_ascii=False,
                                       indent=2)[:1200]
                log.append(f"→ Porkbun raw response:\n{raw_str}")
            except Exception:
                log.append(f"→ raw: {str(reg.get('raw'))[:800]}")
            # Häufigste Ursachen als Checkliste
            msg_low = reg.get("msg", "").lower()
            hints = []
            if "unable to register" in msg_low:
                hints.append("• Porkbun-Dashboard → API → Edit Key → 'Domains' Toggle AN (buy permission)")
                hints.append("• Porkbun-Dashboard → Account → Contact Info alle Felder gefüllt (Name/Adresse/Tel/Land)")
                hints.append("• Bei .de: DENIC braucht komplette Adresse — Postfach reicht nicht")
                hints.append("• Erste Bestellung → evtl. Account-Verify im Porkbun-Dashboard nötig")
                hints.append("• Guthaben checken (Balance im Response oben — wenn None: Account nicht auf Prepaid oder Karte fehlt)")
            elif "insufficient" in msg_low or "balance" in msg_low:
                hints.append("• Guthaben zu niedrig — Porkbun-Dashboard → Add Funds")
            elif "price" in msg_low or "cost" in msg_low:
                hints.append("• Preis-Mismatch — nochmal aus Suche kaufen (kein 10s-Stale-Cache)")
            for h in hints:
                log.append(h)
            return
    log.append(f"✓ Registriert. OrderID: {reg.get('order_id') or '?'}")
    if reg.get("balance") is not None:
        log.append(f"Neue Balance: {reg['balance']}")

    # CF-Zone anlegen + NS setzen
    from .dynadot import _cf_headers_for
    cf_headers, account_id = _cf_headers_for(db, cf_account_id)
    if not cf_headers:
        log.append("Kein CF-Account konfiguriert — überspringe Zone-Anlage.")
        log.append(f"Fertig: {domain} gehört dir, NS noch beim Porkbun-Default.")
        return

    log.append("Cloudflare-Zone anlegen…")
    try:
        import requests as _req
        body = {"name": domain, "type": "full"}
        if account_id:
            body["account"] = {"id": account_id}
        r = _req.post("https://api.cloudflare.com/client/v4/zones",
                      headers=cf_headers, json=body, timeout=20)
        data = r.json()
        if not data.get("success"):
            # Vielleicht existiert die Zone schon
            r = _req.get("https://api.cloudflare.com/client/v4/zones",
                         headers=cf_headers, params={"name": domain}, timeout=15)
            data = r.json()
            if not data.get("success") or not data.get("result"):
                errs = "; ".join(e.get("message", "") for e in data.get("errors", []))
                log.append(f"CF-Zone Fehler: {errs or 'unknown'}")
                return
            zone = data["result"][0]
        else:
            zone = data["result"]
        ns_list = zone.get("name_servers", [])
        zone_id = zone.get("id", "")
        log.append(f"CF-Zone: {zone_id[:12]}… NS: {', '.join(ns_list)}")
    except Exception as e:
        log.append(f"CF-Zone Ausnahme: {e}")
        return

    if not ns_list:
        log.append("Keine NS von CF geliefert — abbruch NS-Set.")
        return

    log.append("Nameserver bei Porkbun setzen…")
    res = pb_set_ns_with_retry(
        api_key, api_secret, domain, ns_list,
        log_step=lambda label, ok, detail: log.append(
            f"{'✓' if ok else '…'} {label}"
            f"{': ' + detail if detail else ''}"),
        proxy=proxy)
    if res["ok"]:
        log.append(f"✓ NS gesetzt. {domain} zeigt jetzt auf Cloudflare.")
    else:
        log.append(f"✗ NS-Set fehlgeschlagen: {res['msg']} — "
                    "später manuell via '→ CF-Zone + NS' retryen.")


@router.post("/porkbun/buy", response_class=HTMLResponse)
async def buy_domain(request: Request, domain: str = Form(""),
                      account_id: int = Form(0),
                      cf_account_id: int = Form(0),
                      cost_cents: int = Form(0)):
    """One-Click Register + CF + NS für eine Porkbun-Domain.
    `cost_cents` optional — aus dem Search-Result-Button — spart einen
    zweiten checkDomain-Aufruf (der wegen Rate-Limit sonst hängen würde)."""
    db = request.app.state.db
    key, secret, name, proxy = _pb_from_account_id(db, account_id)
    if not key or not secret:
        return HTMLResponse('<div class="alert alert-danger">Kein Porkbun-Account gesetzt.</div>')
    domain = domain.strip().lower()
    if not domain or "." not in domain:
        return HTMLResponse('<div class="alert alert-warning">Ungültige Domain.</div>')
    if _pb_progress["running"]:
        return HTMLResponse(
            '<div class="alert alert-warning">Bereits ein Kauf am laufen. '
            'Warte bis der aktuelle fertig ist.</div>')
    _pb_progress.update(running=True, log=[], domain=domain, done=False)

    def worker():
        log = _pb_progress["log"]
        try:
            _do_buy_pb(db, key, secret, domain, cf_account_id, log,
                        cost_cents=cost_cents, proxy=proxy)
        except Exception as e:
            log.append(f"Fatal: {e}")
        finally:
            _pb_progress["done"] = True
            _pb_progress["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return HTMLResponse(
        f'<div class="alert alert-info">Kaufe <code>{escape(domain)}</code>…</div>'
        f'<div id="pb-buy-live" hx-get="/porkbun/buy-progress" '
        f'hx-trigger="every 2s" hx-swap="innerHTML"></div>'
    )


@router.get("/porkbun/buy-progress", response_class=HTMLResponse)
async def buy_progress(request: Request):
    log = _pb_progress["log"]
    done = _pb_progress["done"]
    lines = ""
    for line in log:
        color = "var(--fg)"
        low = line.lower()
        if "✓" in line or "registriert" in low or "fertig" in low or "gesetzt" in low:
            color = "var(--green)"
        elif "✗" in line or "fehlgeschlagen" in low or "fehler" in low or "fatal" in low or "abbruch" in low:
            color = "var(--red)"
        elif "warte" in low or "⚠" in line:
            color = "var(--yellow)"
        lines += f'<div style="color:{color};padding:2px 0">{escape(line)}</div>'
    html = (
        '<div style="font-family:monospace;font-size:12px;background:var(--bg);'
        'padding:12px;border-radius:var(--radius)">' + lines + '</div>'
    )
    if not done:
        html += ('<div hx-get="/porkbun/buy-progress" hx-trigger="every 2s" '
                 'hx-swap="outerHTML"></div>')
    return HTMLResponse(html)


@router.post("/porkbun/quick-cf", response_class=HTMLResponse)
async def porkbun_quick_cf(request: Request, domain: str = Form(""),
                            cf_account_id: int = Form(0),
                            account_id: int = Form(0)):
    return await set_ns_from_cf(request, domain=domain,
                                 cf_account_id=cf_account_id,
                                 account_id=account_id)


@router.post("/porkbun/set-ns-cf", response_class=HTMLResponse)
async def set_ns_from_cf(request: Request, domain: str = Form(""),
                          cf_account_id: int = Form(0),
                          account_id: int = Form(0)):
    """Auto-Flow: CF-Zone anlegen falls nicht da → NS bei Porkbun setzen mit Retry."""
    db = request.app.state.db
    key, secret, pb_name, proxy = _pb_from_account_id(db, account_id)
    if not key or not secret:
        return HTMLResponse('<div class="alert alert-danger">Kein Porkbun-Account gesetzt.</div>')
    domain = domain.strip().lower()
    if not domain or "." not in domain:
        return HTMLResponse('<div class="alert alert-warning">Ungültige Domain.</div>')

    from .dynadot import _cf_headers_for
    headers, cf_account = _cf_headers_for(db, cf_account_id)
    if not headers:
        return HTMLResponse('<div class="alert alert-danger">Kein Cloudflare-Account im Panel — erst dort hinterlegen.</div>')

    import requests as _req
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

    logs = []
    if ns_hint:
        logs.append((ns_hint, True, ""))
    res = pb_set_ns_with_retry(
        key, secret, domain, ns_list,
        log_step=lambda label, ok, detail: logs.append((label, ok, detail)),
        proxy=proxy)
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
