"""Campaigns — CRUD, start/stop, live stats."""
import re
import json
import time
import random
import logging
import threading
from html import escape
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger("trans.campaigns")
router = APIRouter()


# ── Image-Mode Diagnostics ─────────────────────────────
# Pro Kampagne wird gezählt welcher Image-Mode wie oft gewählt wurde und
# wie oft ein gewählter Mode auf CID zurückfallen musste (leerer Pool).
# Read via /campaigns/{cid}/image-stats — hilft bei Debugging warum
# "alle mails per URL" oder "kein CDN" trotz Meta-Rotation.
_image_mode_stats: dict = {}
_image_mode_stats_lock = threading.Lock()

# Ring-Buffer der letzten 20 Subjects pro Kampagne (raw aus dem Pool +
# final nach Macro/Spintax). Sofort erkennbar ob was gecuttet wird.
from collections import deque as _deque
_subject_samples: dict = {}
_subject_samples_lock = threading.Lock()


def _track_subject(campaign_id: int, raw: str, final: str):
    with _subject_samples_lock:
        dq = _subject_samples.get(campaign_id)
        if dq is None:
            dq = _deque(maxlen=20)
            _subject_samples[campaign_id] = dq
        dq.append((raw, final))


def _track_image_mode(campaign_id: int, chosen: str, actual: str,
                       ctrl=None, cdn_pool_size: int = 0,
                       variant_count: int = 0):
    # Snapshot AUSSERHALB des globalen locks holen — er nimmt selber
    # mehrere ProviderHealth-Locks und war beim ersten Rollout die
    # Contention-Quelle die 70+ Worker blockiert hat.
    snap = None
    if ctrl is not None:
        try:
            snap = ctrl.snapshot()
        except Exception:
            snap = None
    with _image_mode_stats_lock:
        d = _image_mode_stats.setdefault(campaign_id, {
            "chosen": {}, "actual": {}, "fallback": 0, "total": 0,
            "last_ctrl": None, "cdn_pool_size": 0, "variant_count": 0,
            "started_at": time.time(),
        })
        d["chosen"][chosen] = d["chosen"].get(chosen, 0) + 1
        d["actual"][actual] = d["actual"].get(actual, 0) + 1
        if chosen != actual:
            d["fallback"] += 1
        d["total"] += 1
        d["cdn_pool_size"] = cdn_pool_size
        d["variant_count"] = variant_count
        if snap is not None:
            d["last_ctrl"] = snap


def _reset_image_stats(campaign_id: int):
    with _image_mode_stats_lock:
        _image_mode_stats.pop(campaign_id, None)
    with _subject_samples_lock:
        _subject_samples.pop(campaign_id, None)


def _append_ref(url: str, email: str, style: str = "base64") -> str:
    """Hänge einen Ref-Parameter an einen Redirect-Link.
    style:
      * 'none'   → keine Änderung
      * 'base64' → ?ref=<base64url(email)> (Default, wie bisher)
      * 'random' → ?ref=<8-char random> — kein Email-Bezug
      * 'email'  → ?u=<email> — cleartext (nur wenn Empfänger es OK ist)
      * 'utm'    → ?utm_source=nl&utm_id=<random>
    """
    if not url or not email or style == "none":
        return url
    import base64
    import secrets
    sep = "&" if "?" in url else "?"
    if style == "base64":
        token = base64.urlsafe_b64encode(email.encode("utf-8")).rstrip(b"=").decode("ascii")
        return f"{url}{sep}ref={token}"
    if style == "random":
        return f"{url}{sep}ref={secrets.token_urlsafe(6)}"
    if style == "email":
        from urllib.parse import quote
        return f"{url}{sep}u={quote(email)}"
    if style == "utm":
        return f"{url}{sep}utm_source=nl&utm_id={secrets.token_urlsafe(6)}"
    return url


def _parse_pools(text: str) -> list:
    """Textarea-Format: Pools durch Leerzeile getrennt, Zeilen innerhalb
    eines Pools = die Werte.
        Betreff A1
        Betreff A2

        Betreff B1
        Betreff B2
        Betreff B3
    → [['Betreff A1','Betreff A2'], ['Betreff B1','Betreff B2','Betreff B3']]
    """
    if not text or not text.strip():
        return []
    pools = []
    current = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            if current:
                pools.append(current)
                current = []
        else:
            current.append(line)
    if current:
        pools.append(current)
    return pools


def _parse_csv_list(text: str) -> list:
    if not text or not text.strip():
        return []
    return [x.strip() for x in text.replace("\n", ",").split(",") if x.strip()]


_runners = {}
_speed = {}


def _enrich(db, cd):
    cd["running"] = cd["id"] in _runners
    cd["speed"] = _speed.get(cd["id"], 0)
    total = cd.get("total_leads", 0) or 0
    sent = cd.get("sent", 0) or 0
    failed = cd.get("failed", 0) or 0
    # Live-Zähler aus trans_leads — sent-Spalte in trans_campaigns wird
    # nur alle 20 Sends persistiert, hinkt deshalb hinter dem echten
    # Stand hinterher. Der COUNT ist billig (Index auf state).
    lid = cd.get("lead_list_id", 0) or 0
    if lid and cd["running"]:
        try:
            states = db.get_lead_states(lid)
            sent = int(states.get("SENT", 0)) or sent
            failed = int(states.get("FAILED", 0)) or failed
            cd["sent"] = sent
            cd["failed"] = failed
        except Exception:
            pass
    cd["pct"] = int(sent / total * 100) if total > 0 else 0
    cd["remaining"] = max(0, total - sent)
    try:
        sl = db._conn().execute("SELECT name FROM trans_smtp_lists WHERE id=?",
                                 (cd.get("smtp_list_id", 0),)).fetchone()
        cd["smtp_list_name"] = sl["name"] if sl else "—"
    except Exception:
        cd["smtp_list_name"] = "—"
    try:
        ll = db._conn().execute("SELECT name FROM trans_lead_lists WHERE id=?",
                                 (cd.get("lead_list_id", 0),)).fetchone()
        cd["lead_list_name"] = ll["name"] if ll else "—"
    except Exception:
        cd["lead_list_name"] = "—"
    try:
        cd["smtp_list_count"] = db.get_smtp_count(cd.get("smtp_list_id", 0))
    except Exception:
        cd["smtp_list_count"] = 0
    try:
        cd["lead_list_count"] = db.get_lead_count(cd.get("lead_list_id", 0))
    except Exception:
        cd["lead_list_count"] = 0
    return cd


@router.get("/campaigns", response_class=HTMLResponse)
async def campaigns_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    campaigns = [_enrich(db, dict(c)) for c in db.get_campaigns(uid)]
    smtp_lists = [dict(sl, count=db.get_smtp_count(sl["id"])) for sl in db.get_smtp_lists(uid)]
    lead_lists = [dict(ll, count=db.get_lead_count(ll["id"])) for ll in db.get_lead_lists(uid)]
    pools = [dict(p, stats=db.pool_stats(p["id"])) for p in db.get_pools(uid)]
    templates = [dict(t) for t in db.get_templates(uid)]
    redirect_pools = [dict(p, count=db.get_redirect_pool_count(p["id"])) for p in db.get_redirect_pools(uid)]
    logo_groups = [dict(g) for g in db.get_logo_groups(uid)]
    return request.app.state.templates.TemplateResponse(request, "campaigns.html", {
        "active": "campaigns", "campaigns": campaigns,
        "smtp_lists": smtp_lists, "lead_lists": lead_lists,
        "pools": pools, "templates": templates,
        "redirect_pools": redirect_pools,
        "logo_groups": logo_groups, "db": db,
    })


@router.post("/campaigns/add")
async def add_campaign(request: Request,
                       name: str = Form(""),
                       smtp_list_id: int = Form(0),
                       lead_list_id: int = Form(0),
                       pool_id: int = Form(0),
                       pool_count: int = Form(0)):
    db = request.app.state.db
    uid = request.state.user['id']

    if not smtp_list_id:
        return RedirectResponse("/campaigns", status_code=303)

    # Pool mode: reserve N leads from pool into a temp lead list
    if pool_id and pool_count > 0:
        stats = db.pool_stats(pool_id)
        available = stats["pending"]
        take = min(pool_count, available)
        if take == 0:
            return RedirectResponse("/campaigns", status_code=303)
        # Create a temp lead list from pool leads
        pool_row = db._conn().execute("SELECT name FROM trans_lead_lists WHERE id=?", (pool_id,)).fetchone()
        pool_name = pool_row["name"] if pool_row else "Pool"
        temp_list_id = db.create_lead_list(
            f"{pool_name} ({take:,} leads)", "", uid)
        # Copy next N pending leads from pool to temp list
        c = db._conn()
        pending = c.execute(
            "SELECT id, email FROM trans_leads WHERE list_id=? AND state='PENDING' ORDER BY id LIMIT ?",
            (pool_id, take)).fetchall()
        batch = [(temp_list_id, r["email"]) for r in pending]
        c.executemany("INSERT INTO trans_leads (list_id,email,state) VALUES (?,?,'PENDING')", batch)
        # Mark pool leads as USED
        ids = [r["id"] for r in pending]
        ph = ",".join("?" for _ in ids)
        c.execute(f"UPDATE trans_leads SET state='USED' WHERE id IN ({ph})", ids)
        c.execute("UPDATE trans_lead_lists SET lead_count=? WHERE id=?", (take, temp_list_id))
        c.commit()
        lead_list_id = temp_list_id
        total = take
    elif lead_list_id:
        total = db.get_lead_count(lead_list_id)
    else:
        return RedirectResponse("/campaigns", status_code=303)

    db.create_campaign(
        name=name.strip() or f"Campaign {time.strftime('%Y-%m-%d %H:%M')}",
        smtp_list_id=smtp_list_id, lead_list_id=lead_list_id,
        total_leads=total, user_id=uid)
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/campaigns/{cid}/save")
async def save_campaign(request: Request, cid: int,
                        name: str = Form(""),
                        smtp_list_id: int = Form(0),
                        lead_list_id: int = Form(0),
                        template_id: int = Form(0),
                        redirect_pool_id: int = Form(0),
                        logo_group_id: int = Form(-1),
                        schedule_time: str = Form(""),
                        assembly_mode_enabled: int = Form(0),
                        antifp_passthrough_rate: float = Form(0.02),
                        antifp_light_rate: float = Form(0.10),
                        live_html_gen_enabled: int = Form(0),
                        htmlgen_extra_randomize: int = Form(0),
                        live_primary_color: str = Form(""),
                        live_accent_color: str = Form(""),
                        rotate_subject_pools: str = Form(""),
                        rotate_from_name_pools: str = Form(""),
                        rotate_image_modes: str = Form(""),
                        rotate_link_ref_styles: str = Form(""),
                        auto_refresh_enabled: int = Form(0),
                        auto_refresh_every: int = Form(100000),
                        auto_refresh_variants: int = Form(1000),
                        auto_refresh_cid_weight: float = Form(3.0)):
    db = request.app.state.db
    updates = {"name": name.strip(), "schedule_time": schedule_time.strip(),
               "template_id": template_id, "redirect_pool_id": redirect_pool_id,
               "assembly_mode_enabled": 1 if assembly_mode_enabled else 0,
               "antifp_passthrough_rate": max(0.0, min(1.0, antifp_passthrough_rate)),
               "antifp_light_rate": max(0.0, min(1.0, antifp_light_rate)),
               "live_html_gen_enabled": 1 if live_html_gen_enabled else 0,
               "htmlgen_extra_randomize": 1 if htmlgen_extra_randomize else 0,
               "live_primary_color": live_primary_color.strip(),
               "live_accent_color": live_accent_color.strip(),
               "rotate_subject_pools": rotate_subject_pools,
               "rotate_from_name_pools": rotate_from_name_pools,
               "rotate_image_modes": rotate_image_modes.strip(),
               "rotate_link_ref_styles": rotate_link_ref_styles.strip(),
               "auto_refresh_enabled": 1 if auto_refresh_enabled else 0,
               "auto_refresh_every": max(0, int(auto_refresh_every or 0)),
               "auto_refresh_variants": max(1, int(auto_refresh_variants or 1)),
               "auto_refresh_cid_weight": max(0.0, float(auto_refresh_cid_weight or 0))}
    # -1 = "inherit from template" (Default). >=0 = expliziter Override.
    if logo_group_id >= 0:
        updates["logo_group_id"] = int(logo_group_id)
    if smtp_list_id:
        updates["smtp_list_id"] = smtp_list_id
    if lead_list_id:
        updates["lead_list_id"] = lead_list_id
        updates["total_leads"] = db.get_lead_count(lead_list_id)
    db.update_campaign(cid, **updates)
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/campaigns/{cid}/reset")
async def reset_campaign(request: Request, cid: int):
    if cid in _runners:
        return RedirectResponse("/campaigns", status_code=303)
    db = request.app.state.db
    camp = db.get_campaign(cid)
    if camp:
        db.reset_leads(camp["lead_list_id"])
        db.update_campaign(cid, sent=0, failed=0, status="DRAFT")
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/campaigns/{cid}/preflight", response_class=HTMLResponse)
async def preflight_check(request: Request, cid: int):
    """Run all pre-flight checks and show Start button only if passed."""
    db = request.app.state.db
    uid = request.state.user['id']
    camp = db.get_campaign(cid)
    if not camp:
        return HTMLResponse('<div class="alert alert-danger">Campaign not found.</div>')
    camp = dict(camp)
    cfg = db.get_config()
    log = []
    all_ok = True

    # 0. Check proxy
    proxy_val = cfg.get("proxy_value", "").strip()
    if proxy_val:
        log.append(f'<span style="color:var(--green)">&#10003; Proxy active: {escape(proxy_val.splitlines()[0][:40])}</span>')
    else:
        log.append('<span style="color:var(--red);font-weight:600">&#9888; NO PROXY — your server IP will be exposed in email headers!</span>')
        all_ok = False

    # 1. Check logos
    if cfg.get("image_enabled"):
        import glob, os
        from .logos import VARIANT_DIR, UPLOAD_DIR
        variants = glob.glob(os.path.join(VARIANT_DIR, "*"))
        sources = glob.glob(os.path.join(UPLOAD_DIR, "*"))
        if variants:
            rotate = cfg.get("logo_rotate_every", 0)
            log.append(f'<span style="color:var(--green)">&#10003; {len(variants)} logo variants ready (rotate every {rotate or "send"})</span>')
        elif sources:
            log.append(f'<span style="color:var(--yellow)">&#9888; {len(sources)} source logos but no variants generated. <a href="/logos" style="color:var(--accent)">Generate now</a></span>')
        else:
            log.append('<span style="color:var(--red)">&#10007; No logos uploaded. <a href="/logos" style="color:var(--accent)">Upload</a></span>')
            all_ok = False
    else:
        log.append('<span style="color:var(--fg2)">Logo embedding disabled</span>')

    # 2. Check redirects
    if cfg.get("redirect_enabled"):
        count = db.get_redirect_count(uid)
        if count > 0:
            log.append(f'<span style="color:var(--green)">&#10003; {count} redirect links in pool</span>')
        else:
            log.append('<span style="color:var(--red)">&#10007; No redirect links. <a href="/redirects" style="color:var(--accent)">Generate</a></span>')
            all_ok = False
    else:
        log.append('<span style="color:var(--fg2)">Redirect links disabled</span>')

    # 3. Check templates
    html_count = len(db.get_all_template_htmls(uid))
    if html_count > 0:
        log.append(f'<span style="color:var(--green)">&#10003; {html_count} HTML templates loaded</span>')
    else:
        log.append('<span style="color:var(--red)">&#10007; No HTML templates. <a href="/templates" style="color:var(--accent)">Add</a></span>')
        all_ok = False

    # 4. Send test mail (with retry)
    test_recips = cfg.get("test_recipients", "")
    if test_recips.strip():
        recip = test_recips.split(",")[0].strip()
        smtp_list_id = camp.get("smtp_list_id", 0)
        smtps = [dict(s) for s in db.get_smtps(smtp_list_id)] if smtp_list_id else []
        if smtps:
            import smtplib as _sl, ssl as _ssl
            from transactional.web.routes.smtps import _connect_smtp
            proxy_str = (cfg.get("proxy_value", "").splitlines()[0].strip()
                         if cfg.get("proxy_value", "").strip() else "")
            s = smtps[0]
            test_ok = False
            for attempt in range(3):
                server, error, _ = _connect_smtp(s["host"], s["port"], s["username"], s["password"], proxy_str)
                if server:
                    try:
                        from email.mime.text import MIMEText
                        msg = MIMEText("Pre-flight test", "plain", "utf-8")
                        msg["From"] = s["username"]
                        msg["To"] = recip
                        msg["Subject"] = "[PRE-FLIGHT] Test"
                        server.send_message(msg)
                        server.quit()
                        test_ok = True
                        break
                    except Exception:
                        pass
                import time; time.sleep(2)
            if test_ok:
                log.append(f'<span style="color:var(--green)">&#10003; Test mail sent to {escape(recip)}</span>')
            else:
                log.append(f'<span style="color:var(--red)">&#10007; Test mail failed after 3 attempts</span>')
                all_ok = False
        else:
            log.append('<span style="color:var(--red)">&#10007; No SMTPs in campaign list</span>')
            all_ok = False
    else:
        log.append('<span style="color:var(--fg2)">No test recipients configured</span>')

    html = '<div style="font-size:13px;line-height:2">' + '<br>'.join(log) + '</div>'
    if all_ok:
        html += (f'<div style="margin-top:14px">'
                 f'<form method="post" action="/campaigns/{cid}/start">'
                 f'<button class="btn btn-success" style="padding:12px 24px;font-size:15px">'
                 f'&#9654; Start Mailing</button></form></div>')
    else:
        html += '<div style="margin-top:10px" class="alert alert-warning">Fix issues above before starting.</div>'
    return HTMLResponse(html)


@router.post("/campaigns/{cid}/start")
async def start_campaign(request: Request, cid: int):
    db = request.app.state.db
    if cid in _runners:
        return RedirectResponse("/campaigns", status_code=303)
    _speed[cid] = 0

    def run():
        try:
            _run_campaign(db, cid)
        except Exception as e:
            logger.error("Campaign %d CRASHED: %s", cid, e, exc_info=True)
            db.update_campaign(cid, status="FAILED")
        finally:
            _runners.pop(cid, None)
            _speed.pop(cid, None)

    t = threading.Thread(target=run, daemon=True)
    _runners[cid] = t
    t.start()
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/campaigns/{cid}/stop")
async def stop_campaign(request: Request, cid: int):
    _runners.pop(cid, None)
    request.app.state.db.update_campaign(cid, status="PAUSED")
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/campaigns/{cid}/resend-failed")
async def resend_failed(request: Request, cid: int):
    """Reset failed leads to PENDING, then start campaign."""
    db = request.app.state.db
    camp = db.get_campaign(cid)
    if not camp:
        return RedirectResponse("/campaigns", status_code=303)
    camp = dict(camp)
    lead_list_id = camp.get("lead_list_id", 0)
    if lead_list_id:
        count = db._conn().execute(
            "SELECT COUNT(*) FROM trans_leads WHERE list_id=? AND state='FAILED'",
            (lead_list_id,)).fetchone()[0]
        if count > 0:
            db._conn().execute(
                "UPDATE trans_leads SET state='PENDING' WHERE list_id=? AND state='FAILED'",
                (lead_list_id,))
            db._conn().commit()
            logger.info("Campaign %d: reset %d failed leads to PENDING", cid, count)
    if cid not in _runners:
        _speed[cid] = 0
        def run():
            try:
                _run_campaign(db, cid)
            except Exception as e:
                logger.error("Campaign %d CRASHED: %s", cid, e, exc_info=True)
                db.update_campaign(cid, status="FAILED")
            finally:
                _runners.pop(cid, None)
                _speed.pop(cid, None)
        t = threading.Thread(target=run, daemon=True)
        _runners[cid] = t
        t.start()
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/campaigns/{cid}/test-send", response_class=HTMLResponse)
async def test_send(request: Request, cid: int):
    """Send test email using real templates + macros + antifingerprint."""
    db = request.app.state.db
    cfg = db.get_config()
    recipients_raw = cfg.get("test_recipients", "")
    if not recipients_raw.strip():
        return HTMLResponse('<div class="alert alert-warning">No test recipients in Config.</div>')

    uid = request.state.user['id']
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    camp = db.get_campaign(cid)
    if not camp:
        return HTMLResponse('<div class="alert alert-danger">Campaign not found.</div>')
    camp = dict(camp)

    smtp_list_id = camp.get("smtp_list_id", 0)
    smtps = [dict(s) for s in db.get_smtps(smtp_list_id)] if smtp_list_id else []
    if not smtps:
        return HTMLResponse('<div class="alert alert-danger">No SMTPs in selected list.</div>')

    html_bodies = db.get_all_template_htmls(uid)
    if not html_bodies:
        return HTMLResponse('<div class="alert alert-danger">No HTML templates. Add on HTML Editor page.</div>')

    macros = {}
    sticky_macros = set()
    # Test-Send nutzt jetzt die gleiche Sticky-Logik wie der echte Send-
    # Loop: mehrere aktive Presets pro Name werden vereint, sticky wenn
    # IRGENDEIN Preset sticky ist. Sonst würde der Test-Send anderes
    # Verhalten zeigen als die echte Kampagne und der User denkt sticky
    # klemmt bei manchen Macros.
    for m in db.get_active_macros(uid):
        md = dict(m)
        lines = [l.strip() for l in (md.get("values_text") or "").splitlines() if l.strip()]
        if not lines:
            continue
        name = md["name"]
        if name in macros:
            seen = set(macros[name])
            macros[name].extend(x for x in lines if x not in seen)
        else:
            macros[name] = list(lines)
        if md.get("sticky"):
            sticky_macros.add(name)

    from_name_cfg = cfg.get("from_name", "") or "Test"
    from_email_cfg = cfg.get("from_email", "")
    subject_cfg = cfg.get("subject", "") or "Test"

    import random, smtplib, ssl

    def _process_vars(text, email, sticky_cache=None):
        user = email.split("@")[0] if "@" in email else email
        domain = email.split("@")[1] if "@" in email else ""
        text = text.replace("{email}", email).replace("{email_user}", user).replace("{domain}", domain)
        # Multi-Pass + sticky_cache: gleich wie im echten Send-Loop
        for _pass in range(5):
            changed = False
            for mname, mlines in macros.items():
                token = f"{{{mname}}}"
                if token not in text:
                    continue
                changed = True
                if mname in sticky_macros:
                    cache = sticky_cache if sticky_cache is not None else {}
                    if mname not in cache:
                        cache[mname] = random.choice(mlines)
                    text = text.replace(token, cache[mname])
                else:
                    while token in text:
                        text = text.replace(token, random.choice(mlines), 1)
            if not changed:
                break
        def _spintax(m):
            return random.choice(m.group(1).split("|"))
        for _ in range(20):
            new = re.sub(r"\{([^{}]+\|[^{}]+)\}", _spintax, text)
            if new == text:
                break
            text = new
        return text

    afp = None
    if cfg.get("advanced_antifingerprint"):
        from mailer.advanced_antifingerprint import AdvancedAntiFingerprintEngine
        afp = AdvancedAntiFingerprintEngine(
            enable_classes=cfg.get("antifingerprint_classes", True),
            structure_variation=cfg.get("structure_variation", 0.5))
    elif cfg.get("antifingerprint_classes"):
        from mailer.antifingerprint import AntiFingerprintEngine
        afp = AntiFingerprintEngine(enable_classes=True)

    # Load redirects + logos for test
    redirect_links = [dict(r)["short_url"] for r in db.get_redirects(uid)] if cfg.get("redirect_enabled") else []

    import glob, os
    logo_files = []
    if cfg.get("image_enabled"):
        from .logos import VARIANT_DIR, UPLOAD_DIR
        logo_files = sorted(glob.glob(os.path.join(VARIANT_DIR, "*")))
        if not logo_files:
            logo_files = sorted(glob.glob(os.path.join(UPLOAD_DIR, "*")))

    results = []
    s = random.choice(smtps)
    from_email = from_email_cfg or s["username"]

    for idx, recipient in enumerate(recipients):
        try:
            # Sticky-Cache pro Empfänger — gleiches Verhalten wie im
            # echten Send-Loop. Sonst kriegt jeder _process_vars-Call
            # einen neuen Wert und man sieht kein sticky.
            sticky_cache = {}
            html = random.choice(html_bodies)
            html = _process_vars(html, recipient, sticky_cache)

            if redirect_links and "{RedirectLink}" in html:
                link = redirect_links[idx % len(redirect_links)]
                if cfg.get("redirect_append_ref"):
                    link = _append_ref(link, recipient)
                html = html.replace("{RedirectLink}", link)

            inline_images = None
            if logo_files and "{Logo}" in html:
                logo_path = random.choice(logo_files)
                try:
                    import mimetypes as mt, secrets as _sec
                    mime_type = mt.guess_type(logo_path)[0] or "image/png"
                    with open(logo_path, "rb") as lf:
                        logo_bytes = lf.read()
                    try:
                        from mailer.image_jitter import (
                            jitter_image_bytes, random_img_tag,
                        )
                        logo_bytes, mime_type = jitter_image_bytes(
                            logo_bytes, mime_type)
                        cid_local = _sec.token_hex(8)
                        cid = f"{cid_local}@{from_email.split('@')[1] if '@' in from_email else 'mail'}"
                        html = html.replace("{Logo}", random_img_tag(cid))
                    except Exception:
                        cid_local = _sec.token_hex(8)
                        cid = f"{cid_local}@{from_email.split('@')[1] if '@' in from_email else 'mail'}"
                        html = html.replace("{Logo}", f'<img src="cid:{cid}" alt="Logo" style="display:block;border:0;max-height:50px;width:auto;">')
                    inline_images = [(logo_bytes, cid, mime_type)]
                except Exception:
                    html = html.replace("{Logo}", "")
            elif "{Logo}" in html:
                if cfg.get("image_mode") == "static_url":
                    static_logo = cfg.get("logo_static_url", "").strip()
                    if static_logo:
                        html = html.replace("{Logo}",
                            f'<img src="{static_logo}" alt="Logo" style="display:block;border:0;max-height:50px;width:auto;">')
                    else:
                        html = html.replace("{Logo}", "")
                else:
                    html = html.replace("{Logo}", "")

            if afp:
                html = afp.transform(html)
            plain = re.sub(r"<[^>]+>", "", html).strip()
            cur_subject = _process_vars(f"[TEST] {subject_cfg}", recipient, sticky_cache)
            cur_from = _process_vars(from_name_cfg, recipient, sticky_cache)

            from mailer.mime_builder import MIMEBuilder
            raw_msg = MIMEBuilder.build_email(
                from_name=cur_from, from_email=from_email,
                to_email=recipient, subject=cur_subject,
                html_body=html, plain_body=plain,
                inline_images=inline_images)

            proxy_str = ""
            pv = cfg.get("proxy_value", "").strip()
            if pv:
                proxy_str = pv.splitlines()[0].strip()

            from transactional.web.routes.smtps import _connect_smtp
            server, error, _ = _connect_smtp(s["host"], s["port"], s["username"], s["password"], proxy_str)
            if server:
                server.sendmail(from_email, [recipient], raw_msg)
                server.quit()
                results.append(f'<div style="color:var(--green);font-size:13px">&#10003; Sent to {escape(recipient)}</div>')
            else:
                results.append(f'<div style="color:var(--red);font-size:13px">&#10007; {escape(recipient)}: {escape(error[:100])}</div>')
        except Exception as e:
            results.append(f'<div style="color:var(--red);font-size:13px">&#10007; {escape(recipient)}: {escape(str(e)[:100])}</div>')

    return HTMLResponse("".join(results))


@router.post("/campaigns/{cid}/delete")
async def delete_campaign(request: Request, cid: int):
    _runners.pop(cid, None)
    request.app.state.db.delete_campaign(cid)
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/campaigns/{cid}/check-proxy", response_class=HTMLResponse)
async def check_proxy(request: Request, cid: int):
    """Pre-flight proxy connectivity test."""
    db = request.app.state.db
    cfg = db.get_config()
    proxy_value = cfg.get("proxy_value", "").strip()
    if not proxy_value:
        return HTMLResponse('<div class="alert alert-warning">No proxy configured. Set one on the Proxies page.</div>')
    first_proxy = proxy_value.splitlines()[0].strip()
    try:
        import socks
        p = first_proxy.replace("socks5://", "").replace("socks://", "")
        parts = p.split(":")
        if len(parts) < 2:
            return HTMLResponse(f'<div class="alert alert-danger">Invalid proxy format: {escape(first_proxy[:40])}</div>')
        s = socks.socksocket()
        host, port = parts[0], int(parts[1])
        user = parts[2] if len(parts) > 2 else ""
        pwd = parts[3] if len(parts) > 3 else ""
        s.set_proxy(socks.SOCKS5, host, port, username=user or None, password=pwd or None)
        s.settimeout(10)
        s.connect(("api.ipify.org", 80))
        s.sendall(b"GET / HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n")
        resp = s.recv(4096).decode()
        s.close()
        ip = resp.split("\r\n\r\n")[-1].strip() if "\r\n\r\n" in resp else "?"
        return HTMLResponse(f'<div class="alert alert-success">Proxy OK — egress IP: <strong>{escape(ip)}</strong></div>')
    except ImportError:
        return HTMLResponse('<div class="alert alert-danger">PySocks not installed</div>')
    except Exception as e:
        return HTMLResponse(f'<div class="alert alert-danger">Proxy failed: {escape(str(e)[:100])}</div>')


@router.post("/campaigns/{cid}/check-blacklist", response_class=HTMLResponse)
async def check_blacklist(request: Request, cid: int):
    """MXToolbox blacklist check."""
    db = request.app.state.db
    cfg = db.get_config()
    api_key = cfg.get("mxtoolbox_api_key", "")
    if not api_key:
        return HTMLResponse('<div class="alert alert-warning">No MXToolbox API key in Config.</div>')
    try:
        from mailer.blacklist_checker import BlacklistChecker
        from mailer.smtp_worker import ProxyConfig
        checker = BlacklistChecker(api_key)
        proxy_val = cfg.get("proxy_value", "").strip()
        proxy_objs = None
        if proxy_val:
            proxy_objs = []
            for line in proxy_val.splitlines()[:5]:
                p = ProxyConfig.parse(line.strip())
                if p:
                    proxy_objs.append(p)
        results = checker.check_sending_ips(proxy_objs or None)
        html_parts = []
        for label, info in results.items():
            if info["clean"]:
                html_parts.append(f'<span style="color:var(--green)">&#10003; {escape(label)} clean</span>')
            else:
                count = len(info.get("details", []))
                html_parts.append(f'<span style="color:var(--red)">&#10007; {escape(label)} listed on {count} blacklists</span>')
        return HTMLResponse('<div style="display:flex;flex-wrap:wrap;gap:12px">' + " ".join(html_parts) + '</div>')
    except Exception as e:
        return HTMLResponse(f'<div class="alert alert-danger">{escape(str(e)[:100])}</div>')


@router.get("/campaigns/{cid}/stats", response_class=HTMLResponse)
async def campaign_stats(request: Request, cid: int):
    db = request.app.state.db
    camp = db.get_campaign(cid)
    if not camp:
        return HTMLResponse("<div>Not found</div>")
    cd = dict(camp)
    total = cd.get("total_leads", 0) or 0
    # Live-Zähler direkt aus trans_leads statt aus trans_campaigns.sent —
    # der wird nur alle 20 Sends persistiert und läuft dem echten Stand
    # deshalb sichtbar hinterher.
    lead_list_id = cd.get("lead_list_id", 0) or 0
    sent = cd.get("sent", 0) or 0
    failed = cd.get("failed", 0) or 0
    if lead_list_id:
        try:
            states = db.get_lead_states(lead_list_id)
            sent = int(states.get("SENT", 0)) or sent
            failed = int(states.get("FAILED", 0)) or failed
        except Exception:
            pass
    remaining = max(0, total - sent)
    pct = int(sent / total * 100) if total > 0 else 0
    running = cid in _runners
    speed = _speed.get(cid, 0)
    speed_str = f"{speed:,}/h" if speed else "—"
    status = cd.get("status", "DRAFT")

    elapsed_str = "—"
    eta_str = "—"
    if cd.get("started_at") and running:
        from datetime import datetime
        try:
            started = datetime.fromisoformat(cd["started_at"])
            elapsed_sec = (datetime.now() - started).total_seconds()
            elapsed_h = int(elapsed_sec // 3600)
            elapsed_m = int((elapsed_sec % 3600) // 60)
            elapsed_str = f"{elapsed_h}h {elapsed_m}m"
            if speed > 0 and remaining > 0:
                eta_sec = remaining / speed * 3600
                eta_h = int(eta_sec // 3600)
                eta_m = int((eta_sec % 3600) // 60)
                eta_str = f"{eta_h}h {eta_m}m"
        except Exception:
            pass

    return HTMLResponse(f"""
    <div class="grid-4" style="margin-bottom:12px">
        <div class="metric"><div class="value" style="color:var(--green)">{sent:,}</div><div class="label">Sent</div></div>
        <div class="metric"><div class="value" style="color:var(--red)">{failed:,}</div><div class="label">Failed</div></div>
        <div class="metric"><div class="value">{remaining:,}</div><div class="label">Remaining</div></div>
        <div class="metric"><div class="value">{speed_str}</div><div class="label">Speed</div></div>
    </div>
    <div class="progress"><div class="progress-bar" style="width:{pct}%">{pct}%</div></div>
    <p style="margin-top:8px;font-size:12px;color:var(--fg2)">
        Status: <span class="badge badge-{'running' if running else status.lower()}">{status}</span>
        &nbsp; Elapsed: {elapsed_str} &nbsp; ETA: {eta_str}
    </p>""")


@router.get("/campaigns/{cid}/image-stats", response_class=HTMLResponse)
async def campaign_image_stats(request: Request, cid: int):
    """Live-Diagnose: welchen Image-Mode wählt diese Kampagne aktuell?
    Zeigt Counter (chosen vs actual — actual weicht ab wenn Fallback auf
    CID greift wegen leerem Pool), Health-Scores der Provider, Weights,
    letzten Refresh-Report, CDN-Pool-Größe, Variant-Count. Poll-Ziel für
    ein Details-Panel auf der Campaign-Seite."""
    db = request.app.state.db
    camp = db.get_campaign(cid)
    if not camp:
        return HTMLResponse('<div class="alert alert-warning">Kampagne nicht gefunden.</div>')
    cd = dict(camp)
    cfg = db.get_config()

    with _image_mode_stats_lock:
        stats = _image_mode_stats.get(cid)
        stats = dict(stats) if stats else None
        if stats:
            stats["chosen"] = dict(stats["chosen"])
            stats["actual"] = dict(stats["actual"])

    rotate_modes = [m for m in _parse_csv_list(cd.get("rotate_image_modes") or "")
                     if m in ("cid", "cloudinary", "cdn", "url", "static_url", "text")]
    fallback_mode = cfg.get("image_mode", "cid")

    lines = ['<div style="font-size:12px">']
    lines.append(f'<div><strong>Config-Fallback</strong> (wenn Meta-Rotation aus): '
                  f'<code>{escape(fallback_mode)}</code></div>')
    if rotate_modes:
        lines.append(f'<div><strong>Meta-Rotation aktiv</strong>: <code>{escape(", ".join(rotate_modes))}</code></div>')
    else:
        lines.append('<div style="color:var(--fg2)"><strong>Meta-Rotation:</strong> aus — es wird immer der Config-Fallback benutzt.</div>')

    if not stats or stats["total"] == 0:
        lines.append('<div style="margin-top:8px;color:var(--fg2)">'
                      'Noch keine Mails in dieser Kampagne verschickt seit dem letzten Serverstart — '
                      'sobald Sends passieren, siehst du hier live welcher Mode wie oft gewählt wurde.</div>')
        lines.append('</div>')
        return HTMLResponse("".join(lines))

    total = stats["total"]
    lines.append(f'<div style="margin-top:8px"><strong>Gesamt getrackt</strong>: {total:,} Sends '
                  f'· <strong>Fallbacks</strong>: {stats["fallback"]:,} '
                  f'({stats["fallback"] * 100 // max(total,1)}%)</div>')

    def _bar(counts):
        rows = []
        for mode in sorted(counts, key=lambda k: -counts[k]):
            n = counts[mode]
            pct = n * 100 // max(total, 1)
            rows.append(
                f'<tr><td style="padding:2px 8px 2px 0"><code>{escape(mode)}</code></td>'
                f'<td style="padding:2px 8px 2px 0">{n:,}</td>'
                f'<td style="padding:2px 8px 2px 0;color:var(--fg2)">{pct}%</td>'
                f'<td style="padding:2px 0;width:120px">'
                f'<div style="background:var(--bg2);border-radius:3px;height:6px;overflow:hidden">'
                f'<div style="width:{pct}%;height:100%;background:var(--accent)"></div></div></td></tr>'
            )
        return f'<table style="border-collapse:collapse;font-size:11px">{"".join(rows)}</table>'

    lines.append('<div style="display:flex;gap:20px;flex-wrap:wrap;margin-top:8px">')
    lines.append(f'<div><div style="font-weight:600">Chosen (vor Fallback)</div>{_bar(stats["chosen"])}</div>')
    lines.append(f'<div><div style="font-weight:600">Actual (nach Fallback)</div>{_bar(stats["actual"])}</div>')
    lines.append('</div>')

    lines.append(f'<div style="margin-top:8px;color:var(--fg2)">'
                  f'CID-Pool aktuell: <strong>{stats["variant_count"]}</strong> Varianten · '
                  f'CDN-Pool aktuell: <strong>{stats["cdn_pool_size"]}</strong> URLs</div>')

    ctrl = stats.get("last_ctrl")
    if ctrl:
        weights = ctrl.get("weights", {})
        health = ctrl.get("health", {})
        lines.append(
            f'<div style="margin-top:8px;padding:6px 8px;background:var(--bg2);'
            f'border-radius:4px;font-family:monospace;font-size:11px">'
            f'<div><strong>Auto-Refresh:</strong> alle {ctrl.get("refresh_every", 0):,} sends · '
            f'{ctrl.get("variants_per_refresh", 0)} Varianten pro Refresh · '
            f'seit letztem Refresh: {ctrl.get("sent_since_refresh", 0):,} · '
            f'Refreshes total: {ctrl.get("refresh_count", 0)}</div>'
            f'<div style="margin-top:4px">Health: '
            f'cid={health.get("cid", {}).get("health", 1):.2f} · '
            f'cloudinary={health.get("cloudinary", {}).get("health", 1):.2f} · '
            f's3={health.get("s3", {}).get("health", 1):.2f}</div>'
            f'<div>Weights: cid={weights.get("cid", 0):.2f} · '
            f'cdn={weights.get("cdn", 0):.2f}</div>'
            f'</div>'
        )
    # Subject-Samples: die letzten Subjects im Klartext, so sieht man
    # sofort ob etwas gecuttet wird oder Macros nicht auflösen.
    with _subject_samples_lock:
        samples = list(_subject_samples.get(cid, []))
    if samples:
        rows = []
        for raw, final in reversed(samples[-15:]):
            cutted = "cut" if len(final) < len(raw) - 5 else ""
            cls = ' style="color:var(--red)"' if cutted else ""
            rows.append(
                f'<tr><td style="padding:2px 8px 2px 0;color:var(--fg2);'
                f'max-width:280px;overflow:hidden;text-overflow:ellipsis;'
                f'white-space:nowrap"{cls}>{escape(raw)}</td>'
                f'<td style="padding:2px 8px 2px 0;max-width:280px;overflow:hidden;'
                f'text-overflow:ellipsis;white-space:nowrap"{cls}>{escape(final)}</td>'
                f'<td style="padding:2px 0;font-size:10px;color:var(--fg3)">'
                f'{len(raw)}→{len(final)}</td></tr>'
            )
        lines.append(
            '<details style="margin-top:8px" open><summary style="cursor:pointer;'
            'font-weight:600;font-size:11px">Letzte Subjects (raw → final)</summary>'
            '<table style="border-collapse:collapse;font-size:10px;margin-top:4px;width:100%">'
            '<thead><tr><th style="text-align:left;color:var(--fg3)">Aus dem Pool</th>'
            '<th style="text-align:left;color:var(--fg3)">Nach Macros/Spintax</th>'
            '<th style="text-align:left;color:var(--fg3)">Len</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></details>'
        )
    lines.append('</div>')
    return HTMLResponse("".join(lines))


@router.post("/campaigns/{cid}/reset-image-stats", response_class=HTMLResponse)
async def reset_image_stats(request: Request, cid: int):
    _reset_image_stats(cid)
    return HTMLResponse('<div class="alert alert-info">Stats zurückgesetzt.</div>')


@router.get("/campaigns/table", response_class=HTMLResponse)
async def campaigns_table(request: Request):
    db = request.app.state.db
    uid = request.state.user['id']
    campaigns = [_enrich(db, dict(c)) for c in db.get_campaigns(uid)]
    html = f'<div class="card-header"><h3>Campaigns <span class="count">{len(campaigns)}</span></h3></div>'
    html += '<table><thead><tr><th>Name</th><th>SMTP</th><th>Leads</th><th>Progress</th><th>Total</th><th>Sent</th><th>Speed</th><th></th></tr></thead><tbody>'
    for c in campaigns:
        bg = ' style="background:var(--green-light)"' if c["running"] else ""
        speed = f'{c["speed"]:,}/h' if c["speed"] else "—"
        start_dis = ' disabled' if c["running"] else ''
        pause_dis = '' if c["running"] else ' disabled'
        btn = (f'<form method="post" action="/campaigns/{c["id"]}/start" style="display:inline">'
               f'<button class="btn btn-success btn-xs"{start_dis}>Start</button></form>'
               f'<form method="post" action="/campaigns/{c["id"]}/stop" style="display:inline">'
               f'<button class="btn btn-danger btn-xs"{pause_dis}>Pause</button></form>')
        html += (f'<tr{bg}><td style="font-weight:500">{c["name"]}</td>'
                 f'<td style="font-size:12px">{c["smtp_list_name"]}</td>'
                 f'<td style="font-size:12px">{c["lead_list_name"]}</td>'
                 f'<td style="min-width:100px"><div class="progress"><div class="progress-bar" style="width:{c["pct"]}%">{c["pct"]}%</div></div></td>'
                 f'<td>{c.get("total_leads",0) or 0:,}</td>'
                 f'<td style="color:var(--green)">{c.get("sent",0) or 0:,}</td>'
                 f'<td>{speed}</td><td style="white-space:nowrap">{btn}</td></tr>')
    html += '</tbody></table>'
    return HTMLResponse(html)


@router.post("/campaigns/{cid}/spam-check", response_class=HTMLResponse)
async def spam_check_campaign(request: Request, cid: int):
    """Build a sample email and check spam score."""
    db = request.app.state.db
    uid = request.state.user["id"]
    cfg = db.get_config()

    html_bodies = db.get_all_template_htmls(uid)
    if not html_bodies:
        return HTMLResponse('<div class="alert alert-warning">No templates to check.</div>')

    html = html_bodies[0]
    html = html.replace("{email}", "test@example.com").replace("{email_user}", "test")
    html = html.replace("{domain}", "example.com").replace("{Logo}", "").replace("{RedirectLink}", "https://example.com")
    plain = re.sub(r"<[^>]+>", "", html).strip()
    from_email = cfg.get("from_email", "") or "test@example.com"

    try:
        from mailer.mime_builder import MIMEBuilder
        raw_msg = MIMEBuilder.build_email(
            from_name=cfg.get("from_name", "Test"), from_email=from_email,
            to_email="check@example.com", subject=cfg.get("subject", "Test"),
            html_body=html, plain_body=plain)
    except Exception as e:
        return HTMLResponse(f'<div class="alert alert-danger">Build: {escape(str(e)[:100])}</div>')

    from .spam_check import check_spam, format_result_html
    result = check_spam(raw_msg, cfg.get("spam_checker", "rspamd"),
                         cfg.get("spam_checker_url", "http://127.0.0.1:11333/checkv2"))
    return HTMLResponse(format_result_html(result))


def _run_campaign(db, cid: int):
    """Execute campaign using mailer core modules + DB config."""
    from mailer.mime_builder import MIMEBuilder
    from mailer.smtp_worker import SMTPPool, SMTPWorker, SendResult
    from mailer.antifingerprint import AntiFingerprintEngine
    from mailer.advanced_antifingerprint import AdvancedAntiFingerprintEngine
    import os, tempfile

    camp = dict(db.get_campaign(cid))
    cfg = db.get_config()
    from datetime import datetime, timedelta

    # Schedule wait
    schedule = camp.get("schedule_time", "")
    if schedule:
        try:
            target_time = datetime.strptime(schedule, "%H:%M").time()
            now = datetime.now()
            target = datetime.combine(now.date(), target_time)
            if target <= now:
                target += timedelta(days=1)
            wait = (target - now).total_seconds()
            logger.info("Campaign %d: scheduled for %s (waiting %ds)", cid, target, int(wait))
            db.update_campaign(cid, status="SCHEDULED")
            while wait > 0 and cid in _runners:
                time.sleep(min(wait, 5))
                wait = (target - datetime.now()).total_seconds()
        except ValueError:
            pass

    if cid not in _runners:
        return

    db.update_campaign(cid, status="RUNNING", started_at=datetime.now().isoformat())

    uid = camp.get("user_id", 0)
    lead_list_id = camp.get("lead_list_id", 0)
    smtp_list_id = camp.get("smtp_list_id", 0)

    if not lead_list_id or not smtp_list_id:
        logger.error("Campaign %d: no SMTP or lead list", cid)
        db.update_campaign(cid, status="FAILED")
        return

    db.reset_in_progress(lead_list_id)
    states = db.get_lead_states(lead_list_id)
    pending = states.get("PENDING", 0)
    failed_leads = states.get("FAILED", 0)
    prev_status = camp.get("status", "DRAFT")
    is_resume = prev_status in ("PAUSED", "FINISHED", "RUNNING")

    if pending == 0 and not is_resume:
        total = sum(states.values())
        if total > 0:
            logger.info("Campaign %d: 0 pending (fresh start), resetting %d leads", cid, total)
            db.reset_leads(lead_list_id)
            pending = total
    elif pending == 0 and is_resume:
        if failed_leads > 0 and cfg.get("auto_retry_failed", True):
            logger.info("Campaign %d: resumed, retrying %d failed leads", cid, failed_leads)
            db._conn().execute(
                "UPDATE trans_leads SET state='PENDING' WHERE list_id=? AND state='FAILED'",
                (lead_list_id,))
            db._conn().commit()
            pending = failed_leads
        else:
            logger.info("Campaign %d: resumed but 0 pending — all leads already processed", cid)
            db.update_campaign(cid, status="FINISHED")
            return

    smtps = [dict(s) for s in db.get_smtps(smtp_list_id)]
    if not smtps:
        db.update_campaign(cid, status="FAILED")
        return

    # Write temp SMTP file for SMTPPool.
    # Zeilenformat: host,port,user,pass,proxy,provider,region,config_set
    smtp_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    for s in smtps:
        if s.get("is_dead"):
            continue
        provider = (s.get("provider_type") or "smtp").strip()
        region = (s.get("ses_region") or "").strip()
        cfg_set = (s.get("ses_config_set") or "").strip()
        smtp_file.write(
            f"{s['host']},{s['port']},{s['username']},{s['password']},"
            f",{provider},{region},{cfg_set}\n")
    smtp_file.close()

    # Write temp proxy file if configured
    proxy_file_path = ""
    proxy_mode = cfg.get("proxy_mode", "off")
    proxy_value = cfg.get("proxy_value", "")
    if proxy_mode != "off" and proxy_value.strip():
        proxy_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        for line in proxy_value.strip().splitlines():
            if line.strip():
                proxy_file.write(line.strip() + "\n")
        proxy_file.close()
        proxy_file_path = proxy_file.name

    try:
        pool = SMTPPool(
            smtp_file.name, timeout=cfg.get("smtp_timeout", 30),
            warmup_delay=cfg.get("warmup_delay", 30.0),
            warmup_count=cfg.get("warmup_count", 5),
            ignore_ssl_errors=cfg.get("ignore_ssl_errors", True),
            proxy_file=proxy_file_path,
            proxy_rotate_every=cfg.get("proxy_rotate_every", 0),
        )
        worker = SMTPWorker(pool,
            normal_delay=cfg.get("normal_delay", 0.3),
            provider_delay=cfg.get("provider_delay", 6.0))

        # Anti-FP-Rates pro Kampagne konfigurierbar
        _afp_pt = float(camp.get("antifp_passthrough_rate", 0.02) or 0.02)
        _afp_light = float(camp.get("antifp_light_rate", 0.10) or 0.10)
        if cfg.get("advanced_antifingerprint"):
            afp = AdvancedAntiFingerprintEngine(
                enable_classes=cfg.get("antifingerprint_classes", True),
                structure_variation=cfg.get("structure_variation", 0.5),
                pass_through_rate=_afp_pt, light_touch_rate=_afp_light)
        elif cfg.get("antifingerprint_classes"):
            afp = AntiFingerprintEngine(enable_classes=True,
                                          pass_through_rate=_afp_pt,
                                          light_touch_rate=_afp_light)
        else:
            afp = None

        # Assembly-Mode: aus Snippet-Pools live pro Send zusammensetzen
        assembly_enabled = bool(camp.get("assembly_mode_enabled"))
        assembly_snippets = {}
        if assembly_enabled:
            from mailer.assembly import group_snippets_by_slot
            assembly_snippets = group_snippets_by_slot(
                db.get_active_snippets(uid))
            if not any(assembly_snippets.values()):
                logger.warning("Campaign %d: assembly-mode enabled but no "
                                "active snippets — falling back to html_bodies", cid)
                assembly_enabled = False
            else:
                logger.info(
                    "Campaign %d: assembly-mode ON — %s",
                    cid, ", ".join(f"{k}:{len(v)}" for k, v in assembly_snippets.items()))

        # Live-HTML-Generation: pro Send ein frisches Template aus der
        # htmlgen-Engine würfeln statt aus vorgeneriertem Pool zu picken.
        # Blocks+Layouts werden einmal geladen und via `_cache` an jeden
        # generate_one() weitergereicht → pro Send <5ms zusätzlich.
        live_html_gen = bool(camp.get("live_html_gen_enabled"))
        _extra_random = bool(camp.get("htmlgen_extra_randomize"))
        htmlgen_cfg = None
        htmlgen_base = None
        htmlgen_cache = None
        htmlgen_generate_one = None
        if live_html_gen:
            try:
                from pathlib import Path as _Path
                from htmlgen.config import load_config as _load_htmlgen_cfg
                from htmlgen.engine import _load_all as _htmlgen_load_all, generate_one as _htmlgen_generate_one
                htmlgen_base = _Path(__file__).resolve().parents[3] / "htmlgen"
                if not htmlgen_base.exists():
                    logger.warning("Campaign %d: htmlgen base not found (%s)",
                                    cid, htmlgen_base)
                    live_html_gen = False
                else:
                    htmlgen_cfg = _load_htmlgen_cfg(htmlgen_base / "config.yaml")
                    # Brand-Farb-Overrides pro Kampagne (leer = Pool-Random)
                    _prim = (camp.get("live_primary_color") or "").strip()
                    _acc = (camp.get("live_accent_color") or "").strip()
                    if _prim:
                        htmlgen_cfg.setdefault("colors", {})["primary"] = [_prim]
                        try:
                            from htmlgen.colors import lighten_color
                            htmlgen_cfg["colors"]["light_accent_bg"] = [
                                lighten_color(_prim,
                                    htmlgen_cfg.get("lighten_amount", 0.85))
                            ]
                        except Exception:
                            pass
                    if _acc:
                        htmlgen_cfg.setdefault("colors", {})["accent"] = [_acc]
                    htmlgen_cache = _htmlgen_load_all(htmlgen_base)
                    htmlgen_generate_one = _htmlgen_generate_one
                    logger.info(
                        "Campaign %d: live-html-gen ON (primary=%s accent=%s extra_random=%s)",
                        cid, _prim or "pool", _acc or "pool",
                        "on" if _extra_random else "off")
            except Exception as e:
                logger.warning("Campaign %d: live-html-gen init failed: %s", cid, e)
                live_html_gen = False

        campaign_template_id = camp.get("template_id", 0) or 0
        # html_bodies + logo_variants are kept as mutable lists so the
        # freshness monitor can swap their contents in-place during the
        # send (workers read via random.choice which sees the swap).
        html_bodies = list(db.get_all_template_htmls(uid, template_id=campaign_template_id))

        # V1 Meta-Rotation — pro Send unabhängig gewürfelt aus mehreren
        # Pools/Optionen. Filter-Clustering von Provider-Seite wird durch
        # rotierende Dimensionen effektiv verhindert.
        _subject_pools = _parse_pools(camp.get("rotate_subject_pools") or "")
        _from_name_pools = _parse_pools(camp.get("rotate_from_name_pools") or "")

        # Meta-Rotation-Alias: extrahiere alle Macro-Namen die als komplette
        # Pool-Zeile "{XYZ}" auftauchen. Wenn im Pool z.B. {Ende}, {Ende2},
        # {Ende4} stehen und {Ende4} gepickt wird → der resolved Wert wird
        # AUCH unter "Ende" und "Ende2" gecacht, damit die Signatur mit
        # {Ende} denselben Namen zeigt wie das From-Feld.
        # Nur wenn die Zeile GENAU ein Macro-Placeholder ist (keine Prosa
        # drumherum), sonst wäre die Semantik unklar.
        def _extract_pool_aliases(pools):
            names = set()
            _pat = re.compile(r"^\s*\{([a-zA-Z_][a-zA-Z0-9_]*)\}\s*$")
            for pool in pools:
                for line in pool:
                    m = _pat.match(line)
                    if m:
                        names.add(m.group(1))
            return names

        _from_pool_aliases = _extract_pool_aliases(_from_name_pools)
        _subject_pool_aliases = _extract_pool_aliases(_subject_pools)
        _image_modes_rot = [m for m in _parse_csv_list(camp.get("rotate_image_modes") or "")
                             if m in ("cid", "cloudinary", "cdn", "url", "static_url", "text")]
        _link_ref_styles_rot = [s for s in _parse_csv_list(camp.get("rotate_link_ref_styles") or "")
                                 if s in ("none", "base64", "random", "email", "utm")]
        if _subject_pools:
            logger.info("Campaign %d: subject-rotation ON — %d pools, %d total lines",
                         cid, len(_subject_pools), sum(len(p) for p in _subject_pools))
        if _from_name_pools:
            logger.info("Campaign %d: from-name-rotation ON — %d pools", cid, len(_from_name_pools))
        if _from_pool_aliases:
            logger.info("Campaign %d: from-pool-alias — sticky-values werden auch unter "
                         "%s gecacht (damit Signatur mit einem anderen dieser Placeholder "
                         "denselben Wert kriegt)", cid, sorted(_from_pool_aliases))
        if _subject_pool_aliases:
            logger.info("Campaign %d: subject-pool-alias — %s",
                         cid, sorted(_subject_pool_aliases))
        if _image_modes_rot:
            logger.info("Campaign %d: image-mode-rotation ON — %s (weighted via auto-refresh-ctrl if enabled)",
                         cid, _image_modes_rot)
        else:
            logger.info("Campaign %d: image-mode-rotation OFF — jeder Send nutzt cfg.image_mode=%r "
                         "(hak in der Kampagne die gewünschten Modi an um zu rotieren)",
                         cid, cfg.get("image_mode"))
        if _link_ref_styles_rot:
            logger.info("Campaign %d: link-ref-style-rotation ON — %s", cid, _link_ref_styles_rot)

        # Auto-Refresh Controller — regeneriert Assets parallel während
        # der Send-Loop läuft. Health-Tracker bestimmt weighted Mode-Choice.
        auto_refresh_ctrl = None
        if camp.get("auto_refresh_enabled"):
            try:
                from mailer.auto_refresh import AutoRefreshController
                auto_refresh_ctrl = AutoRefreshController(
                    refresh_every=int(camp.get("auto_refresh_every", 100000) or 100000),
                    variants_per_refresh=int(camp.get("auto_refresh_variants", 1000) or 1000),
                    cid_base_weight=float(camp.get("auto_refresh_cid_weight", 3.0) or 3.0),
                )
                logger.info(
                    "Campaign %d: auto-refresh ON — every %d sends, "
                    "%d variants, cid_weight=%.1f",
                    cid, auto_refresh_ctrl.refresh_every,
                    auto_refresh_ctrl.variants_per_refresh,
                    auto_refresh_ctrl.cid_base_weight)
            except Exception as e:
                logger.warning("Campaign %d: auto-refresh init failed: %s", cid, e)

        macros = {}
        sticky_macros = set()   # Namen der Macros die pro Mail nur EINMAL gewürfelt werden
        for m in db.get_active_macros(uid):
            md = dict(m)
            lines = [l.strip() for l in (md.get("values_text") or "").splitlines() if l.strip()]
            if not lines:
                continue
            name = md["name"]
            # Mehrere aktive Presets pro Macro-Name → Values-Union nehmen
            # (Schema erlaubt Duplikate: "need multiple presets per name").
            # Sonst überschreibt das letzt-gesehene Preset das erste und
            # Sticky-Verhalten wird inkonsistent je nach ORDER-BY-Ergebnis.
            if name in macros:
                seen = set(macros[name])
                macros[name].extend(x for x in lines if x not in seen)
            else:
                macros[name] = list(lines)
            if md.get("sticky"):
                sticky_macros.add(name)

        # Diagnose beim Kampagnen-Start — welche Macros sind aktiv und
        # welche davon sticky? Falls "sticky greift nur bei einem" beobachtet
        # wird, hier den ground-truth im journal nachschauen.
        if macros:
            _macro_lines = [
                f"{{{n}}}={'sticky' if n in sticky_macros else 'random'}({len(v)})"
                for n, v in sorted(macros.items())
            ]
            logger.info("Campaign %d: macros active — %s", cid,
                          ", ".join(_macro_lines))

        from_name_cfg = cfg.get("from_name", "") or "Newsletter"
        from_email_cfg = cfg.get("from_email", "")
        subject_cfg = cfg.get("subject", "") or "Notification"

        # Logo setup — load from template's logo group variant dir.
        # logo_variants stays mutable so freshness reset can swap it.
        logo_variants = []
        logo_cdn_urls = []
        logo_group_id = 0
        group_logos_for_freshness = []   # source logos so freshness can re-derive
        _group_variant_dir = None        # only defined when image_enabled
        if cfg.get("image_enabled"):
            import glob
            from .logos import VARIANT_DIR, UPLOAD_DIR, _group_variant_dir, _resolve_path as _resolve_logo
            # Campaign-Level Override gewinnt über Template-Default. So
            # kann eine Kampagne den Media-Pool wählen ohne dass fürs
            # Template ein Fork nötig wäre (Multi-Pool-Use-Case).
            camp_group = int(camp.get("logo_group_id") or 0)
            if camp_group:
                logo_group_id = camp_group
            elif campaign_template_id:
                tpl_row = db.get_template(campaign_template_id)
                if tpl_row:
                    logo_group_id = dict(tpl_row).get("logo_group_id", 0) or 0

            if logo_group_id:
                gdir = _group_variant_dir(logo_group_id)
                variant_files = sorted(glob.glob(os.path.join(gdir, "*")))
                if variant_files:
                    logo_variants = list(variant_files)
                    logger.info("Campaign %d: %d variants from group %d", cid, len(logo_variants), logo_group_id)
                else:
                    group_logos = db.get_logos_by_group(logo_group_id)
                    logo_variants = [_resolve_logo(dict(l)["file_path"]) for l in group_logos]
                    logo_variants = [p for p in logo_variants if os.path.isfile(p)]
                    logger.info("Campaign %d: %d source logos from group %d (no variants)", cid, len(logo_variants), logo_group_id)
                # Cache source logos as dicts so freshness can pass them in
                try:
                    group_logos_for_freshness = [dict(l) for l in db.get_logos_by_group(logo_group_id)]
                except Exception:
                    group_logos_for_freshness = []

            if not logo_variants:
                variant_files = sorted(glob.glob(os.path.join(VARIANT_DIR, "v_*")))
                if variant_files:
                    logo_variants = variant_files
                else:
                    source_files = sorted(glob.glob(os.path.join(UPLOAD_DIR, "*")))
                    if source_files:
                        logo_variants = source_files
                logger.info("Campaign %d: %d logos (global fallback)", cid, len(logo_variants))

        # CDN-URL-Pool laden — auch wenn image_enabled aus ist und/oder
        # cfg.image_mode nicht cdn/cloudinary ist. Wichtig: sobald in
        # Meta-Rotation "cdn" oder "cloudinary" aktiviert ist, muss der
        # Pool bereit stehen, sonst rollt der Mode ins Leere und {Logo}
        # wird nicht ersetzt.
        _cdn_needed = (
            cfg.get("image_mode") in ("cloudinary", "cdn")
            or any(m in ("cdn", "cloudinary")
                    for m in _parse_csv_list(camp.get("rotate_image_modes") or ""))
        )
        if _cdn_needed:
            import json as _json
            if logo_group_id:
                grp = db._conn().execute("SELECT cdn_urls_json FROM trans_logo_groups WHERE id=?",
                                          (logo_group_id,)).fetchone()
                if grp and grp["cdn_urls_json"]:
                    logo_cdn_urls = _json.loads(grp["cdn_urls_json"])
            if not logo_cdn_urls:
                for g in db.get_logo_groups(uid):
                    gd = dict(g)
                    if gd.get("cdn_urls_json"):
                        logo_cdn_urls.extend(_json.loads(gd["cdn_urls_json"]))
            try:
                # Multi-Pool: wenn Kampagne eine logo_group_id hat, nur
                # die Uploads dieser Group nehmen — sonst würden Pool-A
                # und Pool-B beide gemischt an alle Kampagnen gehen.
                # None = alle (backwards-compat für alte Kampagnen ohne
                # Group und für Uploads die vor dem Multi-Pool-Feature
                # gemacht wurden — die haben group_id=0).
                pool_group = logo_group_id if logo_group_id else None
                pool_urls = db.get_all_cdn_urls(uid, group_id=pool_group)
                if pool_urls:
                    logo_cdn_urls.extend(pool_urls)
            except Exception as e:
                logger.warning("Campaign %d: cdn-pool query failed: %s", cid, e)
            logger.info("Campaign %d: %d CDN URLs loaded "
                         "(Cloudinary+S3 combined) — cfg.image_mode=%s, "
                         "rotate_image_modes=%s",
                         cid, len(logo_cdn_urls),
                         cfg.get("image_mode"),
                         camp.get("rotate_image_modes"))

        # Redirect setup
        redirect_links = []
        redirect_pool_id = camp.get("redirect_pool_id", 0) or 0
        if redirect_pool_id:
            redirects = db.get_redirects_by_pool(redirect_pool_id)
            redirect_links = [dict(r)["short_url"] for r in redirects]
        elif cfg.get("redirect_enabled"):
            redirects = db.get_redirects(uid)
            redirect_links = [dict(r)["short_url"] for r in redirects]
        if redirect_links:
            logger.info("Campaign %d: %d redirect links loaded", cid, len(redirect_links))
        mime_profile_mode = cfg.get("mime_profile", "default")


        def _classify_error(error_str: str, code: int = 0) -> str:
            e = error_str.lower()

            # ============================================================
            # SMTP-ACCOUNT FAILURES — these kill the SMTP, NOT the lead.
            # Must be checked FIRST so phrases like "SMTP Blocked" don't
            # accidentally fall into the generic "blocked = spam_reject"
            # branch further down.
            # ============================================================

            # --- Authentication (fatal: password is wrong) ---
            if "incorrect authentication" in e or "authentication failed" in e:
                return "auth_fail"
            if "535" in e and ("auth" in e or "login" in e or "credentials" in e):
                return "auth_fail"
            if "invalid login" in e or "username and password not accepted" in e:
                return "auth_fail"

            # --- Account-level block: provider says "you can't send" ---
            if "smtp blocked" in e or "sender blocked" in e or "sender rejected" in e:
                return "smtp_blocked"
            if "outbound blocked" in e or "outgoing blocked" in e:
                return "smtp_blocked"
            if "from address rejected" in e or "envelope sender" in e and "reject" in e:
                return "smtp_blocked"
            if "postmaster" in e and ("reject" in e or "block" in e or "denied" in e):
                return "smtp_blocked"
            if "suspended" in e or "deactivated" in e:
                return "smtp_suspended"
            if "outgoing" in e and ("blocked" in e or "suspended" in e or "disabled" in e):
                return "smtp_suspended"
            if "account" in e and ("disabled" in e or "blocked" in e or "frozen" in e):
                return "smtp_suspended"

            # --- Rate limiting (SMTP-side, but recoverable after cooldown) ---
            if "too many" in e or ("rate" in e and "limit" in e) or "throttl" in e:
                return "rate_limit"
            if "has sent too many" in e or "sending rate" in e or "message rate" in e:
                return "rate_limit"
            if "try again later" in e or "too many connections" in e:
                return "rate_limit"

            # --- Network/transport (transient, suspend SMTP briefly) ---
            if "timeout" in e or "timed out" in e:
                return "timeout"
            if "ssl" in e and ("legacy" in e or "renegotiation" in e or "handshake" in e or "certificate" in e or "unsafe" in e):
                return "connection"
            if "ssl" in e and "error" in e:
                return "connection"
            if "connect" in e and ("refused" in e or "error" in e or "reset" in e):
                return "connection"
            if "eof" in e or "broken pipe" in e or "connection reset" in e or "unexpectedly closed" in e:
                return "connection"
            # smtplib raises this when the socket died mid-session — the
            # connection is gone, the SMTP server is fine. Worker needs
            # to drop this connection and acquire a fresh one.
            if "please run connect" in e or "smtpserverdisconnected" in e:
                return "connection"
            if "server not connected" in e or "not connected" in e:
                return "connection"

            # ============================================================
            # RECIPIENT-LEVEL FAILURES — SMTP is fine, just this address
            # didn't work. Worker keeps the connection and moves on.
            # ============================================================

            if "recipients refused" in e:
                inner = e.split("(", 1)
                if len(inner) > 1:
                    inner_msg = inner[1]
                    # Re-check SMTP-side in the inner so account problems
                    # nested inside SMTPRecipientsRefused still kill the
                    # SMTP instead of looking like a recipient bounce.
                    if "auth" in inner_msg or "535" in inner_msg or "login" in inner_msg:
                        return "auth_fail"
                    if "suspend" in inner_msg or "deactivat" in inner_msg or "disabled" in inner_msg:
                        return "smtp_suspended"
                    if "smtp blocked" in inner_msg or "sender blocked" in inner_msg or "sender rejected" in inner_msg:
                        return "smtp_blocked"
                    if "too many" in inner_msg or "rate" in inner_msg or "limit" in inner_msg:
                        return "rate_limit"
                    if "spam" in inner_msg or "blacklist" in inner_msg or "dnsbl" in inner_msg:
                        return "spam_reject"
                    if "5.1.1" in inner_msg or "5.1.10" in inner_msg:
                        return "mailbox_not_found"
                    if "no such user" in inner_msg or "no such mailbox" in inner_msg or \
                       "no such recipient" in inner_msg or "user unknown" in inner_msg or \
                       "unknown user" in inner_msg or "mailbox not found" in inner_msg or \
                       "mailbox unavailable" in inner_msg or "addressee unknown" in inner_msg:
                        return "mailbox_not_found"
                    if "blocked" in inner_msg or "rejected" in inner_msg:
                        return "spam_reject"
                return "spam_reject"

            # Mailbox truly doesn't exist. Strict — only the DSN 5.1.1
            # code and very specific "no such user / mailbox" phrases.
            # Generic phrases like "recipient rejected", "does not
            # exist" or "invalid recipient" are excluded: those are
            # also emitted for greylisting, IP-rep checks, content
            # filters and routing problems and would falsely add live
            # addresses to the suppression list.
            if "5.1.1" in e or "5.1.10" in e:
                return "mailbox_not_found"
            if "no such user" in e or "no such mailbox" in e or \
               "no such recipient" in e or "user unknown" in e or \
               "unknown user" in e or "mailbox not found" in e or \
               "mailbox unavailable" in e or "addressee unknown" in e:
                return "mailbox_not_found"

            # Spam/content rejection (recipient-side content decision)
            if "spam" in e or "dnsbl" in e or "blacklist" in e:
                return "spam_reject"
            if ("policy" in e and "reject" in e) or ("content" in e and "reject" in e):
                return "spam_reject"

            # Generic "blocked" — ambiguous, but if we reach this point the
            # account-specific patterns above didn't match, so it's
            # probably a recipient/content block.
            if "blocked" in e:
                return "spam_reject"

            # --- SMTP code fallbacks ---
            if code >= 550:
                return "permanent_reject"
            if code >= 400:
                return "rate_limit"

            return "other"


        # Which error classes kill the SMTP vs. just the lead.
        SMTP_FATAL     = {"auth_fail", "smtp_suspended", "smtp_blocked"}
        SMTP_TRANSIENT = {"rate_limit", "connection", "timeout"}
        # Addresses we permanently suppress. Only mailbox_not_found —
        # i.e. the classifier matched a specific "no such user" phrase
        # or a 5.1.1 DSN. permanent_reject is the catch-all for every
        # other 5xx and is too broad: content-filter rejects, SMTP
        # config issues, header problems all surface as 5xx and would
        # otherwise pollute the suppression list with addresses that
        # work fine on another SMTP / next mailing.
        HARD_BOUNCE    = {"mailbox_not_found"}
        suppress_enabled = cfg.get("auto_suppress_hard_bounces", True)
        suppressed_set = db.load_suppression_set(uid) if suppress_enabled else set()

        def _log_bounce(lead_id, email, error_str, code=0, smtp_host="", smtp_user=""):
            etype = _classify_error(error_str, code)
            profile = mime_profile_mode if mime_profile_mode != "rotate" else "rotated"
            db.log_bounce(campaign_id, lead_id, email, code, etype,
                          error_str[:500], profile, smtp_host, smtp_user, uid)

        thread_count = min(cfg.get("threads", 40), pool.size * 2, 200)
        thread_count = max(thread_count, 1)
        logger.info("Campaign %d starting: %d pending, %d SMTPs, %d threads",
                     cid, pending, pool.size, thread_count)

        # Gradual send: linearly ramp the effective send rate from
        # `start_factor` of full speed up to 100% over `ramp_minutes`.
        # Implemented by scaling per-mail sleep inversely with the
        # current ramp factor — at 5% factor each mail waits 20x the
        # configured delay; at 100% it's the configured delay.
        gradual_enabled = bool(cfg.get("gradual_send_enabled", False))
        ramp_seconds    = max(60.0, float(cfg.get("gradual_send_ramp_minutes", 180) or 180) * 60.0)
        start_factor    = max(0.01, min(1.0, float(cfg.get("gradual_send_start_factor", 0.05) or 0.05)))
        base_delay      = float(cfg.get("normal_delay", 0.3) or 0.3)
        campaign_start_mono = time.monotonic()

        def _effective_delay() -> float:
            if not gradual_enabled:
                return base_delay
            elapsed = time.monotonic() - campaign_start_mono
            frac = min(1.0, elapsed / ramp_seconds)
            factor = start_factor + (1.0 - start_factor) * frac
            return base_delay / factor

        if gradual_enabled:
            logger.info(
                "Campaign %d: gradual send ENABLED — %.0f%% -> 100%% over %.0f min "
                "(base_delay=%.2fs, initial effective=%.2fs)",
                cid, start_factor * 100, ramp_seconds / 60, base_delay,
                base_delay / start_factor,
            )

        sent = 0
        failed = 0
        suppressed = 0
        _lock = threading.Lock()

        # Freshness reset barrier — workers pause here while a refresh
        # of the HTML pool and/or logo variant set happens mid-send.
        freshness_barrier = threading.Event()
        freshness_barrier.set()
        freshness_every = int(cfg.get("freshness_every_n_mails", 0) or 0)
        freshness_html  = bool(cfg.get("freshness_reset_html", False))
        freshness_logos = bool(cfg.get("freshness_reset_logos", False))

        def _freshness_monitor():
            """Watches the global `sent` counter and triggers a refresh
            every `freshness_every` successful mails. Runs in its own
            daemon thread; failures are logged but never bubble up."""
            if freshness_every <= 0 or not (freshness_html or freshness_logos):
                return
            next_reset_at = freshness_every
            while campaign_id in _runners:
                time.sleep(2)
                with _lock:
                    cur_sent = sent
                if cur_sent < next_reset_at:
                    continue
                logger.info("Campaign %d: freshness reset triggered at sent=%d",
                             cid, cur_sent)
                freshness_barrier.clear()
                # Give in-flight builds a moment to finish before we swap
                time.sleep(0.5)
                try:
                    if freshness_html:
                        new_bodies = []
                        from mailer.freshness import regenerate_html_pool
                        try:
                            new_bodies = regenerate_html_pool(
                                count=int(cfg.get("freshness_html_count", 25) or 25))
                        except Exception as e:
                            logger.warning("Campaign %d: html regen failed: %s", cid, e)
                        if new_bodies:
                            html_bodies.clear()
                            html_bodies.extend(new_bodies)
                            logger.info("Campaign %d: html pool refreshed (%d new bodies)",
                                         cid, len(new_bodies))

                    if freshness_logos and logo_group_id and group_logos_for_freshness:
                        # Logo regen only meaningfully helps modes that
                        # actually read logo_variants at send time. In
                        # text and cloudinary modes the regenerated
                        # files are never touched, so we skip the work.
                        if image_mode not in ("cid", "url"):
                            logger.info(
                                "Campaign %d: skipping logo regen "
                                "(image_mode=%s doesn't use local variants)",
                                cid, image_mode,
                            )
                        else:
                            from mailer.freshness import regenerate_logo_variants
                            try:
                                gdir = _group_variant_dir(logo_group_id)
                                new_vars = regenerate_logo_variants(
                                    group_logos_for_freshness,
                                    gdir,
                                    int(cfg.get("freshness_logo_count", 25) or 25),
                                    max_colors=int(cfg.get("logo_max_colors", 256) or 256),
                                    quantize=bool(cfg.get("image_quantize", True)),
                                    downscale=bool(cfg.get("image_downscale", False)),
                                )
                                if new_vars:
                                    logo_variants.clear()
                                    logo_variants.extend(new_vars)
                                    logger.info("Campaign %d: logo variants refreshed (%d new)",
                                                 cid, len(new_vars))
                            except Exception as e:
                                logger.warning("Campaign %d: logo regen failed: %s", cid, e)
                except Exception as e:
                    logger.error("Campaign %d: freshness reset crashed: %s", cid, e)
                finally:
                    next_reset_at = cur_sent + freshness_every
                    freshness_barrier.set()

        test_interval = cfg.get("test_interval", 0)
        interval_recips_raw = cfg.get("interval_recipients", "") or cfg.get("test_recipients", "")
        interval_recips = [r.strip() for r in interval_recips_raw.split(",") if r.strip()]

        def speed_tracker():
            nonlocal sent
            last_s, last_t = 0, time.monotonic()
            while cid in _runners:
                time.sleep(5)
                now_t = time.monotonic()
                elapsed = now_t - last_t
                if elapsed > 0:
                    _speed[cid] = int((sent - last_s) / elapsed * 3600)
                last_t, last_s = now_t, sent

        threading.Thread(target=speed_tracker, daemon=True).start()
        threading.Thread(target=_freshness_monitor, daemon=True).start()

        def _process(text, email, sticky_cache=None):
            """sticky_cache: dict pro Mail, wird über alle _process-Calls
            desselben Mails geteilt (from_name, subject, html body etc.).
            Für sticky-Macros: einmal würfeln, dann derselbe Wert überall.
            Non-sticky Macros bleiben pro Vorkommen zufällig."""
            user = email.split("@")[0] if "@" in email else email
            domain = email.split("@")[1] if "@" in email else ""
            text = text.replace("{email}", email).replace("{email_user}", user).replace("{domain}", domain)
            # Multi-Pass damit nested Macros aufgelöst werden:
            # {A} = "Hallo {B}" und {B} = "Peter" → nach 1 Pass wäre
            # {B} nicht ersetzt (falls B im macros-dict vor A iteriert
            # wurde und der Text initial nur {A} enthielt). Bis zu 5
            # Passes, dann break wenn nichts mehr geändert.
            for _pass in range(5):
                changed = False
                for mname, mlines in macros.items():
                    token = f"{{{mname}}}"
                    if token not in text:
                        continue
                    changed = True
                    if mname in sticky_macros:
                        cache = sticky_cache if sticky_cache is not None else {}
                        if mname not in cache:
                            cache[mname] = random.choice(mlines)
                        text = text.replace(token, cache[mname])
                    else:
                        # nicht sticky → jedes Vorkommen darf ein anderer Wert sein
                        while token in text:
                            text = text.replace(token, random.choice(mlines), 1)
                if not changed:
                    break
            def _spintax(m):
                return random.choice(m.group(1).split("|"))
            for _ in range(20):
                new = re.sub(r"\{([^{}]+\|[^{}]+)\}", _spintax, text)
                if new == text:
                    break
                text = new
            import string
            def _randstr(m):
                length, charset_name, case = int(m.group(1)), m.group(2), m.group(3)
                cs = {"a-z": string.ascii_lowercase, "A-Z": string.ascii_uppercase,
                      "0-9": string.digits, "a-z0-9": string.ascii_lowercase + string.digits,
                      "a-zA-Z0-9": string.ascii_letters + string.digits}.get(charset_name, string.ascii_lowercase)
                r = "".join(random.choice(cs) for _ in range(length))
                return r.upper() if case == "upper" else r.lower() if case == "lower" else r
            text = re.sub(r"\[RANDSTR:(\d+):([a-zA-Z0-9\-]+):(\w+)\]", _randstr, text)
            return text

        campaign_id = cid
        import queue
        mail_queue = queue.Queue()
        image_mode = cfg.get("image_mode", "cid")

        def _trigger_auto_refresh():
            """Feuert einen Refresh in einem Background-Thread ab.
            Ergebnis wird in auto_refresh_ctrl.last_refresh_report gespeichert
            und logo_cdn_urls / logo_variants werden erweitert."""
            if not auto_refresh_ctrl:
                return

            def _run():
                try:
                    # Source-Logos: aus /logos aller Groups
                    sources = []
                    try:
                        for l in db.get_logos(uid):
                            ld = dict(l)
                            p = ld.get("file_path", "")
                            if p.startswith("/static/"):
                                p = os.path.abspath(os.path.join(
                                    os.path.dirname(__file__), "..", p.lstrip("/")))
                            if p and os.path.isfile(p):
                                sources.append((ld.get("filename") or "logo.png", p))
                    except Exception as e:
                        logger.warning("auto-refresh: source-logo load: %s", e)
                    if not sources:
                        logger.warning("Campaign %d: auto-refresh skipped — no sources", campaign_id)
                        auto_refresh_ctrl.refresh_running = False
                        return

                    # Cloudinary-Config aus /config
                    cloud_cfg = None
                    if cfg.get("cloudinary_cloud_name") and cfg.get("cloudinary_api_key"):
                        cloud_cfg = {
                            "cloud_name":  cfg.get("cloudinary_cloud_name", ""),
                            "api_key":     cfg.get("cloudinary_api_key", ""),
                            "api_secret":  cfg.get("cloudinary_api_secret", ""),
                            "folder":      cfg.get("cloudinary_folder", ""),
                        }

                    # S3-Accounts + Proxy (nimmt Proxy vom ersten Account)
                    s3_accs = []
                    s3_proxy = ""
                    try:
                        for a in db.get_s3_accounts(uid):
                            ad = dict(a)
                            if ad.get("buckets"):
                                s3_accs.append(ad)
                        if s3_accs and s3_accs[0].get("proxy_id"):
                            _pr = db.get_proxy(s3_accs[0]["proxy_id"])
                            if _pr:
                                s3_proxy = (dict(_pr).get("value") or "").splitlines()[0].strip()
                    except Exception:
                        pass

                    # Callback für neue URLs → in DB persistieren damit sie
                    # sofort im logo_cdn_urls-Pool landen (bei nächster Iteration)
                    def _on_cdn_url(url, bucket=None, key=None, account_id=None):
                        try:
                            if bucket is not None:
                                # S3 — group_id folgt der Kampagne, sonst
                                # würde der auto-refresh-Upload beim
                                # nächsten Cycle vom Pool-Filter aussortiert
                                upload_id = db.add_s3_upload(
                                    account_id or 0, "auto-refresh", uid,
                                    group_id=logo_group_id or 0)
                                db.add_s3_link(upload_id, url, bucket, key or "")
                            # Live in den Pool zu adden ist dank Live-Query in
                            # db.get_all_cdn_urls automatisch. logo_cdn_urls
                            # in campaigns.py ist ein snapshot — wir extenden
                            # ihn direkt damit's ohne kompletten reload wirkt.
                            logo_cdn_urls.append(url)
                        except Exception as e:
                            logger.debug("auto-refresh persist fail: %s", e)

                    # Bytes-Tweak reused
                    def _tweak(src, seed):
                        try:
                            from mailer.image_jitter import jitter_image_bytes
                            with open(src, "rb") as fh:
                                data = fh.read()
                            out, _ = jitter_image_bytes(data, "image/png")
                            return out or data
                        except Exception:
                            with open(src, "rb") as fh:
                                return fh.read()

                    variant_dir = ""
                    if logo_group_id:
                        try:
                            from .logos import _group_variant_dir as _gv
                            variant_dir = _gv(logo_group_id)
                        except Exception:
                            pass

                    logger.info("Campaign %d: auto-refresh starting — %d sources, "
                                 "cid=%s cloudinary=%s s3=%d accounts",
                                 campaign_id, len(sources),
                                 bool(variant_dir), bool(cloud_cfg), len(s3_accs))
                    auto_refresh_ctrl.run_refresh(
                        source_logo_paths=sources,
                        variant_dir=variant_dir,
                        cloudinary_config=cloud_cfg,
                        s3_accounts=s3_accs,
                        s3_proxy=s3_proxy,
                        on_cdn_url_added=_on_cdn_url,
                        tweak_bytes_fn=_tweak,
                    )
                    # Nach dem Refresh: logo_variants aus dem variant_dir
                    # neu einlesen damit CID die neuen files nutzt
                    if variant_dir:
                        import glob as _glob
                        new_vars = sorted(_glob.glob(os.path.join(variant_dir, "*")))
                        if new_vars:
                            logo_variants.clear()
                            logo_variants.extend(new_vars)
                except Exception as e:
                    logger.error("Campaign %d: auto-refresh crashed: %s",
                                  campaign_id, e, exc_info=True)
                    auto_refresh_ctrl.refresh_running = False

            threading.Thread(target=_run, daemon=True).start()

        def _build_and_send(server_obj, account, lead_id, email):
            """Build email and send over existing connection. Returns True on success."""
            nonlocal sent, failed, suppressed
            # Skip addresses on the suppression list (dead from a prior
            # campaign). Mark terminal so they aren't retried, don't open
            # a connection, don't count as a failure.
            if suppress_enabled and email.strip().lower() in suppressed_set:
                with _lock:
                    db.mark_suppressed(lead_id, "suppressed (prior hard bounce)")
                    suppressed += 1
                return True
            cur_from_email = from_email_cfg or account.user
            # Shared sticky-cache für alle _process-Calls dieser Mail — so
            # kriegen {Name} in From, Subject, HTML-Body und Signatur alle
            # denselben Wert (wenn das Macro als sticky markiert ist).
            sticky_cache = {}
            try:
                # === Meta-Rotation: from-name-pool ===
                if _from_name_pools:
                    _pool = random.choice(_from_name_pools)
                    cur_from_name = _process(random.choice(_pool), email, sticky_cache)
                    # Alias-Apply: die anderen Sticky-Macros im gleichen
                    # Pool bekommen den resolved Wert auch — so kann in
                    # der Signatur ein anderer Alias-Name stehen und
                    # kriegt trotzdem den From-Name.
                    for _alias in _from_pool_aliases:
                        if _alias in sticky_macros and _alias not in sticky_cache:
                            sticky_cache[_alias] = cur_from_name
                else:
                    cur_from_name = _process(from_name_cfg, email, sticky_cache)
                # === Meta-Rotation: subject-pool ===
                if _subject_pools:
                    _pool = random.choice(_subject_pools)
                    raw_subject = random.choice(_pool)
                    cur_subject = _process(raw_subject, email, sticky_cache)
                    for _alias in _subject_pool_aliases:
                        if _alias in sticky_macros and _alias not in sticky_cache:
                            sticky_cache[_alias] = cur_subject
                    _track_subject(cid, raw_subject, cur_subject)
                else:
                    raw_subject = subject_cfg
                    cur_subject = _process(subject_cfg, email, sticky_cache)
                    _track_subject(cid, raw_subject, cur_subject)

                if live_html_gen and htmlgen_generate_one is not None:
                    # Per-Send fresh HTML aus der htmlgen-Engine.
                    try:
                        # Extra-Randomize (opt-in): 15% Referenz raus,
                        # 15% Hinweis raus. Als cfg-copy, damit die
                        # Kampagnen-Config nicht persistent verändert wird.
                        _hg_cfg = htmlgen_cfg
                        if _extra_random:
                            _hg_cfg = dict(htmlgen_cfg)
                            _hg_cfg["blocks"] = dict(htmlgen_cfg.get("blocks", {}))
                            if random.random() < 0.15:
                                _hg_cfg["blocks"]["referenz"] = False
                            if random.random() < 0.15:
                                _hg_cfg["blocks"]["hinweis"] = False
                        html = htmlgen_generate_one(_hg_cfg, htmlgen_base,
                                                     _cache=htmlgen_cache)
                        # Post-Render Placeholder-Twists (pre-_process, damit
                        # der Swap vor der Macro-Auflösung passiert).
                        if _extra_random:
                            if random.random() < 0.20:
                                # Footer2 komplett raus. Auch das " <br>"
                                # das Footer1 und Footer2 verbindet mit weg,
                                # sonst hängt ein leerer <br> rum.
                                html = html.replace("<br>{Footer2}", "")
                                html = html.replace("{Footer2}", "")
                            if random.random() < 0.25:
                                # FristText1 <-> FristText2 tauschen. Via
                                # Zwischentoken damit double-replace nicht
                                # zusammenläuft.
                                _tok = "\x00FT1SWAP\x00"
                                html = html.replace("{FristText1}", _tok)
                                html = html.replace("{FristText2}", "{FristText1}")
                                html = html.replace(_tok, "{FristText2}")
                    except Exception as _hg_err:
                        logger.warning("Campaign %d: htmlgen fail, fallback: %s",
                                        campaign_id, _hg_err)
                        html = (random.choice(html_bodies) if html_bodies
                                else "<p>Hello {email_user}</p>")
                elif assembly_enabled and assembly_snippets:
                    from mailer.assembly import assemble_html
                    html = assemble_html(assembly_snippets)
                elif html_bodies:
                    html = random.choice(html_bodies)
                else:
                    html = "<p>Hello {email_user}</p>"
                html = _process(html, email, sticky_cache)

                # === Meta-Rotation: image-mode + link-ref-style ===
                if _image_modes_rot:
                    if auto_refresh_ctrl:
                        cur_image_mode = auto_refresh_ctrl.choose_mode(_image_modes_rot)
                    else:
                        cur_image_mode = random.choice(_image_modes_rot)
                else:
                    cur_image_mode = image_mode
                cur_ref_style = (random.choice(_link_ref_styles_rot)
                                  if _link_ref_styles_rot
                                  else ("base64" if cfg.get("redirect_append_ref") else "none"))

                if redirect_links and "{RedirectLink}" in html:
                    link = random.choice(redirect_links)
                    link = _append_ref(link, email, style=cur_ref_style)
                    html = html.replace("{RedirectLink}", link)
                    cur_subject = cur_subject.replace("{RedirectLink}", link)

                inline_images = None
                if "{Logo}" in html:
                    # CDN → wenn Pool leer, fällt hart auf CID zurück damit
                    # {Logo} nie leer bleibt. Gleiches für andere Modi.
                    _mode = cur_image_mode
                    if _mode in ("cloudinary", "cdn") and not logo_cdn_urls:
                        _mode = "cid"
                    if _mode == "static_url" and not cfg.get("logo_static_url", "").strip():
                        _mode = "cid"
                    if _mode == "url" and not (logo_variants and cfg.get("logo_base_url", "").strip()):
                        _mode = "cid"
                    _track_image_mode(cid, cur_image_mode, _mode,
                                       ctrl=auto_refresh_ctrl,
                                       cdn_pool_size=len(logo_cdn_urls or []),
                                       variant_count=len(logo_variants or []))

                    if _mode == "static_url":
                        static_logo = cfg.get("logo_static_url", "").strip()
                        html = html.replace("{Logo}",
                            f'<img src="{static_logo}" alt="Logo" style="display:block;border:0;max-height:50px;width:auto;">')
                    elif _mode == "text":
                        logo_text = _process(cfg.get("logo_text", "{Logo}"), email, sticky_cache)
                        html = html.replace("{Logo}",
                            f'<span style="font-weight:bold;font-size:16px;color:{cfg.get("logo_text_color", "#333333")};">{logo_text}</span>')
                    elif _mode in ("cloudinary", "cdn"):
                        html = html.replace("{Logo}",
                            f'<img src="{random.choice(logo_cdn_urls)}" alt="Logo" style="display:block;border:0;max-height:50px;width:auto;">')
                    elif _mode == "url" and logo_variants:
                        base = cfg.get("logo_base_url", "").rstrip("/")
                        html = html.replace("{Logo}",
                            f'<img src="{base}/{os.path.basename(random.choice(logo_variants))}" alt="Logo" style="display:block;border:0;max-height:50px;width:auto;">' if base else "")
                    elif logo_variants:
                        logo_path = random.choice(logo_variants)
                        try:
                            import mimetypes as mt
                            mime_type = mt.guess_type(logo_path)[0] or "image/png"
                            with open(logo_path, "rb") as lf:
                                logo_bytes = lf.read()
                            # Per-Send-Mikro-Jitter: jeder Empfänger
                            # kriegt ein pixel-unique Logo (subpixel
                            # offset + brightness/contrast/color feinjust
                            # + tiny crop + quantize-Palette random).
                            # Visuell identisch, Byte-Hash unique.
                            try:
                                from mailer.image_jitter import (
                                    jitter_image_bytes, random_img_tag,
                                )
                                logo_bytes, mime_type = jitter_image_bytes(
                                    logo_bytes, mime_type)
                                import secrets as _sec
                                cid_val = f"{_sec.token_hex(8)}@{cur_from_email.split('@')[1] if '@' in cur_from_email else 'mail'}"
                                html = html.replace("{Logo}",
                                    random_img_tag(cid_val))
                            except Exception:
                                import secrets as _sec
                                cid_val = f"{_sec.token_hex(8)}@{cur_from_email.split('@')[1] if '@' in cur_from_email else 'mail'}"
                                html = html.replace("{Logo}",
                                    f'<img src="cid:{cid_val}" alt="Logo" style="display:block;border:0;max-height:50px;width:auto;">')
                            inline_images = [(logo_bytes, cid_val, mime_type)]
                        except Exception:
                            html = html.replace("{Logo}", "")
                    else:
                        html = html.replace("{Logo}", "")

                if afp:
                    html = afp.transform(html)
                plain = re.sub(r"<br\s*/?>", "\n", html)
                plain = re.sub(r"<[^>]+>", "", plain).strip()

                raw_msg = MIMEBuilder.build_email(
                    from_name=cur_from_name, from_email=cur_from_email,
                    to_email=email, subject=cur_subject,
                    html_body=html, plain_body=plain,
                    inline_images=inline_images)

                if mime_profile_mode == "rotate":
                    from mailer.mime_profiles import get_random_profile, apply_profile
                    raw_msg = apply_profile(raw_msg, get_random_profile(), cur_from_email)
                elif mime_profile_mode != "default":
                    from mailer.mime_profiles import apply_profile
                    raw_msg = apply_profile(raw_msg, mime_profile_mode, cur_from_email)

            except Exception as build_exc:
                with _lock:
                    db.mark_failed(lead_id, f"BUILD: {str(build_exc)[:400]}")
                    failed += 1
                return True

            try:
                server_obj.sendmail(cur_from_email, email, raw_msg)
            except Exception as send_exc:
                # Pull the SMTP code if smtplib gave us one (sendmail
                # raises SMTPRecipientsRefused with a dict of code/msg).
                code = 0
                if hasattr(send_exc, "smtp_code"):
                    code = getattr(send_exc, "smtp_code", 0) or 0
                elif hasattr(send_exc, "recipients"):
                    try:
                        first = next(iter(send_exc.recipients.values()))
                        code = first[0] if isinstance(first, tuple) else 0
                    except Exception:
                        code = 0

                err_str = str(send_exc)
                etype = _classify_error(err_str, code)

                with _lock:
                    db.log_bounce(campaign_id, lead_id, email, code, etype,
                                  err_str[:500],
                                  mime_profile_mode if mime_profile_mode != "rotate" else "rotated",
                                  account.host, account.user, uid)
                    if suppress_enabled and etype in HARD_BOUNCE:
                        # Dead mailbox — suppress globally and mark terminal
                        # so neither this campaign's retry passes nor any
                        # future campaign hits it again.
                        db.add_suppression(email, reason=etype, bounce_code=code,
                                           error_message=err_str, source="auto",
                                           campaign_id=campaign_id, user_id=uid)
                        suppressed_set.add(email.strip().lower())
                        db.mark_suppressed(lead_id, err_str[:500])
                        suppressed += 1
                    else:
                        db.mark_failed(lead_id, err_str[:500])
                        failed += 1

                if etype in SMTP_FATAL:
                    # SMTP account is broken — kick it out of the pool
                    # immediately, do not pick it again this campaign.
                    pool.mark_dead(account, f"{etype}: {err_str[:200]}")
                    raise Exception(send_exc)
                if etype in SMTP_TRANSIENT:
                    # Transient: cooldown via the pool's escalating backoff.
                    pool.suspend(account, etype)
                    raise Exception(send_exc)
                # Recipient-level failure — SMTP is fine, keep the
                # connection and move on to the next mail.
                return False

            with _lock:
                db.mark_sent(lead_id)
                sent += 1
                # Persist alle 5 Sends statt alle 20 — sonst hinkt der
                # UI-Counter (der ist jetzt live aus trans_leads, aber
                # nach Restart wird trans_campaigns.sent gebraucht).
                if (sent + failed) % 5 == 0:
                    db.update_campaign(campaign_id, sent=sent, failed=failed)
            pool.record_success(account)

            # Track success für image-mode-health (nach dem eigentlichen
            # Send — heißt der ganze Build inkl. Logo-Fetch/Attach hat
            # geklappt).
            if auto_refresh_ctrl:
                try:
                    if cur_image_mode in ("cloudinary", "cdn"):
                        auto_refresh_ctrl.health_cloudinary.record(True)
                        auto_refresh_ctrl.health_s3.record(True)
                    elif cur_image_mode == "cid":
                        auto_refresh_ctrl.health_cid.record(True)
                    # Refresh triggern falls Zähler voll
                    if auto_refresh_ctrl.report_send():
                        _trigger_auto_refresh()
                except Exception as _rerr:
                    logger.debug("auto-refresh hook glitch: %s", _rerr)

            delay = worker.get_delay(email)
            if delay > 0:
                time.sleep(delay)
            return True

        def _worker_thread():
            """Each thread grabs an SMTP, connects, sends many mails."""
            nonlocal sent, failed
            # Robust loop semantics: the only authoritative "done"
            # signal is queue.Empty raised repeatedly from get(timeout=2)
            # in the inner loop. Checking mail_queue.empty() at the
            # outer loop head races with other workers and causes
            # workers to die before the queue is actually drained —
            # which produced campaigns ending at "0/N sent, FINISHED".
            no_smtp_strikes = 0
            while campaign_id in _runners:
                account = pool.acquire()
                if account is None:
                    no_smtp_strikes += 1
                    if no_smtp_strikes == 1:
                        logger.info(
                            "Campaign %d: no SMTPs available, waiting...",
                            campaign_id,
                        )
                    if no_smtp_strikes >= 60:
                        logger.warning(
                            "Campaign %d: no SMTPs available for 60s, "
                            "exiting worker — retry pass will resume.",
                            campaign_id,
                        )
                        return
                    time.sleep(1)
                    continue
                no_smtp_strikes = 0

                try:
                    server_obj = pool.connect(account)
                except Exception as e:
                    logger.warning("Campaign %d: connect failed %s: %s", campaign_id, account.user, e)
                    pool.suspend(account, "connect_failed")
                    time.sleep(1)
                    continue

                warmup_wait = pool.get_warmup_delay(account)
                empty_strikes = 0
                inner_done = False

                try:
                    while campaign_id in _runners:
                        # Block here while a freshness reset swaps the
                        # html_bodies / logo_variants under us. Returns
                        # immediately when the barrier is set (default).
                        freshness_barrier.wait(timeout=30)

                        try:
                            lead_id, email = mail_queue.get(timeout=2)
                            empty_strikes = 0
                        except queue.Empty:
                            empty_strikes += 1
                            # 3 consecutive 2s timeouts = 6s of confirmed
                            # empty queue. Other workers don't have items
                            # in flight that need to come back to us, so
                            # we're really done.
                            if empty_strikes >= 3:
                                inner_done = True
                                break
                            continue

                        if warmup_wait > 0:
                            time.sleep(warmup_wait)
                            warmup_wait = pool.get_warmup_delay(account)

                        try:
                            ok = _build_and_send(server_obj, account, lead_id, email)
                            # ok=False = recipient-level failure: SMTP is
                            # still usable, try the next mail with the
                            # same connection. (Old behaviour broke here,
                            # which forced a reconnect after every bounce.)
                        except Exception:
                            # SMTP-level failure raised inside _build_and_send.
                            # Pool was already notified via suspend/mark_dead.
                            break

                        time.sleep(_effective_delay())
                except Exception:
                    pass
                finally:
                    try:
                        server_obj.quit()
                    except Exception:
                        pass
                # Inner loop confirmed the queue is empty — really done.
                # If we just broke out because the SMTP died mid-stream,
                # loop back and acquire a fresh one.
                if inner_done:
                    return

        def _run_send_pass():
            """Fill queue from DB, run worker threads."""
            db.reset_in_progress(lead_list_id)
            while mail_queue.qsize() > 0:
                try:
                    mail_queue.get_nowait()
                except queue.Empty:
                    break

            leads = db._conn().execute(
                "SELECT id, email FROM trans_leads WHERE list_id=? AND state='PENDING' ORDER BY id",
                (lead_list_id,)).fetchall()
            if not leads:
                logger.warning(
                    "Campaign %d: send pass requested but 0 PENDING leads. "
                    "Lead state breakdown: %s",
                    cid, db.get_lead_states(lead_list_id),
                )
                return
            logger.info("Campaign %d: send pass queuing %d leads across %d threads",
                         cid, len(leads), thread_count)

            for lead in leads:
                db._conn().execute("UPDATE trans_leads SET state='IN_PROGRESS' WHERE id=?", (lead["id"],))
                mail_queue.put((lead["id"], lead["email"]))
            db._conn().commit()

            threads = []
            for _ in range(thread_count):
                t = threading.Thread(target=_worker_thread, daemon=True)
                t.start()
                threads.append(t)

            for t in threads:
                t.join()

        try:
            _run_send_pass()

            # Blanket retry: every FAILED lead gets up to two more attempts.
            # No error-type filtering — bounce_log keeps classification for
            # stats, but we don't use it for flow decisions. A "permanent"
            # error from one SMTP is often transient on a second IP, and
            # truly dead mailboxes just fail again at near-zero cost.
            if cid in _runners and cfg.get("auto_retry_failed", True):
                retry_pauses = [5, 30]   # seconds before pass 2 / pass 3
                total_passes = len(retry_pauses) + 1
                for attempt, pause in enumerate(retry_pauses, start=2):
                    if cid not in _runners:
                        break
                    db.reset_in_progress(lead_list_id)
                    db._conn().execute(
                        "UPDATE trans_leads SET state='PENDING' "
                        "WHERE list_id=? AND state='FAILED'",
                        (lead_list_id,))
                    db._conn().commit()
                    pending = db._conn().execute(
                        "SELECT COUNT(*) FROM trans_leads "
                        "WHERE list_id=? AND state='PENDING'",
                        (lead_list_id,)).fetchone()[0]
                    if pending == 0:
                        break
                    logger.info(
                        "Campaign %d: retry pass %d/%d on %d failed leads, "
                        "pausing %ds first", cid, attempt, total_passes,
                        pending, pause)
                    time.sleep(pause)
                    _run_send_pass()
        finally:
            db.reset_in_progress(lead_list_id)

        # cid stays in _runners for the whole _run_campaign run; it's
        # only removed when the user calls stop_campaign or when the
        # outer run() wrapper's finally block fires after we return.
        # So "still in runners" here = normal completion; "missing" = the
        # user pressed Pause.
        status = "FINISHED" if cid in _runners else "PAUSED"
        from datetime import datetime
        logger.info("Campaign %d %s: sent=%d, failed=%d, suppressed=%d",
                     cid, status, sent, failed, suppressed)
        db.update_campaign(cid, status=status, sent=sent, failed=failed,
                            finished_at=datetime.now().isoformat())
        db.reset_in_progress(lead_list_id)

        if status == "FINISHED":
            from .logos import clear_variants
            clear_variants()
            logger.info("Campaign %d: logo variants cleared", cid)

    finally:
        os.unlink(smtp_file.name)
        if proxy_file_path:
            os.unlink(proxy_file_path)
