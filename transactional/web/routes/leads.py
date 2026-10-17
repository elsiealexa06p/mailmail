"""Lead Lists — list-based management, import, upload."""
import os
import re
from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import List as TList

router = APIRouter()
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


@router.get("/leads", response_class=HTMLResponse)
async def leads_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    lead_lists = []
    for ll in db.get_lead_lists(uid):
        lld = dict(ll)
        lld["count"] = db.get_lead_count(ll["id"])
        lead_lists.append(lld)
    return request.app.state.templates.TemplateResponse(request, "leads.html", {
        "active": "leads", "lead_lists": lead_lists, "db": db,
    })


@router.post("/leads/create-list")
async def create_list(request: Request, name: str = Form("")):
    if name.strip():
        uid = request.state.user['id']
        request.app.state.db.create_lead_list(name.strip(), '', uid)
    return RedirectResponse("/leads", status_code=303)


@router.post("/leads/bulk-upload")
async def bulk_upload(request: Request, files: TList[UploadFile] = File(...)):
    """Upload multiple files — each file becomes its own list, named by filename."""
    db = request.app.state.db
    for f in files:
        if not f.filename:
            continue
        content = (await f.read()).decode("utf-8", errors="replace")
        emails = [e.lower() for e in EMAIL_RE.findall(content)]
        if not emails:
            continue
        name = os.path.splitext(f.filename)[0]
        uid = request.state.user['id']
        lid = db.create_lead_list(name, f.filename, uid)
        db.import_leads(lid, emails)
    return RedirectResponse("/leads", status_code=303)


@router.post("/leads/{lid}/import-text", response_class=HTMLResponse)
async def import_text(request: Request, lid: int, leads_text: str = Form("")):
    db = request.app.state.db
    emails = [e.lower() for e in EMAIL_RE.findall(leads_text)]
    added = db.import_leads(lid, emails)
    from html import escape
    return HTMLResponse(f'<div class="alert alert-success">{added} leads imported. '
                        f'<a href="/leads" style="color:var(--accent)">Reload</a></div>')


@router.post("/leads/{lid}/upload")
async def upload_leads(request: Request, lid: int,
                       files: TList[UploadFile] = File(...)):
    db = request.app.state.db
    total = 0
    for f in files:
        content = (await f.read()).decode("utf-8", errors="replace")
        emails = [e.lower() for e in EMAIL_RE.findall(content)]
        total += db.import_leads(lid, emails)
    db._conn().execute("UPDATE trans_lead_lists SET lead_count=? WHERE id=?",
                        (db.get_lead_count(lid), lid))
    db._conn().commit()
    return RedirectResponse("/leads", status_code=303)


@router.post("/leads/list/{lid}/delete")
async def delete_list(request: Request, lid: int):
    request.app.state.db.delete_lead_list(lid)
    return RedirectResponse("/leads", status_code=303)


@router.post("/leads/{lid}/delete-lead")
async def delete_lead(request: Request, lid: int, lead_id: int = Form(0)):
    if lead_id:
        db = request.app.state.db
        db._conn().execute("DELETE FROM trans_leads WHERE id=? AND list_id=?", (lead_id, lid))
        db._conn().execute("UPDATE trans_lead_lists SET lead_count=(SELECT COUNT(*) FROM trans_leads WHERE list_id=?) WHERE id=?", (lid, lid))
        db._conn().commit()
    return RedirectResponse(f"/leads/{lid}/browse", status_code=303)


@router.post("/leads/{lid}/delete-domain")
async def delete_domain(request: Request, lid: int, domain: str = Form("")):
    if domain.strip():
        db = request.app.state.db
        db._conn().execute("DELETE FROM trans_leads WHERE list_id=? AND email LIKE ?",
                            (lid, f"%@{domain.strip()}"))
        db._conn().execute("UPDATE trans_lead_lists SET lead_count=(SELECT COUNT(*) FROM trans_leads WHERE list_id=?) WHERE id=?", (lid, lid))
        db._conn().commit()
    return RedirectResponse(f"/leads/{lid}/browse", status_code=303)


@router.get("/leads/{lid}/browse", response_class=HTMLResponse)
async def browse_leads(request: Request, lid: int, q: str = ""):
    db = request.app.state.db
    ll = db._conn().execute("SELECT * FROM trans_lead_lists WHERE id=?", (lid,)).fetchone()
    if not ll:
        return RedirectResponse("/leads", status_code=303)
    ll = dict(ll)
    states = db.get_lead_states(lid)

    if q:
        leads = db._conn().execute(
            "SELECT id,email,state FROM trans_leads WHERE list_id=? AND email LIKE ? LIMIT 500",
            (lid, f"%{q}%")).fetchall()
    else:
        leads = db._conn().execute(
            "SELECT id,email,state FROM trans_leads WHERE list_id=? LIMIT 500",
            (lid,)).fetchall()

    return request.app.state.templates.TemplateResponse(request, "leads_detail.html", {
        "active": "leads", "list": ll, "leads": [dict(l) for l in leads],
        "states": states, "query": q, "lid": lid,
    })
