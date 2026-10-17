"""Domain-Panel — Dynadot (Kauf + Search) + Cloudflare (Zones + DNS)
+ End-to-End Pipeline (Kauf → CF Zone → A-Records → Turnstile → Gate + Links)."""
import os
import time
import secrets
import logging
from html import escape
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

# Zielverzeichnis für hochgeladene Gate-Logos (per Pipeline). Wird via
# StaticFiles-Mount im app/main.py als /static/logo/uploads/ serviert.
LOGO_UPLOAD_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "static", "logo", "uploads"))
os.makedirs(LOGO_UPLOAD_DIR, exist_ok=True)
_LOGO_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
_LOGO_MAX_BYTES = 2 * 1024 * 1024

from ..presets import MODE_PRESETS, gen_slug, detect_public_ip

logger = logging.getLogger("antibot.domains")
router = APIRouter()

DYNADOT_BASE = "https://api.dynadot.com/api3.json"
CF_BASE = "https://api.cloudflare.com/client/v4"


# ── Dynadot helpers ────────────────────────────────────────

def _dyn_call(api_key: str, command: str, params: dict = None,
              secret: str = "") -> dict:
    """Dynadot API v3 (JSON). Returns parsed dict, or {'error': msg}.
    `secret` is Dynadot's optional API-secret (needed for some actions
    like register / renew when the account has 'API secret' enabled)."""
    import requests
    if not api_key:
        return {"error": "Kein Dynadot API-Key gesetzt."}
    url = f"{DYNADOT_BASE}?key={api_key}&command={command}"
    if secret:
        url += f"&secret={secret}"
    if params:
        for k, v in params.items():
            url += f"&{k}={v}"
    try:
        r = requests.get(url, timeout=25)
        return r.json()
    except Exception as e:
        return {"error": f"Dynadot request failed: {e}"}


def _dyn_balance(api_key: str, secret: str = "") -> str:
    resp = _dyn_call(api_key, "account_info", secret=secret)
    if "error" in resp:
        return f"— ({resp['error']})"
    try:
        return resp.get("AccountInfoResponse", {}).get("Account", {}).get("AccountBalance", "?")
    except Exception:
        return "?"


def _dyn_search(api_key: str, domain: str, currency: str = "USD",
                 secret: str = "") -> dict:
    """Check availability + price for a single domain."""
    resp = _dyn_call(api_key, "search",
                     {"domain0": domain, "currency": currency}, secret=secret)
    if "error" in resp:
        return {"available": False, "price": "", "error": resp["error"]}
    try:
        r = resp["SearchResponse"]["SearchResults"][0]
        return {
            "available": r.get("Available") == "yes",
            "price": r.get("Price", ""),
            "raw": r,
            "error": "",
        }
    except Exception as e:
        return {"available": False, "price": "", "error": f"Parse: {e}", "raw": resp}


def _dyn_register(api_key: str, domain: str, currency: str = "USD",
                   duration: int = 1, secret: str = "") -> dict:
    """Actually buy the domain. Returns {'ok': bool, 'msg': str, 'raw': ...}.

    Dynadot ist bei den Response-Typen inkonsistent — mal Integer 0,
    mal String '0', mal Status 'success', mal ResponseCode 'success'.
    Wir akzeptieren alle plausiblen Erfolgs-Signale und loggen bei
    Unsicherheit die Raw-Response mit."""
    params = {"domain": domain, "duration": duration, "currency": currency}
    resp = _dyn_call(api_key, "register", params, secret=secret)
    if "error" in resp:
        return {"ok": False, "msg": resp["error"]}
    try:
        r = resp["RegisterResponse"]
    except (KeyError, TypeError):
        return {"ok": False, "msg": f"kein RegisterResponse: {str(resp)[:200]}"}

    # Success-Signale einsammeln (Dynadot ist sloppy mit den Typen)
    code_raw = r.get("ResponseCode")
    code_str = str(code_raw).strip().lower() if code_raw is not None else ""
    status_str = (r.get("Status") or "").strip().lower()
    error_str = (r.get("Error") or "").strip()

    is_success = (
        code_str in ("0", "success", "ok")
        or status_str in ("success", "ok")
        or (not error_str and "RegisterContent" in r)
    )
    if is_success:
        return {"ok": True, "msg": f"gekauft (status={status_str or code_str or '?'})", "raw": r}
    return {"ok": False,
            "msg": error_str or status_str or code_str or f"unklar: {str(r)[:200]}",
            "raw": r}


def _dyn_domains(api_key: str, secret: str = "") -> list:
    resp = _dyn_call(api_key, "list_domain", secret=secret)
    if "error" in resp:
        return []
    try:
        d = resp["ListDomainInfoResponse"]["MainDomains"]["Domain"]
        if isinstance(d, dict):
            d = [d]
        return d
    except Exception:
        return []


def _dyn_set_ns(api_key: str, domain: str, ns_list: list,
                 secret: str = "") -> dict:
    """set_ns bei Dynadot. Erwartet mindestens ns1+ns2. Nur EIN Versuch —
    Retry-Loop macht der Aufrufer damit er Delay-Steps loggen kann.
    Returns {ok, msg, not_ready_yet: bool}."""
    if not ns_list or len(ns_list) < 2:
        return {"ok": False, "msg": "keine NS geliefert von CF"}
    params = {"domain": domain, "ns0": ns_list[0], "ns1": ns_list[1]}
    # Dynadot's set_ns nimmt ns0..nsN (nicht ns1..nsN wie manche docs sagen)
    for i, ns in enumerate(ns_list[2:6], start=2):
        params[f"ns{i}"] = ns
    resp = _dyn_call(api_key, "set_ns", params, secret=secret)
    if "error" in resp:
        return {"ok": False, "msg": resp["error"], "not_ready_yet": False}

    r = resp.get("SetNsResponse", resp)
    code_str = str(r.get("ResponseCode", "-1")).strip().lower()
    status_str = (r.get("Status") or "").strip().lower()
    r_lower = str(r).lower()
    # Dynadot Sondermeldung: frisch gekaufte Domain ist noch nicht "settled"
    not_ready = ("dns queries" in r_lower or "must respond" in r_lower
                 or "not registered" in r_lower or "pending" in r_lower)
    if code_str in ("0", "success", "ok") or status_str in ("success", "ok"):
        return {"ok": True, "msg": "gesetzt", "not_ready_yet": False}
    if not_ready:
        return {"ok": False, "msg": "Domain bei Dynadot noch nicht settled",
                "not_ready_yet": True}
    return {"ok": False,
            "msg": r.get("Error") or status_str or code_str or str(r)[:200],
            "not_ready_yet": False}


def _dyn_set_ns_with_retry(api_key: str, domain: str, ns_list: list,
                             secret: str = "", log_step=None) -> dict:
    """Set NS mit Retry-Loop (wie im bulk mailer):
      .de → 4 Versuche, 30s Delay
      andere → 2 Versuche, 10s Delay
    log_step (optional) = callable(label, ok, detail) für step-by-step Feedback."""
    is_de = domain.lower().endswith(".de")
    max_retries = 4 if is_de else 2
    wait_s = 30 if is_de else 10

    last = {"ok": False, "msg": "kein Versuch"}
    for attempt in range(max_retries):
        if attempt > 0:
            if log_step:
                log_step(f"NS: warte {wait_s}s (Attempt {attempt+1}/{max_retries})",
                         True, "Dynadot braucht kurz bis der Domain-Datensatz settled ist")
            time.sleep(wait_s)
        r = _dyn_set_ns(api_key, domain, ns_list, secret=secret)
        last = r
        if r["ok"]:
            return r
        if not r.get("not_ready_yet"):
            # echter Fehler (nicht "warte noch") → abbrechen
            return r
    return last


# ── Cloudflare helpers ────────────────────────────────────

def _cf_auth(cfg: dict) -> dict:
    """Prefer Global-API-Key + Email if both are set, else fall back to
    Bearer token. Returns headers dict (or empty dict if nothing configured)."""
    gk = (cfg.get("cloudflare_global_api_key") or "").strip()
    email = (cfg.get("cloudflare_auth_email") or "").strip()
    if gk and email:
        return {
            "X-Auth-Key": gk,
            "X-Auth-Email": email,
            "Content-Type": "application/json",
        }
    token = (cfg.get("cloudflare_api_token") or "").strip()
    if token:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
    return {}


def _cf_get(cfg: dict, path: str, params: dict = None) -> dict:
    import requests
    headers = _cf_auth(cfg)
    if not headers:
        return {"success": False, "errors": [{"message": "Weder CF-Token noch Global-Key/Email gesetzt."}]}
    try:
        r = requests.get(f"{CF_BASE}{path}", headers=headers,
                         params=params or {}, timeout=20)
        return r.json()
    except Exception as e:
        return {"success": False, "errors": [{"message": str(e)}]}


def _cf_post(cfg: dict, path: str, body: dict) -> dict:
    import requests
    headers = _cf_auth(cfg)
    if not headers:
        return {"success": False, "errors": [{"message": "Kein CF-Auth."}]}
    try:
        r = requests.post(f"{CF_BASE}{path}", headers=headers,
                          json=body, timeout=20)
        return r.json()
    except Exception as e:
        return {"success": False, "errors": [{"message": str(e)}]}


def _cf_delete(cfg: dict, path: str) -> dict:
    import requests
    headers = _cf_auth(cfg)
    if not headers:
        return {"success": False, "errors": [{"message": "Kein CF-Auth."}]}
    try:
        r = requests.delete(f"{CF_BASE}{path}", headers=headers, timeout=20)
        return r.json()
    except Exception as e:
        return {"success": False, "errors": [{"message": str(e)}]}


def _cf_zones(cfg: dict) -> list:
    resp = _cf_get(cfg, "/zones", {"per_page": 50})
    if not resp.get("success"):
        return []
    return resp.get("result", [])


def _cf_records(cfg: dict, zone_id: str) -> list:
    resp = _cf_get(cfg, f"/zones/{zone_id}/dns_records", {"per_page": 100})
    if not resp.get("success"):
        return []
    return resp.get("result", [])


