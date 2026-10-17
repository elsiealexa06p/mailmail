"""Macros — spintax pools, presets, file import, edit."""
import os
from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import List as TList

router = APIRouter()


@router.get("/macros", response_class=HTMLResponse)
async def macros_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    macros = [dict(m) for m in db.get_macros(uid)]
    for m in macros:
        lines = [l for l in (m.get("values_text") or "").splitlines() if l.strip()]
        m["line_count"] = len(lines)

    grouped = {}
    for m in macros:
        grouped.setdefault(m["name"], []).append(m)

    return request.app.state.templates.TemplateResponse(request, "macros.html", {
        "active": "macros", "macros": macros, "grouped": grouped, "db": db,
    })


@router.post("/macros/add")
async def add_macro(request: Request, name: str = Form(""),
                    preset_name: str = Form(""),
                    values_text: str = Form(""), rotate_every: int = Form(0)):
    if name.strip():
        uid = request.state.user['id']
        request.app.state.db.add_macro(name.strip(), values_text, rotate_every, uid,
                                        preset_name.strip() or "Default")
    return RedirectResponse("/macros", status_code=303)


@router.post("/macros/{mid}/save")
async def save_macro(request: Request, mid: int,
                     values_text: str = Form(""), rotate_every: int = Form(0),
                     preset_name: str = Form(""),
                     sticky: int = Form(0)):
    db = request.app.state.db
    db.update_macro(mid, values_text, rotate_every)
    c = db._conn()
    updates = []
    params = []
    if preset_name.strip():
        updates.append("preset_name=?")
        params.append(preset_name.strip())
    updates.append("sticky=?")
    params.append(1 if sticky else 0)
    params.append(mid)
    c.execute(f"UPDATE trans_macros SET {', '.join(updates)} WHERE id=?", params)
    c.commit()
    return RedirectResponse("/macros", status_code=303)


@router.post("/macros/{mid}/activate")
async def activate_macro(request: Request, mid: int):
    request.app.state.db.activate_macro(mid)
    return RedirectResponse("/macros", status_code=303)


@router.post("/macros/{mid}/duplicate")
async def duplicate_macro(request: Request, mid: int,
                          new_preset_name: str = Form("")):
    db = request.app.state.db
    row = db._conn().execute("SELECT * FROM trans_macros WHERE id=?", (mid,)).fetchone()
    if row:
        row = dict(row)
        preset = new_preset_name.strip() or f"{row.get('preset_name', 'Default')} Copy"
        db.add_macro(row["name"], row.get("values_text", ""),
                     row.get("rotate_every", 0), row.get("user_id", 0), preset)
    return RedirectResponse("/macros", status_code=303)


@router.post("/macros/add-preset")
async def add_preset(request: Request, macro_name: str = Form(""),
                     preset_name: str = Form("")):
    if macro_name.strip() and preset_name.strip():
        uid = request.state.user['id']
        request.app.state.db.add_macro(macro_name.strip(), "", 0, uid, preset_name.strip())
    return RedirectResponse("/macros", status_code=303)


@router.post("/macros/{mid}/delete")
async def delete_macro(request: Request, mid: int):
    request.app.state.db.delete_macro(mid)
    return RedirectResponse("/macros", status_code=303)


@router.post("/macros/import-files", response_class=HTMLResponse)
async def import_files(request: Request, files: TList[UploadFile] = File(...)):
    """Import .txt files as macros. Filename (without ext) = macro name."""
    db = request.app.state.db
    uid = request.state.user['id']
    added = 0
    for f in files:
        if not f.filename:
            continue
        content = (await f.read()).decode("utf-8", errors="replace")
        name = os.path.splitext(f.filename)[0]
        db.add_macro(name, content.strip(), 0, uid)
        added += 1
    from html import escape
    return HTMLResponse(
        f'<div class="alert alert-success">{added} macro(s) imported. '
        f'<a href="/macros" style="color:var(--accent)">Reload</a></div>')
