"""Job-Panel + Cancel-Endpoints. Der Panel wird im base.html überall
eingebunden und pollt sich alle 2s selbst nach — so sieht der User jede
laufende Generation, egal auf welchem Tab er ist."""
from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..jobs import job_manager

router = APIRouter()


def _job_row_html(j) -> str:
    pct = j.pct()
    status_color = {
        "running": "var(--accent)",
        "done": "var(--green)",
        "cancelled": "var(--orange, #f5a623)",
        "error": "var(--red)",
    }.get(j.status, "var(--fg2)")
    kind_badge = escape(j.kind.replace("_", " "))
    title = escape(j.title or j.kind)
    page = escape(j.page_url or "")
    page_link = (f'<a href="{page}" style="color:var(--accent);font-size:11px;'
                  f'text-decoration:none">↗ öffnen</a>' if page else "")

    if j.status == "running":
        bar = (f'<div style="background:var(--bg2);border-radius:3px;'
                f'height:6px;overflow:hidden;margin:4px 0">'
                f'<div style="width:{pct}%;height:100%;background:{status_color};'
                f'transition:width .3s"></div></div>'
                f'<div style="font-size:10px;color:var(--fg2)">'
                f'{j.done}/{j.total} · {j.ok} ok · {j.errors} err · {pct}%</div>')
        cancel_btn = (f'<button class="btn btn-danger btn-xs" '
                       f'hx-post="/jobs/{j.id}/cancel" '
                       f'hx-target="closest .job-row" hx-swap="outerHTML" '
                       f'style="padding:1px 6px;font-size:10px">abbrechen</button>')
    else:
        status_lbl = j.status.upper()
        err_html = ""
        if j.status == "error" and j.error_msg:
            err_html = (f'<div style="font-size:10px;color:var(--red)">'
                         f'{escape(j.error_msg[:200])}</div>')
        bar = (f'<div style="font-size:10px;color:{status_color};font-weight:600">'
                f'✓ {status_lbl}: {j.done}/{j.total} ({j.ok} ok, {j.errors} err)'
                f'</div>{err_html}')
        cancel_btn = (f'<button class="btn btn-secondary btn-xs" '
                       f'hx-post="/jobs/{j.id}/dismiss" '
                       f'hx-target="closest .job-row" hx-swap="outerHTML" '
                       f'style="padding:1px 6px;font-size:10px">×</button>')

    log_html = ""
    if j.log:
        recent = j.log[-3:]
        log_html = (f'<details style="margin-top:2px">'
                     f'<summary style="font-size:10px;color:var(--fg2);cursor:pointer">'
                     f'log ({len(j.log)})</summary>'
                     f'<pre style="font-size:9px;max-height:100px;overflow:auto;'
                     f'margin:2px 0;padding:2px 4px;background:var(--bg2);'
                     f'border-radius:2px">'
                     f'{escape(chr(10).join(recent))}</pre></details>')

    return (
        f'<div class="job-row" style="border-left:2px solid {status_color};'
        f'padding:4px 6px 4px 8px;margin-bottom:4px;background:var(--bg1);'
        f'border-radius:2px">'
        f'<div style="display:flex;gap:6px;align-items:center">'
        f'<span style="font-size:11px;font-weight:600;flex:1;overflow:hidden;'
        f'text-overflow:ellipsis;white-space:nowrap">{title}</span>'
        f'{page_link}{cancel_btn}</div>'
        f'<div style="font-size:9px;color:var(--fg3);text-transform:uppercase">'
        f'{kind_badge}</div>'
        f'{bar}{log_html}</div>'
    )


@router.get("/jobs/panel", response_class=HTMLResponse)
async def jobs_panel(request: Request):
    """HTML-Fragment mit allen aktiven Jobs des Users, plus die letzten
    3 fertigen (mit dismiss-Button). Poll-Ziel für das Widget."""
    if not getattr(request.state, "user", None):
        return HTMLResponse("")
    uid = request.state.user["id"]
    active = job_manager.list_active(uid)
    recent = job_manager.list_recent(uid, limit=3)

    if not active and not recent:
        # Nichts los → Widget ausblenden aber weiter pollen
        return HTMLResponse(
            '<div id="jobs-widget" hx-get="/jobs/panel" hx-trigger="every 3s" '
            'hx-swap="outerHTML" style="display:none"></div>'
        )

    # Poll-Interval: schneller wenn was läuft
    interval = "every 1s" if active else "every 5s"

    parts = ['<div id="jobs-widget" hx-get="/jobs/panel" '
             f'hx-trigger="{interval}" hx-swap="outerHTML" '
             'style="position:fixed;bottom:12px;right:12px;width:320px;'
             'z-index:9999;background:var(--bg);border:1px solid var(--border);'
             'border-radius:6px;box-shadow:0 4px 16px rgba(0,0,0,.15);'
             'max-height:70vh;overflow-y:auto">']
    parts.append(
        f'<div style="padding:6px 10px;font-size:11px;font-weight:600;'
        f'border-bottom:1px solid var(--border);background:var(--bg2);'
        f'display:flex;align-items:center;gap:6px">'
        f'<span>⚡ Jobs</span>'
        f'<span style="color:var(--fg2);font-weight:400">'
        f'{len(active)} aktiv · {len(recent)} fertig</span>'
        f'</div>'
    )
    parts.append('<div style="padding:6px">')
    for j in active:
        parts.append(_job_row_html(j))
    for j in recent:
        parts.append(_job_row_html(j))
    parts.append('</div></div>')
    return HTMLResponse("".join(parts))


@router.post("/jobs/{jid}/cancel", response_class=HTMLResponse)
async def cancel_job(request: Request, jid: int):
    if not getattr(request.state, "user", None):
        return HTMLResponse('<div class="job-row"></div>')
    uid = request.state.user["id"]
    ok = job_manager.cancel(jid, uid)
    j = job_manager.get(jid)
    if not j:
        return HTMLResponse('<div class="job-row"></div>')
    return HTMLResponse(_job_row_html(j))


@router.post("/jobs/{jid}/dismiss", response_class=HTMLResponse)
async def dismiss_job(request: Request, jid: int):
    """Entfernt einen fertigen Job aus der Anzeige. Laufende können
    nicht dismissed werden — erst cancel, dann dismiss."""
    if not getattr(request.state, "user", None):
        return HTMLResponse('<div class="job-row"></div>')
    uid = request.state.user["id"]
    j = job_manager.get(jid)
    if j and j.user_id == uid and j.status != "running":
        job_manager._jobs.pop(jid, None)
    return HTMLResponse("")