def _cf_add_zone(cfg: dict, name: str, account_id: str) -> dict:
    body = {"name": name, "type": "full"}
    if account_id:
        body["account"] = {"id": account_id}
    return _cf_post(cfg, "/zones", body)


def _cf_add_record(cfg: dict, zone_id: str, rtype: str, name: str,
                    content: str, proxied: bool = False, ttl: int = 1) -> dict:
    return _cf_post(cfg, f"/zones/{zone_id}/dns_records", {
        "type": rtype, "name": name, "content": content,
        "proxied": proxied, "ttl": int(ttl),
    })


def _cf_delete_record(cfg: dict, zone_id: str, record_id: str) -> dict:
    return _cf_delete(cfg, f"/zones/{zone_id}/dns_records/{record_id}")


def _cf_configured(cfg: dict) -> bool:
    return bool(_cf_auth(cfg))


def _cf_list_turnstile_widgets(cfg: dict) -> list:
    """Liste aller Turnstile-Widgets im CF-Account."""
    account_id = (cfg.get("cloudflare_account_id") or "").strip()
    if not account_id:
        return []
    resp = _cf_get(cfg, f"/accounts/{account_id}/challenges/widgets",
                   {"per_page": 50})
    if not resp.get("success"):
        return []
    return resp.get("result", []) or []


def _cf_get_turnstile_secret(cfg: dict, sitekey: str) -> str:
    """Holt/rotiert den Secret für ein existierendes Widget.
    Der LIST-Endpoint gibt Secrets nicht raus; der GET-single-Endpoint
    manchmal auch nicht. Sicherster Weg: rotate_secret — liefert immer
    einen Secret zurück. Nachteil: alte Secrets sind ab dann ungültig
    (aber wenn sie im Antibot nirgends gespeichert waren — was hier der
    Fall ist — egal)."""
    account_id = (cfg.get("cloudflare_account_id") or "").strip()
    if not account_id:
        return ""
    # Erst normalen GET versuchen (falls Secret zurückgegeben wird)
    resp = _cf_get(cfg, f"/accounts/{account_id}/challenges/widgets/{sitekey}")
    if resp.get("success"):
        secret = (resp.get("result", {}).get("secret") or "").strip()
        if secret and secret.startswith("0x"):
            return secret
    # Fallback: rotate_secret
    resp = _cf_post(cfg,
                    f"/accounts/{account_id}/challenges/widgets/{sitekey}/rotate_secret",
                    {"invalidate_immediately": False})
    if resp.get("success"):
        return (resp.get("result", {}).get("secret") or "").strip()
    return ""


def _cf_create_turnstile_widget(cfg: dict, hostname: str) -> dict:
    """Erstellt ein Turnstile-Widget via CF-API für einen Hostname.
    Braucht account_id. Gibt {ok, site_key, secret_key, msg} zurück."""
    account_id = (cfg.get("cloudflare_account_id") or "").strip()
    if not account_id:
        return {"ok": False, "msg": "Keine CF Account-ID gesetzt (nötig für Turnstile-API)."}
    # bot_fight_mode ist ein Paid-Feature — freie CF-Accounts kriegen dann
    # "not entitled to widgets with bot_fight_mode set to true". Weglassen.
    resp = _cf_post(cfg, f"/accounts/{account_id}/challenges/widgets", {
        "name": f"antibot-{hostname}",
        "domains": [hostname],
        "mode": "managed",
        "region": "world",
    })
    if not resp.get("success"):
        errs = "; ".join(e.get("message", "") for e in resp.get("errors", []))
        return {"ok": False, "msg": errs or "unknown"}
    r = resp.get("result", {})
    return {
        "ok": True,
        "site_key": r.get("sitekey", ""),
        "secret_key": r.get("secret", ""),
    }


# ── Panel routes ──────────────────────────────────────────

@router.get("/admin/domains", response_class=HTMLResponse)
async def domains_page(request: Request, tab: str = "dynadot"):
    db = request.app.state.db
    cfg = db.get_config()

    dyn_balance = ""
    dyn_domains = []
    if cfg.get("dynadot_api_key"):
        dyn_balance = _dyn_balance(cfg["dynadot_api_key"], cfg.get("dynadot_api_secret", ""))
        dyn_domains = _dyn_domains(cfg["dynadot_api_key"], cfg.get("dynadot_api_secret", ""))

    cf_zones = []
    if _cf_configured(cfg):
        cf_zones = _cf_zones(cfg)

    return request.app.state.templates.TemplateResponse(request, "admin_domains.html", {
        "cfg": cfg,
        "tab": tab if tab in ("dynadot", "cloudflare", "combined") else "dynadot",
        "dyn_balance": dyn_balance,
        "dyn_domains": dyn_domains,
        "cf_zones": cf_zones,
        "presets": MODE_PRESETS,
    })


@router.post("/admin/domains/dynadot/save")
async def dyn_save(request: Request, api_key: str = Form(""),
                    api_secret: str = Form(""),
                    buy_currency: str = Form("USD")):
    db = request.app.state.db
    cur = db.get_config()
    # Leeres Feld = "nicht ändern" (nicht überschreiben) — verhindert dass
    # Browser-Password-Manager mit geleertem Feld existierende Secrets killt.
    updates = {"buy_currency": buy_currency.strip() or "USD"}
    if api_key.strip() or not cur.get("dynadot_api_key"):
        updates["dynadot_api_key"] = api_key.strip()
    if api_secret.strip() or not cur.get("dynadot_api_secret"):
        updates["dynadot_api_secret"] = api_secret.strip()
    db.set_config(**updates)
    return RedirectResponse("/admin/domains?tab=dynadot&saved=1", status_code=303)


@router.post("/admin/domains/dynadot/search", response_class=HTMLResponse)
async def dyn_search_route(request: Request, domains: str = Form("")):
    db = request.app.state.db
    cfg = db.get_config()
    lines = [d.strip().lower() for d in domains.replace(",", "\n").splitlines()
              if d.strip()]
    lines = lines[:20]
    if not cfg.get("dynadot_api_key"):
        return HTMLResponse('<div class="alert alert-danger">Kein Dynadot-Key gesetzt.</div>')
    rows_html = []
    for d in lines:
        r = _dyn_search(cfg["dynadot_api_key"], d, cfg.get("buy_currency", "USD"),
                        secret=cfg.get("dynadot_api_secret", ""))
        badge = ('<span class="v-allow">verfügbar</span>' if r["available"]
                 else '<span class="v-block">belegt</span>')
        price = escape(str(r.get("price", "")))
        err = f'<span class="muted">{escape(r["error"])}</span>' if r.get("error") else ""
        buy_form = ""
        if r["available"]:
            buy_form = (f'<form method="post" action="/admin/domains/dynadot/buy" style="display:inline" '
                        f'onsubmit="return confirm(\'Wirklich {escape(d)} kaufen für {price}?\');">'
                        f'<input type="hidden" name="domain" value="{escape(d)}">'
                        f'<button class="btn btn-success btn-xs">Kaufen</button>'
                        f'</form>')
        rows_html.append(f'<tr><td>{escape(d)}</td><td>{badge}</td>'
                         f'<td>{price}</td><td>{buy_form} {err}</td></tr>')
    return HTMLResponse(
        '<table style="font-size:12px"><thead><tr><th>Domain</th><th>Status</th>'
        '<th>Preis</th><th></th></tr></thead><tbody>' + "".join(rows_html)
        + '</tbody></table>'
    )


@router.post("/admin/domains/dynadot/buy", response_class=HTMLResponse)
async def dyn_buy(request: Request, domain: str = Form("")):
    db = request.app.state.db
    cfg = db.get_config()
    if not cfg.get("dynadot_api_key"):
        return HTMLResponse('<span class="v-block">Kein Dynadot-Key</span>')
    domain = domain.strip().lower()
    if not domain:
        return HTMLResponse('<span class="v-block">Empty</span>')
    res = _dyn_register(cfg["dynadot_api_key"], domain,
                        currency=cfg.get("buy_currency", "USD"),
                        secret=cfg.get("dynadot_api_secret", ""))
    if res["ok"]:
        return HTMLResponse(f'<span class="v-allow">✓ {escape(domain)} gekauft.</span> '
                            f'<a href="/admin/domains?tab=combined&connect={escape(domain)}" '
                            f'style="font-size:12px;margin-left:6px">→ Jetzt mit Cloudflare verbinden</a>')
    return HTMLResponse(f'<span class="v-block">✗ {escape(res["msg"])}</span>')


# ── Cloudflare panel actions ─────────────────────────────

@router.post("/admin/domains/cf/save")
async def cf_save(request: Request, api_token: str = Form(""),
                   global_api_key: str = Form(""),
                   auth_email: str = Form(""),
                   account_id: str = Form("")):
    db = request.app.state.db
    cur = db.get_config()
    updates = {
        "cloudflare_auth_email": auth_email.strip(),
        "cloudflare_account_id": account_id.strip(),
    }
    # Secrets nur überschreiben wenn Feld befüllt (Browser-PW-Manager-safe)
    if api_token.strip() or not cur.get("cloudflare_api_token"):
        updates["cloudflare_api_token"] = api_token.strip()
    if global_api_key.strip() or not cur.get("cloudflare_global_api_key"):
        updates["cloudflare_global_api_key"] = global_api_key.strip()
    db.set_config(**updates)
    return RedirectResponse("/admin/domains?tab=cloudflare&saved=1", status_code=303)


