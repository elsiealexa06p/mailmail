"""Auth — login, setup, session management."""
import os
import hashlib
import secrets
import hmac
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()
SECRET_KEY = os.environ.get("TRANS_SECRET", secrets.token_hex(32))
SESSION_COOKIE = "trans_session"
SESSION_MAX_AGE = 86400 * 7


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000)
    return f"{salt}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if "$" not in stored:
        return False
    salt, h = stored.split("$", 1)
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000)
    return hmac.compare_digest(check.hex(), h)


def create_session_token(user_id: int) -> str:
    nonce = secrets.token_hex(16)
    payload = f"{user_id}:{nonce}"
    sig = hmac.new(SECRET_KEY.encode(), payload.encode(), "sha256").hexdigest()[:32]
    return f"{payload}:{sig}"


def verify_session_token(token: str):
    if not token or token.count(":") != 2:
        return None
    uid_str, nonce, sig = token.split(":", 2)
    payload = f"{uid_str}:{nonce}"
    expected = hmac.new(SECRET_KEY.encode(), payload.encode(), "sha256").hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        return int(uid_str)
    except ValueError:
        return None


def get_current_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    uid = verify_session_token(token)
    if uid is None:
        return None
    row = request.app.state.db.get_user_by_id(uid)
    if not row:
        return None
    user = dict(row)
    user["perms"] = json.loads(user.get("permissions_json") or "{}")
    return user


import json

ROUTE_PERMISSIONS = {
    "/campaigns": "campaigns", "/smtps": "smtps", "/leads": "leads",
    "/templates": "templates", "/macros": "macros", "/logos": "logos",
    "/redirects": "redirects", "/proxies": "proxies", "/ai": "ai",
    "/config": "config", "/settings": "settings",
}


def check_permission(user: dict, path: str) -> bool:
    if not user:
        return False
    if user.get("role") == "admin":
        return True
    for prefix, perm in ROUTE_PERMISSIONS.items():
        if path == prefix or path.startswith(prefix + "/"):
            return user.get("perms", {}).get(perm, False)
    return True


import time as _time
import threading

# Rate limiting: track failed attempts per IP
_login_attempts = {}  # ip -> [timestamps]
_login_lock = threading.Lock()
_MAX_ATTEMPTS = 5
_WINDOW = 300  # 5 minutes
_LOCKOUT = 900  # 15 minutes


def _check_rate_limit(ip: str) -> tuple:
    """Returns (allowed: bool, wait_seconds: int)."""
    now = _time.time()
    with _login_lock:
        if ip not in _login_attempts:
            _login_attempts[ip] = []
        # Clean old entries
        _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < _LOCKOUT]
        attempts = _login_attempts[ip]
        # Count recent failures in window
        recent = [t for t in attempts if now - t < _WINDOW]
        if len(recent) >= _MAX_ATTEMPTS:
            wait = int(_LOCKOUT - (now - attempts[-_MAX_ATTEMPTS]))
            return False, max(0, wait)
        return True, 0


def _record_failed(ip: str):
    now = _time.time()
    with _login_lock:
        if ip not in _login_attempts:
            _login_attempts[ip] = []
        _login_attempts[ip].append(now)


def _clear_attempts(ip: str):
    with _login_lock:
        _login_attempts.pop(ip, None)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    db = request.app.state.db
    if db.user_count() == 0:
        return request.app.state.templates.TemplateResponse(request, "setup.html", {"error": ""})
    app_cfg = db.get_app_config()
    return request.app.state.templates.TemplateResponse(request, "login.html", {"error": "", "app_cfg": app_cfg})


@router.post("/login")
async def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
    ip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Real-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")

    # Rate limit check
    allowed, wait = _check_rate_limit(ip)
    if not allowed:
        return request.app.state.templates.TemplateResponse(
            request, "login.html", {
                "error": f"Too many failed attempts. Try again in {wait // 60} min {wait % 60}s.",
                "app_cfg": request.app.state.db.get_app_config()})

    db = request.app.state.db
    user = db.get_user(username.strip())
    if not user or not verify_password(password, user["password_hash"]):
        _record_failed(ip)
        remaining = _MAX_ATTEMPTS - len([t for t in _login_attempts.get(ip, []) if _time.time() - t < _WINDOW])
        error = "Invalid credentials."
        if remaining <= 2:
            error += f" {remaining} attempt(s) remaining."
        return request.app.state.templates.TemplateResponse(
            request, "login.html", {"error": error, "app_cfg": db.get_app_config()})
    if not dict(user).get("is_active", 1):
        _record_failed(ip)
        return request.app.state.templates.TemplateResponse(
            request, "login.html", {"error": "Account disabled.", "app_cfg": db.get_app_config()})
    _clear_attempts(ip)
    token = create_session_token(user["id"])
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax")
    return resp


@router.post("/setup")
async def setup_submit(request: Request, username: str = Form(""), password: str = Form(""),
                       password2: str = Form(""), display_name: str = Form("")):
    db = request.app.state.db
    if db.user_count() > 0:
        return RedirectResponse("/login", status_code=303)
    if not username.strip() or len(password) < 6 or password != password2:
        return request.app.state.templates.TemplateResponse(
            request, "setup.html", {"error": "Check username and password (min 6 chars, must match)."})
    pw_hash = hash_password(password)
    uid = db.create_user(username.strip(), pw_hash, display_name.strip() or username.strip())
    all_perms = {k: True for k, _ in ROUTE_PERMISSIONS.items()}
    db.update_user(uid, role="admin", permissions_json=json.dumps(all_perms))
    token = create_session_token(uid)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax")
    return resp


@router.get("/logout")
async def logout(request: Request):
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp
