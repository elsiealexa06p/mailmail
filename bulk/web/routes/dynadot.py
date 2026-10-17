"""Dynadot — Domain search, purchase, auto CF zone + NS setup."""
import time
import json
import threading
from html import escape
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()

DYNADOT_BASE = "https://api.dynadot.com/api3.json"

_buy_progress = {"running": False, "log": [], "domain": "", "done": False}


def _dynadot_call(api_key: str, command: str, params: dict = None) -> dict:
    import requests
    url = f"{DYNADOT_BASE}?key={api_key}&command={command}"
    if params:
        for k, v in params.items():
            url += f"&{k}={v}"
    resp = requests.get(url, timeout=20)
    return resp.json()


def _cf_headers_for(db, cf_account_id: int = 0) -> tuple:
    """Get CF auth headers + account_id for a specific or first CF account."""
    if cf_account_id:
        row = db._conn().execute("SELECT * FROM cf_accounts WHERE id=?", (cf_account_id,)).fetchone()
        accounts = [row] if row else []
    else:
        accounts = db.get_cf_accounts()
    if not accounts:
        return None, None
    acct = dict(accounts[0])
    account_id = acct.get("account_id", "")
    if acct.get("global_api_key") and acct.get("auth_email"):
        headers = {
            "X-Auth-Key": acct["global_api_key"],
            "X-Auth-Email": acct["auth_email"],
            "Content-Type": "application/json"
        }
    else:
        headers = {
            "Authorization": f"Bearer {acct.get('api_token', '')}",
            "Content-Type": "application/json"
        }
    return headers, account_id


def _get_api_key(db, dynadot_account_id: int = 0) -> str:
    """Get API key from specific account, or the primary account,
    or legacy config as last resort."""
    if dynadot_account_id:
        acct = db.get_dynadot_account(dynadot_account_id)
        if acct:
            return dict(acct).get("api_key", "")
    # Fall back to primary account
    primary = db.get_primary_dynadot_account()
    if primary:
        return dict(primary).get("api_key", "")
    # Last resort: legacy config (to be retired)
    config = db.get_dynadot_config()
    return config.get("api_key", "")


def _account_config(db, dynadot_account_id: int = 0) -> dict:
    """Resolve the effective Dynadot account configuration: API key plus
    per-account flags (send_currency, secret). Falls back to the primary
    account when no specific id was passed."""
    row = None
    if dynadot_account_id:
        row = db.get_dynadot_account(dynadot_account_id)
    if row is None:
        row = db.get_primary_dynadot_account()
    if row is not None:
        r = dict(row)
        return {
            "api_key": r.get("api_key", ""),
            "secret": r.get("secret", "") or "",
            "send_currency": bool(r.get("send_currency", 0)),
        }
    # Legacy fallback
    legacy = db.get_dynadot_config()
    return {
        "api_key": legacy.get("api_key", ""),
        "secret": legacy.get("secret", "") or "",
        "send_currency": False,
    }


@router.get("/domains", response_class=HTMLResponse)
async def domains_page(request: Request):
    db = request.app.state.db
    tpl = request.app.state.templates
    config = db.get_dynadot_config()
    purchased = [dict(d) for d in db.get_purchased_domains()]
    cf_accounts = [dict(a) for a in db.get_cf_accounts()]
    dynadot_accounts = [dict(a) for a in db.get_dynadot_accounts()]
    return tpl.TemplateResponse(request, "domains.html", {
        "active": "domains",
        "config": config,
        "purchased": purchased,
        "cf_accounts": cf_accounts,
        "dynadot_accounts": dynadot_accounts,
        "db": db,
    })


@router.post("/domains/add-dynadot-account")
async def add_dynadot_account(request: Request,
                               name: str = Form(""),
                               api_key: str = Form(""),
                               secret: str = Form(""),
                               send_currency: str = Form("")):
    if name.strip() and api_key.strip():
        request.app.state.db.add_dynadot_account(
            name.strip(), api_key.strip(),
            secret=secret.strip(),
            send_currency=1 if send_currency else 0,
        )
    return RedirectResponse("/domains", status_code=303)


@router.post("/domains/dynadot/{aid}/update")
async def update_dynadot_account(request: Request, aid: int,
                                  secret: str = Form(""),
                                  send_currency: str = Form("")):
    request.app.state.db.update_dynadot_account(
        aid,
        secret=secret.strip(),
        send_currency=1 if send_currency else 0,
    )
    return RedirectResponse("/domains", status_code=303)


@router.post("/domains/dynadot/{aid}/set-primary")
async def set_primary_dynadot(request: Request, aid: int):
    request.app.state.db.set_primary_dynadot_account(aid)
    return RedirectResponse("/domains", status_code=303)


@router.post("/domains/dynadot/{aid}/delete")
async def delete_dynadot_account(request: Request, aid: int):
    request.app.state.db.delete_dynadot_account(aid)
    return RedirectResponse("/domains", status_code=303)


@router.post("/domains/config")
async def save_config(request: Request, api_key: str = Form(""), secret: str = Form("")):
    request.app.state.db.save_dynadot_config(api_key.strip(), secret.strip())
    return RedirectResponse("/domains", status_code=303)


@router.post("/domains/search", response_class=HTMLResponse)
async def search_domains(request: Request, query: str = Form(""),
                         dynadot_account_id: int = Form(0),
                         debug: str = Form("")):
    db = request.app.state.db
    api_key = _get_api_key(db, dynadot_account_id)
    if not api_key:
        return HTMLResponse('<div class="alert alert-danger">No Dynadot API key. Add an account first.</div>')

    domains_raw = [d.strip() for d in query.replace(",", "\n").splitlines() if d.strip()]
    if not domains_raw:
        return HTMLResponse('<div class="alert alert-warning">Enter at least one domain.</div>')

    debug_mode = bool(debug)
    from mailer import whois_check as whois_mod
    results = []
    last_data = None
    for d in domains_raw[:50]:
        try:
            data = _dynadot_call(api_key, "search", {
                "domain0": d, "show_price": "1", "currency": "EUR"
            })
            last_data = data
            search_resp = data.get("SearchResponse", {})
            items = search_resp.get("SearchResults", [])
            if not isinstance(items, list):
                items = [items] if items else []
            for item in items:
                name = item.get("DomainName", d)
                status_val = str(item.get("Available", "")).lower().strip()
                status_field = str(item.get("Status", "")).lower().strip()
                price = item.get("Price", "")

                # Dynadot returns: yes / no / premium / offline / system_busy / error
                # premium = available at higher price; offline / system_busy = could not determine
                whois_verdict = ""
                whois_override = False
                if status_val in ("yes", "true", "available"):
                    available, label = True, "Available"
                elif status_val == "premium":
                    available, label = True, "Premium"
                elif status_val in ("offline", "system_busy", "error", ""):
                    # Dynadot inconclusive — trust WHOIS instead
                    whois_verdict, _raw = whois_mod.check(name)
                    if whois_verdict == "available":
                        available, label = True, "WHOIS-Free"
                        whois_override = True
                    elif whois_verdict == "taken":
                        available, label = False, "Taken (WHOIS)"
                    else:
                        available, label = False, f"Unknown ({status_val or 'empty'})"
                elif status_val in ("no", "false", "taken"):
                    # Verify Dynadot's "no" via WHOIS — known issue with .de & some TLDs
                    whois_verdict, _raw = whois_mod.check(name)
                    if whois_verdict == "available":
                        available, label = True, "WHOIS-Free"
                        whois_override = True
                    else:
                        available, label = False, "Taken"
                else:
                    available, label = False, f"? ({status_val})"

                results.append({
                    "domain": name,
                    "available": available,
                    "label": label,
                    "price": price,
                    "raw_available": status_val,
                    "raw_status": status_field,
                    "whois_verdict": whois_verdict,
                    "whois_override": whois_override,
                    "raw_item": item if debug_mode else None,
                })
            if not items:
                results.append({
                    "domain": d, "available": False, "label": "No result",
                    "price": "", "raw_available": "", "raw_status": "",
                    "whois_verdict": "", "whois_override": False,
                    "raw_item": search_resp if debug_mode else None,
                })
        except Exception as e:
            results.append({
                "domain": d, "available": False, "label": f"Error",
                "price": f"{e}", "raw_available": "exception",
                "raw_status": "", "whois_verdict": "", "whois_override": False,
                "raw_item": None,
            })

    if not results:
        return HTMLResponse(
            f'<div class="alert alert-info">No results. Raw response: '
            f'<pre style="font-size:11px;white-space:pre-wrap">{escape(json.dumps(last_data))}</pre></div>'
        )

    rows = ""
    for r in results:
        badge = "badge-running" if r["available"] else "badge-failed"
        price_str = f'{r["price"]}' if r["price"] and r["available"] else "—"
        buy_btn = ""
        if r["available"]:
            # If WHOIS overrode Dynadot, force=1 so register skips Dynadot's bogus 'no'
            force_attr = ',"force":"1"' if r.get("whois_override") else ''
            confirm_txt = (f"Force-buy {r['domain']} (Dynadot says 'no' but WHOIS says free)?"
                            if r.get("whois_override") else
                            f"Buy {r['domain']} for {price_str}?")
            btn_label = "Force Buy" if r.get("whois_override") else "Buy"
            buy_btn = (
                f'<button class="btn btn-primary btn-xs" '
                f'hx-post="/domains/buy" '
                f'hx-vals=\'{{"domain":"{escape(r["domain"])}"{force_attr}}}\' '
                f'hx-include="#cf-account-select,#dynadot-account-select" '
                f'hx-target="#buy-result" hx-swap="innerHTML" '
                f'hx-confirm="{escape(confirm_txt)}">{btn_label}</button>'
            )
        elif r.get("whois_verdict") == "unknown" and r["raw_available"] in ("no", "false", "taken"):
            # Dynadot says no, WHOIS couldn't verify — offer manual force-buy
            buy_btn = (
                f'<button class="btn btn-secondary btn-xs" '
                f'hx-post="/domains/buy" '
                f'hx-vals=\'{{"domain":"{escape(r["domain"])}","force":"1"}}\' '
                f'hx-include="#cf-account-select,#dynadot-account-select" '
                f'hx-target="#buy-result" hx-swap="innerHTML" '
                f'hx-confirm="Force-buy {escape(r["domain"])} (no WHOIS verdict)?">Try anyway</button>'
            )
        debug_cell = ""
        if debug_mode:
            raw_json = escape(json.dumps(r.get("raw_item") or {}, ensure_ascii=False, indent=2)[:1200])
            whois_info = f"WHOIS={escape(r.get('whois_verdict', '') or 'not checked')}"
            debug_cell = (
                f'<td style="font-family:monospace;font-size:10px;color:var(--fg2)">'
                f'<details><summary>raw</summary>'
                f'<div>Available=<b>{escape(r["raw_available"])}</b> '
                f'Status=<b>{escape(r["raw_status"])}</b> {whois_info}</div>'
                f'<pre style="white-space:pre-wrap;max-width:480px">{raw_json}</pre>'
                f'</details></td>'
            )
        rows += (
            f'<tr>'
            f'<td style="font-weight:500">{escape(r["domain"])}</td>'
            f'<td><span class="badge {badge}">{escape(r["label"])}</span></td>'
            f'<td>{escape(price_str)}</td>'
            f'<td>{buy_btn}</td>'
            f'{debug_cell}'
            f'</tr>'
        )

    available_domains = [r["domain"] for r in results if r["available"]]
    buy_all_btn = ""
    if len(available_domains) > 1:
        buy_all_btn = (
            f'<div style="margin-top:10px">'
            f'<button class="btn btn-primary btn-sm" '
            f'hx-post="/domains/buy-bulk" '
            f'hx-vals=\'{{"domains": {json.dumps(json.dumps(available_domains))}}}\' '
            f'hx-include="#cf-account-select,#dynadot-account-select" '
            f'hx-target="#buy-result" hx-swap="innerHTML" '
            f'hx-confirm="Buy all {len(available_domains)} available domains?">Buy All Available ({len(available_domains)})</button>'
            f'</div>'
        )

    debug_header = '<th>Debug</th>' if debug_mode else ''
    return HTMLResponse(
        f'<table><thead><tr><th>Domain</th><th>Status</th><th>Price</th><th></th>{debug_header}</tr></thead>'
        f'<tbody>{rows}</tbody></table>{buy_all_btn}'
    )


@router.post("/domains/balance", response_class=HTMLResponse)
async def check_balance(request: Request, dynadot_account_id: int = Form(0)):
    db = request.app.state.db
    api_key = _get_api_key(db, dynadot_account_id)
    if not api_key:
        return HTMLResponse('<span style="color:var(--red)">No API key</span>')
    try:
        data = _dynadot_call(api_key, "get_account_balance")
        bal_resp = data.get("GetAccountBalanceResponse", data)
        bal_list = bal_resp.get("BalanceList", [])
        if bal_list and isinstance(bal_list, list):
            balance = bal_list[0].get("Amount", "?")
            currency = bal_list[0].get("Currency", "USD")
        else:
            balance = bal_resp.get("AccountBalance", "?")
            currency = bal_resp.get("Currency", "USD")
        if balance == "?":
            return HTMLResponse(
                f'<span style="color:var(--red)">Parse error. Raw: '
                f'<pre style="font-size:10px;white-space:pre-wrap">{escape(json.dumps(data))}</pre></span>')
        return HTMLResponse(
            f'<span style="color:var(--green);font-weight:600">{balance} {currency}</span>'
        )
    except Exception as e:
        return HTMLResponse(f'<span style="color:var(--red)">{escape(str(e))}</span>')


@router.post("/domains/buy", response_class=HTMLResponse)
async def buy_domain(request: Request, domain: str = Form(""),
                     cf_account_id: int = Form(0),
                     dynadot_account_id: int = Form(0),
                     force: str = Form("")):
    db = request.app.state.db
    import logging
    log = logging.getLogger("bulk.dynadot")
    acct_name = "(legacy)"
    if dynadot_account_id:
        acct = db.get_dynadot_account(dynadot_account_id)
        if acct:
            acct_name = dict(acct).get("name", "?")
    api_key = _get_api_key(db, dynadot_account_id)
    log.info("BUY: domain=%s cf_id=%s dynadot_id=%s name=%s key=%s... force=%s",
             domain, cf_account_id, dynadot_account_id, acct_name,
             api_key[:6] if api_key else "NONE", bool(force))
    if not api_key or not domain.strip():
        return HTMLResponse('<div class="alert alert-danger">Missing API key or domain.</div>')

    if _buy_progress["running"]:
        return HTMLResponse('<div class="alert alert-warning">Purchase already in progress. Wait for it to finish.</div>')

    domain = domain.strip().lower()
    config = _account_config(db, dynadot_account_id)
    force_buy = bool(force)
    _buy_progress.update(running=True, log=[], domain=domain, done=False)

    def worker():
        log = _buy_progress["log"]
        try:
            _do_buy(db, config, domain, cf_account_id, log, force=force_buy)
        except Exception as e:
            log.append(f"Fatal error: {e}")
        finally:
            _buy_progress["done"] = True
            _buy_progress["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return HTMLResponse(
        f'<div class="alert alert-info">Purchasing {escape(domain)}{" (force)" if force_buy else ""}...</div>'
        f'<div id="buy-live" hx-get="/domains/buy-progress" hx-trigger="every 2s" hx-swap="innerHTML"></div>'
    )


