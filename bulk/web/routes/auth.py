"""Authentication — login, logout, session management, password hashing."""
import os
import hashlib
import secrets
import hmac
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()

SECRET_KEY = os.environ.get("BULK_SECRET", secrets.token_hex(32))
SESSION_COOKIE = "bulk_session"
SESSION_MAX_AGE = 86400 * 7  # 7 days


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
    return dict(row)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    db = request.app.state.db
    if db.user_count() == 0:
        return request.app.state.templates.TemplateResponse(
            request, "setup.html", {"error": ""})
    return request.app.state.templates.TemplateResponse(
        request, "login.html", {"error": ""})


import time as _time
import threading

_login_attempts = {}
_login_lock = threading.Lock()
_MAX_ATTEMPTS = 5
_WINDOW = 300
_LOCKOUT = 900


def _check_rate_limit(ip: str) -> tuple:
    now = _time.time()
    with _login_lock:
        if ip not in _login_attempts:
            _login_attempts[ip] = []
        _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < _LOCKOUT]
        recent = [t for t in _login_attempts[ip] if now - t < _WINDOW]
        if len(recent) >= _MAX_ATTEMPTS:
            wait = int(_LOCKOUT - (now - _login_attempts[ip][-_MAX_ATTEMPTS]))
            return False, max(0, wait)
        return True, 0


def _record_failed(ip: str):
    with _login_lock:
        if ip not in _login_attempts:
            _login_attempts[ip] = []
        _login_attempts[ip].append(_time.time())


def _clear_attempts(ip: str):
    with _login_lock:
        _login_attempts.pop(ip, None)


@router.post("/login")
async def login_submit(request: Request,
                       username: str = Form(""),
                       password: str = Form("")):
    ip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Real-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")

    allowed, wait = _check_rate_limit(ip)
    if not allowed:
        return request.app.state.templates.TemplateResponse(
            request, "login.html", {
                "error": f"Too many failed attempts. Try again in {wait // 60}m {wait % 60}s."})

    db = request.app.state.db
    user = db.get_user(username.strip())
    if not user or not verify_password(password, user["password_hash"]):
        _record_failed(ip)
        return request.app.state.templates.TemplateResponse(
            request, "login.html", {"error": "Invalid username or password."})
    _clear_attempts(ip)
    token = create_session_token(user["id"])
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_MAX_AGE,
                    httponly=True, samesite="lax")
    return resp


@router.post("/setup")
async def setup_submit(request: Request,
                       username: str = Form(""),
                       password: str = Form(""),
                       password2: str = Form(""),
                       display_name: str = Form("")):
    db = request.app.state.db
    if db.user_count() > 0:
        return RedirectResponse("/login", status_code=303)

    errors = []
    if not username.strip():
        errors.append("Username is required.")
    if len(password) < 6:
        errors.append("Password must be at least 6 characters.")
    if password != password2:
        errors.append("Passwords do not match.")

    if errors:
        return request.app.state.templates.TemplateResponse(
            request, "setup.html", {"error": " ".join(errors)})

    pw_hash = hash_password(password)
    uid = db.create_user(username.strip(), pw_hash, display_name.strip() or username.strip())
    token = create_session_token(uid)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_MAX_AGE,
                    httponly=True, samesite="lax")
    return resp


@router.get("/logout")
async def logout(request: Request):
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp
