"""Config — ALL settings from config.ini, proxy, content, image, redirect."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    db = request.app.state.db
    cfg = db.get_config()
    return request.app.state.templates.TemplateResponse(request, "config.html", {
        "active": "config", "cfg": cfg, "db": db,
    })


@router.post("/config/save")
async def save_config(request: Request):
    form = await request.form()
    db = request.app.state.db
    cfg = db.get_config()

    for key in ["from_name", "from_email", "subject", "test_recipients",
                 "interval_recipients", "schedule_time", "proxy_value",
                 "proxy_mode", "image_mode", "redirect_target_url",
                 "cloudinary_cloud_name", "cloudinary_api_key",
                 "cloudinary_api_secret", "mxtoolbox_api_key",
                 "spam_checker", "spam_checker_url", "mime_profile",
                 "logo_base_url", "logo_text", "logo_text_color",
                 "logo_static_url"]:
        if key in form:
            cfg[key] = str(form[key]).strip()

    for key in ["threads", "warmup_count", "smtp_timeout", "test_interval",
                 "logo_max_colors", "logo_rotate_every", "redirect_rotate_every",
                 "redirect_gen_threads", "proxy_rotate_every", "html_rotate_every",
                 "freshness_every_n_mails", "freshness_html_count", "freshness_logo_count",
                 "gradual_send_ramp_minutes"]:
        if key in form:
            try:
                cfg[key] = int(form[key])
            except (ValueError, TypeError):
                pass

    for key in ["normal_delay", "provider_delay", "warmup_delay", "structure_variation",
                 "gradual_send_start_factor"]:
        if key in form:
            try:
                cfg[key] = float(form[key])
            except (ValueError, TypeError):
                pass

    for key in ["ignore_ssl_errors", "antifingerprint_classes",
                 "advanced_antifingerprint", "image_enabled", "image_quantize",
                 "image_downscale", "redirect_enabled", "auto_retry_failed",
                 "auto_suppress_hard_bounces",
                 "freshness_reset_html", "freshness_reset_logos",
                 "gradual_send_enabled"]:
        cfg[key] = key in form

    db.save_config(cfg)
    return RedirectResponse("/config", status_code=303)