@router.get("/admin/domains/cf/zone/{zone_id}", response_class=HTMLResponse)
async def cf_zone_view(request: Request, zone_id: str):
    db = request.app.state.db
    cfg = db.get_config()
    if not _cf_configured(cfg):
        return HTMLResponse('<div class="alert alert-danger">Weder CF-Token noch Global-Key/Email gesetzt.</div>')
    records = _cf_records(cfg, zone_id)
    zones = _cf_zones(cfg)
    zone = next((z for z in zones if z["id"] == zone_id), None)
    zone_name = zone["name"] if zone else zone_id
    rows_html = []
    for r in records:
        rows_html.append(
            f'<tr><td>{escape(r.get("type", ""))}</td>'
            f'<td style="font-family:monospace;font-size:12px">{escape(r.get("name", ""))}</td>'
            f'<td style="font-family:monospace;font-size:12px">{escape(str(r.get("content", "")))}</td>'
            f'<td>{"proxied" if r.get("proxied") else "DNS only"}</td>'
            f'<td><form method="post" action="/admin/domains/cf/zone/{zone_id}/record/{r["id"]}/delete" '
            f'style="display:inline" onsubmit="return confirm(\'Record löschen?\');">'
            f'<button class="btn btn-danger btn-xs">×</button></form></td></tr>')
    return HTMLResponse(
        f'<h3>{escape(zone_name)}</h3>'
        f'<form method="post" action="/admin/domains/cf/zone/{zone_id}/record/add" '
        f'style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px">'
        f'<select name="rtype"><option>A</option><option>AAAA</option>'
        f'<option>CNAME</option><option>TXT</option><option>MX</option></select>'
        f'<input name="name" placeholder="sub (oder @)" required style="width:120px">'
        f'<input name="content" placeholder="1.2.3.4 / target.example.com" required style="flex:1;min-width:200px">'
        f'<label style="display:flex;align-items:center;gap:4px">'
        f'<input type="checkbox" name="proxied" value="1"> proxied</label>'
        f'<button class="btn btn-primary btn-sm">Add</button></form>'
        f'<table><thead><tr><th>Type</th><th>Name</th><th>Content</th><th></th><th></th></tr></thead>'
        f'<tbody>{"".join(rows_html)}</tbody></table>'
    )


@router.post("/admin/domains/cf/zone/{zone_id}/record/add", response_class=HTMLResponse)
async def cf_record_add(request: Request, zone_id: str,
                         rtype: str = Form("A"), name: str = Form(""),
                         content: str = Form(""), proxied: str = Form("")):
    db = request.app.state.db
    cfg = db.get_config()
    resp = _cf_add_record(cfg, zone_id, rtype,
                           name.strip(), content.strip(), bool(proxied))
    if not resp.get("success"):
        errs = "; ".join(e.get("message", "") for e in resp.get("errors", []))
        return HTMLResponse(f'<div class="alert alert-danger">{escape(errs) or "Fehler"}</div>')
    return await cf_zone_view(request, zone_id)


@router.post("/admin/domains/cf/zone/{zone_id}/record/{rec_id}/delete", response_class=HTMLResponse)
async def cf_record_delete(request: Request, zone_id: str, rec_id: str):
    db = request.app.state.db
    cfg = db.get_config()
    _cf_delete(cfg, f"/zones/{zone_id}/dns_records/{rec_id}")
    return await cf_zone_view(request, zone_id)


@router.post("/admin/domains/cf/add-zone", response_class=HTMLResponse)
async def cf_add_zone_route(request: Request, name: str = Form("")):
    """Add an existing domain to CF (as a full zone)."""
    db = request.app.state.db
    cfg = db.get_config()
    name = name.strip().lower()
    if not name or "." not in name:
        return HTMLResponse('<div class="alert alert-warning">Gültige Domain angeben.</div>')
    resp = _cf_add_zone(cfg, name, cfg.get("cloudflare_account_id", ""))
    if not resp.get("success"):
        errs = "; ".join(e.get("message", "") for e in resp.get("errors", []))
        return HTMLResponse(f'<div class="alert alert-danger">{escape(errs) or "Fehler"}</div>')
    zone = resp.get("result", {})
    ns_list = zone.get("name_servers", [])
    ns_html = "<br>".join(f"<code>{escape(ns)}</code>" for ns in ns_list)
    return HTMLResponse(
        f'<div class="alert alert-success">Zone <code>{escape(name)}</code> angelegt. '
        f'Bei <strong>Dynadot</strong> die Nameserver auf folgende umstellen:<br>{ns_html}</div>')


# ── Combined: buy + auto-add CF zone + A-record ──────────

@router.get("/admin/domains/combined", response_class=HTMLResponse)
async def combined_view(request: Request):
    return RedirectResponse("/admin/domains?tab=combined", status_code=303)


@router.post("/admin/domains/combined/connect", response_class=HTMLResponse)
async def combined_connect(request: Request,
                            domain: str = Form(""),
                            ip: str = Form(""),
                            add_www: str = Form("1")):
    """Take an already-registered (or existing) domain, add it as CF zone,
    then create an A-record @ → ip (and optionally www)."""
    db = request.app.state.db
    cfg = db.get_config()
    if not _cf_configured(cfg):
        return HTMLResponse('<div class="alert alert-danger">Weder CF-Token noch Global-Key/Email gesetzt.</div>')
    domain = domain.strip().lower()
    ip = ip.strip()
    if not domain or not ip:
        return HTMLResponse('<div class="alert alert-warning">Domain und IP nötig.</div>')

    # Ist die Zone schon in CF? Sonst neu anlegen.
    zones = _cf_zones(cfg)
    zone = next((z for z in zones if z["name"] == domain), None)
    ns_hint = ""
    if not zone:
        resp = _cf_add_zone(cfg, domain, cfg.get("cloudflare_account_id", ""))
        if not resp.get("success"):
            errs = "; ".join(e.get("message", "") for e in resp.get("errors", []))
            return HTMLResponse(f'<div class="alert alert-danger">Zone add failed: {escape(errs)}</div>')
        zone = resp.get("result", {})
        ns_list = zone.get("name_servers", [])
        ns_hint = ('<div class="alert alert-warn">Neue CF-Zone angelegt. Bei Dynadot die '
                   'Nameserver umstellen auf:<br>'
                   + "<br>".join(f"<code>{escape(ns)}</code>" for ns in ns_list) + '</div>')

    zid = zone["id"]

    logs = []
    r1 = _cf_add_record(cfg, zid, "A", domain, ip, proxied=False)
    logs.append(("@ → " + ip, r1.get("success"), r1))
    if add_www:
        r2 = _cf_add_record(cfg, zid, "A", f"www.{domain}", ip, proxied=False)
        logs.append(("www → " + ip, r2.get("success"), r2))

    log_html = "".join(
        f'<li>{escape(label)}: '
        + ('<span class="v-allow">OK</span>' if ok else
           f'<span class="v-block">Fehler: {escape("; ".join(e.get("message","") for e in raw.get("errors", [])))}</span>')
        + '</li>'
        for label, ok, raw in logs
    )
    return HTMLResponse(
        f'{ns_hint}'
        f'<div class="alert alert-success">Verbunden: <code>{escape(domain)}</code> → <code>{escape(ip)}</code></div>'
        f'<ul style="font-size:13px">{log_html}</ul>'
        f'<p class="muted">DNS-Propagation kann bis 5 Min dauern. Danach kann Caddy auf dem Ziel-Server das Cert holen.</p>'
    )


# ── Pipeline: Suchen → Kaufen → CF → Turnstile → Gate → Links ────────

