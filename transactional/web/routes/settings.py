"""Settings — global config, logs, utilities."""
import os
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()

LOG_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "smtp_errors.log")


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    db = request.app.state.db
    config = db.get_config()
    log_text = _read_log(LOG_FILE)
    return request.app.state.templates.TemplateResponse(request, "trans_settings.html", {
        "active": "settings", "config": config, "log_text": log_text, "db": db,
    })


@router.post("/settings/save")
async def save_settings(request: Request):
    form = await request.form()
    config = {}
    for key in form:
        config[key] = form[key]
    request.app.state.db.save_config(config)
    return RedirectResponse("/settings", status_code=303)


@router.get("/settings/log", response_class=HTMLResponse)
async def log_tail(request: Request):
    text = _read_log(LOG_FILE)
    from html import escape
    return HTMLResponse(f'<pre class="log">{escape(text)}</pre>')


@router.post("/settings/log/clear")
async def clear_log(request: Request):
    path = os.path.abspath(LOG_FILE)
    try:
        if os.path.isfile(path):
            with open(path, "w") as f:
                f.write("")
    except Exception:
        pass
    return RedirectResponse("/settings", status_code=303)


def _read_log(path: str, lines: int = 50) -> str:
    try:
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            return "(no log file)"
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return "".join(tail)
    except Exception as e:
        return f"(error: {e})"
