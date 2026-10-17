"""Snippets — Live-Assembly-Bausteine für Kampagnen im Assembly-Mode.

Pro Slot (header/intro/body/outro/footer) hat der User einen Pool von
HTML-Fragmenten. Ist Assembly-Mode auf einer Kampagne an, würfelt der
Sende-Loop pro Mail aus jedem Slot einen Snippet und setzt sie zusammen.
"""
from html import escape
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()

SLOTS = ("header", "intro", "body", "outro", "footer")
SLOT_LABELS = {
    "header": "Header (oben, evtl. Logo/Brand)",
    "intro":  "Intro (Begrüßung + Opener)",
    "body":   "Body (Hauptinhalt)",
    "outro":  "Outro (Abschluss)",
    "footer": "Footer (Signatur, Unsubscribe)",
}


@router.get("/snippets", response_class=HTMLResponse)
async def snippets_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    rows = [dict(r) for r in db.get_snippets(uid)]
    grouped = {slot: [] for slot in SLOTS}
    for r in rows:
        if r["slot"] in grouped:
            grouped[r["slot"]].append(r)
    return request.app.state.templates.TemplateResponse(request, "snippets.html", {
        "active": "snippets",
        "slots": SLOTS,
        "slot_labels": SLOT_LABELS,
        "grouped": grouped,
    })


@router.post("/snippets/add")
async def add_snippet(request: Request,
                        slot: str = Form(""),
                        label: str = Form(""),
                        content: str = Form("")):
    if slot not in SLOTS:
        return RedirectResponse("/snippets", status_code=303)
    if not content.strip():
        return RedirectResponse("/snippets", status_code=303)
    uid = request.state.user["id"]
    request.app.state.db.add_snippet(slot, label, content, uid)
    return RedirectResponse("/snippets", status_code=303)


@router.post("/snippets/{sid}/save")
async def save_snippet(request: Request, sid: int,
                        label: str = Form(""),
                        content: str = Form(""),
                        is_active: int = Form(0)):
    db = request.app.state.db
    db.update_snippet(sid, label=label.strip(), content=content,
                       is_active=1 if is_active else 0)
    return RedirectResponse("/snippets", status_code=303)


@router.post("/snippets/{sid}/toggle")
async def toggle_snippet(request: Request, sid: int):
    request.app.state.db.toggle_snippet(sid)
    return RedirectResponse("/snippets", status_code=303)


@router.post("/snippets/{sid}/delete")
async def delete_snippet(request: Request, sid: int):
    request.app.state.db.delete_snippet(sid)
    return RedirectResponse("/snippets", status_code=303)


@router.get("/snippets/preview", response_class=HTMLResponse)
async def preview_assembly(request: Request):
    """Zufällige Assembly zeigen — wie eine Mail im Send-Loop aussehen würde."""
    db = request.app.state.db
    uid = request.state.user["id"]
    rows = [dict(r) for r in db.get_active_snippets(uid)]
    from mailer.assembly import group_snippets_by_slot, assemble_html
    grouped = group_snippets_by_slot(rows)
    if not grouped:
        return HTMLResponse('<div class="alert alert-warning">Keine aktiven '
                             'Snippets vorhanden.</div>')
    html = assemble_html(grouped)
    return HTMLResponse(
        '<div style="border:1px solid var(--border);border-radius:var(--radius);'
        'max-height:500px;overflow:auto;padding:8px;background:#fff">'
        + html +
        '</div>'
        f'<pre style="font-size:11px;background:var(--bg);padding:8px;'
        f'margin-top:8px;overflow:auto;max-height:200px">{escape(html[:2000])}'
        f'{"..." if len(html) > 2000 else ""}</pre>'
    )
