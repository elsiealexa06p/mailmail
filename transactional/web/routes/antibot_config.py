"""Antibot-Integration Config — Basis-URL + HMAC-Secret + Toggle.
Wenn aktiv, wickelt der share.google-Generator seine Ziel-URL vorher
in ein antibot-Token ein, sodass Google intern die antibot-URL als
Redirect-Ziel speichert (nicht die echte Ziel-URL)."""
import time
import json
import hashlib
import hmac
import base64
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def build_antibot_url(base_url: str, hmac_secret: str, target: str,
                       ttl_seconds: int = 0, campaign: str = "") -> str:
    """Mirror of antibot's tokens.create_token — kept local to avoid a
    hard runtime dep on the antibot repo. If the token schema changes
    there, keep this in sync."""
    payload = {"t": target}
    if campaign:
        payload["c"] = campaign
    if ttl_seconds > 0:
        payload["exp"] = int(time.time()) + ttl_seconds
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = _b64e(raw)
    sig = _b64e(hmac.new(hmac_secret.encode(), body.encode(),
                          hashlib.sha256).digest())
    tok = f"{body}.{sig}"
    return f"{base_url.rstrip('/')}/go/{tok}"


@router.get("/antibot-config", response_class=HTMLResponse)
async def antibot_config_page(request: Request):
    db = request.app.state.db
    cfg = db.get_config()
    preview = ""
    if cfg.get("antibot_base_url") and cfg.get("antibot_hmac_secret"):
        preview = build_antibot_url(
            cfg["antibot_base_url"], cfg["antibot_hmac_secret"],
            "https://echtes-ziel.example.com/landing",
            ttl_seconds=int(cfg.get("antibot_token_ttl_hours", 168)) * 3600)
    return request.app.state.templates.TemplateResponse(request, "antibot_config.html", {
        "active": "antibot", "cfg": cfg, "preview": preview,
    })


@router.post("/antibot-config")
async def antibot_config_save(request: Request,
                               antibot_enabled: str = Form(""),
                               antibot_base_url: str = Form(""),
                               antibot_hmac_secret: str = Form(""),
                               antibot_token_ttl_hours: int = Form(168)):
    db = request.app.state.db
    updates = {
        "antibot_enabled": bool(antibot_enabled),
        "antibot_base_url": antibot_base_url.strip().rstrip("/"),
        "antibot_hmac_secret": antibot_hmac_secret.strip(),
        "antibot_token_ttl_hours": max(1, min(int(antibot_token_ttl_hours or 168), 8760)),
    }
    db.update_config(**updates)
    return RedirectResponse("/antibot-config?saved=1", status_code=303)


@router.post("/antibot-config/test", response_class=HTMLResponse)
async def antibot_config_test(request: Request):
    """Fire a live HEAD to <base>/health to verify the target is reachable."""
    import requests as _req
    db = request.app.state.db
    cfg = db.get_config()
    base = (cfg.get("antibot_base_url") or "").strip().rstrip("/")
    if not base:
        return HTMLResponse('<span style="color:var(--red)">Keine Base-URL gesetzt.</span>')
    try:
        r = _req.get(f"{base}/health", timeout=8)
        if r.status_code == 200 and r.json().get("ok"):
            return HTMLResponse(f'<span style="color:var(--green)">✓ {base}/health erreichbar (HTTP 200)</span>')
        return HTMLResponse(f'<span style="color:var(--red)">✗ HTTP {r.status_code}</span>')
    except Exception as e:
        return HTMLResponse(f'<span style="color:var(--red)">✗ {str(e)[:120]}</span>')