def _pipeline_one_domain(db, cfg: dict, domain: str, pcfg: dict) -> dict:
    """Führt die komplette Ende-zu-Ende-Pipeline für EINE Domain aus.
    Returns {domain, ok, steps=[(label, ok, detail), ...], gate_id, ns_hint}."""
    steps = []
    ns_hint = ""

    # 1. Dynadot Kauf (nur wenn buy_dynadot=True und Key vorhanden)
    if pcfg.get("buy_dynadot") and cfg.get("dynadot_api_key"):
        res = _dyn_register(cfg["dynadot_api_key"], domain,
                             currency=cfg.get("buy_currency", "USD"),
                             secret=cfg.get("dynadot_api_secret", ""))
        steps.append(("Dynadot: Kauf", res["ok"], res["msg"]))
        if not res["ok"]:
            return {"domain": domain, "ok": False, "steps": steps,
                    "gate_id": None, "ns_hint": ""}
    elif pcfg.get("buy_dynadot"):
        steps.append(("Dynadot: Kauf", False, "Kein Dynadot-Key gesetzt"))
        return {"domain": domain, "ok": False, "steps": steps,
                "gate_id": None, "ns_hint": ""}

    # 2. Server-IP + CF-Auth check
    server_ip = cfg.get("server_public_ip", "") or detect_public_ip()
    if server_ip and not cfg.get("server_public_ip"):
        db.set_config(server_public_ip=server_ip)
    if not _cf_configured(cfg):
        steps.append(("CF: Setup", False, "Kein CF-Auth (Token oder Global-Key+Email)"))
        return {"domain": domain, "ok": False, "steps": steps,
                "gate_id": None, "ns_hint": ""}
    if not server_ip:
        steps.append(("Server-IP", False, "eigene IP konnte nicht ermittelt werden"))
        return {"domain": domain, "ok": False, "steps": steps,
                "gate_id": None, "ns_hint": ""}

    # 3. CF-Zone anlegen falls nicht schon da
    zones = _cf_zones(cfg)
    zone = next((z for z in zones if z["name"] == domain), None)
    is_new_zone = False
    if not zone:
        zr = _cf_add_zone(cfg, domain, cfg.get("cloudflare_account_id", ""))
        if not zr.get("success"):
            errs = "; ".join(e.get("message", "") for e in zr.get("errors", []))
            steps.append(("CF: Zone anlegen", False, errs))
            return {"domain": domain, "ok": False, "steps": steps,
                    "gate_id": None, "ns_hint": ""}
        zone = zr.get("result", {})
        is_new_zone = True
        steps.append(("CF: Zone angelegt", True, f"ID {zone.get('id', '?')[:12]}…"))
    else:
        steps.append(("CF: Zone existiert", True, ""))

    zid = zone["id"]

    # 3a. Nameserver automatisch bei Dynadot setzen (statt "User macht's manuell")
    # Nur wenn wir Dynadot-Key haben UND die Zone gerade neu angelegt wurde
    # (bei existierenden Zonen davon ausgehen dass NS schon stehen).
    ns_list = zone.get("name_servers", [])
    if ns_list:
        ns_hint = ", ".join(ns_list)
    if is_new_zone and cfg.get("dynadot_api_key") and ns_list:
        def _log_ns_step(label, ok, detail):
            steps.append((label, ok, detail))
        ns_res = _dyn_set_ns_with_retry(
            cfg["dynadot_api_key"], domain, ns_list,
            secret=cfg.get("dynadot_api_secret", ""),
            log_step=_log_ns_step,
        )
        if ns_res["ok"]:
            steps.append((f"Dynadot: NS gesetzt → {ns_list[0]}, {ns_list[1]}",
                           True, ""))
            ns_hint = ""   # brauchen wir dem User nicht mehr anzeigen
        else:
            steps.append(("Dynadot: NS setzen fehlgeschlagen", False,
                           ns_res["msg"] + " — bitte manuell im Dynadot-Panel setzen"))
            # NICHT abort — Rest der Pipeline (A-Records, Gate) läuft trotzdem

    # 4. A-Records — IMMER grau (DNS only) initial damit LE-Cert geholt
    #    werden kann. User kann später via Gate-Panel auf orange schalten.
    a1 = _cf_add_record(cfg, zid, "A", domain, server_ip, proxied=False)
    steps.append((f"CF: A @ → {server_ip}", a1.get("success"),
                   "" if a1.get("success") else str(a1.get("errors"))))
    if pcfg.get("add_www"):
        a2 = _cf_add_record(cfg, zid, "A", f"www.{domain}", server_ip, proxied=False)
        steps.append((f"CF: A www → {server_ip}", a2.get("success"),
                       "" if a2.get("success") else str(a2.get("errors"))))

    # 5. Turnstile Widget (optional)
    ts_site, ts_secret = "", ""
    if pcfg.get("auto_turnstile"):
        ts = _cf_create_turnstile_widget(cfg, domain)
        steps.append(("Turnstile Widget", ts["ok"], ts.get("msg", "")))
        if ts["ok"]:
            ts_site = ts["site_key"]
            ts_secret = ts["secret_key"]

    # 6. Gate anlegen (oder skippen wenn schon da).
    # Brand-Overrides pro Pipeline-Run erlaubt (Logo/Farbe/Text/Ziel).
    # Leer → Fallback auf globale Config.
    existing = db.get_gate_by_host(domain)
    brand_color = (pcfg.get("brand_color") or "").strip() \
        or cfg.get("brand_color", "#005eb8")
    brand_text = (pcfg.get("brand_text") or "").strip() \
        or cfg.get("brand_text", "Sicherheitsprüfung läuft …")
    logo_path = (pcfg.get("logo_path") or "").strip() \
        or cfg.get("brand_logo_path", "")
    if existing:
        gate_id = existing["id"]
        # Wenn Brand-Overrides gesetzt sind, auch das bestehende Gate updaten
        updates = {}
        if pcfg.get("brand_color"):
            updates["brand_color"] = brand_color
        if pcfg.get("brand_text"):
            updates["brand_text"] = brand_text
        if pcfg.get("logo_path"):
            updates["logo_path"] = logo_path
        if pcfg.get("target_url"):
            updates["target_url"] = pcfg["target_url"]
        if updates:
            db.update_gate(gate_id, **updates)
            steps.append(("Gate existiert — Brand/Ziel aktualisiert", True,
                           ", ".join(updates.keys())))
        else:
            steps.append(("Gate existiert bereits", True, f"ID {gate_id} — nicht überschrieben"))
    else:
        gate_id = db.add_gate(
            hostname=domain,
            mode=pcfg.get("mode", "medium"),
            target_url=pcfg.get("target_url", ""),
            brand_text=brand_text,
            brand_color=brand_color,
            logo_path=logo_path,
            turnstile_site_key=ts_site,
            turnstile_secret_key=ts_secret,
        )
        steps.append(("Gate angelegt", True, f"ID {gate_id}, Modus {pcfg.get('mode','medium')}"))

    # 7. Ready-Links (sammle Slugs — pipeline_run macht daraus die
    #    kopierbare URL-Liste am Ende).
    n = max(0, min(int(pcfg.get("initial_links") or 0), 500))
    generated_slugs = []
    for _ in range(n):
        for _try in range(5):
            slug = gen_slug(8)
            if not db.get_gate_link(gate_id, slug):
                db.add_gate_link(gate_id, slug)
                generated_slugs.append(slug)
                break
    if n:
        steps.append((f"{len(generated_slugs)} Ready-Links generiert", True, ""))

    return {"domain": domain, "ok": True, "steps": steps,
            "gate_id": gate_id, "ns_hint": ns_hint,
            "generated_slugs": generated_slugs}


@router.post("/admin/domains/pipeline/precheck", response_class=HTMLResponse)
async def pipeline_precheck(request: Request):
    """Testet alle nötigen APIs read-only bevor der User Domains kauft.
    Rückgabe ist eine Ampel-Tabelle: DNS, Dynadot, Cloudflare, Turnstile-Fähigkeit."""
    db = request.app.state.db
    cfg = db.get_config()
    results = []

    # 1. Eigene Public-IP
    ip = cfg.get("server_public_ip", "") or detect_public_ip()
    if ip and not cfg.get("server_public_ip"):
        db.set_config(server_public_ip=ip)
    results.append(("Server-Public-IP", bool(ip), ip or "konnte nicht ermittelt werden"))

    # 2. Dynadot: account_info (safe, read-only, gibt Balance zurück)
    dyn_key = cfg.get("dynadot_api_key", "")
    if not dyn_key:
        results.append(("Dynadot API", False, "Kein API-Key gesetzt (Tab Dynadot)"))
    else:
        resp = _dyn_call(dyn_key, "account_info", secret=cfg.get("dynadot_api_secret", ""))
        if "error" in resp:
            results.append(("Dynadot API", False, resp["error"]))
        else:
            try:
                bal = resp.get("AccountInfoResponse", {}).get("Account", {}).get("AccountBalance", "?")
                results.append(("Dynadot API", True, f"Kontostand: {bal}"))
            except Exception as e:
                results.append(("Dynadot API", False,
                                 f"Response gelesen, aber unerwartetes Format: {str(resp)[:150]}"))

    # 3. Cloudflare: zones list (safe, read-only)
    if not _cf_configured(cfg):
        results.append(("Cloudflare API", False, "Weder Bearer-Token noch Global-Key+Email gesetzt"))
    else:
        auth_kind = ("Global-Key + Email"
                     if cfg.get("cloudflare_global_api_key") and cfg.get("cloudflare_auth_email")
                     else "Bearer-Token")
        resp = _cf_get(cfg, "/zones", {"per_page": 5})
        if not resp.get("success"):
            errs = "; ".join(e.get("message", "") for e in resp.get("errors", []))
            results.append(("Cloudflare API", False, f"({auth_kind}) {errs}"))
        else:
            n = len(resp.get("result", []))
            results.append(("Cloudflare API", True,
                             f"({auth_kind}) {n}+ Zone(n) sichtbar"))

    # 4. Cloudflare: verify token permissions (nur wenn Token benutzt wird)
    #    Für Zone-Anlegen brauchen wir explizit account-level Berechtigungen
    #    → schauen wir mit Account-ID + list-accounts endpoint nach
    if _cf_configured(cfg) and cfg.get("cloudflare_account_id"):
        acc_id = cfg["cloudflare_account_id"]
        resp = _cf_get(cfg, f"/accounts/{acc_id}")
        if resp.get("success"):
            acc_name = resp.get("result", {}).get("name", "")
            results.append(("CF Account-ID", True, f"OK ({acc_name})"))
        else:
            errs = "; ".join(e.get("message", "") for e in resp.get("errors", []))
            results.append(("CF Account-ID", False, errs or "unbekannter Fehler"))
    elif _cf_configured(cfg):
        results.append(("CF Account-ID", False,
                         "nicht gesetzt — Turnstile + Zone-Create werden nicht funktionieren"))

    # 5. Turnstile-Widgets list (nur wenn Account-ID + Auth vorhanden)
    if _cf_configured(cfg) and cfg.get("cloudflare_account_id"):
        acc_id = cfg["cloudflare_account_id"]
        resp = _cf_get(cfg, f"/accounts/{acc_id}/challenges/widgets", {"per_page": 5})
        if resp.get("success"):
            n = len(resp.get("result", []))
            results.append(("Turnstile-Fähigkeit", True, f"OK ({n} vorhandene Widget(s))"))
        else:
            errs = "; ".join(e.get("message", "") for e in resp.get("errors", []))
            results.append(("Turnstile-Fähigkeit", False, errs))

    # Render
    rows_html = []
    for label, ok, detail in results:
        icon = ("✓" if ok else "✗")
        color = "var(--green)" if ok else "var(--red)"
        rows_html.append(
            f'<tr><td style="color:{color};font-weight:600">{icon} {escape(label)}</td>'
            f'<td style="font-family:monospace;font-size:11px">{escape(detail)}</td></tr>'
        )
    all_ok = all(ok for _, ok, _ in results)
    header = ('<div class="alert alert-success">Alle Checks grün — Pipeline safe zu starten.</div>'
              if all_ok else
              '<div class="alert alert-warn">Einige Checks failen — Pipeline würde fehlschlagen. Erst die roten Punkte fixen.</div>')
    return HTMLResponse(
        header +
        '<table style="font-size:12px"><tbody>' + "".join(rows_html) + '</tbody></table>'
    )


@router.post("/admin/domains/pipeline/search", response_class=HTMLResponse)
async def pipeline_search(request: Request, domains: str = Form("")):
    """Verfügbarkeits-Check aller eingegebenen Domains via Dynadot."""
    db = request.app.state.db
    cfg = db.get_config()
    if not cfg.get("dynadot_api_key"):
        return HTMLResponse('<div class="alert alert-danger">Kein Dynadot-Key gesetzt (Tab „Dynadot").</div>')
    lines = [d.strip().lower() for d in domains.replace(",", "\n").splitlines() if d.strip()]
    lines = list(dict.fromkeys(lines))[:20]  # dedupe + cap
    if not lines:
        return HTMLResponse('<div class="alert alert-warning">Keine gültigen Domains.</div>')

    rows_html = []
    n_available = 0
    for d in lines:
        r = _dyn_search(cfg["dynadot_api_key"], d, cfg.get("buy_currency", "USD"),
                        secret=cfg.get("dynadot_api_secret", ""))
        if r["available"]:
            n_available += 1
            badge = '<span class="v-allow">verfügbar</span>'
            checkbox = f'<input type="checkbox" name="pipeline_domain" value="{escape(d)}" checked class="pipe-domain-cb">'
        else:
            badge = '<span class="v-block">belegt</span>'
            checkbox = '—'
        err = f'<span class="muted" style="font-size:11px">{escape(r["error"])}</span>' if r.get("error") else ""
        rows_html.append(
            f'<tr><td>{checkbox}</td>'
            f'<td style="font-family:monospace">{escape(d)}</td>'
            f'<td>{badge}</td><td>{escape(str(r.get("price", "")))}</td>'
            f'<td>{err}</td></tr>'
        )
    check_all_btn = ('<div style="margin-bottom:8px"><button type="button" class="btn btn-secondary btn-xs" '
                     'onclick="document.querySelectorAll(\'.pipe-domain-cb\').forEach(cb=>cb.checked=true)">'
                     'Alle Verfügbaren auswählen</button> '
                     '<button type="button" class="btn btn-secondary btn-xs" '
                     'onclick="document.querySelectorAll(\'.pipe-domain-cb\').forEach(cb=>cb.checked=false)">'
                     'Auswahl leeren</button></div>')
    return HTMLResponse(
        f'<div class="alert alert-info">{n_available}/{len(lines)} verfügbar — pick + „Pipeline starten" unten.</div>'
        + check_all_btn
        + '<table style="font-size:12px"><thead>'
        '<tr><th></th><th>Domain</th><th>Status</th><th>Preis</th><th></th></tr>'
        '</thead><tbody>' + "".join(rows_html) + '</tbody></table>'
    )


@router.post("/admin/domains/pipeline/run", response_class=HTMLResponse)
async def pipeline_run(request: Request):
    """Läuft die Pipeline für alle ausgewählten Domains synchron.
    Rückgabe ist eine Report-Tabelle mit allen Schritten pro Domain."""
    db = request.app.state.db
    cfg = db.get_config()
    form = await request.form()
    selected = form.getlist("pipeline_domain") or []
    selected = [d.strip().lower() for d in selected if d.strip()]
    if not selected:
        return HTMLResponse('<div class="alert alert-warning">Keine Domains ausgewählt. '
                            'Zuerst „Suchen" → dann Checkboxen setzen.</div>')

    # Logo-Upload (multipart file) — wenn eine Datei mitkommt, unter
    # /static/logo/uploads/ speichern und Pfad als logo_path benutzen.
    uploaded_logo_path = ""
    logo_upload = form.get("logo_file")
    if logo_upload and hasattr(logo_upload, "filename") and logo_upload.filename:
        ext = os.path.splitext(logo_upload.filename)[1].lower()
        if ext not in _LOGO_EXTS:
            return HTMLResponse(
                f'<div class="alert alert-danger">Logo hat unerlaubte Endung '
                f'({escape(ext)}). Erlaubt: {", ".join(sorted(_LOGO_EXTS))}.</div>')
        raw = await logo_upload.read()
        if not raw:
            pass  # user hat den file input leer gesubmitted — ignoriere
        elif len(raw) > _LOGO_MAX_BYTES:
            return HTMLResponse(
                f'<div class="alert alert-danger">Logo zu groß '
                f'({len(raw)//1024} KB, max 2048 KB).</div>')
        else:
            safe = f"gate_{int(time.time())}_{secrets.token_hex(4)}{ext}"
            dest = os.path.join(LOGO_UPLOAD_DIR, safe)
            with open(dest, "wb") as fh:
                fh.write(raw)
            uploaded_logo_path = f"/static/logo/uploads/{safe}"

    pcfg = {
        "buy_dynadot": bool(form.get("buy_dynadot")),
        "add_www": bool(form.get("add_www")),
        "auto_turnstile": bool(form.get("auto_turnstile")),
        "target_url": (form.get("target_url") or "").strip(),
        "mode": form.get("mode") or "medium",
        "initial_links": int(form.get("initial_links") or 10),
        # Brand-Overrides — für alle Domains dieses Runs identisch.
        # Leer = Fallback auf globale Config im _pipeline_one_domain.
        "brand_color": (form.get("brand_color") or "").strip(),
        "brand_text": (form.get("brand_text") or "").strip(),
        "logo_path": uploaded_logo_path,
    }
    if pcfg["mode"] not in MODE_PRESETS:
        pcfg["mode"] = "medium"

    results = []
    for d in selected[:20]:  # safety cap
        res = _pipeline_one_domain(db, cfg, d, pcfg)
        results.append(res)

    # Report — pro Domain
    sections = []
    all_ns_hints = set()
    all_ready_links = []   # (domain, slug) für die Sammel-Ausgabe unten
    all_gate_ids = []
    for r in results:
        color = "var(--green)" if r["ok"] else "var(--red)"
        head = (f'<h3 style="color:{color};margin:12px 0 6px">'
                f'{"✓" if r["ok"] else "✗"} {escape(r["domain"])}</h3>')
        steps_html = "".join(
            f'<li>{"✓" if ok else "✗"} <strong>{escape(label)}</strong>'
            f'{": " + escape(detail) if detail else ""}</li>'
            for label, ok, detail in r["steps"]
        )
        gate_link = ""
        if r.get("gate_id"):
            gate_link = (f'<p><a href="/admin/gates/{r["gate_id"]}" '
                         f'class="btn btn-primary btn-xs">Gate + Ready-Links öffnen</a></p>')
            all_gate_ids.append(r["gate_id"])
        sections.append(f'{head}<ul style="font-size:13px">{steps_html}</ul>{gate_link}')
        if r.get("ns_hint"):
            all_ns_hints.add(r["ns_hint"])
        for slug in r.get("generated_slugs", []):
            all_ready_links.append((r["domain"], slug))

    ns_alert = ""
    if all_ns_hints:
        ns_alert = ('<div class="alert alert-warn">Neue CF-Zonen angelegt — bei Dynadot '
                    'die Nameserver umstellen auf:<br>'
                    + "<br>".join(f"<code>{escape(ns)}</code>" for ns in all_ns_hints)
                    + '</div>')

    ok_count = sum(1 for r in results if r["ok"])
    summary = (f'<div class="alert alert-{"success" if ok_count == len(results) else "warn"}">'
               f'{ok_count}/{len(results)} erfolgreich durchgelaufen.</div>')

    # ── Sammel-Ausgabe: alle Ready-Links über ALLE Domains als
    #    kopierbare Textarea. So spart sich der User das Öffnen jedes
    #    einzelnen Gates.
    ready_block = ""
    if all_ready_links:
        # Klartext-URLs — der Mailer hängt `?ref=…` selber per _append_ref
        # dran, deshalb hier ohne. Gate-Redirect forward `?ref` an Ziel
        # (siehe /admin/domains/ref-forward-info).
        lines = [f"https://{host}/go/{slug}"
                 for host, slug in all_ready_links]
        ready_block = (
            f'<div class="card" style="margin-top:12px;background:#f0fff4">'
            f'<h3>&#128279; {len(lines)} Ready-Links über {len(set(h for h,_ in all_ready_links))} Domain(s)</h3>'
            f'<p class="muted" style="font-size:12px;margin:4px 0">'
            f'Ein Klick ins Textfeld markiert alles. Der Mailer hängt beim '
            f'Versand automatisch <code>?ref=…</code> an (Config in der Kampagne). '
            f'Das Antibot-Gate leitet den ref-Parameter transparent an die '
            f'Ziel-URL weiter.</p>'
            f'<textarea readonly rows="{min(len(lines)+1, 15)}" '
            f'onclick="this.select()" style="font-family:monospace;font-size:11px;'
            f'width:100%">' + escape("\n".join(lines)) + f'</textarea>'
            f'</div>'
        )

    # ── Bulk „CF-Wolke auf orange" Button — für alle Gates die dieser
    #    Pipeline-Run gerade angelegt oder aktualisiert hat. Erst wenn
    #    das erste HTTPS-Cert geholt wurde sinnvoll (siehe Hinweis unten).
    bulk_orange = ""
    if all_gate_ids:
        ids_csv = ",".join(str(g) for g in all_gate_ids)
        bulk_orange = (
            f'<div class="card" style="margin-top:12px">'
            f'<h3>&#127774; CF-Wolke Bulk-Toggle</h3>'
            f'<p class="muted" style="font-size:12px;margin:4px 0">'
            f'Wenn dein Caddy auf all diesen Domains das LE-Cert geholt hat '
            f'(dauert ~1-5 Min ab dem ersten HTTPS-Request), einmal klicken um '
            f'alle A-Records auf CF-orange zu setzen.</p>'
            f'<button class="btn btn-secondary btn-sm" '
            f'hx-post="/admin/domains/pipeline/bulk-orange" '
            f'hx-vals=\'{{"gate_ids":"{ids_csv}","proxied":"1"}}\' '
            f'hx-target="#bulk-orange-result" hx-swap="innerHTML" '
            f'hx-confirm="Alle {len(all_gate_ids)} Gates auf CF-orange setzen?">'
            f'Alle {len(all_gate_ids)} auf orange</button>'
            f'<button class="btn btn-secondary btn-sm" style="margin-left:6px" '
            f'hx-post="/admin/domains/pipeline/bulk-orange" '
            f'hx-vals=\'{{"gate_ids":"{ids_csv}","proxied":"0"}}\' '
            f'hx-target="#bulk-orange-result" hx-swap="innerHTML">'
            f'Alle wieder auf grau</button>'
            f'<div id="bulk-orange-result" style="margin-top:8px;font-size:12px"></div>'
            f'</div>'
        )

    return HTMLResponse(summary + ns_alert + "".join(sections) + ready_block + bulk_orange
                        + '<p class="muted">DNS-Propagation dauert bis ~5 Min. Danach holt Caddy '
                          'beim ersten HTTPS-Request automatisch das LE-Cert. '
                          'Wenn du CF-Wolke orange willst: <strong>erst nach dem ersten '
                          'Cert-Holen</strong> — der Bulk-Button oben macht das für alle Gates dieses Runs auf einmal.</p>')