@router.get("/domains/buy-progress", response_class=HTMLResponse)
async def buy_progress(request: Request):
    log = _buy_progress["log"]
    done = _buy_progress["done"]
    html = _fmt_log(log)
    if not done:
        html += '<div hx-get="/domains/buy-progress" hx-trigger="every 2s" hx-swap="outerHTML"></div>'
    return HTMLResponse(html)


def _do_buy(db, config, domain, cf_account_id, log, force: bool = False):
    """Run the full buy+CF+NS flow in a background thread.
    force=True skips the Dynadot availability check (used when WHOIS
    says the domain is free but Dynadot's search returns 'no')."""
    if force:
        log.append(f"Force-buy: skipping Dynadot availability check for {domain}.")
    else:
        log.append(f"Checking availability of {domain}...")
        try:
            check = _dynadot_call(config["api_key"], "search", {
                "domain0": domain, "show_price": "1", "currency": "EUR"
            })
            sr = check.get("SearchResponse", {})
            items = sr.get("SearchResults", [])
            if not isinstance(items, list):
                items = [items] if items else []
            if items:
                status_val = str(items[0].get("Available", "")).lower().strip()
                if status_val in ("yes", "true", "available"):
                    price = items[0].get("Price", "?")
                    log.append(f"Available! Price: {price}")
                elif status_val == "premium":
                    price = items[0].get("Price", "?")
                    log.append(f"Premium available! Price: {price}")
                elif status_val in ("offline", "system_busy", "error", ""):
                    log.append(f"Dynadot inconclusive ({status_val or 'empty'}) — attempting register anyway.")
                else:
                    log.append(f"Domain {domain} is NOT available ({status_val}).")
                    return
            else:
                log.append("Could not verify availability, proceeding...")
        except Exception as e:
            log.append(f"Availability check error: {e}, proceeding...")

    # Pre-flight balance check with the same key being used for register.
    # If this reports $0 while the Dynadot dashboard shows funds, the key
    # is bound to a different sub-account than the one we think it is.
    try:
        bal_data = _dynadot_call(config["api_key"], "get_account_balance")
        bal_resp = bal_data.get("GetAccountBalanceResponse", bal_data)
        bal_list = bal_resp.get("BalanceList", [])
        if bal_list and isinstance(bal_list, list):
            bal_amount = bal_list[0].get("Amount", "?")
            bal_currency = bal_list[0].get("Currency", "?")
        else:
            bal_amount = bal_resp.get("AccountBalance", "?")
            bal_currency = bal_resp.get("Currency", "?")
        log.append(f"Pre-flight balance (same key): {bal_amount} {bal_currency}")
        log.append(f"Pre-flight raw: {json.dumps(bal_data)[:300]}")
    except Exception as e:
        log.append(f"Pre-flight balance check error: {e}")

    register_params = {"domain": domain, "duration": "1"}
    if config.get("send_currency"):
        register_params["currency"] = "USD"
        log.append(f"Sending currency=USD with register (workaround toggle).")
    log.append(f"Registering {domain}... (key: {config['api_key'][:6]}...{config['api_key'][-4:]})")
    try:
        data = _dynadot_call(config["api_key"], "register", register_params)
        reg_resp = data.get("RegisterResponse", data)
        resp_code = str(reg_resp.get("ResponseCode", "-1"))
        log.append(f"Register raw response: {json.dumps(data)[:500]}")
        if resp_code != "0":
            error_msg = reg_resp.get("Error", reg_resp.get("Status", str(reg_resp)))
            log.append(f"Registration failed: {error_msg}")
            return
            return
        log.append("Registration successful!")
    except Exception as e:
        log.append(f"Registration error: {e}")
        return

    cf_headers, account_id = _cf_headers_for(db, cf_account_id)
    zone_id = ""
    ns1 = ""
    ns2 = ""

    if cf_headers and account_id:
        log.append("Creating Cloudflare zone...")
        try:
            import requests
            resp = requests.post(
                "https://api.cloudflare.com/client/v4/zones",
                headers=cf_headers,
                json={"name": domain, "account": {"id": account_id}, "jump_start": False},
                timeout=15
            )
            zdata = resp.json()
            if zdata.get("success"):
                result = zdata["result"]
                zone_id = result.get("id", "")
                ns_list = result.get("name_servers", [])
                ns1 = ns_list[0] if len(ns_list) > 0 else ""
                ns2 = ns_list[1] if len(ns_list) > 1 else ""
                log.append(f"Zone created: {zone_id[:12]}... NS: {ns1}, {ns2}")
            else:
                errs = zdata.get("errors", [])
                log.append(f"CF zone warning: {errs}")
        except Exception as e:
            log.append(f"CF zone error: {e}")
    else:
        log.append("No Cloudflare account — skipping zone setup.")

    db.add_purchased_domain(domain, zone_id, ns1, ns2)
    log.append("Domain saved to database.")

    if ns1 and ns2:
        # Beim Kauf NUR EINMAL NS-set probieren. Dynadot akzeptiert bei .de
        # (und oft auch anderen TLDs) direkt nach register keine NS-Updates
        # ("Domain initialization is still in progress") — kann 10-30 min
        # dauern. Statt im Hintergrund minutenlang zu blocken: CF-Zone +
        # NS-Config sind jetzt in purchased_domains gespeichert, der User
        # drückt im Purchased-Tab "Connect NS" wenn's soweit ist.
        log.append("Setting nameservers (one attempt — manual retry via "
                    "'Set NS' button in Purchased tab if pending)...")
        try:
            data = _dynadot_call(config["api_key"], "set_ns", {
                "domain": domain, "ns1": ns1, "ns2": ns2
            })
            ns_resp = data.get("SetNsResponse", data)
            resp_code = str(ns_resp.get("ResponseCode", "-1"))
            ns_str = str(ns_resp).lower()
            if resp_code == "0":
                db.update_purchased_domain(domain, ns_set=1)
                log.append("✓ Nameservers set successfully!")
            elif ("initialization" in ns_str or "in progress" in ns_str
                    or "try again later" in ns_str or "pending" in ns_str
                    or "still processing" in ns_str or "dns queries" in ns_str
                    or "must respond" in ns_str):
                log.append(f"NS pending (Dynadot: '{ns_resp.get('Error', 'in progress')[:120]}'). "
                            f"→ CF-Zone + NS sind gespeichert. Im Purchased-Tab "
                            f"den 'Set NS' Button klicken sobald die Domain "
                            f"settled ist (meist 10-30 min nach register).")
            else:
                err = ns_resp.get("Error", str(ns_resp)[:150])
                log.append(f"NS error: {err}. "
                            f"→ Später via 'Set NS' Button im Purchased-Tab retryen.")
        except Exception as e:
            log.append(f"NS exception: {e}. "
                        f"→ Später via 'Set NS' Button im Purchased-Tab retryen.")
    else:
        log.append("No nameservers to set — skipping.")

    log.append(f"Done! {domain} is ready.")


