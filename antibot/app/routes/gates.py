"""Multi-Domain Gate Management.

Jede Gate = eine Domain mit eigenem Modus (Preset), Ziel, Branding und
optional eigenem Turnstile-Widget. Ready-to-paste Links pro Gate.
Auto-Deploy einer neuen Gate: (optional) via Dynadot kaufen, per CF-API
A-Record setzen, Turnstile-Widget erstellen, alles in einem Rutsch.
"""
import os
import time
import secrets
import logging
from html import escape
from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse

from ..presets import MODE_PRESETS, resolve_mode, gen_slug, detect_public_ip
from .domains import (_cf_configured, _cf_zones, _cf_add_zone, _cf_add_record,
                       _cf_create_turnstile_widget, _dyn_register)

logger = logging.getLogger("antibot.gates")
router = APIRouter()

STATIC_LOGO_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "static", "logo")


@router.get("/admin/gates", response_class=HTMLResponse)
async def gates_page(request: Request):
    db = request.app.state.db
    cfg = db.get_config()
    gates = db.list_gates()
    for g in gates:
        g["link_count"] = len(db.list_gate_links(g["id"], limit=10000))
        g["mode_label"] = MODE_PRESETS.get(g["mode"], MODE_PRESETS["medium"])["label"]
    return request.app.state.templates.TemplateResponse(request, "admin_gates.html", {
        "cfg": cfg, "gates": gates,
        "presets": MODE_PRESETS,
        "server_ip": cfg.get("server_public_ip", ""),
    })


@router.get("/admin/gates/new", response_class=HTMLResponse)
async def gate_new(request: Request):
    db = request.app.state.db
    cfg = db.get_config()
    # Detect + persist public IP if we haven't yet
    ip = cfg.get("server_public_ip", "")
    if not ip:
        ip = detect_public_ip()
        if ip:
            db.set_config(server_public_ip=ip)
    return request.app.state.templates.TemplateResponse(request, "admin_gate_wizard.html", {
        "cfg": cfg, "presets": MODE_PRESETS, "server_ip": ip,
        "dynadot_ready": bool(cfg.get("dynadot_api_key")),
        "cf_ready": _cf_configured(cfg),
    })


