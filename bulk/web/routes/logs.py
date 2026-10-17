"""Logs — SMTP error log viewer + mailing history."""
import os
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()

LOG_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "smtp_errors.log")


def _read_log_tail(path: str, lines: int = 50) -> str:
    try:
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            return "(no log file found)"
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return "".join(tail)
    except Exception as e:
        return f"(error reading log: {e})"


def _get_mailing_history(db) -> list:
    rows = db._conn().execute(
        "SELECT m.id, m.name, m.status, m.total_leads, m.sent, m.failed, "
        "m.excluded, m.started_at, m.finished_at, m.created_at, "
        "s.name AS smtp_name, d.domain "
        "FROM mailings m "
        "LEFT JOIN smtp_presets s ON m.smtp_preset_id = s.id "
        "LEFT JOIN domains d ON m.domain_id = d.id "
        "ORDER BY m.created_at DESC LIMIT 100"
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    db = request.app.state.db
    tpl = request.app.state.templates
    log_text = _read_log_tail(LOG_FILE, 50)
    history = _get_mailing_history(db)
    return tpl.TemplateResponse(request, "logs.html", {
        "active": "logs", "db": db,
        "log_text": log_text, "history": history,
    })


@router.post("/logs/clear")
async def clear_log(request: Request):
    path = os.path.abspath(LOG_FILE)
    try:
        if os.path.isfile(path):
            with open(path, "w") as f:
                f.write("")
    except Exception:
        pass
    return RedirectResponse("/logs", status_code=303)


@router.get("/logs/tail", response_class=HTMLResponse)
async def log_tail(request: Request):
    """HTMX endpoint — returns last 50 lines of the SMTP error log."""
    text = _read_log_tail(LOG_FILE, 50)
    # Return raw preformatted text for HTMX swap
    from html import escape
    return HTMLResponse(f'<pre class="log">{escape(text)}</pre>')
