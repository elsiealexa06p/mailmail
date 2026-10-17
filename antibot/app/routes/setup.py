"""First-run wizard — runs when db.admin_count() == 0 or setup_done != '1'."""
import os
import time
import secrets
from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse

from .auth import COOKIE_NAME

router = APIRouter()

STATIC_LOGO_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "static", "logo")


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    db = request.app.state.db
    cfg = db.get_config()
    if cfg.get("setup_done") == "1" and db.admin_count() > 0:
        return RedirectResponse("/login", status_code=303)
    db.ensure_secrets()
    cfg = db.get_config()
    return request.app.state.templates.TemplateResponse(request, "setup.html", {
        "cfg": cfg,
        "hmac_secret_preview": cfg["hmac_secret"],
    })


@router.post("/setup")
async def setup_submit(request: Request,
                        username: str = Form(""),
                        password: str = Form(""),
                        password2: str = Form(""),
                        default_target: str = Form(""),
                        brand_text: str = Form(""),
                        brand_color: str = Form("#005eb8"),
                        threshold_allow: int = Form(40),
                        threshold_block: int = Form(70),
                        turnstile_site_key: str = Form(""),
                        turnstile_secret_key: str = Form(""),
                        maxmind_license_key: str = Form(""),
                        webhook_url: str = Form(""),
                        logo: UploadFile = File(None)):
    db = request.app.state.db
    if db.admin_count() > 0 and db.get_config().get("setup_done") == "1":
        return RedirectResponse("/login", status_code=303)
    if not username.strip() or len(password) < 8 or password != password2:
        return RedirectResponse("/setup?e=bad", status_code=303)
    if not default_target.startswith("http"):
        return RedirectResponse("/setup?e=target", status_code=303)

    admin_id = db.add_admin(username.strip(), password)

    logo_path = ""
    if logo and logo.filename:
        os.makedirs(STATIC_LOGO_DIR, exist_ok=True)
        ext = os.path.splitext(logo.filename)[1].lower() or ".png"
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
            ext = ".png"
        fname = f"logo_{int(time.time())}_{secrets.token_hex(4)}{ext}"
        with open(os.path.join(STATIC_LOGO_DIR, fname), "wb") as fh:
            fh.write(await logo.read())
        logo_path = f"/static/logo/{fname}"

    updates = {
        "default_target": default_target.strip(),
        "brand_text": brand_text.strip() or "Sicherheitsprüfung läuft …",
        "brand_color": brand_color.strip() or "#005eb8",
        "threshold_allow": str(max(0, min(100, threshold_allow))),
        "threshold_block": str(max(0, min(100, threshold_block))),
        "turnstile_site_key": turnstile_site_key.strip(),
        "turnstile_secret_key": turnstile_secret_key.strip(),
        "maxmind_license_key": maxmind_license_key.strip(),
        "webhook_url": webhook_url.strip(),
        "setup_done": "1",
    }
    if logo_path:
        updates["logo_path"] = logo_path
    db.set_config(**updates)

    tok = db.create_session(admin_id)
    resp = RedirectResponse("/admin?welcome=1", status_code=303)
    resp.set_cookie(COOKIE_NAME, tok, httponly=True, samesite="lax",
                    secure=True, max_age=86400 * 7, path="/")
    return resp