@router.post("/admin/gates/new", response_class=HTMLResponse)
async def gate_new_submit(request: Request,
                           hostname: str = Form(""),
                           mode: str = Form("medium"),
                           target_url: str = Form(""),
                           brand_text: str = Form(""),
                           brand_color: str = Form("#005eb8"),
                           buy_dynadot: str = Form(""),
                           auto_cf: str = Form(""),
                           auto_turnstile: str = Form(""),
                           add_www: str = Form("1"),
                           initial_links: int = Form(10),
                           logo: UploadFile = File(None)):
    db = request.app.state.db
    cfg = db.get_config()
    hostname = hostname.strip().lower().lstrip("https://").lstrip("http://").split("/")[0]
    if not hostname or "." not in hostname:
        return HTMLResponse('<div class="alert alert-danger">Gültige Domain angeben.</div>')
    if db.get_gate_by_host(hostname):
        return HTMLResponse('<div class="alert alert-danger">Diese Domain ist bereits als Gate angelegt.</div>')
    if mode not in MODE_PRESETS:
        mode = "medium"

    steps = []  # (label, ok, detail)

    # Optional: buy domain via Dynadot
    if buy_dynadot and cfg.get("dynadot_api_key"):
        res = _dyn_register(cfg["dynadot_api_key"], hostname,
                             currency=cfg.get("buy_currency", "USD"),
                             secret=cfg.get("dynadot_api_secret", ""))
        steps.append(("Dynadot: Kauf", res["ok"], res["msg"]))
        if not res["ok"]:
            return _wizard_error(request, steps, "Kauf fehlgeschlagen — Gate wurde NICHT angelegt.")

    # Optional: CF-Zone anlegen + A-Records
    server_ip = cfg.get("server_public_ip", "") or detect_public_ip()
    if server_ip and not cfg.get("server_public_ip"):
        db.set_config(server_public_ip=server_ip)

    if auto_cf and _cf_configured(cfg):
        if not server_ip:
            steps.append(("CF: A-Records", False, "Eigene Server-IP konnte nicht ermittelt werden."))
        else:
            zones = _cf_zones(cfg)
            zone = next((z for z in zones if z["name"] == hostname), None)
            if not zone:
                zr = _cf_add_zone(cfg, hostname, cfg.get("cloudflare_account_id", ""))
                if not zr.get("success"):
                    errs = "; ".join(e.get("message", "") for e in zr.get("errors", []))
                    steps.append(("CF: Zone anlegen", False, errs))
                    return _wizard_error(request, steps,
                                          "CF-Zone konnte nicht angelegt werden. Gate wird trotzdem gespeichert — DNS musst du manuell setzen.")
                zone = zr.get("result", {})
                ns = ", ".join(zone.get("name_servers", []))
                steps.append(("CF: Zone angelegt", True,
                              f"Nameserver bei Dynadot umstellen auf: {ns}"))
            zid = zone["id"]
            a1 = _cf_add_record(cfg, zid, "A", hostname, server_ip, proxied=False)
            steps.append((f"CF: A @ → {server_ip}", a1.get("success"),
                           "" if a1.get("success") else str(a1.get("errors"))))
            if add_www:
                a2 = _cf_add_record(cfg, zid, "A", f"www.{hostname}", server_ip, proxied=False)
                steps.append((f"CF: A www → {server_ip}", a2.get("success"),
                               "" if a2.get("success") else str(a2.get("errors"))))

    # Optional: Turnstile-Widget für diese Domain
    ts_site, ts_secret = "", ""
    if auto_turnstile and _cf_configured(cfg):
        ts = _cf_create_turnstile_widget(cfg, hostname)
        steps.append(("Turnstile-Widget", ts["ok"], ts.get("msg", "")))
        if ts["ok"]:
            ts_site = ts["site_key"]
            ts_secret = ts["secret_key"]

    # Optional: Logo speichern
    logo_path = ""
    if logo and logo.filename:
        os.makedirs(STATIC_LOGO_DIR, exist_ok=True)
        ext = os.path.splitext(logo.filename)[1].lower() or ".png"
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
            ext = ".png"
        fname = f"gate_{int(time.time())}_{secrets.token_hex(4)}{ext}"
        with open(os.path.join(STATIC_LOGO_DIR, fname), "wb") as fh:
            fh.write(await logo.read())
        logo_path = f"/static/logo/{fname}"

    # Gate anlegen
    gate_id = db.add_gate(
        hostname=hostname,
        mode=mode,
        target_url=target_url.strip(),
        logo_path=logo_path,
        brand_text=brand_text.strip() or cfg.get("brand_text", "Sicherheitsprüfung läuft …"),
        brand_color=brand_color.strip() or "#005eb8",
        turnstile_site_key=ts_site,
        turnstile_secret_key=ts_secret,
    )
    steps.append(("Gate anlegen", True, f"ID {gate_id}, Modus {mode}"))

    # Initial-Batch Ready-Links generieren
    n = max(0, min(int(initial_links or 0), 500))
    for _ in range(n):
        for _try in range(5):
            slug = gen_slug(8)
            if not db.get_gate_link(gate_id, slug):
                db.add_gate_link(gate_id, slug)
                break

    steps.append((f"{n} Ready-Links generiert", True, ""))

    # Erfolgsseite mit allen Schritten + Link-Liste
    links = db.list_gate_links(gate_id, limit=n or 10)
    link_txt = "\n".join(f"https://{hostname}/go/{l['slug']}" for l in links)
    steps_html = "".join(
        f'<li>{"✓" if ok else "✗"} <strong>{escape(label)}</strong>'
        f'{": " + escape(detail) if detail else ""}</li>'
        for label, ok, detail in steps
    )
    return HTMLResponse(f'''
    <div class="alert alert-success">Gate <code>{escape(hostname)}</code> live.</div>
    <div class="card">
        <h3>Ausführungs-Log</h3>
        <ul style="font-size:13px;line-height:1.6">{steps_html}</ul>
    </div>
    <div class="card">
        <h3>Deine ersten {n} Ready-Links</h3>
        <p class="muted">Copy-paste direkt in deine Mailer-Kampagne. Ziel ist überall <code>{escape(target_url)}</code>, änderbar unter Edit.</p>
        <textarea readonly rows="{min(15, max(3, n))}" onclick="this.select()"
                  style="width:100%;font-family:monospace;font-size:12px">{escape(link_txt)}</textarea>
    </div>
    <p><a href="/admin/gates/{gate_id}" class="btn btn-primary btn-sm">Gate bearbeiten</a>
       <a href="/admin/gates" class="btn btn-secondary btn-sm">Zurück zur Übersicht</a></p>
    ''')


def _wizard_error(request, steps, header):
    steps_html = "".join(
        f'<li>{"✓" if ok else "✗"} <strong>{escape(label)}</strong>'
        f'{": " + escape(detail) if detail else ""}</li>'
        for label, ok, detail in steps
    )
    return HTMLResponse(f'<div class="alert alert-danger">{escape(header)}</div>'
                        f'<ul style="font-size:13px">{steps_html}</ul>')


@router.get("/admin/gates/{gate_id}", response_class=HTMLResponse)
async def gate_edit(request: Request, gate_id: int):
    db = request.app.state.db
    g = db.get_gate(gate_id)
    if not g:
        return HTMLResponse('<div class="alert alert-danger">Gate nicht gefunden.</div>', status_code=404)
    links = db.list_gate_links(gate_id, limit=500)
    return request.app.state.templates.TemplateResponse(request, "admin_gate_edit.html", {
        "cfg": db.get_config(),
        "gate": g, "links": links,
        "presets": MODE_PRESETS,
    })


@router.post("/admin/gates/{gate_id}/save")
async def gate_save(request: Request, gate_id: int,
                     mode: str = Form("medium"),
                     target_url: str = Form(""),
                     brand_text: str = Form(""),
                     brand_color: str = Form("#005eb8"),
                     active: str = Form(""),
                     turnstile_site_key: str = Form(""),
                     turnstile_secret_key: str = Form(""),
                     logo: UploadFile = File(None)):
    db = request.app.state.db
    if not db.get_gate(gate_id):
        return RedirectResponse("/admin/gates", status_code=303)
    if mode not in MODE_PRESETS:
        mode = "medium"
    # Turnstile-Handling:
    #   * leer + existierender valider Wert → behalten (verhindert Kill durch
    #     stale Config-Form nach CF-Picker-Apply)
    #   * gültiger neuer Wert (0x…) → übernehmen
    #   * ungültiger Wert → verwerfen (nicht in DB, warnen)
    # Explizites Löschen geht über den "Turnstile deaktivieren"-Button.
    current_gate = db.get_gate(gate_id) or {}
    ts_site_in = turnstile_site_key.strip()
    ts_secret_in = turnstile_secret_key.strip()
    ts_bad = []

    def _keep_or_replace(new_val, current_val, label):
        if not new_val:
            return current_val  # empty = keep existing
        if new_val.startswith("0x") and len(new_val) >= 20:
            return new_val
        # ungültig — behalten + warnen
        ts_bad.append(f"{label} ungültig ({len(new_val)} chars, muss mit 0x anfangen)")
        return current_val

    ts_site = _keep_or_replace(ts_site_in, current_gate.get("turnstile_site_key", ""), "Site-Key")
    ts_secret = _keep_or_replace(ts_secret_in, current_gate.get("turnstile_secret_key", ""), "Secret-Key")

    updates = {
        "mode": mode,
        "target_url": target_url.strip(),
        "brand_text": brand_text.strip() or "Sicherheitsprüfung läuft …",
        "brand_color": brand_color.strip() or "#005eb8",
        "active": 1 if active else 0,
        "turnstile_site_key": ts_site,
        "turnstile_secret_key": ts_secret,
    }
    if logo and logo.filename:
        os.makedirs(STATIC_LOGO_DIR, exist_ok=True)
        ext = os.path.splitext(logo.filename)[1].lower() or ".png"
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
            ext = ".png"
        fname = f"gate_{int(time.time())}_{secrets.token_hex(4)}{ext}"
        with open(os.path.join(STATIC_LOGO_DIR, fname), "wb") as fh:
            fh.write(await logo.read())
        updates["logo_path"] = f"/static/logo/{fname}"
    db.update_gate(gate_id, **updates)
    q = "?saved=1"
    if ts_bad:
        q += "&ts_bad=" + ",".join(ts_bad).replace(" ", "+")[:200]
    return RedirectResponse(f"/admin/gates/{gate_id}{q}", status_code=303)


@router.post("/admin/gates/{gate_id}/turnstile-clear", response_class=HTMLResponse)
async def gate_turnstile_clear(request: Request, gate_id: int):
    """Explizit die Turnstile-Keys leeren (weil normales Save leere Felder
    als 'behalten' interpretiert)."""
    db = request.app.state.db
    if not db.get_gate(gate_id):
        return HTMLResponse('<span style="color:var(--red)">Gate weg</span>')
    db.update_gate(gate_id, turnstile_site_key="", turnstile_secret_key="")
    resp = HTMLResponse('')
    resp.headers["HX-Redirect"] = f"/admin/gates/{gate_id}?ts_cleared=1"
    return resp


@router.post("/admin/gates/{gate_id}/delete")
async def gate_delete(request: Request, gate_id: int):
    request.app.state.db.delete_gate(gate_id)
    return RedirectResponse("/admin/gates", status_code=303)


# ── Link management ────────────────────────────────────────

@router.post("/admin/gates/bulk-edit", response_class=HTMLResponse)
async def bulk_edit_gates(request: Request):
    """Bulk-Edit mehrerer Gates: Target-URL, Modus, Brand-Color, Brand-Text,
    Logo. Jedes Feld optional — leer bleibt unverändert. Logo-Upload
    optional, wird dann an alle ausgewählten Gates geknüpft (gleiches
    Logo für alle)."""
    db = request.app.state.db
    form = await request.form()
    ids = [int(x) for x in form.getlist("gate_ids") if str(x).isdigit()]
    if not ids:
        return HTMLResponse('<div class="alert alert-warning">Keine Gates ausgewählt.</div>')

    updates = {}
    target_url = (form.get("bulk_target_url") or "").strip()
    mode = (form.get("bulk_mode") or "").strip()
    brand_color = (form.get("bulk_brand_color") or "").strip()
    brand_text = (form.get("bulk_brand_text") or "").strip()
    if target_url:
        updates["target_url"] = target_url
    if mode:
        if mode not in MODE_PRESETS:
            return HTMLResponse(f'<div class="alert alert-danger">Ungültiger Modus: {escape(mode)}</div>')
        updates["mode"] = mode
    if brand_color:
        updates["brand_color"] = brand_color
    if brand_text:
        updates["brand_text"] = brand_text

    logo = form.get("bulk_logo")
    if logo and hasattr(logo, "filename") and logo.filename:
        ext = os.path.splitext(logo.filename)[1].lower() or ".png"
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"):
            return HTMLResponse(
                f'<div class="alert alert-danger">Logo hat unerlaubte Endung ({escape(ext)}). '
                f'Erlaubt: png/jpg/jpeg/webp/svg/gif.</div>')
        raw = await logo.read()
        if raw and len(raw) <= 2 * 1024 * 1024:
            os.makedirs(STATIC_LOGO_DIR, exist_ok=True)
            fname = f"bulk_{int(time.time())}_{secrets.token_hex(4)}{ext}"
            with open(os.path.join(STATIC_LOGO_DIR, fname), "wb") as fh:
                fh.write(raw)
            updates["logo_path"] = f"/static/logo/{fname}"
        elif raw:
            return HTMLResponse(
                f'<div class="alert alert-danger">Logo zu groß '
                f'({len(raw)//1024} KB, max 2048 KB).</div>')

    if not updates:
        return HTMLResponse(
            '<div class="alert alert-warning">Nichts geändert — alle Felder waren leer. '
            'Mindestens eins ausfüllen (oder Logo hochladen).</div>')

    done = 0
    missing = 0
    for gid in ids:
        if not db.get_gate(gid):
            missing += 1
            continue
        db.update_gate(gid, **updates)
        done += 1

    changed_fields = ", ".join(
        f"<code>{k}</code>" + (" (Logo)" if k == "logo_path" else "")
        for k in updates
    )
    note = f" · {missing} nicht gefunden" if missing else ""
    return HTMLResponse(
        f'<div class="alert alert-success">'
        f'✓ {done} Gate(s) aktualisiert{note} — geändert: {changed_fields}'
        f'</div>'
        f'<p class="muted" style="font-size:11px;margin-top:4px">'
        f'Reload der Seite um die neuen Werte in der Tabelle zu sehen.</p>'
    )