@router.post("/admin/gates/bulk-cf-proxy", response_class=HTMLResponse)
async def all_gates_bulk_cf(request: Request, proxied: str = Form("1")):
    """Bulk-Toggle über ALLE Gates im System — Aufruf vom
    Gates-Übersicht-Panel (admin_gates.html). Delegiert an denselben
    Backend-Code wie pipeline_bulk_orange nur mit gate_ids="alle"."""
    db = request.app.state.db
    all_gates = db.list_gates() if hasattr(db, "list_gates") else []
    if not all_gates:
        # Fallback: direkt aus der Tabelle lesen
        rows = db._conn().execute("SELECT id FROM gates ORDER BY id").fetchall()
        gate_ids = ",".join(str(r["id"] if hasattr(r, "keys") else r[0]) for r in rows)
    else:
        gate_ids = ",".join(str(dict(g)["id"]) for g in all_gates)
    if not gate_ids:
        return HTMLResponse('<span style="color:var(--fg2)">Keine Gates vorhanden.</span>')
    return await pipeline_bulk_orange(request, gate_ids=gate_ids, proxied=proxied)


@router.post("/admin/domains/pipeline/bulk-orange", response_class=HTMLResponse)
async def pipeline_bulk_orange(request: Request,
                                 gate_ids: str = Form(""),
                                 proxied: str = Form("1")):
    """Bulk: setzt CF-Proxy=orange (oder grau) für alle angegebenen Gates.
    Läuft synchron — bei 20 Domains à ~2s = ~40s. Report als Liste."""
    db = request.app.state.db
    cfg = db.get_config()
    if not _cf_configured(cfg):
        return HTMLResponse('<span style="color:var(--red)">CF nicht konfiguriert.</span>')
    want = proxied == "1"
    ids = [int(x) for x in gate_ids.split(",") if x.strip().isdigit()]
    if not ids:
        return HTMLResponse('<span style="color:var(--red)">Keine Gate-IDs.</span>')
    zones = _cf_zones(cfg)
    zone_by_name = {z["name"]: z for z in zones}
    import requests as _req
    headers = _cf_auth(cfg)
    lines = []
    ok = 0
    for gid in ids:
        gate = db.get_gate(gid)
        if not gate:
            lines.append(f'<div style="color:var(--red)">Gate #{gid}: weg</div>')
            continue
        host = gate["hostname"]
        z = zone_by_name.get(host)
        if not z:
            lines.append(f'<div style="color:var(--red)">{escape(host)}: keine CF-Zone</div>')
            continue
        records = _cf_records(cfg, z["id"])
        patched = 0
        for r in records:
            if r.get("type") not in ("A", "AAAA", "CNAME"):
                continue
            if r.get("proxied") == want:
                continue
            resp = _req.patch(f"{CF_BASE}/zones/{z['id']}/dns_records/{r['id']}",
                              headers=headers, json={"proxied": want}, timeout=15)
            if resp.status_code == 200 and resp.json().get("success"):
                patched += 1
        color = "var(--green)" if patched > 0 else "var(--fg2)"
        state = "orange" if want else "grau"
        lines.append(f'<div style="color:{color}">✓ {escape(host)}: {patched} Record(s) → {state}</div>')
        if patched > 0:
            ok += 1
    return HTMLResponse(
        f'<div style="margin-bottom:6px"><strong>{ok}/{len(ids)}</strong> '
        f'Gates umgeschaltet.</div>' + "".join(lines))


# ── Gate: CF-Wolke orange/grau umschalten ────────────────

@router.get("/admin/gates/{gate_id}/turnstile-manage", response_class=HTMLResponse)
async def gate_turnstile_manage(request: Request, gate_id: int):
    """Zeigt alle Turnstile-Widgets aus dem CF-Account, matched welche zur
    Gate-Domain passen. User picked eins → Keys werden geholt und ins Gate
    übernommen. Alternative: neues Widget für die Domain erstellen."""
    db = request.app.state.db
    cfg = db.get_config()
    gate = db.get_gate(gate_id)
    if not gate:
        return HTMLResponse('<div class="alert alert-danger">Gate weg</div>')
    if not _cf_configured(cfg):
        return HTMLResponse('<div class="alert alert-warn">Kein CF-Auth im Panel — Turnstile-Management deaktiviert.</div>')
    if not cfg.get("cloudflare_account_id"):
        return HTMLResponse('<div class="alert alert-warn">CF Account-ID fehlt — brauch ich für die Turnstile-API.</div>')

    hostname = gate["hostname"]
    widgets = _cf_list_turnstile_widgets(cfg)
    matching = [w for w in widgets if hostname in (w.get("domains") or [])]
    others = [w for w in widgets if w not in matching]

    def _row(w, highlight):
        sitekey = w.get("sitekey", "")
        name = w.get("name", "?")
        domains = ", ".join(w.get("domains", []))
        bg = "background:#e7f7ea" if highlight else ""
        return (f'<tr style="{bg}">'
                 f'<td style="font-family:monospace;font-size:11px">{escape(sitekey[:16])}…{escape(sitekey[-4:] if len(sitekey) > 20 else "")}</td>'
                 f'<td>{escape(name)}</td>'
                 f'<td style="font-size:11px">{escape(domains)}</td>'
                 f'<td><button class="btn btn-success btn-xs" '
                 f'hx-post="/admin/gates/{gate_id}/turnstile-apply" '
                 f'hx-vals=\'{{"sitekey":"{escape(sitekey)}"}}\' '
                 f'hx-target="#ts-manage-result" hx-swap="innerHTML" '
                 f'hx-confirm="Widget {escape(name)} übernehmen? (Secret wird rotiert falls nicht anders auslesbar)">'
                 f'Übernehmen</button></td></tr>')

    rows = []
    if matching:
        rows.append(f'<tr><td colspan="4" style="background:#e7f7ea;font-size:11px;padding:4px 8px;font-weight:600">Widgets für <code>{escape(hostname)}</code>:</td></tr>')
        rows.extend(_row(w, True) for w in matching)
    if others:
        rows.append(f'<tr><td colspan="4" style="background:#f0f2f5;font-size:11px;padding:4px 8px;color:var(--fg2)">Andere Widgets ({len(others)}):</td></tr>')
        rows.extend(_row(w, False) for w in others[:20])

    create_btn = (f'<button class="btn btn-primary btn-sm" style="margin-top:12px" '
                   f'hx-post="/admin/gates/{gate_id}/turnstile-create" '
                   f'hx-target="#ts-manage-result" hx-swap="innerHTML" '
                   f'hx-confirm="Neues Turnstile-Widget für {escape(hostname)} erstellen?">'
                   f'Neues Widget für <code>{escape(hostname)}</code> erstellen</button>')

    tbl = ""
    if rows:
        tbl = ('<table style="font-size:12px;margin-top:8px"><thead>'
               '<tr><th>Site-Key</th><th>Name</th><th>Domains</th><th></th></tr>'
               '</thead><tbody>' + "".join(rows) + '</tbody></table>')
    else:
        tbl = '<p class="muted">Noch keine Widgets im CF-Account.</p>'

    return HTMLResponse(
        f'<h4 style="margin:0 0 8px">CF-Turnstile-Widgets ({len(widgets)})</h4>'
        f'<p class="muted" style="font-size:12px;margin:0 0 6px">'
        f'Grün markiert: Widget das explizit für <code>{escape(hostname)}</code> konfiguriert ist.</p>'
        f'{tbl}{create_btn}'
        f'<div id="ts-manage-result" style="margin-top:10px"></div>'
    )


@router.post("/admin/gates/{gate_id}/turnstile-apply", response_class=HTMLResponse)
async def gate_turnstile_apply(request: Request, gate_id: int,
                                 sitekey: str = Form("")):
    db = request.app.state.db
    cfg = db.get_config()
    gate = db.get_gate(gate_id)
    if not gate:
        return HTMLResponse('<span style="color:var(--red)">Gate weg</span>')
    sitekey = sitekey.strip()
    if not sitekey or not sitekey.startswith("0x"):
        return HTMLResponse('<span style="color:var(--red)">Kein gültiger Site-Key</span>')
    secret = _cf_get_turnstile_secret(cfg, sitekey)
    if not secret:
        return HTMLResponse(
            '<span style="color:var(--red)">Secret konnte nicht geholt werden. '
            'Probiere manuell im CF-Dashboard → Widget → Rotate Secret.</span>'
        )
    db.update_gate(gate_id, turnstile_site_key=sitekey,
                    turnstile_secret_key=secret)
    # HX-Redirect: Seite neu laden damit die Felder die frischen Werte zeigen —
    # sonst überschreibt der nächste "Speichern"-Klick sie mit leer.
    resp = HTMLResponse('')
    resp.headers["HX-Redirect"] = f"/admin/gates/{gate_id}?ts_updated=1"
    return resp


@router.post("/admin/gates/{gate_id}/turnstile-create", response_class=HTMLResponse)
async def gate_turnstile_create(request: Request, gate_id: int):
    db = request.app.state.db
    cfg = db.get_config()
    gate = db.get_gate(gate_id)
    if not gate:
        return HTMLResponse('<span style="color:var(--red)">Gate weg</span>')
    ts = _cf_create_turnstile_widget(cfg, gate["hostname"])
    if not ts["ok"]:
        return HTMLResponse(
            f'<span style="color:var(--red)">Widget-Erstellung fehlgeschlagen: {escape(ts.get("msg", "?"))}</span>'
        )
    db.update_gate(gate_id, turnstile_site_key=ts["site_key"],
                    turnstile_secret_key=ts["secret_key"])
    resp = HTMLResponse('')
    resp.headers["HX-Redirect"] = f"/admin/gates/{gate_id}?ts_updated=1"
    return resp


