"""HMAC-signed tokens (for gate links) and verification cookies.

Token payload for /go/{token}:
    {
        "t":  "<target_url>",
        "c":  "<campaign_id>",           optional
        "r":  "<recipient_hash>",        optional (base64url sha256 of email)
        "exp": <unix_ts>                 optional expiry
    }

Wire format: base64url(json_payload) + "." + base64url(hmac-sha256)
"""
import hmac
import json
import time
import hashlib
import base64
from typing import Optional


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(msg: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode(), msg, hashlib.sha256).digest()


def create_token(secret: str, target: str = "", campaign: str = "",
                 recipient: str = "", ttl_seconds: int = 0) -> str:
    payload = {"t": target}
    if campaign:
        payload["c"] = campaign
    if recipient:
        payload["r"] = recipient
    if ttl_seconds > 0:
        payload["exp"] = int(time.time()) + ttl_seconds
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = _b64e(raw)
    sig = _b64e(_sign(body.encode(), secret))
    return f"{body}.{sig}"


def verify_token(secret: str, token: str) -> Optional[dict]:
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expected = _b64e(_sign(body.encode(), secret))
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_b64d(body))
    except Exception:
        return None
    if "exp" in payload and int(payload["exp"]) < int(time.time()):
        return None
    return payload


# ── Verification cookie (for "already proven human this session") ──

def issue_verify_cookie(secret: str, session_bucket: str, ttl_seconds: int) -> str:
    """Cookie value proving 'this session bucket passed the challenge'."""
    exp = int(time.time()) + ttl_seconds
    body = f"{session_bucket}|{exp}"
    sig = _b64e(_sign(body.encode(), secret))
    return f"{_b64e(body.encode())}.{sig}"


def verify_cookie(secret: str, cookie_value: str, session_bucket: str) -> bool:
    if not cookie_value or "." not in cookie_value:
        return False
    body_enc, sig = cookie_value.rsplit(".", 1)
    try:
        body = _b64d(body_enc).decode()
    except Exception:
        return False
    expected = _b64e(_sign(body.encode(), secret))
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        bucket, exp_str = body.rsplit("|", 1)
        exp = int(exp_str)
    except Exception:
        return False
    if exp < int(time.time()):
        return False
    return bucket == session_bucket


def session_bucket_from_request(ip: str, user_agent: str, salt: str) -> str:
    """Cheap per-visitor bucket — IP + UA fingerprint. Not for authn, just
    for rate-limiting and verification-cookie binding."""
    h = hashlib.sha256(f"{ip}|{user_agent}|{salt}".encode()).digest()
    return _b64e(h)[:16]
