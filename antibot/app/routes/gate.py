"""Public gate endpoints — the actual bot filter surface."""
import time
import json
import random
import logging
import hashlib
import threading
import requests as _req
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse

from ..tokens import (verify_token, session_bucket_from_request,
                      verify_cookie, issue_verify_cookie)
from ..scoring import score_request
from ..presets import resolve_mode

logger = logging.getLogger("antibot.gate")
router = APIRouter()

VERIFY_COOKIE = "abo_verified"

# Query-Parameter die das Antibot-Gate transparent an die Ziel-URL
# weiterreicht. So funktioniert `?ref=USER123` (oder `?u=…`) end-to-end:
# der Mailer hängt an den Gate-Link ran, das Gate leitet sie an die
# Landing weiter. Whitelist gehalten damit keine ungewollten Params
# (tokens, bypass, etc.) durchsickern.
_REF_PARAM_WHITELIST = ("ref", "u", "utm_source", "utm_medium",
                         "utm_campaign", "utm_content", "utm_id",
                         "utm_term", "sub", "sid", "aff")


def _pick_target(raw: str) -> str:
    """Multi-Target-Rotation: wenn target_url oder target_override mehrere
    URLs enthält (durch Newline / Komma / Semikolon getrennt), random eine
    picken. Single-URL bleibt unverändert."""
    if not raw:
        return ""
    # trennt an \n, \r, komma, semikolon — leere lines und whitespace-only weg
    import re as _re
    parts = [p.strip() for p in _re.split(r"[\r\n,;]+", raw) if p.strip()]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return random.choice(parts)


def _forward_ref_params(target: str, request: Request) -> str:
    """Hängt whitelisted query-params (ref/u/utm_*) an das Target an.
    Wenn im Target schon vorhanden, wird NICHT überschrieben — der
    ursprüngliche Wert im Target hat Vorrang."""
    if not target:
        return target
    from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse
    incoming = {k: v for k, v in request.query_params.items()
                if k.lower() in _REF_PARAM_WHITELIST and v}
    if not incoming:
        return target
    try:
        parsed = urlparse(target)
        existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
        for k, v in incoming.items():
            existing.setdefault(k, v)   # target-value wins bei Konflikt
        new_query = urlencode(existing, doseq=False)
        return urlunparse(parsed._replace(query=new_query))
    except Exception:
        # Wenn urlparse aus irgendeinem Grund failt (z.B. sehr kaputte
        # target-URL), lieber das Original zurückgeben als crashen.
        return target


def _redirect_or_wait(request: Request, cfg: dict, target: str,
                       response=None) -> HTMLResponse:
    """Wenn wait_seconds > 0: Wait-Screen mit Logo zeigen und dann per JS/meta
    weiterleiten. Sonst direkter 302. `response` optional wenn wir Cookies
    setzen müssen (verify-branch) — dann kopieren wir sie in die HTMLResponse.
    Whitelist-Query-Params (ref, u, utm_*) werden ans Target weitergereicht."""
    target = _forward_ref_params(target, request)
    try:
        wait_s = int(cfg.get("wait_seconds", "0"))
    except (ValueError, TypeError):
        wait_s = 0
    if wait_s <= 0:
        r = RedirectResponse(target, status_code=302)
        if response is not None:
            for k, v in response.headers.raw:
                if k.lower() == b"set-cookie":
                    r.headers.raw.append((k, v))
        return r
    resp = request.app.state.templates.TemplateResponse(request, "wait_screen.html", {
        "cfg": cfg, "target": target, "wait_seconds": max(1, min(wait_s, 20)),
    })
    if response is not None:
        for k, v in response.headers.raw:
            if k.lower() == b"set-cookie":
                resp.headers.raw.append((k, v))
    return resp


def _resolve_gate(request: Request, db):
    """Match request.host → gates table; None if no per-host gate."""
    host = (request.headers.get("host") or "").split(":")[0].lower()
    return db.get_gate_by_host(host) if host else None


def _effective_cfg(cfg: dict, gate: dict = None) -> dict:
    """Merge a gate's per-domain overrides on top of the global config."""
    eff = dict(cfg)
    if not gate:
        return eff
    if gate.get("target_url"):
        eff["default_target"] = gate["target_url"]
    if gate.get("brand_text"):
        eff["brand_text"] = gate["brand_text"]
    if gate.get("brand_color"):
        eff["brand_color"] = gate["brand_color"]
    if gate.get("logo_path"):
        eff["logo_path"] = gate["logo_path"]
    if gate.get("turnstile_site_key"):
        eff["turnstile_site_key"] = gate["turnstile_site_key"]
    if gate.get("turnstile_secret_key"):
        eff["turnstile_secret_key"] = gate["turnstile_secret_key"]
    # Score-Preset überschreibt globale Schwellwerte
    preset = resolve_mode(gate.get("mode", "medium"))
    eff["threshold_allow"] = str(preset["threshold_allow"])
    eff["threshold_block"] = str(preset["threshold_block"])
    eff["pow_difficulty"] = str(preset["pow_difficulty"])
    eff["rate_limit_per_min"] = str(preset["rate_limit_per_min"])
    return eff


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.headers.get("x-real-ip", "") or (request.client.host if request.client else "")


def _fire_webhook(cfg: dict, payload: dict):
    url = cfg.get("webhook_url", "").strip()
    if not url:
        return
    def _send():
        try:
            _req.post(url, json=payload, timeout=3)
        except Exception as e:
            logger.warning("webhook fire failed: %s", e)
    threading.Thread(target=_send, daemon=True).start()


@router.get("/go/{param}", response_class=HTMLResponse)
async def gate_entry(request: Request, param: str):
    """Mail-link landing. Zwei Modi:
      * `param` enthält '.' → HMAC-Token (klassisch, share.google-Wrap)
      * sonst → Slug in gate_links (Multi-Domain-Ready-Links)
    """
    db = request.app.state.db
    global_cfg = db.get_config()
    gate = _resolve_gate(request, db)
    cfg = _effective_cfg(global_cfg, gate)
    ip = _client_ip(request)
    ua = request.headers.get("user-agent", "")
    hmac_secret = global_cfg.get("hmac_secret", "")
    cookie_secret = global_cfg.get("cookie_secret", "")

    # Token-Modus: hat immer '.' als HMAC-Trenner
    payload = None
    token_valid = False
    link_row = None
    if "." in param and hmac_secret:
        payload = verify_token(hmac_secret, param)
        token_valid = payload is not None
    else:
        # Slug-Modus: braucht ein aktives Gate für diese Domain
        if gate:
            link_row = db.get_gate_link(gate["id"], param)

    # Ohne gültigen Token UND ohne bekannten Slug: 404 (kein Random-Traffic auf's Ziel)
    if not payload and not link_row:
        return PlainTextResponse("not found", status_code=404)

    target = ""
    if payload:
        target = payload.get("t") or ""
    elif link_row:
        target = _pick_target(link_row.get("target_override") or gate.get("target_url") or "")
    if not target:
        target = _pick_target(cfg.get("default_target", ""))
    if not target:
        return PlainTextResponse("no target configured", status_code=500)

    # Für die Antwort: nutze param als "token" (Verify-Round schickt's zurück)
    token = param

    bucket = session_bucket_from_request(ip, ua, cookie_secret or "salt")

    # Owner bypass — HMAC-signed "?bypass=..." for the owner to test live
    # (bypass umgeht ALLES, auch den Wartescreen, damit man's schnell testen kann)
    if request.query_params.get("bypass") == _owner_bypass(cookie_secret):
        db.log_decision(ip=ip, asn="", country="", user_agent=ua, target=target,
                        verdict="allow", score=0, signals_json='{"owner_bypass":true}',
                        token_valid=1 if token_valid else 0,
                        dry_run=1 if cfg.get("dry_run") == "1" else 0)
        return RedirectResponse(_forward_ref_params(target, request), status_code=302)

    # If token invalid AND no default_target fallback wanted, 404
    if not token_valid and not target:
        return PlainTextResponse("not found", status_code=404)

    # Verified cookie shortcut — skip the challenge, aber Wait-Screen bleibt
    if verify_cookie(cookie_secret, request.cookies.get(VERIFY_COOKIE, ""), bucket):
        db.log_decision(ip=ip, asn="", country="", user_agent=ua, target=target,
                        verdict="allow", score=0,
                        signals_json='{"verify_cookie":true}',
                        token_valid=1 if token_valid else 0,
                        dry_run=1 if cfg.get("dry_run") == "1" else 0)
        if link_row:
            db.bump_gate_link_hits(link_row["id"])
        return _redirect_or_wait(request, cfg, target)

    # Score server-side signals
    result = score_request(db, cfg, ip=ip, user_agent=ua, rate_bucket=bucket)
    hint = result["verdict_hint"]
    dry_run = cfg.get("dry_run") == "1"

    # Log every decision
    dec_id = db.log_decision(
        ip=ip, asn=result["network"].get("asn", ""),
        country=result["network"].get("country", ""), user_agent=ua,
        target=target, verdict=hint, score=result["score"],
        signals_json=result["signals"],
        token_valid=1 if token_valid else 0,
        dry_run=1 if dry_run else 0)

    if result["score"] >= int(cfg.get("webhook_min_score", "70")):
        _fire_webhook(cfg, {"id": dec_id, "ip": ip, "score": result["score"],
                             "verdict": hint, "signals": result["signals"],
                             "ts": int(time.time())})

    # DRY-RUN: log verdict but always redirect (via Wait-Screen wenn konfiguriert)
    if dry_run:
        if link_row:
            db.bump_gate_link_hits(link_row["id"])
        return _redirect_or_wait(request, cfg, target)

    if hint == "allow":
        if link_row:
            db.bump_gate_link_hits(link_row["id"])
        return _redirect_or_wait(request, cfg, target)
    if hint == "block":
        return _honeypot(request, cfg)
    # challenge
    return _challenge(request, cfg, token=token, target=target, bucket=bucket)


