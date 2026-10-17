"""Admin settings — branding, API keys, thresholds, dry-run toggle,
HMAC-secret rotation."""
import os
import time
import secrets
from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()

STATIC_LOGO_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "static", "logo")


@router.get("/admin/settings", response_class=HTMLResponse)
async def settings_view(request: Request):
    return request.app.state.templates.TemplateResponse(request, "admin_settings.html", {
        "cfg": request.app.state.db.get_config(),
    })


@router.post("/admin/settings/branding")
async def branding_save(request: Request,
                         brand_text: str = Form(""),
                         brand_color: str = Form("#005eb8"),
                         default_target: str = Form(""),
                         wait_seconds: int = Form(0),
                         logo: UploadFile = File(None)):
    db = request.app.state.db
    updates = {
        "brand_text": brand_text.strip() or "Sicherheitsprüfung läuft …",
        "brand_color": brand_color.strip() or "#005eb8",
        "wait_seconds": str(max(0, min(int(wait_seconds or 0), 20))),
    }
    if default_target.strip():
        updates["default_target"] = default_target.strip()
    if logo and logo.filename:
        os.makedirs(STATIC_LOGO_DIR, exist_ok=True)
        ext = os.path.splitext(logo.filename)[1].lower() or ".png"
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
            ext = ".png"
        fname = f"logo_{int(time.time())}_{secrets.token_hex(4)}{ext}"
        with open(os.path.join(STATIC_LOGO_DIR, fname), "wb") as fh:
            fh.write(await logo.read())
        updates["logo_path"] = f"/static/logo/{fname}"
    db.set_config(**updates)
    return RedirectResponse("/admin/settings", status_code=303)


@router.post("/admin/settings/thresholds")
async def thresholds_save(request: Request,
                            threshold_allow: int = Form(40),
                            threshold_block: int = Form(70),
                            rate_limit_per_min: int = Form(60),
                            verification_ttl_hours: int = Form(6),
                            pow_difficulty: int = Form(5)):
    db = request.app.state.db
    db.set_config(
        threshold_allow=str(max(0, min(threshold_allow, 100))),
        threshold_block=str(max(0, min(threshold_block, 100))),
        rate_limit_per_min=str(max(1, min(rate_limit_per_min, 10000))),
        verification_ttl_hours=str(max(1, min(verification_ttl_hours, 168))),
        pow_difficulty=str(max(3, min(pow_difficulty, 8))),
    )
    return RedirectResponse("/admin/settings", status_code=303)


@router.post("/admin/settings/api-keys")
async def api_keys_save(request: Request,
                          turnstile_site_key: str = Form(""),
                          turnstile_secret_key: str = Form(""),
                          maxmind_license_key: str = Form(""),
                          webhook_url: str = Form(""),
                          webhook_min_score: int = Form(70)):
    db = request.app.state.db
    db.set_config(
        turnstile_site_key=turnstile_site_key.strip(),
        turnstile_secret_key=turnstile_secret_key.strip(),
        maxmind_license_key=maxmind_license_key.strip(),
        webhook_url=webhook_url.strip(),
        webhook_min_score=str(max(0, min(webhook_min_score, 100))),
    )
    return RedirectResponse("/admin/settings", status_code=303)


@router.post("/admin/settings/dry-run")
async def dryrun_toggle(request: Request, dry_run: str = Form("0")):
    request.app.state.db.set_config(dry_run="1" if dry_run == "1" else "0")
    return RedirectResponse("/admin/settings", status_code=303)


@router.post("/admin/settings/rotate-secret")
async def rotate_secret(request: Request, confirm: str = Form("")):
    """Rotate HMAC secret — will INVALIDATE ALL EXISTING mailer links."""
    if confirm != "rotate":
        return RedirectResponse("/admin/settings?e=noconfirm", status_code=303)
    db = request.app.state.db
    db.set_config(hmac_secret=secrets.token_urlsafe(48))
    return RedirectResponse("/admin/settings?rotated=1", status_code=303)


@router.post("/admin/settings/prune-log")
async def prune_log(request: Request, keep_days: int = Form(30)):
    request.app.state.db.prune_decisions(max(1, min(keep_days, 365)))
    return RedirectResponse("/admin/log", status_code=303)