@router.post("/admin/gates/bulk-generate-links", response_class=HTMLResponse)
async def bulk_generate_links(request: Request,
                                 count_per_gate: int = Form(10),
                                 slug_length: int = Form(8)):
    """Bulk: N Ready-Links pro ausgewähltem Gate generieren.
    Rückgabe: kopierbare Textarea mit allen URLs über alle Gates —
    genau der "5 Domains, 10 Links each, alles in einem"-Case."""
    db = request.app.state.db
    form = await request.form()
    ids = [int(x) for x in form.getlist("gate_ids") if str(x).isdigit()]
    if not ids:
        return HTMLResponse('<div class="alert alert-warning">Keine Gates ausgewählt — '
                             'oben in der Liste Checkboxen setzen.</div>')
    n_per = max(1, min(int(count_per_gate or 10), 500))
    slug_len = max(4, min(int(slug_length or 8), 24))
    lines = []
    total = 0
    per_domain = []
    for gid in ids:
        g = db.get_gate(gid)
        if not g:
            per_domain.append((gid, "gate weg", 0))
            continue
        gd = dict(g)
        host = gd["hostname"]
        made = 0
        for _ in range(n_per):
            for _try in range(5):
                slug = gen_slug(slug_len)
                if not db.get_gate_link(gid, slug):
                    db.add_gate_link(gid, slug)
                    lines.append(f"https://{host}/go/{slug}")
                    made += 1
                    break
        per_domain.append((gid, host, made))
        total += made
    per_dom_html = "".join(
        f'<div style="font-size:11px;color:var(--fg2)">✓ '
        f'<code>{escape(str(host))}</code>: <strong>{made}</strong> Links</div>'
        for _gid, host, made in per_domain
    )
    return HTMLResponse(
        f'<div class="card" style="margin-top:8px;background:#f0fff4">'
        f'<h3>&#128279; {total} Ready-Links über {len(ids)} Domain(s) generiert</h3>'
        f'{per_dom_html}'
        f'<p class="muted" style="font-size:11px;margin:6px 0">'
        f'Ein Klick markiert alles. Der Mailer hängt beim Versand '
        f'<code>?ref=…</code> automatisch dran, das Gate reicht es ans Ziel weiter.</p>'
        f'<textarea readonly rows="{min(len(lines)+1, 20)}" onclick="this.select()" '
        f'style="width:100%;font-family:monospace;font-size:11px">'
        + escape("\n".join(lines)) +
        f'</textarea></div>'
    )


@router.post("/admin/gates/{gate_id}/links/generate", response_class=HTMLResponse)
async def links_generate(request: Request, gate_id: int,
                          count: int = Form(10), slug_length: int = Form(8),
                          target_override: str = Form(""), label: str = Form("")):
    db = request.app.state.db
    if not db.get_gate(gate_id):
        return HTMLResponse('<span style="color:var(--red)">Gate weg</span>')
    n = max(1, min(int(count or 10), 500))
    slug_len = max(4, min(int(slug_length or 8), 24))
    override = target_override.strip()
    generated = 0
    for _ in range(n):
        for _try in range(5):
            slug = gen_slug(slug_len)
            if not db.get_gate_link(gate_id, slug):
                db.add_gate_link(gate_id, slug, override, label.strip())
                generated += 1
                break
    return HTMLResponse(f'<div class="alert alert-success">{generated} Links generiert. '
                        f'<a href="/admin/gates/{gate_id}" style="color:var(--accent)">Reload</a></div>')


@router.get("/admin/gates/{gate_id}/links.txt")
async def links_export(request: Request, gate_id: int):
    db = request.app.state.db
    g = db.get_gate(gate_id)
    if not g:
        return PlainTextResponse("no gate", status_code=404)
    links = db.list_gate_links(gate_id, limit=10000)
    body = "\n".join(f"https://{g['hostname']}/go/{l['slug']}" for l in links)
    from fastapi.responses import Response
    return Response(body, media_type="text/plain",
                    headers={"Content-Disposition": f"attachment; filename=gate_{gate_id}_links.txt"})


@router.post("/admin/gates/{gate_id}/links/clear")
async def links_clear(request: Request, gate_id: int):
    request.app.state.db.delete_all_gate_links(gate_id)
    return RedirectResponse(f"/admin/gates/{gate_id}", status_code=303)


# ── Caddy on-demand TLS check ─────────────────────────────

@router.get("/tls-check")
async def tls_check(request: Request, domain: str = ""):
    """Caddy asks us before requesting a Let's-Encrypt cert:
    'darf ich für <domain> ein Cert holen?' — wir sagen ja, wenn's ein
    aktives Gate ist oder die Panel-Domain selbst."""
    db = request.app.state.db
    cfg = db.get_config()
    d = (domain or "").strip().lower()
    if not d:
        return PlainTextResponse("no domain", status_code=400)
    if db.get_gate_by_host(d):
        return PlainTextResponse("ok")
    # Panel-Domain — die dürfen wir auch selbst
    panel_default = (cfg.get("panel_hostname") or "").strip().lower()
    if panel_default and d == panel_default:
        return PlainTextResponse("ok")
    return PlainTextResponse("not allowed", status_code=404)