def _owner_bypass(cookie_secret: str) -> str:
    """Deterministic per-secret bypass token — reveal only in admin UI."""
    return hashlib.sha256(f"owner:{cookie_secret}".encode()).hexdigest()[:24]


def _honeypot(request: Request, cfg: dict) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(request, "honeypot.html", {
        "cfg": cfg,
    }, status_code=403)


def _challenge(request: Request, cfg: dict, *, token: str, target: str,
               bucket: str) -> HTMLResponse:
    """Serve the silent JS challenge page."""
    difficulty = int(cfg.get("pow_difficulty", "5"))
    pow_seed = hashlib.sha256(f"{bucket}|{int(time.time() // 60)}|{cfg.get('cookie_secret', '')}".encode()).hexdigest()[:16]
    return request.app.state.templates.TemplateResponse(request, "challenge.html", {
        "cfg": cfg,
        "token": token,
        "pow_seed": pow_seed,
        "pow_difficulty": difficulty,
        "target_preview": target,
    })


# ── verify: challenge form submits here ────────────────────

@router.post("/verify")
async def verify(request: Request,
                  token: str = Form(""),
                  honeypot: str = Form(""),
                  webdriver: str = Form(""),
                  webgl_vendor: str = Form(""),
                  canvas_hash: str = Form(""),
                  no_plugins: str = Form(""),
                  submit_ms: str = Form(""),
                  pow_seed: str = Form(""),
                  pow_answer: str = Form(""),
                  pow_hash: str = Form("")):
    db = request.app.state.db
    global_cfg = db.get_config()
    gate = _resolve_gate(request, db)
    cfg = _effective_cfg(global_cfg, gate)
    ip = _client_ip(request)
    ua = request.headers.get("user-agent", "")
    hmac_secret = global_cfg.get("hmac_secret", "")
    cookie_secret = global_cfg.get("cookie_secret", "")

    payload = None
    link_row = None
    if "." in token and hmac_secret:
        payload = verify_token(hmac_secret, token)
    else:
        if gate:
            link_row = db.get_gate_link(gate["id"], token)
    target = ""
    if payload:
        target = payload.get("t") or ""
    elif link_row:
        target = _pick_target(link_row.get("target_override") or gate.get("target_url") or "")
    if not target:
        target = _pick_target(cfg.get("default_target", ""))
    if not target:
        return PlainTextResponse("no target", status_code=400)

    bucket = session_bucket_from_request(ip, ua, cookie_secret or "salt")

    # PoW verification: sha256(seed:answer) must have >= difficulty leading '0' nibbles
    difficulty = int(cfg.get("pow_difficulty", "5"))
    pow_ok = False
    if pow_seed and pow_answer:
        h = hashlib.sha256(f"{pow_seed}:{pow_answer}".encode()).hexdigest()
        pow_ok = h.startswith("0" * difficulty) and (not pow_hash or h == pow_hash)

    # Turnstile-Verify (falls Gate ein Widget hat)
    ts_secret = (cfg.get("turnstile_secret_key") or "").strip()
    ts_response = ""
    try:
        form_data = await request.form()
        ts_response = (form_data.get("cf-turnstile-response") or "").strip()
    except Exception:
        pass
    turnstile_ok = None   # None = kein Turnstile aktiv, True/False = Ergebnis
    if ts_secret:
        turnstile_ok = False
        if ts_response:
            try:
                import requests as _req
                r = _req.post(
                    "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                    data={"secret": ts_secret, "response": ts_response,
                          "remoteip": ip},
                    timeout=6,
                )
                if r.status_code == 200:
                    turnstile_ok = bool(r.json().get("success"))
            except Exception as e:
                logger.warning("Turnstile verify failed: %s", e)

    client_signals = {
        "honeypot": bool(honeypot),
        "webdriver": webdriver == "true",
        "webgl_vendor": webgl_vendor,
        "canvas_hash": canvas_hash,
        "no_plugins": no_plugins == "true",
        "submit_ms": submit_ms,
        "pow_ok": pow_ok,
        "turnstile_ok": turnstile_ok,
    }
    result = score_request(db, cfg, ip=ip, user_agent=ua,
                            client_signals=client_signals, rate_bucket=bucket)
    hint = result["verdict_hint"]
    dry_run = cfg.get("dry_run") == "1"

    dec_id = db.log_decision(
        ip=ip, asn=result["network"].get("asn", ""),
        country=result["network"].get("country", ""), user_agent=ua,
        target=target, verdict=hint, score=result["score"],
        signals_json=result["signals"],
        token_valid=1 if payload else 0,
        dry_run=1 if dry_run else 0)

    if result["score"] >= int(cfg.get("webhook_min_score", "70")):
        _fire_webhook(cfg, {"id": dec_id, "ip": ip, "score": result["score"],
                             "verdict": hint, "signals": result["signals"],
                             "phase": "verify", "ts": int(time.time())})

    # Verify-Semantik: hard-block only bleibt block. Alles andere (auch wenn
    # der Score noch im "challenge"-Bereich ist) gilt als "hat die Challenge
    # bestanden" — schließlich hat der Nutzer PoW gelöst UND Signals geliefert.
    # Nur echte Bot-Signale (honeypot, webdriver=true, PoW-fail, submit_ms<500)
    # ziehen ihn in den Block-Bereich; wenn er trotz Challenge dort landet
    # ist's auch klar Bot.
    if hint == "block":
        return _honeypot(request, cfg)

    ttl = int(cfg.get("verification_ttl_hours", "6")) * 3600
    if link_row:
        db.bump_gate_link_hits(link_row["id"])
    resp = RedirectResponse(_forward_ref_params(target, request), status_code=302)
    resp.set_cookie(VERIFY_COOKIE,
                    issue_verify_cookie(cookie_secret, bucket, ttl),
                    max_age=ttl, httponly=True, samesite="strict",
                    secure=True, path="/")
    return resp


# ── /api/check — for external integration (POST) ──────────

@router.post("/api/check")
async def api_check(request: Request):
    """Programmatic check. POST JSON: {ip, user_agent, headers, client_signals}.
    Returns {verdict, score, signals}. Requires HMAC secret in X-Antibot-Auth
    header (constant-time compare)."""
    db = request.app.state.db
    cfg = db.get_config()
    import hmac as _hmac
    given = request.headers.get("x-antibot-auth", "")
    if not given or not _hmac.compare_digest(given, cfg.get("hmac_secret", "")):
        return PlainTextResponse("forbidden", status_code=403)
    try:
        body = await request.json()
    except Exception:
        return PlainTextResponse("bad json", status_code=400)
    ip = body.get("ip", "")
    ua = body.get("user_agent", "")
    client_signals = body.get("client_signals") or {}
    bucket = session_bucket_from_request(ip, ua, cfg.get("cookie_secret", ""))
    result = score_request(db, cfg, ip=ip, user_agent=ua,
                            client_signals=client_signals, rate_bucket=bucket)
    return {
        "score": result["score"],
        "verdict": result["verdict_hint"],
        "signals": result["signals"],
        "network": {
            "asn": result["network"].get("asn", ""),
            "country": result["network"].get("country", ""),
        },
    }
