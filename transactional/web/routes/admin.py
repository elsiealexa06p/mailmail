"""Admin — User management, permissions, app config, backup."""
import os
import json
import io
import zipfile
import secrets
import time
from html import escape
from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from .auth import hash_password

router = APIRouter()

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "..", "static", "uploads")

FEATURES = [
    ("campaigns", "Campaigns"),
    ("smtps", "SMTP Lists"),
    ("leads", "Lead Lists"),
    ("templates", "HTML Editor"),
    ("macros", "Macros"),
    ("logos", "Logos"),
    ("redirects", "Redirects"),
    ("proxies", "Proxies"),
    ("ai", "AI Assistant"),
    ("config", "Config"),
    ("settings", "Logs"),
]


def _check_admin(request: Request):
    user = request.state.user
    return user and user.get("role") == "admin"


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    if not _check_admin(request):
        return RedirectResponse("/campaigns", status_code=303)
    db = request.app.state.db
    users = [dict(u) for u in db.get_all_users()]
    for u in users:
        u["perms"] = json.loads(u.get("permissions_json") or "{}")
    app_cfg = db.get_app_config()
    return request.app.state.templates.TemplateResponse(request, "admin.html", {
        "active": "admin", "users": users, "features": FEATURES,
        "app_cfg": app_cfg, "db": db,
    })


@router.post("/admin/add-user")
async def add_user(request: Request,
                   username: str = Form(""),
                   password: str = Form(""),
                   display_name: str = Form(""),
                   role: str = Form("user")):
    if not _check_admin(request):
        return RedirectResponse("/campaigns", status_code=303)
    db = request.app.state.db
    if not username.strip() or len(password) < 6:
        return RedirectResponse("/admin", status_code=303)
    try:
        pw_hash = hash_password(password)
        admin_id = request.state.user["id"]
        perms = {f[0]: True for f in FEATURES} if role == "admin" else {}
        db.create_user(username.strip(), pw_hash, display_name.strip() or username.strip())
        user = db.get_user(username.strip())
        if user:
            db.update_user(user["id"], role=role, created_by=admin_id,
                           permissions_json=json.dumps(perms))
    except Exception:
        pass
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/user/{uid}/permissions")
async def save_permissions(request: Request, uid: int):
    if not _check_admin(request):
        return RedirectResponse("/campaigns", status_code=303)
    form = await request.form()
    db = request.app.state.db
    perms = {}
    for feat, _ in FEATURES:
        perms[feat] = feat in form
    role = form.get("role", "user")
    if role == "admin":
        perms = {f[0]: True for f in FEATURES}
    db.update_user(uid, permissions_json=json.dumps(perms), role=role)
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/user/{uid}/toggle")
async def toggle_user(request: Request, uid: int):
    if not _check_admin(request):
        return RedirectResponse("/campaigns", status_code=303)
    db = request.app.state.db
    user = db.get_user_by_id(uid)
    if user and uid != request.state.user["id"]:
        db.update_user(uid, is_active=0 if user["is_active"] else 1)
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/user/{uid}/delete")
async def delete_user(request: Request, uid: int):
    if not _check_admin(request):
        return RedirectResponse("/campaigns", status_code=303)
    if uid != request.state.user["id"]:
        request.app.state.db.delete_user(uid)
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/user/{uid}/reset-password")
async def reset_password(request: Request, uid: int, new_password: str = Form("")):
    if not _check_admin(request):
        return RedirectResponse("/campaigns", status_code=303)
    if len(new_password) >= 6:
        request.app.state.db.update_user_password(uid, hash_password(new_password))
    return RedirectResponse("/admin", status_code=303)


# ── Profile ───────────────────────────────────────────
@router.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    user = request.state.user
    return request.app.state.templates.TemplateResponse(request, "profile.html", {
        "active": "profile", "user": user, "db": request.app.state.db,
    })


@router.post("/profile/update")
async def update_profile(request: Request, display_name: str = Form("")):
    db = request.app.state.db
    db.update_user(request.state.user["id"], display_name=display_name.strip())
    return RedirectResponse("/profile", status_code=303)


@router.post("/profile/logo")
async def upload_profile_logo(request: Request, logo: UploadFile = File(...)):
    db = request.app.state.db
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    ext = os.path.splitext(logo.filename)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".svg", ".webp"):
        return RedirectResponse("/profile", status_code=303)
    data = await logo.read()
    if len(data) > 2 * 1024 * 1024:
        return RedirectResponse("/profile", status_code=303)
    fname = f"profile_{request.state.user['id']}_{secrets.token_hex(4)}{ext}"
    dest = os.path.join(UPLOAD_DIR, fname)
    with open(dest, "wb") as f:
        f.write(data)
    db.update_user(request.state.user["id"], logo_path=f"/static/uploads/{fname}")
    return RedirectResponse("/profile", status_code=303)


@router.post("/profile/logo/delete")
async def delete_profile_logo(request: Request):
    db = request.app.state.db
    user = db.get_user_by_id(request.state.user["id"])
    if user and user["logo_path"]:
        path = os.path.join(os.path.dirname(__file__), "..", user["logo_path"].lstrip("/"))
        if os.path.isfile(path):
            try:
                os.unlink(path)
            except OSError:
                pass
    db.update_user(request.state.user["id"], logo_path="")
    return RedirectResponse("/profile", status_code=303)


@router.post("/profile/password")
async def change_password(request: Request,
                          current_password: str = Form(""),
                          new_password: str = Form("")):
    from .auth import verify_password
    db = request.app.state.db
    user = db.get_user_by_id(request.state.user["id"])
    if user and verify_password(current_password, user["password_hash"]) and len(new_password) >= 6:
        db.update_user_password(user["id"], hash_password(new_password))
    return RedirectResponse("/profile", status_code=303)


# ── App Config (login logo) ───────────────────────────
@router.post("/admin/app-config")
async def save_app_config(request: Request,
                          app_name: str = Form(""),
                          login_logo: UploadFile = File(None)):
    if not _check_admin(request):
        return RedirectResponse("/campaigns", status_code=303)
    db = request.app.state.db
    updates = {}
    if app_name.strip():
        updates["app_name"] = app_name.strip()
    if login_logo and login_logo.filename:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        ext = os.path.splitext(login_logo.filename)[1].lower()
        if ext in (".png", ".jpg", ".jpeg", ".svg", ".webp"):
            data = await login_logo.read()
            if len(data) <= 2 * 1024 * 1024:
                fname = f"login_logo_{secrets.token_hex(4)}{ext}"
                dest = os.path.join(UPLOAD_DIR, fname)
                with open(dest, "wb") as f:
                    f.write(data)
                updates["login_logo"] = f"/static/uploads/{fname}"
    if updates:
        db.save_app_config(**updates)
    return RedirectResponse("/admin", status_code=303)


# ── Backup ────────────────────────────────────────────
@router.get("/admin/backup")
async def create_backup(request: Request):
    if not _check_admin(request):
        return RedirectResponse("/campaigns", status_code=303)
    db = request.app.state.db
    backup = {
        "version": 1,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": db.get_config(),
        "smtp_lists": [],
        "lead_lists": [],
        "macros": [dict(m) for m in db.get_macros()],
        "templates": [],
        "logos": [dict(l) for l in db.get_logos()],
        "redirects": [dict(r) for r in db.get_redirects()],
        "proxies": [dict(p) for p in db.get_proxies()],
        "campaigns": [dict(c) for c in db.get_campaigns()],
    }
    for sl in db.get_smtp_lists():
        sld = dict(sl)
        sld["smtps"] = [dict(s) for s in db.get_smtps(sl["id"])]
        backup["smtp_lists"].append(sld)
    for ll in db.get_lead_lists():
        lld = dict(ll)
        lld["lead_count"] = db.get_lead_count(ll["id"])
        backup["lead_lists"].append(lld)
    for t in db.get_templates():
        td = dict(t)
        td["files"] = [dict(f) for f in db.get_template_files(t["id"])]
        backup["templates"].append(td)

    content = json.dumps(backup, indent=2, ensure_ascii=False, default=str)
    return StreamingResponse(
        io.BytesIO(content.encode("utf-8")),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=backup_{time.strftime('%Y%m%d_%H%M%S')}.json"})


@router.post("/admin/restore", response_class=HTMLResponse)
async def restore_backup(request: Request, file: UploadFile = File(...)):
    if not _check_admin(request):
        return HTMLResponse("Forbidden", status_code=403)
    db = request.app.state.db
    try:
        content = (await file.read()).decode("utf-8")
        backup = json.loads(content)
        restored = []

        if "config" in backup:
            db.save_config(backup["config"])
            restored.append("Config")
        if "macros" in backup:
            for m in backup["macros"]:
                db.add_macro(m["name"], m.get("values_text", ""), m.get("rotate_every", 0))
            restored.append(f"{len(backup['macros'])} Macros")
        if "smtp_lists" in backup:
            for sl in backup["smtp_lists"]:
                lid = db.create_smtp_list(sl["name"])
                for s in sl.get("smtps", []):
                    db._conn().execute(
                        "INSERT INTO trans_smtps (list_id,host,port,username,password) VALUES (?,?,?,?,?)",
                        (lid, s["host"], s["port"], s["username"], s["password"]))
                db._conn().commit()
            restored.append(f"{len(backup['smtp_lists'])} SMTP Lists")
        if "proxies" in backup:
            for p in backup["proxies"]:
                db.add_proxy(p["name"], p.get("proxy_type", "single"),
                             p.get("value", ""), p.get("rotate_every", 0))
            restored.append(f"{len(backup['proxies'])} Proxies")
        if "templates" in backup:
            for t in backup["templates"]:
                tid = db.add_template(t["name"], t.get("html_content", ""))
                for f in t.get("files", []):
                    db.add_template_file(tid, f.get("filename", ""), f.get("html_content", ""))
            restored.append(f"{len(backup['templates'])} Templates")

        return HTMLResponse(f'<div class="alert alert-success">Restored: {", ".join(restored)}</div>')
    except Exception as e:
        return HTMLResponse(f'<div class="alert alert-danger">Error: {escape(str(e))}</div>')