@router.post("/admin/gates/{gate_id}/health-check", response_class=HTMLResponse)
async def gate_health_check(request: Request, gate_id: int):
    """End-to-End Check pro Gate:
      1. DNS: löst der Hostname auf deine Server-IP auf?
      2. TLS: hat der Server ein gültiges Cert für die Domain?
      3. HTTP: antwortet /health mit 200?
      4. Turnstile: sind Site+Secret gesetzt UND funktionieren sie?
      5. CF: existiert die Zone? Sind Records aktuell proxied?
    """
    import socket, ssl
    import requests as _req
    from datetime import datetime, timezone
    db = request.app.state.db
    cfg = db.get_config()
    gate = db.get_gate(gate_id)
    if not gate:
        return HTMLResponse('<div class="alert alert-danger">Gate weg</div>')
    hostname = gate["hostname"]
    checks = []

    # 1. DNS
    server_ip = cfg.get("server_public_ip", "")
    try:
        resolved = socket.gethostbyname(hostname)
        if server_ip and resolved == server_ip:
            checks.append(("DNS", "ok", f"{hostname} → {resolved} (unser Server ✓)"))
        elif server_ip:
            # CF-proxied? Dann zeigt DNS auf CF-IP (104.16.x.x, 104.17.x.x, 172.64-71.x.x, 172.67.x.x, 188.114.9x.x, 188.114.9x.x)
            is_cf = (resolved.startswith(("104.16.", "104.17.", "104.18.", "104.19.",
                                            "104.20.", "104.21.", "172.64.", "172.65.",
                                            "172.66.", "172.67.", "172.68.", "172.69.",
                                            "172.70.", "172.71.", "188.114.9", "188.114.10")))
            if is_cf:
                checks.append(("DNS", "info",
                                f"{hostname} → {resolved} (Cloudflare-IP — Wolke ist AN)"))
            else:
                checks.append(("DNS", "warn",
                                f"{hostname} → {resolved} — aber unser Server ist {server_ip}"))
        else:
            checks.append(("DNS", "ok", f"{hostname} → {resolved}"))
    except Exception as e:
        checks.append(("DNS", "fail", f"Auflösung fehlgeschlagen: {e}"))

    # 2. TLS-Zertifikat via SNI-Handshake, ohne HTTP-Request
    cert_ok = False
    cert_msg = ""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((hostname, 443), timeout=6) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
                subject = dict(x[0] for x in cert.get("subject", []))
                issuer = dict(x[0] for x in cert.get("issuer", []))
                not_after = cert.get("notAfter", "")
                cert_ok = True
                cert_msg = (f"CN={subject.get('commonName', '?')} · "
                             f"Issuer={issuer.get('organizationName', '?')} · "
                             f"gültig bis {not_after}")
        checks.append(("TLS-Cert", "ok", cert_msg))
    except ssl.SSLCertVerificationError as e:
        checks.append(("TLS-Cert", "fail", f"Cert nicht valide: {e}"))
    except socket.timeout:
        checks.append(("TLS-Cert", "fail",
                        "Port 443 timeout — DNS zeigt auf falschen Server, oder Caddy läuft nicht"))
    except Exception as e:
        checks.append(("TLS-Cert", "fail", f"{type(e).__name__}: {e}"))

    # 3. HTTP-Ping — /go/nonexistent sollte 404 zurückgeben (heißt Antibot bearbeitet Request)
    if cert_ok:
        try:
            r = _req.get(f"https://{hostname}/go/nonexistent-test", timeout=6,
                          allow_redirects=False)
            if r.status_code in (200, 302, 303, 404):
                checks.append(("HTTP-Ping", "ok",
                                f"HTTPS {r.status_code} — Antibot antwortet für diese Domain"))
            else:
                checks.append(("HTTP-Ping", "warn",
                                f"HTTP {r.status_code} — unerwartet"))
        except Exception as e:
            checks.append(("HTTP-Ping", "fail", f"{type(e).__name__}: {e}"))

    # 4. Turnstile
    ts_site = (gate.get("turnstile_site_key") or "").strip()
    ts_secret = (gate.get("turnstile_secret_key") or "").strip()

    def _fmt_key(k: str) -> str:
        if len(k) < 12:
            return f'"{k}" ({len(k)} chars)'
        return f'{k[:8]}…{k[-4:]} ({len(k)} chars)'

    if not ts_site and not ts_secret:
        checks.append(("Turnstile", "info", "nicht konfiguriert (optional)"))
    elif not ts_site or not ts_secret:
        checks.append(("Turnstile", "warn",
                        "nur Site-Key ODER Secret-Key gesetzt — beide nötig"))
    else:
        # Format-Sanity: Turnstile-Keys starten immer mit '0x'
        format_warns = []
        if not ts_site.startswith("0x"):
            format_warns.append(f"Site-Key sieht ungewöhnlich aus: {_fmt_key(ts_site)} — sollte mit '0x' anfangen")
        if not ts_secret.startswith("0x"):
            format_warns.append(f"Secret-Key sieht ungewöhnlich aus: {_fmt_key(ts_secret)} — sollte mit '0x' anfangen")
        for w in format_warns:
            checks.append(("Turnstile Format", "warn", w))
        # Verify-Trick: wir schicken den Secret + einen bewusst leeren Response.
        # CF antwortet:
        #   error-codes: ["missing-input-response"]  → Secret akzeptiert, Token fehlte (= OK)
        #   error-codes: ["invalid-input-secret"]    → Secret war falsch (= BAD)
        #   error-codes: ["missing-input-secret"]    → Secret fehlte, sollte hier nie kommen
        # Bei allem anderen dass success=False sagt: Secret wurde auch akzeptiert
        # (der Fehler bezieht sich auf die Response, nicht den Secret).
        try:
            r = _req.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": ts_secret, "response": ""},
                timeout=8,
            )
            try:
                data = r.json()
            except Exception:
                data = {}
            errs = data.get("error-codes") or []
            secret_bad = ("invalid-input-secret" in errs
                          or "missing-input-secret" in errs)
            if secret_bad:
                checks.append(("Turnstile", "fail",
                                f"Secret-Key bei CF ungültig ({', '.join(errs)})"))
            elif data.get("success") is False:
                # Irgendein anderer error-code → Secret war ok, nur unser
                # Dummy-Response wurde (korrekt) abgelehnt.
                errs_str = ", ".join(errs) if errs else "no error-codes"
                checks.append(("Turnstile", "ok",
                                f"Secret akzeptiert (CF: {errs_str}) · "
                                f"Site-Key: {_fmt_key(ts_site)}"))
            elif data.get("success") is True:
                # Extrem unerwartet — leerer Response validiert nie
                checks.append(("Turnstile", "warn",
                                "success=true auf leeren Response — Test-Konfiguration?"))
            else:
                # Wirklich leerer/kaputter Body → HTTP-Code + Body-Snippet zeigen
                body_snip = r.text[:150].replace("\n", " ") if r.text else "(empty)"
                checks.append(("Turnstile", "warn",
                                f"CF-API HTTP {r.status_code} · body: {body_snip}"))
        except Exception as e:
            checks.append(("Turnstile", "fail", f"CF-API nicht erreichbar: {e}"))

    # 5. Cloudflare-Zone-Status
    proxied_records = 0
    total_records = 0
    zone = None
    if _cf_configured(cfg):
        zones = _cf_zones(cfg)
        zone = next((z for z in zones if z["name"] == hostname), None)
        if not zone:
            checks.append(("CF-Zone", "warn",
                            f"Keine CF-Zone für {hostname} — Records nicht managbar"))
        else:
            recs = _cf_records(cfg, zone["id"])
            for r in recs:
                if r.get("type") in ("A", "AAAA", "CNAME"):
                    total_records += 1
                    if r.get("proxied"):
                        proxied_records += 1
            if total_records == 0:
                checks.append(("CF-Zone", "warn", "Zone existiert aber keine A/CNAME-Records"))
            elif proxied_records == 0:
                checks.append(("CF-Wolke", "info",
                                f"{total_records} Record(s) auf grau (DNS-only)"))
            elif proxied_records == total_records:
                checks.append(("CF-Wolke", "ok",
                                f"alle {total_records} Records auf orange (proxied)"))
            else:
                checks.append(("CF-Wolke", "warn",
                                f"{proxied_records}/{total_records} auf orange (Mix)"))
    else:
        checks.append(("CF-Zone", "info", "Kein CF-Auth im Panel — Skip"))

    # Hinweis-Zeile: wenn Cert vorhanden + Wolke grau + Turnstile ok → empfehle orange
    tips = []
    if cert_ok and zone and total_records > 0 and proxied_records == 0:
        tips.append("💡 Cert steht, Records sind grau — du kannst jetzt auf CF-Wolke orange "
                    "umschalten (Button unten in dieser Card).")
    if not cert_ok:
        tips.append("⚠️ Kein Cert erreichbar — Caddy holt sich das automatisch beim ersten "
                    "HTTPS-Request. Test: <code>curl -sI https://" + escape(hostname) + "/health</code>")

    # Render
    icon_map = {"ok": ("✓", "var(--green)"),
                "warn": ("!", "var(--orange, #d97706)"),
                "info": ("ℹ", "var(--fg2)"),
                "fail": ("✗", "var(--red)")}
    rows = []
    for label, status, detail in checks:
        icon, color = icon_map.get(status, ("?", "var(--fg2)"))
        rows.append(
            f'<tr><td style="color:{color};font-weight:600;white-space:nowrap">{icon} {escape(label)}</td>'
            f'<td style="font-size:11px">{escape(detail)}</td></tr>'
        )
    tips_html = ""
    if tips:
        tips_html = ('<div style="margin-top:10px;padding:8px;background:#fff8e6;'
                     'border-left:3px solid #f2c74a;font-size:12px">'
                     + "<br>".join(tips) + '</div>')
    return HTMLResponse(
        '<table style="font-size:12px"><tbody>' + "".join(rows) + '</tbody></table>'
        + tips_html
    )