@router.post("/domains/buy-bulk", response_class=HTMLResponse)
async def buy_bulk(request: Request, domains: str = Form("[]"),
                   cf_account_id: int = Form(0),
                   dynadot_account_id: int = Form(0)):
    """Buy multiple domains sequentially with live progress."""
    db = request.app.state.db
    config = _account_config(db, dynadot_account_id)
    if not config["api_key"]:
        return HTMLResponse('<div class="alert alert-danger">No API key.</div>')

    try:
        domain_list = json.loads(domains)
    except (json.JSONDecodeError, TypeError):
        return HTMLResponse('<div class="alert alert-danger">Invalid domain list.</div>')

    if not domain_list:
        return HTMLResponse('<div class="alert alert-warning">No domains to buy.</div>')

    if _buy_progress["running"]:
        return HTMLResponse('<div class="alert alert-warning">Purchase already in progress.</div>')
    _buy_progress.update(running=True, log=[], domain=f"{len(domain_list)} domains", done=False)

    def worker():
        log = _buy_progress["log"]
        for i, d in enumerate(domain_list):
            log.append(f"── [{i+1}/{len(domain_list)}] {d} ──")
            try:
                _do_buy(db, config, d.strip().lower(), cf_account_id, log)
            except Exception as e:
                log.append(f"Error: {e}")
            log.append("")
        log.append(f"Bulk purchase complete: {len(domain_list)} domains processed.")
        _buy_progress["done"] = True
        _buy_progress["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return HTMLResponse(
        f'<div class="alert alert-info">Buying {len(domain_list)} domains...</div>'
        f'<div id="buy-live" hx-get="/domains/buy-progress" hx-trigger="every 2s" hx-swap="innerHTML"></div>'
    )


