"""DBL Watcher — Routes für UI, Add/Delete/Toggle, Settings, Manual Check,
Test-Telegram. Scheduler läuft als asyncio-Task im lifespan."""
from __future__ import annotations

import json
import logging
from html import escape

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from bulk.mailer.dbl_checker import check_domain, send_telegram, format_alert
from bulk.mailer import dbl_scheduler

logger = logging.getLogger("bulk.dbl")
router = APIRouter()


def _settings(db) -> dict:
    row = db._conn().execute("SELECT * FROM dbl_settings WHERE id=1").fetchone()
    return dict(row) if row else {}


def _watches(db) -> list[dict]:
    rows = db._conn().execute(
        "SELECT * FROM dbl_watch ORDER BY status='listed' DESC, domain ASC"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["detail_parsed"] = json.loads(d.get("detail") or "{}")
        except Exception:
            d["detail_parsed"] = {}
        out.append(d)
    return out


@router.get("/dbl", response_class=HTMLResponse)
async def dbl_page(request: Request):
    db = request.app.state.db
    s = _settings(db)
    watches = _watches(db)
    last_summary = {}
    try:
        last_summary = json.loads(s.get("last_run_summary") or "{}")
    except Exception:
        pass
    return request.app.state.templates.TemplateResponse(request, "dbl.html", {
        "active": "dbl",
        "settings": s,
        "watches": watches,
        "last_summary": last_summary,
        "stats": {
            "total": len(watches),
            "listed": sum(1 for w in watches if w.get("status") == "listed"),
            "ok":     sum(1 for w in watches if w.get("status") == "ok"),
            "error":  sum(1 for w in watches if w.get("status") == "error"),
            "pending":sum(1 for w in watches if w.get("status") == "pending"),
        },
    })


@router.post("/dbl/add")
async def dbl_add(request: Request, domains: str = Form(...)):
    db = request.app.state.db
    added, skipped = 0, 0
    for raw in domains.replace(",", "\n").splitlines():
        d = raw.strip().strip(".").lower()
        if not d or "." not in d:
            continue
        try:
            db._conn().execute(
                "INSERT INTO dbl_watch (domain) VALUES (?)", (d,))
            added += 1
        except Exception:
            skipped += 1
    db._conn().commit()
    logger.info("DBL add: %d added, %d skipped", added, skipped)
    return RedirectResponse(f"/dbl?added={added}&skipped={skipped}", status_code=303)


@router.post("/dbl/{wid}/delete")
async def dbl_delete(request: Request, wid: int):
    db = request.app.state.db
    db._conn().execute("DELETE FROM dbl_watch WHERE id=?", (wid,))
    db._conn().commit()
    return RedirectResponse("/dbl", status_code=303)


@router.post("/dbl/{wid}/toggle")
async def dbl_toggle(request: Request, wid: int):
    db = request.app.state.db
    db._conn().execute(
        "UPDATE dbl_watch SET enabled=1-enabled WHERE id=?", (wid,))
    db._conn().commit()
    return RedirectResponse("/dbl", status_code=303)


@router.post("/dbl/{wid}/recheck", response_class=HTMLResponse)
async def dbl_recheck(request: Request, wid: int):
    """Einzelcheck für eine Domain — gibt das aktualisierte Table-Row-HTML zurück."""
    db = request.app.state.db
    row = db._conn().execute("SELECT * FROM dbl_watch WHERE id=?", (wid,)).fetchone()
    if not row:
        return HTMLResponse("<tr><td colspan='99'>nicht gefunden</td></tr>")
    settings = _settings(db)
    try:
        dbl_scheduler.check_one(db, dict(row), settings)
    except Exception as e:
        logger.exception("dbl recheck failed: %s", e)
    # neu laden & als Row rendern
    row = dict(db._conn().execute(
        "SELECT * FROM dbl_watch WHERE id=?", (wid,)).fetchone())
    try:
        row["detail_parsed"] = json.loads(row.get("detail") or "{}")
    except Exception:
        row["detail_parsed"] = {}
    return request.app.state.templates.TemplateResponse(
        request, "_dbl_row.html", {"w": row})


@router.post("/dbl/recheck-all")
async def dbl_recheck_all(request: Request):
    """Kompletten Cycle sofort auslösen (synchron, kann je nach Domain-Anzahl
    ein paar Sekunden dauern)."""
    db = request.app.state.db
    summary = dbl_scheduler.run_cycle(db)
    return RedirectResponse(
        f"/dbl?ran=1&checked={summary.get('checked', 0)}"
        f"&new={len(summary.get('newly_listed', []))}", status_code=303)


@router.post("/dbl/settings")
async def dbl_settings_update(
    request: Request,
    enabled: int = Form(0),
    check_interval_minutes: int = Form(60),
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    resolvers: str = Form(""),
    dqs_key: str = Form(""),
    check_dbl: int = Form(0),
    check_surbl: int = Form(0),
    check_uribl: int = Form(0),
    renotify_hours: int = Form(24),
):
    db = request.app.state.db
    db._conn().execute("""
        UPDATE dbl_settings SET
            enabled=?, check_interval_minutes=?,
            telegram_bot_token=?, telegram_chat_id=?,
            resolvers=?, dqs_key=?,
            check_dbl=?, check_surbl=?, check_uribl=?,
            renotify_hours=?
        WHERE id=1
    """, (
        1 if enabled else 0,
        max(1, int(check_interval_minutes or 60)),
        telegram_bot_token.strip(), telegram_chat_id.strip(),
        resolvers.strip(), dqs_key.strip(),
        1 if check_dbl else 0,
        1 if check_surbl else 0,
        1 if check_uribl else 0,
        max(1, int(renotify_hours or 24)),
    ))
    db._conn().commit()
    return RedirectResponse("/dbl?saved=1", status_code=303)


@router.post("/dbl/test-telegram", response_class=HTMLResponse)
async def dbl_test_telegram(request: Request):
    db = request.app.state.db
    s = _settings(db)
    token = (s.get("telegram_bot_token") or "").strip()
    chat_id = (s.get("telegram_chat_id") or "").strip()
    if not token or not chat_id:
        return HTMLResponse(
            "<span class='v-block'>Token oder Chat-ID fehlt</span>")
    ok, err = send_telegram(
        token, chat_id,
        "✅ <b>DBL-Watcher Test</b>\nTelegram-Verbindung funktioniert.")
    if ok:
        return HTMLResponse("<span class='v-allow'>✓ Nachricht geschickt</span>")
    return HTMLResponse(f"<span class='v-block'>Fehler: {escape(err)}</span>")


@router.post("/dbl/test-check", response_class=HTMLResponse)
async def dbl_test_check(request: Request, test_domain: str = Form(...)):
    """Einmaliger ad-hoc-Check einer Domain OHNE sie zu speichern — damit man
    schnell probieren kann ob der eigene Resolver funktioniert."""
    db = request.app.state.db
    s = _settings(db)
    r = check_domain(
        test_domain.strip(),
        check_dbl=bool(s.get("check_dbl", 1)),
        check_surbl=bool(s.get("check_surbl", 0)),
        check_uribl=bool(s.get("check_uribl", 0)),
        dqs_key=(s.get("dqs_key") or "").strip(),
        resolvers=[x.strip() for x in (s.get("resolvers") or "").replace(";", ",").split(",") if x.strip()],
    )
    parts = [f"<b>{escape(r.domain)}</b> → <code>{escape(r.status)}</code>"]
    for l in r.lists:
        if l.listed:
            parts.append(
                f"<span class='v-block'>{escape(l.name.upper())}: "
                f"{escape(', '.join(l.reasons) or ', '.join(l.codes))}</span>")
        elif l.error:
            parts.append(
                f"<span class='v-warn'>{escape(l.name.upper())}: err {escape(l.error)}</span>")
        else:
            parts.append(f"<span class='v-allow'>{escape(l.name.upper())}: ok</span>")
    return HTMLResponse("<div>" + " · ".join(parts) + "</div>")
