"""Mailing Lists — import, search, delete, compare."""
import os
import re
from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import List as TList

router = APIRouter()
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
DEFAULT_EXCLUDES = "@spam.com\nspam@\ndatenschutz@\ndsgvo@\nnoreply@\nabuse@\npostmaster@\nmailer-daemon@"

def _should_exclude(email, rules):
    email = email.lower()
    local = email.split("@")[0] if "@" in email else ""
    domain = email.split("@")[1] if "@" in email else ""
    for rule in rules:
        if rule.startswith("@") and domain == rule[1:]: return True
        if rule.endswith("@") and local == rule[:-1]: return True
        if rule in email: return True
    return False

@router.get("/lists", response_class=HTMLResponse)
async def lists_page(request: Request):
    db = request.app.state.db
    lists_data = []
    for l in db.get_lists():
        ld = dict(l)
        ld["count"] = db.get_list_lead_count(l["id"])
        ld["used_by"] = []
        rows = db._conn().execute(
            "SELECT b.name, u.used_at FROM brand_list_usage u "
            "JOIN brands b ON b.id=u.brand_id WHERE u.list_id=?", (l["id"],)).fetchall()
        ld["used_by"] = [dict(r) for r in rows]
        lists_data.append(ld)

    brands = [dict(b) for b in db.get_brands()]
    for bd in brands:
        bd["used_lists"] = [dict(r) for r in db.get_used_lists(bd["id"])]
        bd["unused_lists"] = [dict(r) for r in db.get_unused_lists(bd["id"])]

    return request.app.state.templates.TemplateResponse(request, "lists.html", {
        "active": "lists", "lists": lists_data, "brands": brands, "db": db,
        "default_excludes": DEFAULT_EXCLUDES,
    })

@router.post("/lists/import")
async def import_lists(request: Request, exclude_rules: str = Form(""),
                        files: TList[UploadFile] = File(...)):
    db = request.app.state.db
    rules = [r.strip().lower() for r in exclude_rules.splitlines() if r.strip()]
    summary = []
    for f in files:
        content = (await f.read()).decode("utf-8", errors="replace")
        name = os.path.splitext(f.filename)[0]
        list_id = db.create_list(name, f.filename)
        all_emails = [m.lower() for m in EMAIL_RE.findall(content)]
        filtered = [e for e in all_emails if not _should_exclude(e, rules)] if rules else all_emails
        added = db.import_leads(list_id, filtered)
        excluded = len(all_emails) - len(filtered)
        summary.append(f"{name}: {added:,} leads" + (f" ({excluded} excluded)" if excluded else ""))
    return request.app.state.templates.TemplateResponse(request, "lists.html", {
        "active": "lists", "summary": summary,
        "lists": [dict(l, count=db.get_list_lead_count(l["id"])) for l in db.get_lists()],
        "db": db, "default_excludes": DEFAULT_EXCLUDES,
    })

@router.get("/lists/{lid}/leads", response_class=HTMLResponse)
async def list_leads(request: Request, lid: int, q: str = ""):
    db = request.app.state.db
    if q:
        leads = [dict(r) for r in db.search_leads(lid, q)]
    else:
        leads = [dict(r) for r in db._conn().execute(
            "SELECT id, email, state FROM leads WHERE list_id=? LIMIT 500", (lid,)).fetchall()]
    stats = db.mailing_stats(lid)
    list_name = db._conn().execute("SELECT name FROM lead_lists WHERE id=?", (lid,)).fetchone()
    return request.app.state.templates.TemplateResponse(request, "lists_detail.html", {
        "active": "lists", "leads": leads, "stats": stats,
        "lid": lid, "query": q, "list_name": list_name["name"] if list_name else str(lid),
    })

@router.post("/lists/{lid}/delete-domain")
async def delete_domain_leads(request: Request, lid: int, domain: str = Form("")):
    if domain.strip():
        request.app.state.db.delete_leads_by_domain(lid, domain.strip())
    return RedirectResponse(f"/lists/{lid}/leads", status_code=303)

@router.post("/lists/{lid}/delete")
async def delete_list(request: Request, lid: int):
    request.app.state.db.delete_list(lid)
    return RedirectResponse("/lists", status_code=303)

@router.post("/lists/compare", response_class=HTMLResponse)
async def compare_lists(request: Request, list_a: int = Form(0), list_b: int = Form(0)):
    db = request.app.state.db
    c = db._conn()
    a_emails = {r[0] for r in c.execute("SELECT email FROM leads WHERE list_id=?", (list_a,)).fetchall()}
    b_emails = {r[0] for r in c.execute("SELECT email FROM leads WHERE list_id=?", (list_b,)).fetchall()}
    return request.app.state.templates.TemplateResponse(request, "lists.html", {
        "active": "lists", "db": db, "default_excludes": DEFAULT_EXCLUDES,
        "lists": [dict(l, count=db.get_list_lead_count(l["id"])) for l in db.get_lists()],
        "compare": {"both": len(a_emails & b_emails), "only_a": len(a_emails - b_emails),
                     "only_b": len(b_emails - a_emails), "list_a": list_a, "list_b": list_b},
    })

@router.post("/lists/compare/save")
async def save_compare(request: Request, list_a: int = Form(0), list_b: int = Form(0), which: str = Form("a")):
    db = request.app.state.db
    c = db._conn()
    a_emails = {r[0] for r in c.execute("SELECT email FROM leads WHERE list_id=?", (list_a,)).fetchall()}
    b_emails = {r[0] for r in c.execute("SELECT email FROM leads WHERE list_id=?", (list_b,)).fetchall()}
    emails = list(a_emails - b_emails) if which == "a" else list(b_emails - a_emails)
    if emails:
        lid = db.create_list(f"only_in_{'A' if which == 'a' else 'B'}")
        db.import_leads(lid, emails)
    return RedirectResponse("/lists", status_code=303)