@router.post("/domains/{did}/transfer-cf", response_class=HTMLResponse)
async def transfer_cf(request: Request, did: int, cf_account_id: int = Form(0)):
    """Delete zone from old CF account, create in new one, update NS at Dynadot."""
    import requests as req_lib
    db = request.app.state.db
    row = db._conn().execute("SELECT * FROM purchased_domains WHERE id=?", (did,)).fetchone()
    if not row:
        return HTMLResponse('<span style="color:var(--red)">Not found</span>')
    row = dict(row)
    domain = row["domain"]

    cf_headers, account_id = _cf_headers_for(db, cf_account_id)
    if not cf_headers:
        return HTMLResponse('<span style="color:var(--red)">No CF account selected</span>')

    log = []

    # Delete old zone if exists
    old_zone_id = row.get("cf_zone_id", "")
    if old_zone_id:
        try:
            resp = req_lib.delete(
                f"https://api.cloudflare.com/client/v4/zones/{old_zone_id}",
                headers=cf_headers, timeout=15)
            if resp.status_code == 200:
                log.append(f"Old zone deleted")
            else:
                log.append(f"Old zone delete: {resp.status_code}")
        except Exception as e:
            log.append(f"Old zone delete failed: {e}")

    # Create new zone
    try:
        resp = req_lib.post(
            "https://api.cloudflare.com/client/v4/zones",
            headers=cf_headers,
            json={"name": domain, "account": {"id": account_id}, "jump_start": False},
            timeout=15)
        zdata = resp.json()
        if zdata.get("success"):
            result = zdata["result"]
            zone_id = result.get("id", "")
            ns_list = result.get("name_servers", [])
            ns1 = ns_list[0] if len(ns_list) > 0 else ""
            ns2 = ns_list[1] if len(ns_list) > 1 else ""
            log.append(f"New zone: {zone_id[:12]}... NS: {ns1}, {ns2}")

            c = db._conn()
            c.execute("UPDATE purchased_domains SET cf_zone_id=?, cf_ns1=?, cf_ns2=?, ns_set=0 WHERE id=?",
                      (zone_id, ns1, ns2, did))
            c.commit()

            # Set NS at Dynadot
            api_key = _get_api_key(db)
            if api_key and ns1 and ns2:
                try:
                    data = _dynadot_call(api_key, "set_ns", {"domain": domain, "ns1": ns1, "ns2": ns2})
                    ns_resp = data.get("SetNsResponse", data)
                    if str(ns_resp.get("ResponseCode", "-1")) == "0":
                        db.update_purchased_domain(domain, ns_set=1)
                        log.append("NS updated at Dynadot!")
                    else:
                        log.append(f"NS pending: {ns_resp.get('Error', 'retry later')}")
                except Exception as e:
                    log.append(f"NS error: {e}")
        else:
            errs = zdata.get("errors", [])
            log.append(f"Zone create failed: {errs}")
    except Exception as e:
        log.append(f"Zone error: {e}")

    html = '<div style="font-size:12px">' + '<br>'.join(f"{'✓' if 'delete' in l.lower() or 'updated' in l.lower() or 'new zone' in l.lower() else '⚠'} {escape(l)}" for l in log) + '</div>'
    return HTMLResponse(html)


@router.post("/domains/{did}/set-ns", response_class=HTMLResponse)
async def retry_set_ns(request: Request, did: int):
    db = request.app.state.db
    row = db._conn().execute("SELECT * FROM purchased_domains WHERE id=?", (did,)).fetchone()
    if not row:
        return HTMLResponse('<span style="color:var(--red)">Not found</span>')
    row = dict(row)
    # Nutze den Multi-Account-Helper statt der Legacy-config (die ist bei
    # Multi-Account-Setup oft leer → "No API key" trotz vorhandenem Account).
    api_key = _get_api_key(db)
    if not api_key:
        return HTMLResponse('<span style="color:var(--red)">No API key — '
                             'Dynadot-Account anlegen oder einen als primary markieren.</span>')

    ns1 = row.get("cf_ns1", "")
    ns2 = row.get("cf_ns2", "")
    if not ns1 or not ns2:
        return HTMLResponse('<span style="color:var(--fg2)">No nameservers — create CF zone first</span>')

    try:
        data = _dynadot_call(api_key, "set_ns", {
            "domain": row["domain"], "ns1": ns1, "ns2": ns2
        })
        ns_resp = data.get("SetNsResponse", {})
        resp_code = str(ns_resp.get("ResponseCode", "-1"))
        ns_str = str(ns_resp).lower()
        if resp_code == "0":
            db.update_purchased_domain(row["domain"], ns_set=1)
            return HTMLResponse('<span class="badge badge-running">NS set!</span>')
        # Pending-Marker — Hinweis auf längeren Flow geben
        pending = ("initialization" in ns_str or "in progress" in ns_str
                    or "try again later" in ns_str or "pending" in ns_str
                    or "still processing" in ns_str or "dns queries" in ns_str
                    or "must respond" in ns_str)
        if pending:
            return HTMLResponse(
                f'<span style="color:var(--yellow)">… noch nicht ready. '
                f'Für .de bis zu 15 min nach register warten — oder im '
                f'„List remote domains" den „→ CF verbinden" Button nutzen '
                f'(der retried 15× automatisch).</span>'
            )
        return HTMLResponse(
                f'<span style="color:var(--red)">Failed. Raw: '
                f'<pre style="font-size:10px;white-space:pre-wrap">{escape(json.dumps(data)[:300])}</pre></span>')
    except Exception as e:
        return HTMLResponse(f'<span style="color:var(--red)">{escape(str(e))}</span>')


@router.post("/domains/list-remote", response_class=HTMLResponse)
async def list_remote_domains(request: Request, dynadot_account_id: int = Form(0)):
    db = request.app.state.db
    api_key = _get_api_key(db, dynadot_account_id)
    if not api_key:
        return HTMLResponse('<div class="alert alert-danger">No API key</div>')
    try:
        data = _dynadot_call(api_key, "list_domain")
        list_resp = data.get("ListDomainInfoResponse", data.get("DomainListResponse", {}))
        items = list_resp.get("MainDomains", list_resp.get("DomainList", {}).get("DomainInfo", []))
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            items = []

        if not items:
            return HTMLResponse(
                f'<div class="alert alert-info">No domains found. Raw: '
                f'<pre style="font-size:10px;white-space:pre-wrap">{escape(json.dumps(data)[:500])}</pre></div>')

        rows = ""
        for d in items:
            name = d.get("Name", d.get("name", ""))
            status = d.get("Status", d.get("status", ""))
            exp = d.get("Expiration", d.get("expiration", ""))
            did_safe = str(name).replace(".", "-").replace("/", "")
            # "Connect to Cloudflare" Button pro Zeile — macht das gleiche
            # wie _cf_connect (CF-Zone + NS set), aber Background-Thread
            # mit mehr Patience für .de ("still in progress" sauber retried).
            connect_btn = (
                f'<button type="button" class="btn btn-primary btn-xs" '
                f'hx-post="/domains/remote-connect-cf" '
                f'hx-vals=\'{{"domain":"{escape(str(name))}",'
                f'"dynadot_account_id":"{dynadot_account_id}"}}\' '
                f'hx-include="#dyn-remote-cf-account" '
                f'hx-target="#dyn-remote-result-{did_safe}" hx-swap="innerHTML" '
                f'hx-confirm="CF-Zone anlegen + NS bei Dynadot für {escape(str(name))} setzen?">'
                f'→ CF verbinden</button>'
                f'<div id="dyn-remote-result-{did_safe}" style="margin-top:4px;font-size:11px"></div>'
            )
            rows += (
                f'<tr>'
                f'<td>{escape(str(name))}</td>'
                f'<td>{escape(str(status))}</td>'
                f'<td style="font-size:12px">{escape(str(exp))}</td>'
                f'<td>{connect_btn}</td>'
                f'</tr>'
            )

        # CF-Account-Picker für die Buttons oben einbinden
        cf_accounts = [dict(a) for a in db.get_cf_accounts()] if hasattr(db, "get_cf_accounts") else []
        cf_options = "".join(
            f'<option value="{a["id"]}">{escape(a.get("name", "?"))}</option>'
            for a in cf_accounts
        )
        cf_picker = (
            f'<div style="margin:6px 0;padding:6px 8px;background:var(--bg);border-radius:4px">'
            f'<label style="font-size:11px;margin-right:6px">CF-Account für Connect-Button:</label>'
            f'<select id="dyn-remote-cf-account" name="cf_account_id" style="font-size:12px">'
            f'<option value="0">— (kein CF-Account hinterlegt)</option>{cf_options}</select>'
            f'</div>'
            if cf_accounts else
            '<div style="font-size:11px;color:var(--fg2);margin:6px 0">'
            '&#9432; Für „CF verbinden" Buttons: erst einen Cloudflare-Account anlegen.</div>'
        )

        return HTMLResponse(
            cf_picker +
            f'<table><thead><tr><th>Domain</th><th>Status</th><th>Expires</th><th></th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
            f'<p style="font-size:12px;color:var(--fg2);margin-top:6px">{len(items)} domains</p>'
        )
    except Exception as e:
        return HTMLResponse(f'<div class="alert alert-danger">{escape(str(e))}</div>')


# Progress-dict für den manuellen Remote-Connect (eine Domain = ein Job).
_remote_connect_progress = {}


@router.post("/domains/remote-connect-cf", response_class=HTMLResponse)
async def remote_connect_cf(request: Request, domain: str = Form(""),
                              cf_account_id: int = Form(0),
                              dynadot_account_id: int = Form(0)):
    """Manueller Flow: eine Domain aus dem Dynadot-Account picken, CF-Zone
    anlegen falls nicht da, dann NS bei Dynadot setzen MIT extra-vielen
    Retries (bis 15 min) — für Domains die beim Kauf an "Domain
    initialization still in progress" gescheitert sind."""
    db = request.app.state.db
    domain = (domain or "").strip().lower()
    if not domain or "." not in domain:
        return HTMLResponse('<span style="color:var(--red)">Ungültige Domain</span>')
    api_key = _get_api_key(db, dynadot_account_id)
    if not api_key:
        return HTMLResponse('<span style="color:var(--red)">Kein Dynadot API-Key</span>')
    if _remote_connect_progress.get(domain, {}).get("running"):
        return HTMLResponse(
            f'<span style="color:var(--yellow)">Läuft bereits — '
            f'<a href="javascript:void(0)" onclick="location.reload()">reload</a></span>')

    _remote_connect_progress[domain] = {"running": True, "log": [], "done": False}

    def worker():
        prog = _remote_connect_progress[domain]
        log = prog["log"]
        try:
            # 1) CF-Zone anlegen oder bestehende holen
            cf_headers, cf_account = _cf_headers_for(db, cf_account_id)
            if not cf_headers:
                log.append("✗ Kein Cloudflare-Account konfiguriert.")
                return

            import requests as _req
            ns1 = ns2 = ""
            zone_id = ""
            try:
                r = _req.get("https://api.cloudflare.com/client/v4/zones",
                              headers=cf_headers, params={"name": domain}, timeout=15)
                data = r.json()
                if data.get("success") and data.get("result"):
                    zone_id = data["result"][0]["id"]
                    nsl = data["result"][0].get("name_servers", [])
                    if len(nsl) >= 2:
                        ns1, ns2 = nsl[0], nsl[1]
                    log.append(f"CF-Zone existiert bereits: {zone_id[:12]}…")
            except Exception as e:
                log.append(f"CF-Zone-Lookup error: {e}")

            if not zone_id:
                try:
                    body = {"name": domain, "jump_start": False}
                    if cf_account:
                        body["account"] = {"id": cf_account}
                    r = _req.post("https://api.cloudflare.com/client/v4/zones",
                                   headers=cf_headers, json=body, timeout=20)
                    data = r.json()
                    if not data.get("success"):
                        errs = "; ".join(e.get("message", "") for e in data.get("errors", []))
                        log.append(f"✗ CF-Zone anlegen: {errs}")
                        return
                    zone_id = data["result"]["id"]
                    nsl = data["result"].get("name_servers", [])
                    if len(nsl) >= 2:
                        ns1, ns2 = nsl[0], nsl[1]
                    log.append(f"✓ CF-Zone angelegt: {zone_id[:12]}…")
                except Exception as e:
                    log.append(f"✗ CF-Zone error: {e}")
                    return

            if not ns1 or not ns2:
                log.append("✗ CF hat keine Nameserver geliefert.")
                return
            log.append(f"CF-Nameserver: {ns1}, {ns2}")

            # 2) Domain in unsere DB adden (falls noch nicht da) damit
            # retry-set-ns später wiedergefunden wird
            try:
                db.add_purchased_domain(domain, zone_id, ns1, ns2)
            except Exception:
                pass   # evtl. UNIQUE conflict — egal

            # 3) NS bei Dynadot — EIN Versuch. Wenn pending: User drückt
            # später nochmal. Kein minutenlanges Background-Blocken mehr.
            log.append("Setze NS bei Dynadot (ein Versuch)…")
            try:
                data = _dynadot_call(api_key, "set_ns", {
                    "domain": domain, "ns1": ns1, "ns2": ns2
                })
                ns_resp = data.get("SetNsResponse", data)
                resp_code = str(ns_resp.get("ResponseCode", "-1"))
                ns_str = str(ns_resp).lower()
                if resp_code == "0":
                    db.update_purchased_domain(domain, ns_set=1)
                    log.append("✓ Nameserver bei Dynadot gesetzt!")
                elif ("initialization" in ns_str or "in progress" in ns_str
                        or "try again later" in ns_str or "pending" in ns_str
                        or "still processing" in ns_str or "dns queries" in ns_str
                        or "must respond" in ns_str):
                    log.append(f"NS noch nicht ready: {ns_resp.get('Error', 'pending')[:150]}")
                    log.append("→ CF-Zone + NS sind in DB gespeichert. "
                                "In 10-30 min nochmal diesen Button drücken.")
                else:
                    err = ns_resp.get("Error", str(ns_resp)[:150])
                    log.append(f"✗ NS error: {err}")
            except Exception as e:
                log.append(f"NS exception: {e}")
        except Exception as e:
            log.append(f"Fatal: {e}")
        finally:
            prog["done"] = True
            prog["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return HTMLResponse(
        f'<div class="alert alert-info" style="padding:6px 10px;font-size:11px">'
        f'Starte Connect für <code>{escape(domain)}</code>…</div>'
        f'<div hx-get="/domains/remote-connect-progress?domain={escape(domain)}" '
        f'hx-trigger="every 3s" hx-swap="innerHTML"></div>'
    )


@router.get("/domains/remote-connect-progress", response_class=HTMLResponse)
async def remote_connect_progress(request: Request, domain: str = ""):
    prog = _remote_connect_progress.get(domain.strip().lower())
    if not prog:
        return HTMLResponse('<span style="color:var(--fg2);font-size:11px">—</span>')
    lines = ""
    for ln in prog["log"]:
        color = "var(--fg)"
        low = ln.lower()
        if "✓" in ln or "angelegt" in low or "gesetzt" in low:
            color = "var(--green)"
        elif "✗" in ln or "error" in low or "fatal" in low:
            color = "var(--red)"
        elif "warte" in low or "noch nicht" in low:
            color = "var(--yellow)"
        lines += f'<div style="color:{color};padding:1px 0;font-size:11px">{escape(ln)}</div>'
    html = (
        f'<div style="font-family:monospace;background:var(--bg);'
        f'padding:6px 8px;border-radius:4px;max-height:300px;overflow:auto">'
        f'{lines}</div>'
    )
    if not prog["done"]:
        html += (f'<div hx-get="/domains/remote-connect-progress?domain={escape(domain)}" '
                 f'hx-trigger="every 3s" hx-swap="outerHTML"></div>')
    return HTMLResponse(html)


def _fmt_log(lines: list) -> str:
    html = (
        '<div style="font-family:monospace;font-size:12px;'
        'background:var(--bg);padding:12px;border-radius:var(--radius)">'
    )
    for line in lines:
        if "successful" in line.lower() or "done" in line.lower() or "set!" in line.lower():
            color = "var(--green)"
        elif "failed" in line.lower() or "error" in line.lower():
            color = "var(--red)"
        elif "waiting" in line.lower() or "warning" in line.lower():
            color = "var(--yellow)"
        else:
            color = "var(--fg)"
        html += f'<div style="color:{color};padding:2px 0">{escape(line)}</div>'
    html += '</div>'
    return html
