"""SMTP Presets page — CRUD + editing + connection testing."""
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()


@router.get("/smtp", response_class=HTMLResponse)
async def smtp_page(request: Request):
    db = request.app.state.db
    db.reset_daily_counts()
    smtps = [dict(s) for s in db.get_smtps()]
    return request.app.state.templates.TemplateResponse(request, "smtp.html",
        {"active": "smtp", "smtps": smtps, "db": db})


@router.post("/smtp/add")
async def add_smtp(request: Request, name: str = Form(""), host: str = Form(""),
                    port: int = Form(587), username: str = Form(""),
                    password: str = Form(""), provider_type: str = Form("generic"),
                    daily_limit: int = Form(0), proxy: str = Form(""),
                    proxy_required: int = Form(0),
                    threads_per_smtp: int = Form(1)):
    request.app.state.db.add_smtp(name, host, port, username, password,
                                   provider_type, daily_limit, proxy, proxy_required)
    lid = request.app.state.db._conn().execute("SELECT last_insert_rowid()").fetchone()[0]
    request.app.state.db._conn().execute(
        "UPDATE smtp_presets SET threads_per_smtp=? WHERE id=?",
        (max(1, min(threads_per_smtp, 50)), lid))
    request.app.state.db._conn().commit()
    return RedirectResponse("/smtp", status_code=303)


@router.post("/smtp/{sid}/save")
async def save_smtp(request: Request, sid: int,
                    name: str = Form(""), host: str = Form(""),
                    port: int = Form(587), username: str = Form(""),
                    password: str = Form(""), provider_type: str = Form("generic"),
                    daily_limit: int = Form(0), proxy: str = Form(""),
                    proxy_required: int = Form(0),
                    threads_per_smtp: int = Form(1)):
    db = request.app.state.db
    c = db._conn()
    tps = max(1, min(threads_per_smtp, 50))
    if password.strip():
        c.execute("UPDATE smtp_presets SET name=?,host=?,port=?,username=?,password=?,"
                  "provider_type=?,daily_limit=?,proxy=?,proxy_required=?,threads_per_smtp=? WHERE id=?",
                  (name, host, port, username, password, provider_type,
                   daily_limit, proxy, proxy_required, tps, sid))
    else:
        c.execute("UPDATE smtp_presets SET name=?,host=?,port=?,username=?,"
                  "provider_type=?,daily_limit=?,proxy=?,proxy_required=?,threads_per_smtp=? WHERE id=?",
                  (name, host, port, username, provider_type,
                   daily_limit, proxy, proxy_required, tps, sid))
    c.commit()
    return RedirectResponse("/smtp", status_code=303)


@router.post("/smtp/import-ses", response_class=HTMLResponse)
async def import_ses(request: Request,
                      ses_text: str = Form(""),
                      region: str = Form("eu-central-1"),
                      config_set: str = Form(""),
                      daily_limit: int = Form(0)):
    """Bulk-Import SES-Accounts. Zeilenformat:
       IAM_KEY,IAM_SECRET[,region[,config_set[,name]]]
       Pipe (|) statt Komma auch OK."""
    added = request.app.state.db.import_ses_smtps(
        ses_text, default_region=region.strip() or "eu-central-1",
        default_config_set=config_set.strip(), daily_limit=daily_limit)
    return HTMLResponse(
        f'<div class="alert alert-success">{added} SES-Account(s) importiert. '
        f'<a href="/smtp" style="color:var(--accent)">Reload</a></div>')


@router.post("/smtp/{sid}/delete")
async def delete_smtp(request: Request, sid: int):
    request.app.state.db.delete_smtp(sid)
    return RedirectResponse("/smtp", status_code=303)


@router.post("/smtp/{sid}/test", response_class=HTMLResponse)
async def test_smtp(request: Request, sid: int):
    db = request.app.state.db
    row = db._conn().execute("SELECT * FROM smtp_presets WHERE id=?", (sid,)).fetchone()
    if not row:
        return HTMLResponse('<span style="color:var(--red)">SMTP not found</span>')

    row = dict(row)
    from bulk.mailer.smtp_client import SMTPClient
    client = SMTPClient(
        row["host"], row["port"], row["username"], row["password"],
        proxy=row.get("proxy", ""),
        proxy_required=bool(row.get("proxy_required", 0)),
        send_mode=row.get("send_mode", "smtp") or "smtp",
        region=row.get("ses_region", "") or "",
        config_set=row.get("ses_config_set", "") or "",
    )

    results = []

    # SES-Accounts nutzen kein Proxy und kein SMTP-Handshake
    if not client.is_ses_api and client.has_proxy:
        ok, msg = client.test_proxy()
        color = "var(--green)" if ok else "var(--red)"
        icon = "&#10003;" if ok else "&#10007;"
        results.append(f'<div style="color:{color};font-size:13px">{icon} Proxy: {msg}</div>')

    ok, msg = client.test_connection()
    color = "var(--green)" if ok else "var(--red)"
    icon = "&#10003;" if ok else "&#10007;"
    label = "SES-API" if client.is_ses_api else "SMTP"
    results.append(f'<div style="color:{color};font-size:13px">{icon} {label}: {msg}</div>')

    return HTMLResponse("".join(results))
