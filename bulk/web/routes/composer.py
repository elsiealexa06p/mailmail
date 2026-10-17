"""Composer — Message Templates CRUD + HTML upload."""
import os
import json
import secrets
from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import List as TList

router = APIRouter()

HTML_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "bulk_html")


def _ensure_html_dir():
    os.makedirs(HTML_DIR, exist_ok=True)


def _template_list(db):
    templates = []
    for t in db.get_templates():
        td = dict(t)
        td["html_files"] = json.loads(td.get("html_files_json") or "[]")
        td["settings"] = json.loads(td.get("settings_json") or "{}")
        templates.append(td)
    return templates


@router.get("/composer", response_class=HTMLResponse)
async def composer_page(request: Request):
    db = request.app.state.db
    tpl = request.app.state.templates
    return tpl.TemplateResponse(request, "composer.html", {
        "active": "composer", "templates": _template_list(db), "db": db,
    })


@router.post("/composer/add")
async def add_template(request: Request,
                       name: str = Form(""),
                       subject_macro: str = Form(""),
                       html_files: str = Form(""),
                       html_rotate_every: int = Form(0),
                       pdf_path: str = Form(""),
                       pdf_macro_enabled: int = Form(0)):
    if not name.strip():
        return RedirectResponse("/composer", status_code=303)

    html_list = [f.strip() for f in html_files.splitlines() if f.strip()]

    db = request.app.state.db
    db.add_template(
        name=name.strip(),
        subject_macro=subject_macro.strip(),
        html_files_json=json.dumps(html_list, ensure_ascii=False),
        html_rotate_every=html_rotate_every,
        pdf_path=pdf_path.strip(),
        pdf_macro_enabled=pdf_macro_enabled,
    )
    return RedirectResponse("/composer", status_code=303)


@router.post("/composer/{tid}/upload-html")
async def upload_html(request: Request, tid: int,
                      files: TList[UploadFile] = File(...)):
    """Upload multiple HTML files and add them to the template."""
    _ensure_html_dir()
    db = request.app.state.db
    row = db._conn().execute(
        "SELECT html_files_json FROM message_templates WHERE id=?", (tid,)).fetchone()
    if not row:
        return RedirectResponse("/composer", status_code=303)

    existing = json.loads(row["html_files_json"] or "[]")
    for f in files:
        content = await f.read()
        safe_name = f.filename.replace("/", "_").replace("\\", "_")
        dest = os.path.join(HTML_DIR, f"{tid}_{safe_name}")
        with open(dest, "wb") as fh:
            fh.write(content)
        abs_path = os.path.abspath(dest)
        if abs_path not in existing:
            existing.append(abs_path)

    c = db._conn()
    c.execute("UPDATE message_templates SET html_files_json=? WHERE id=?",
              (json.dumps(existing, ensure_ascii=False), tid))
    c.commit()
    return RedirectResponse("/composer", status_code=303)


@router.post("/composer/{tid}/paste-html")
async def paste_html(request: Request, tid: int,
                     html_content: str = Form("")):
    """Save pasted HTML and add it to the template."""
    if not html_content.strip():
        return RedirectResponse("/composer", status_code=303)

    _ensure_html_dir()
    db = request.app.state.db
    row = db._conn().execute(
        "SELECT html_files_json FROM message_templates WHERE id=?", (tid,)).fetchone()
    if not row:
        return RedirectResponse("/composer", status_code=303)

    existing = json.loads(row["html_files_json"] or "[]")
    token = secrets.token_hex(4)
    dest = os.path.join(HTML_DIR, f"{tid}_pasted_{token}.html")
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(html_content)
    abs_path = os.path.abspath(dest)
    existing.append(abs_path)

    c = db._conn()
    c.execute("UPDATE message_templates SET html_files_json=? WHERE id=?",
              (json.dumps(existing, ensure_ascii=False), tid))
    c.commit()
    return RedirectResponse("/composer", status_code=303)


@router.post("/composer/{tid}/remove-html")
async def remove_html(request: Request, tid: int,
                      html_path: str = Form("")):
    """Remove an HTML file from the template list."""
    if not html_path:
        return RedirectResponse("/composer", status_code=303)
    db = request.app.state.db
    row = db._conn().execute(
        "SELECT html_files_json FROM message_templates WHERE id=?", (tid,)).fetchone()
    if not row:
        return RedirectResponse("/composer", status_code=303)

    existing = json.loads(row["html_files_json"] or "[]")
    existing = [f for f in existing if f != html_path]
    c = db._conn()
    c.execute("UPDATE message_templates SET html_files_json=? WHERE id=?",
              (json.dumps(existing, ensure_ascii=False), tid))
    c.commit()
    return RedirectResponse("/composer", status_code=303)


@router.post("/composer/{tid}/save")
async def save_template(request: Request, tid: int,
                        name: str = Form(""),
                        subject_macro: str = Form(""),
                        html_rotate_every: int = Form(0),
                        pdf_path: str = Form(""),
                        pdf_macro_enabled: int = Form(0)):
    """Save edits to an existing template."""
    c = request.app.state.db._conn()
    c.execute("UPDATE message_templates SET name=?, subject_macro=?, "
              "html_rotate_every=?, pdf_path=?, pdf_macro_enabled=? WHERE id=?",
              (name.strip(), subject_macro.strip(), html_rotate_every,
               pdf_path.strip(), pdf_macro_enabled, tid))
    c.commit()
    return RedirectResponse("/composer", status_code=303)


@router.post("/composer/{tid}/delete")
async def delete_template(request: Request, tid: int):
    request.app.state.db.delete_template(tid)
    return RedirectResponse("/composer", status_code=303)
