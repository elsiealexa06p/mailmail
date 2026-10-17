"""MIME Profile Rotation — verified header structures from real mail clients.

Each profile modifies the raw MIME output from MIMEBuilder AFTER it's built.
The original builder stays untouched.
Profiles verified against real-world email headers (April 2026).
"""
import re
import struct
import secrets
import time
import random
import uuid
import base64

PROFILES = {
    "default": {
        "description": "Standard (current MIMEBuilder output)",
    },
    "outlook_desktop": {
        "description": "Microsoft Outlook 16.0 Desktop (verified + FILETIME-correct)",
    },
    "apple_mail_mac": {
        "description": "Apple Mail macOS (verified)",
    },
    "apple_mail_iphone": {
        "description": "iPhone Mail (verified)",
    },
    "thunderbird": {
        "description": "Mozilla Thunderbird (verified)",
    },
}


def _windows_filetime_now() -> int:
    """Current time as Windows FILETIME (100ns intervals since 1601-01-01)."""
    epoch_diff = 116444736000000000
    return int(time.time() * 10000000) + epoch_diff


def _filetime_to_parts(ft: int) -> tuple:
    """Split FILETIME into high and low 32-bit words."""
    lo = ft & 0xFFFFFFFF
    hi = (ft >> 32) & 0xFFFFFFFF
    return hi, lo


def _gen_outlook_message_id(domain: str) -> str:
    # On-Prem Exchange style: <HEX40@hostname>
    # Real: <A235C8DCE1BB03468D77EB21972CA6EAE6F0A516@mail.firma.local>
    hex_id = secrets.token_hex(20).upper()
    return f"<{hex_id}@{domain}>"


def _gen_outlook_boundary() -> str:
    # Real: ----=_NextPart_000_00BE_01DCD341.8D93F050
    # Format: 000 = counter, XXXX = seq, 01DCXXXX.XXXXXXXX = FILETIME hi.lo
    ft = _windows_filetime_now()
    # Add small jitter to simulate real timing
    ft += random.randint(-50000000, 50000000)
    hi, lo = _filetime_to_parts(ft)
    seq = f"{random.randint(0,0xFFFF):04X}"
    return f"----=_NextPart_000_{seq}_{hi:08X}.{lo:08X}"


def _gen_outlook_thread_index() -> str:
    # Real Thread-Index: 22 bytes minimum = 1 reserved + 5 FILETIME + 16 GUID
    # Base64 encoded = ~32 chars
    ft = _windows_filetime_now()
    ft_bytes = struct.pack(">Q", ft)[2:7]  # 5 bytes from FILETIME (big-endian, skip top 3)
    reserved = b'\x01'
    guid = secrets.token_bytes(16)
    raw = reserved + ft_bytes + guid  # 22 bytes
    return base64.b64encode(raw).decode()


def _gen_apple_boundary() -> str:
    return f"Apple-Mail-{str(uuid.uuid4()).upper()}"


def _gen_apple_message_id(domain: str) -> str:
    return f"<{str(uuid.uuid4()).upper()}@{domain}>"


def _gen_thunderbird_message_id(domain: str) -> str:
    return f"<{str(uuid.uuid4())}@{domain}>"


def _gen_thunderbird_boundary() -> str:
    # Real: ------------dG5iYXNkZjFt or ------------020406080402050302090800
    return f"------------{secrets.token_hex(12)}"


_TB_VERSIONS = ["128.5.0", "128.4.0", "128.3.0", "115.15.0", "115.14.0"]

_IOS_BUILDS = [
    "22F82",   # iOS 18.5
    "22E252",  # iOS 18.4
    "22D72",   # iOS 18.3.1
    "22C161",  # iOS 18.2.1
    "22B83",   # iOS 18.1.1
    "21G93",   # iOS 17.6.1
    "21F90",   # iOS 17.5.1
]


def apply_profile(raw_mime: str, profile_name: str, from_email: str = "") -> str:
    if profile_name == "default" or profile_name not in PROFILES:
        return raw_mime

    domain = from_email.split("@")[1] if "@" in from_email else "mail.local"

    if "\r\n\r\n" in raw_mime:
        header_part, body = raw_mime.split("\r\n\r\n", 1)
    else:
        return raw_mime

    headers = header_part.split("\r\n")

    if profile_name == "outlook_desktop":
        return _apply_outlook(headers, body, domain)
    elif profile_name == "apple_mail_mac":
        return _apply_apple_mac(headers, body, domain)
    elif profile_name == "apple_mail_iphone":
        return _apply_apple_iphone(headers, body, domain)
    elif profile_name == "thunderbird":
        return _apply_thunderbird(headers, body, domain)

    return raw_mime


def _get_hv(headers, name):
    for h in headers:
        if h.lower().startswith(name.lower() + ":"):
            return h.split(":", 1)[1].strip()
    return ""


def _get_h(headers, name):
    for h in headers:
        if h.lower().startswith(name.lower() + ":"):
            return h
    return None


def _replace_bounds(text, gen_func):
    old = re.findall(r'boundary="([^"]+)"', text)
    mapping = {}
    for ob in old:
        if ob not in mapping:
            mapping[ob] = gen_func()
    for ob, nb in mapping.items():
        text = text.replace(ob, nb)
    return text, mapping


def _apply_outlook(headers, body, domain):
    date_val = _get_hv(headers, "Date")
    from_val = _get_hv(headers, "From")
    to_val = _get_hv(headers, "To")
    subject_val = _get_hv(headers, "Subject")
    ct = _get_h(headers, "Content-Type") or "Content-Type: multipart/alternative"

    new_mid = _gen_outlook_message_id(domain)
    thread_idx = _gen_outlook_thread_index()

    # Replace boundaries in Content-Type and body
    ct_new, bmap = _replace_bounds(ct, _gen_outlook_boundary)
    for ob, nb in bmap.items():
        body = body.replace(ob, nb)

    # Outlook: MIME-Version AFTER Content-Type
    new_headers = [
        f"From: {from_val}",
        f"To: {to_val}",
        f"Subject: {subject_val}",
        f"Date: {date_val}",
        f"Message-ID: {new_mid}",
        ct_new,
        "MIME-Version: 1.0",
        "X-Mailer: Microsoft Outlook 16.0",
        f"Thread-Index: {thread_idx}",
        "Content-Language: de",
    ]

    return "\r\n".join(new_headers) + "\r\n\r\n" + body


def _apply_apple_mac(headers, body, domain):
    date_val = _get_hv(headers, "Date")
    from_val = _get_hv(headers, "From")
    to_val = _get_hv(headers, "To")
    subject_val = _get_hv(headers, "Subject")
    ct = _get_h(headers, "Content-Type") or "Content-Type: multipart/alternative"

    new_mid = _gen_apple_message_id(domain)
    ct_new, bmap = _replace_bounds(ct, _gen_apple_boundary)
    for ob, nb in bmap.items():
        body = body.replace(ob, nb)

    new_headers = [
        ct_new,
        "Content-Transfer-Encoding: 7bit",
        f"From: {from_val}",
        "Mime-Version: 1.0 (1.0)",
        f"Subject: {subject_val}",
        f"Message-Id: {new_mid}",
        f"Date: {date_val}",
        f"To: {to_val}",
    ]

    return "\r\n".join(new_headers) + "\r\n\r\n" + body


def _apply_apple_iphone(headers, body, domain):
    date_val = _get_hv(headers, "Date")
    from_val = _get_hv(headers, "From")
    to_val = _get_hv(headers, "To")
    subject_val = _get_hv(headers, "Subject")
    ct = _get_h(headers, "Content-Type") or "Content-Type: multipart/alternative"

    new_mid = _gen_apple_message_id(domain)
    ct_new, bmap = _replace_bounds(ct, _gen_apple_boundary)
    for ob, nb in bmap.items():
        body = body.replace(ob, nb)

    ios_builds = _IOS_BUILDS

    new_headers = [
        ct_new,
        "Content-Transfer-Encoding: 7bit",
        f"From: {from_val}",
        "Mime-Version: 1.0 (1.0)",
        f"Date: {date_val}",
        f"Subject: {subject_val}",
        f"Message-Id: {new_mid}",
        f"To: {to_val}",
        f"X-Mailer: iPhone Mail ({random.choice(ios_builds)})",
    ]

    return "\r\n".join(new_headers) + "\r\n\r\n" + body


def _apply_thunderbird(headers, body, domain):
    date_val = _get_hv(headers, "Date")
    from_val = _get_hv(headers, "From")
    to_val = _get_hv(headers, "To")
    subject_val = _get_hv(headers, "Subject")
    ct = _get_h(headers, "Content-Type") or "Content-Type: text/plain; charset=UTF-8"

    new_mid = _gen_thunderbird_message_id(domain)

    # Replace boundaries with Thunderbird-style (------------ + hex)
    ct_new, bmap = _replace_bounds(ct, _gen_thunderbird_boundary)
    for ob, nb in bmap.items():
        body = body.replace(ob, nb)

    tb_ua = "Mozilla Thunderbird"

    new_headers = [
        f"Message-ID: {new_mid}",
        f"Date: {date_val}",
        "MIME-Version: 1.0",
        f"User-Agent: {tb_ua}",
        f"To: {to_val}",
        f"From: {from_val}",
        f"Subject: {subject_val}",
        ct_new,
    ]

    return "\r\n".join(new_headers) + "\r\n\r\n" + body


def get_random_profile() -> str:
    return random.choice(list(PROFILES.keys()))


def get_profile_names() -> list:
    return [(k, v.get("description", k)) for k, v in PROFILES.items()]
