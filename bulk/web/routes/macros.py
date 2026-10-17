"""Macros — CRUD + import/export."""
import json
from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

router = APIRouter()


@router.get("/macros", response_class=HTMLResponse)
async def macros_page(request: Request):
    db = request.app.state.db
    tpl = request.app.state.templates
    macros = []
    for m in db.get_macros():
        md = dict(m)
        md["values"] = json.loads(md.get("values_json") or "[]")
        macros.append(md)
    return tpl.TemplateResponse(request, "macros.html", {
        "active": "macros", "macros": macros, "db": db,
    })


@router.post("/macros/add")
async def add_macro(request: Request,
                    name: str = Form(""),
                    values: str = Form("")):
    name = name.strip()
    if not name:
        return RedirectResponse("/macros", status_code=303)
    vals = [v.strip() for v in values.splitlines() if v.strip()]
    request.app.state.db.add_macro(name, vals)
    return RedirectResponse("/macros", status_code=303)


@router.post("/macros/{mid}/save")
async def save_macro(request: Request, mid: int,
                     values: str = Form(""),
                     rotate_every: int = Form(0)):
    vals = [v.strip() for v in values.splitlines() if v.strip()]
    db = request.app.state.db
    db.update_macro(mid, vals)
    c = db._conn()
    c.execute("UPDATE macros SET rotate_every=? WHERE id=?", (rotate_every, mid))
    c.commit()
    return RedirectResponse("/macros", status_code=303)


@router.post("/macros/{mid}/delete")
async def delete_macro(request: Request, mid: int):
    request.app.state.db.delete_macro(mid)
    return RedirectResponse("/macros", status_code=303)


@router.get("/macros/export")
async def export_macros(request: Request):
    data = request.app.state.db.export_macros()
    return JSONResponse(
        content=json.loads(data),
        headers={"Content-Disposition": "attachment; filename=macros.json"},
    )


@router.post("/macros/import")
async def import_macros(request: Request, file: UploadFile = File(...)):
    content = (await file.read()).decode("utf-8", errors="replace")
    db = request.app.state.db
    try:
        added = db.import_macros(content)
    except (json.JSONDecodeError, ValueError):
        added = 0
    macros = []
    for m in db.get_macros():
        md = dict(m)
        md["values"] = json.loads(md.get("values_json") or "[]")
        macros.append(md)
    return request.app.state.templates.TemplateResponse(request, "macros.html", {
        "active": "macros", "macros": macros, "db": db,
        "flash": f"Imported {added} macros" if added else "Import failed — invalid JSON",
    })
