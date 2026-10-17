"""Profile — user settings, logo upload, password change."""
import os
import secrets
from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse

from .auth import get_current_user, hash_password, verify_password

router = APIRouter()

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "..", "static", "uploads")


def _ensure_upload_dir():
    os.makedirs(UPLOAD_DIR, exist_ok=True)


@router.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return request.app.state.templates.TemplateResponse(
        request, "profile.html", {"active": "profile", "user": user})


@router.post("/profile/update")
async def profile_update(request: Request,
                         display_name: str = Form("")):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    db = request.app.state.db
    db.update_user_profile(user["id"], display_name.strip(),
                           user.get("logo_path", ""))
    return RedirectResponse("/profile", status_code=303)


@router.post("/profile/logo")
async def upload_logo(request: Request,
                      logo: UploadFile = File(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    _ensure_upload_dir()

    ext = os.path.splitext(logo.filename)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif"):
        return RedirectResponse("/profile", status_code=303)

    data = await logo.read()
    if len(data) > 2 * 1024 * 1024:
        return RedirectResponse("/profile", status_code=303)

    fname = f"logo_{user['id']}_{secrets.token_hex(4)}{ext}"
    dest = os.path.join(UPLOAD_DIR, fname)
    with open(dest, "wb") as f:
        f.write(data)

    logo_url = f"/static/uploads/{fname}"
    db = request.app.state.db
    db.update_user_profile(user["id"], user.get("display_name", ""), logo_url)
    return RedirectResponse("/profile", status_code=303)


@router.post("/profile/logo/delete")
async def delete_logo(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    db = request.app.state.db
    if user.get("logo_path"):
        path = os.path.join(UPLOAD_DIR, os.path.basename(user["logo_path"]))
        if os.path.isfile(path):
            try:
                os.unlink(path)
            except OSError:
                pass
    db.update_user_profile(user["id"], user.get("display_name", ""), "")
    return RedirectResponse("/profile", status_code=303)


@router.post("/profile/password")
async def change_password(request: Request,
                          current_password: str = Form(""),
                          new_password: str = Form(""),
                          new_password2: str = Form("")):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    db = request.app.state.db
    full_user = db.get_user_by_id(user["id"])
    if not full_user:
        return RedirectResponse("/login", status_code=303)

    error = ""
    if not verify_password(current_password, full_user["password_hash"]):
        error = "Current password is incorrect."
    elif len(new_password) < 6:
        error = "New password must be at least 6 characters."
    elif new_password != new_password2:
        error = "New passwords do not match."

    if error:
        return request.app.state.templates.TemplateResponse(
            request, "profile.html", {
                "active": "profile", "user": user, "pw_error": error})

    db.update_user_password(user["id"], hash_password(new_password))
    return request.app.state.templates.TemplateResponse(
        request, "profile.html", {
            "active": "profile", "user": user, "pw_success": "Password changed."})