@router.post("/admin/gates/{gate_id}/retry-set-ns", response_class=HTMLResponse)
async def gate_retry_set_ns(request: Request, gate_id: int):
    """Manueller Retry für Dynadot NS-Setting (falls's beim Deploy nicht klappte)."""
    db = request.app.state.db
    cfg = db.get_config()
    gate = db.get_gate(gate_id)
    if not gate:
        return HTMLResponse('<span style="color:var(--red)">Gate weg</span>')
    if not cfg.get("dynadot_api_key"):
        return HTMLResponse('<span style="color:var(--red)">Kein Dynadot-Key gesetzt</span>')
    if not _cf_configured(cfg):
        return HTMLResponse('<span style="color:var(--red)">Kein CF-Auth</span>')
    zones = _cf_zones(cfg)
    zone = next((z for z in zones if z["name"] == gate["hostname"]), None)
    if not zone:
        return HTMLResponse(f'<span style="color:var(--red)">Keine CF-Zone für {escape(gate["hostname"])}</span>')
    ns_list = zone.get("name_servers", [])
    if not ns_list:
        return HTMLResponse('<span style="color:var(--red)">CF liefert keine NS für diese Zone</span>')
    logs = []
    res = _dyn_set_ns_with_retry(
        cfg["dynadot_api_key"], gate["hostname"], ns_list,
        secret=cfg.get("dynadot_api_secret", ""),
        log_step=lambda label, ok, detail: logs.append((label, ok, detail)),
    )
    log_html = "".join(f'<li>{"✓" if ok else "…"} {escape(label)}'
                        f'{": " + escape(detail) if detail else ""}</li>'
                        for label, ok, detail in logs)
    if res["ok"]:
        return HTMLResponse(
            f'<div style="color:var(--green)">✓ NS bei Dynadot gesetzt auf {escape(", ".join(ns_list[:2]))}</div>'
            f'<ul style="font-size:11px;color:var(--fg2)">{log_html}</ul>'
        )
    return HTMLResponse(
        f'<div style="color:var(--red)">✗ {escape(res["msg"])}</div>'
        f'<ul style="font-size:11px;color:var(--fg2)">{log_html}</ul>'
        f'<div style="font-size:11px;color:var(--fg2);margin-top:6px">Manuell im Dynadot-Panel setzen: '
        f'<code>{escape(", ".join(ns_list))}</code></div>'
    )


@router.post("/admin/gates/{gate_id}/cf-proxy-toggle", response_class=HTMLResponse)
async def gate_cf_proxy_toggle(request: Request, gate_id: int,
                                 proxied: str = Form("")):
    """Alle A/CNAME-Records dieser Domain in CF auf proxied=True/False setzen."""
    db = request.app.state.db
    cfg = db.get_config()
    gate = db.get_gate(gate_id)
    if not gate:
        return HTMLResponse('<span style="color:var(--red)">Gate weg</span>')
    if not _cf_configured(cfg):
        return HTMLResponse('<span style="color:var(--red)">Kein CF-Auth konfiguriert.</span>')
    zones = _cf_zones(cfg)
    zone = next((z for z in zones if z["name"] == gate["hostname"]), None)
    if not zone:
        return HTMLResponse(f'<span style="color:var(--red)">Keine CF-Zone für {escape(gate["hostname"])}.</span>')
    want_proxied = proxied == "1"
    records = _cf_records(cfg, zone["id"])
    n = 0
    for r in records:
        if r.get("type") not in ("A", "AAAA", "CNAME"):
            continue
        if r.get("proxied") == want_proxied:
            continue
        # Update via PATCH — hier tun's wir per PUT über _cf_post nicht direkt,
        # verwenden stattdessen den low-level requests wrapper mit PATCH.
        import requests as _req
        headers = _cf_auth(cfg)
        resp = _req.patch(f"{CF_BASE}/zones/{zone['id']}/dns_records/{r['id']}",
                          headers=headers, json={"proxied": want_proxied}, timeout=15)
        if resp.status_code == 200 and resp.json().get("success"):
            n += 1
    return HTMLResponse(
        f'<span style="color:var(--green)">✓ {n} Record(s) auf '
        f'<strong>{"orange" if want_proxied else "grau"}</strong> umgeschaltet.</span>'
    )


# ── Server-IP manuell setzen / auto-detect nachziehen ───────

@router.post("/admin/domains/set-server-ip", response_class=HTMLResponse)
async def set_server_ip(request: Request, ip: str = Form("")):
    db = request.app.state.db
    ip = ip.strip()
    if ip:
        # Sanity check IPv4
        parts = ip.split(".")
        if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            return HTMLResponse('<span style="color:var(--red)">Keine gültige IPv4.</span>')
        db.set_config(server_public_ip=ip)
        return HTMLResponse(f'<span style="color:var(--green)">✓ Gespeichert: <code>{escape(ip)}</code></span>')
    # ip leer → auto-detect nochmal versuchen
    detected = detect_public_ip()
    if detected:
        db.set_config(server_public_ip=detected)
        return HTMLResponse(f'<span style="color:var(--green)">✓ Auto-detected: <code>{escape(detected)}</code></span>')
    return HTMLResponse('<span style="color:var(--red)">Auto-detect fehlgeschlagen — bitte IP von Hand eintragen '
                        '(dein Server ist evtl. hinter NAT oder blockt Outbound zu Public-IP-APIs).</span>')


# ── Existierende CF-Zone als Gate deployen (ohne Kauf-Step) ─

@router.get("/admin/domains/cf/zone/{zone_id}/deploy-form", response_class=HTMLResponse)
async def zone_deploy_form(request: Request, zone_id: str):
    db = request.app.state.db
    cfg = db.get_config()
    if not _cf_configured(cfg):
        return HTMLResponse('<div class="alert alert-danger">Kein CF-Auth.</div>')
    zones = _cf_zones(cfg)
    zone = next((z for z in zones if z["id"] == zone_id), None)
    if not zone:
        return HTMLResponse('<div class="alert alert-danger">Zone nicht gefunden.</div>')
    hostname = zone["name"]
    existing = db.get_gate_by_host(hostname)
    warn = ""
    if existing:
        warn = (f'<div class="alert alert-warn">Für <code>{escape(hostname)}</code> gibt es schon '
                f'einen Gate (ID {existing["id"]}). Deploy wird ihn NICHT überschreiben, '
                f'nur A-Records + Turnstile erneuern falls nötig.</div>')
    presets_html = "".join(
        f'<option value="{k}" {"selected" if k == "medium" else ""}>{p["label"]}</option>'
        for k, p in MODE_PRESETS.items()
    )
    return HTMLResponse(f'''
    {warn}
    <form hx-post="/admin/domains/cf/zone/{zone_id}/deploy-as-gate"
          hx-target="#zone-deploy-result-{zone_id}" hx-swap="innerHTML"
          style="padding:12px;background:#f8f9fb;border-radius:4px">
        <p><strong>{escape(hostname)}</strong> als antibot-Gate deployen —
        A-Records @ + www setzen, optional Turnstile, Gate anlegen, Ready-Links generieren.</p>

        <div class="grid-2">
            <div>
                <label>Modus</label>
                <select name="mode">{presets_html}</select>
                <label>Ziel-URL</label>
                <input name="target_url" placeholder="https://real-landing.de/x" required>
            </div>
            <div>
                <label>Ready-Links</label>
                <input name="initial_links" type="number" value="10" min="0" max="500">
                <label style="display:flex;align-items:center;gap:6px;font-weight:400;cursor:pointer;margin-top:6px">
                    <input type="checkbox" name="add_www" value="1" checked>
                    <span>www.-Subdomain als A-Record dazu</span>
                </label>
                <label style="display:flex;align-items:center;gap:6px;font-weight:400;cursor:pointer">
                    <input type="checkbox" name="auto_turnstile" value="1"
                           {"" if cfg.get("cloudflare_account_id") else "disabled"}>
                    <span>Turnstile-Widget erstellen</span>
                </label>
            </div>
        </div>
        <div style="margin-top:10px">
            <button class="btn btn-primary btn-sm">Als Gate deployen</button>
        </div>
    </form>
    <div id="zone-deploy-result-{zone_id}" style="margin-top:10px"></div>
    ''')


@router.post("/admin/domains/cf/zone/{zone_id}/deploy-as-gate", response_class=HTMLResponse)
async def zone_deploy_as_gate(request: Request, zone_id: str):
    db = request.app.state.db
    cfg = db.get_config()
    form = await request.form()
    zones = _cf_zones(cfg)
    zone = next((z for z in zones if z["id"] == zone_id), None)
    if not zone:
        return HTMLResponse('<div class="alert alert-danger">Zone nicht gefunden.</div>')
    pcfg = {
        "buy_dynadot": False,  # explizit KEIN Kauf — Domain ist ja schon da
        "add_www": bool(form.get("add_www")),
        "auto_turnstile": bool(form.get("auto_turnstile")),
        "target_url": (form.get("target_url") or "").strip(),
        "mode": form.get("mode") or "medium",
        "initial_links": int(form.get("initial_links") or 10),
    }
    if pcfg["mode"] not in MODE_PRESETS:
        pcfg["mode"] = "medium"

    res = _pipeline_one_domain(db, cfg, zone["name"], pcfg)
    color = "var(--green)" if res["ok"] else "var(--red)"
    steps_html = "".join(
        f'<li>{"✓" if ok else "✗"} <strong>{escape(label)}</strong>'
        f'{": " + escape(detail) if detail else ""}</li>'
        for label, ok, detail in res["steps"]
    )
    gate_link = ""
    if res.get("gate_id"):
        gate_link = (f'<p><a href="/admin/gates/{res["gate_id"]}" '
                     f'class="btn btn-primary btn-xs">Gate + Ready-Links öffnen</a></p>')
    return HTMLResponse(
        f'<div style="color:{color};font-weight:600;margin-bottom:6px">'
        f'{"✓" if res["ok"] else "✗"} {escape(res["domain"])}</div>'
        f'<ul style="font-size:13px">{steps_html}</ul>{gate_link}'
    )
