"""AWS S3 Uploader mit SigV4 — public-read PUT + Auto-Bucket-Setup
(Create + Public-Access-Config + Bucket-Policy). Kein boto3, alles per
SigV4-signierten Requests. Optional durch SOCKS/HTTP-Proxy.
"""
from __future__ import annotations
import datetime
import hashlib
import hmac
import logging
import json
from urllib.parse import quote
from typing import Optional

logger = logging.getLogger("mailer.s3")

ALGO = "AWS4-HMAC-SHA256"


# ── SigV4 core ─────────────────────────────────────────────

def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signature_key(secret: str, date_stamp: str, region: str,
                    service: str = "s3") -> bytes:
    k_date = _sign(("AWS4" + secret).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "aws4_request")


def _sigv4_headers(method: str, host: str, path: str, query_string: str,
                    region: str, body_bytes: bytes, content_type: str,
                    iam_key: str, iam_secret: str,
                    extra_headers: Optional[dict] = None,
                    service: str = "s3") -> dict:
    """SigV4-signed headers for any S3 method. query_string in canonical
    form (sorted, uri-encoded values)."""
    now = datetime.datetime.utcnow()
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body_bytes).hexdigest()

    hdrs = {
        "host":                 host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date":           amz_date,
    }
    if content_type:
        hdrs["content-type"] = content_type
    if extra_headers:
        for k, v in extra_headers.items():
            hdrs[k.lower()] = v

    signed_names = sorted(hdrs.keys())
    canonical_headers = "".join(f"{n}:{hdrs[n]}\n" for n in signed_names)
    signed_headers = ";".join(signed_names)

    canonical_request = (
        f"{method}\n{path}\n{query_string}\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"{ALGO}\n{amz_date}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )
    signing_key = _signature_key(iam_secret, date_stamp, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()

    hdrs["authorization"] = (
        f"{ALGO} Credential={iam_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return hdrs


def _proxy_dict(proxy: str) -> Optional[dict]:
    """Normalisiere alle möglichen Proxy-Formate zu requests-Format.
    Akzeptiert:
      host:port
      host:port:user:pass
      scheme://host:port
      scheme://user:pass@host:port
      scheme://host:port:user:pass          ← häufiges Colon-Auth-Format
    """
    if not proxy or not proxy.strip():
        return None
    s = proxy.strip()
    scheme = "socks5h"
    rest = s
    if "://" in s:
        scheme, rest = s.split("://", 1)
    # Wenn bereits user:pass@host:port drin, unverändert
    if "@" in rest:
        url = f"{scheme}://{rest}"
        return {"http": url, "https": url}
    parts = rest.split(":")
    if len(parts) == 2:
        url = f"{scheme}://{parts[0]}:{parts[1]}"
    elif len(parts) == 4:
        # host:port:user:pass — Colon-Auth-Format umschreiben
        url = f"{scheme}://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    else:
        return None
    return {"http": url, "https": url}


class S3Error(Exception):
    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status


def _s3_request(method: str, iam_key: str, iam_secret: str, region: str,
                 bucket: str, path: str = "/", query_string: str = "",
                 body: bytes = b"", content_type: str = "",
                 extra_headers: Optional[dict] = None,
                 proxy: str = "", timeout: int = 30,
                 accept_status: tuple = (200, 204)) -> tuple:
    """Low-level S3-Request. Returns (status_code, response_text)."""
    import requests
    host = (f"{bucket}.s3.amazonaws.com" if region == "us-east-1"
             else f"{bucket}.s3.{region}.amazonaws.com")
    if not path.startswith("/"):
        path = "/" + path
    hdrs = _sigv4_headers(method, host, path, query_string, region,
                           body, content_type, iam_key, iam_secret,
                           extra_headers)
    url = f"https://{host}{path}"
    if query_string:
        url = f"{url}?{query_string}"
    kwargs = {"headers": hdrs, "timeout": timeout}
    if body:
        kwargs["data"] = body
    px = _proxy_dict(proxy)
    if px:
        kwargs["proxies"] = px
    try:
        r = requests.request(method, url, **kwargs)
    except Exception as e:
        raise S3Error(f"HTTP failed: {e}", 0)
    if r.status_code not in accept_status and r.status_code >= 400:
        raise S3Error(f"{method} {path}?{query_string} [{r.status_code}]: "
                       f"{r.text[:400]}", r.status_code)
    return r.status_code, r.text


# ── High-level ──────────────────────────────────────────────

def s3_bucket_url(bucket: str, region: str, key: str) -> str:
    if region == "us-east-1":
        return f"https://{bucket}.s3.amazonaws.com/{key}"
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def s3_create_bucket(iam_key: str, iam_secret: str, region: str,
                      bucket: str, proxy: str = "") -> dict:
    """PUT / mit CreateBucketConfiguration. us-east-1 braucht keinen Body.
    Idempotent: 409 BucketAlreadyOwnedByYou ist OK, 200 ist OK."""
    if region == "us-east-1":
        body = b""
    else:
        body = (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<CreateBucketConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f'<LocationConstraint>{region}</LocationConstraint>'
            f'</CreateBucketConfiguration>'
        ).encode("utf-8")
    try:
        status, _ = _s3_request("PUT", iam_key, iam_secret, region, bucket,
                                 body=body, content_type="application/xml" if body else "",
                                 proxy=proxy, accept_status=(200,))
        return {"ok": True, "created": True, "status": status}
    except S3Error as e:
        # Bereits vorhanden = OK
        if e.status in (409,) or "already" in str(e).lower():
            return {"ok": True, "created": False, "status": e.status,
                    "note": "bucket bereits vorhanden (OK)"}
        raise


def s3_disable_block_public_access(iam_key: str, iam_secret: str,
                                     region: str, bucket: str,
                                     proxy: str = "") -> dict:
    """PUT ?publicAccessBlock — schaltet alle 4 Block-Public-Access flags aus."""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<PublicAccessBlockConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        '<BlockPublicAcls>false</BlockPublicAcls>'
        '<IgnorePublicAcls>false</IgnorePublicAcls>'
        '<BlockPublicPolicy>false</BlockPublicPolicy>'
        '<RestrictPublicBuckets>false</RestrictPublicBuckets>'
        '</PublicAccessBlockConfiguration>'
    ).encode("utf-8")
    md5 = hashlib.md5(body).digest()
    import base64
    hdrs = {"content-md5": base64.b64encode(md5).decode("ascii")}
    status, _ = _s3_request("PUT", iam_key, iam_secret, region, bucket,
                             query_string="publicAccessBlock=",
                             body=body, content_type="application/xml",
                             extra_headers=hdrs, proxy=proxy,
                             accept_status=(200,))
    return {"ok": True, "status": status}


def s3_put_public_read_policy(iam_key: str, iam_secret: str,
                                region: str, bucket: str,
                                proxy: str = "") -> dict:
    """PUT ?policy mit Public-Read auf allen Objekten des Buckets."""
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "MailerPublicRead",
            "Effect": "Allow",
            "Principal": "*",
            "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{bucket}/*",
        }]
    }
    body = json.dumps(policy, separators=(",", ":")).encode("utf-8")
    status, _ = _s3_request("PUT", iam_key, iam_secret, region, bucket,
                             query_string="policy=",
                             body=body, content_type="application/json",
                             proxy=proxy, accept_status=(200, 204))
    return {"ok": True, "status": status}


def s3_setup_bucket(iam_key: str, iam_secret: str, region: str,
                     bucket: str, proxy: str = "") -> dict:
    """Full-Auto: Bucket erstellen (falls fehlt), Public-Access-Block
    ausschalten, Bucket-Policy für public-read anhängen. Idempotent.
    Rückgabe: {ok, steps: [...]}."""
    steps = []
    try:
        r1 = s3_create_bucket(iam_key, iam_secret, region, bucket, proxy)
        steps.append({"step": "create_bucket", "result": r1})
    except S3Error as e:
        return {"ok": False, "steps": steps, "error": f"create_bucket: {e}"}
    try:
        r2 = s3_disable_block_public_access(iam_key, iam_secret, region, bucket, proxy)
        steps.append({"step": "disable_block_public_access", "result": r2})
    except S3Error as e:
        return {"ok": False, "steps": steps, "error": f"public_access: {e}"}
    try:
        r3 = s3_put_public_read_policy(iam_key, iam_secret, region, bucket, proxy)
        steps.append({"step": "put_policy", "result": r3})
    except S3Error as e:
        return {"ok": False, "steps": steps, "error": f"policy: {e}"}
    return {"ok": True, "steps": steps}


def s3_upload_object(iam_key: str, iam_secret: str, region: str,
                      bucket: str, key: str, body: bytes,
                      content_type: str = "image/png",
                      public: bool = True,      # kept for API-compat
                      proxy: str = "",
                      timeout: int = 30) -> str:
    """PUT bytes → S3. Returns public URL.

    Hinweis: seit April 2023 haben neue AWS-Buckets standardmäßig
    „Bucket owner enforced" gesetzt, was Object-ACLs abschaltet. Deshalb
    schicken wir KEINEN x-amz-acl-Header — der Bucket ist über die
    Bucket-Policy (aus s3_setup_bucket) bereits public-read. Der `public`
    Parameter bleibt aus Kompatibilität erhalten, hat aber keinen Effekt
    mehr auf den Request selbst."""
    key_path = "/" + quote(key.lstrip("/"))
    _s3_request("PUT", iam_key, iam_secret, region, bucket,
                 path=key_path, body=body, content_type=content_type,
                 proxy=proxy, timeout=timeout,
                 accept_status=(200,))
    return s3_bucket_url(bucket, region, key)


def s3_list_objects(iam_key: str, iam_secret: str, region: str,
                     bucket: str, continuation_token: str = "",
                     proxy: str = "", timeout: int = 30) -> tuple:
    """GET /?list-type=2 → (keys[], next_continuation_token or "").
    Max 1000 keys pro Request — paginiere via continuation_token."""
    import re
    qs_parts = ["list-type=2"]
    if continuation_token:
        qs_parts.append("continuation-token=" + quote(continuation_token, safe=""))
    query_string = "&".join(sorted(qs_parts))
    status, text = _s3_request("GET", iam_key, iam_secret, region, bucket,
                                 query_string=query_string,
                                 proxy=proxy, timeout=timeout,
                                 accept_status=(200,))
    keys = re.findall(r"<Key>([^<]+)</Key>", text)
    next_tok = ""
    m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", text)
    if m:
        next_tok = m.group(1)
    return keys, next_tok


def s3_delete_objects(iam_key: str, iam_secret: str, region: str,
                       bucket: str, keys: list, proxy: str = "",
                       timeout: int = 60) -> int:
    """POST /?delete — löscht bis zu 1000 Objekte in einem Request.
    Returns count deleted (best-effort, counted from response)."""
    if not keys:
        return 0
    import base64 as _b64
    import re as _re
    from xml.sax.saxutils import escape as _xesc
    objs = "".join(f"<Object><Key>{_xesc(k)}</Key></Object>" for k in keys[:1000])
    body = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<Delete><Quiet>true</Quiet>{objs}</Delete>'
    ).encode("utf-8")
    md5 = _b64.b64encode(hashlib.md5(body).digest()).decode("ascii")
    hdrs = {"content-md5": md5}
    status, text = _s3_request("POST", iam_key, iam_secret, region, bucket,
                                 query_string="delete=",
                                 body=body, content_type="application/xml",
                                 extra_headers=hdrs, proxy=proxy,
                                 timeout=timeout, accept_status=(200,))
    errors = _re.findall(r"<Error>", text)
    return len(keys[:1000]) - len(errors)


def s3_delete_bucket(iam_key: str, iam_secret: str, region: str,
                      bucket: str, proxy: str = "", timeout: int = 60) -> dict:
    """DELETE / — Bucket muss leer sein."""
    status, _ = _s3_request("DELETE", iam_key, iam_secret, region, bucket,
                             proxy=proxy, timeout=timeout,
                             accept_status=(204, 200))
    return {"ok": True, "status": status}


def s3_empty_and_delete_bucket(iam_key: str, iam_secret: str, region: str,
                                 bucket: str, proxy: str = "",
                                 timeout: int = 60) -> dict:
    """Full-Auto: alle Objekte löschen (paginiert + batched), danach Bucket
    droppen. Idempotent: 404 NoSuchBucket = OK."""
    deleted = 0
    try:
        cont = ""
        while True:
            keys, cont = s3_list_objects(iam_key, iam_secret, region, bucket,
                                          continuation_token=cont, proxy=proxy,
                                          timeout=timeout)
            if not keys:
                break
            # Batches à 1000
            for i in range(0, len(keys), 1000):
                batch = keys[i:i + 1000]
                deleted += s3_delete_objects(iam_key, iam_secret, region,
                                              bucket, batch, proxy=proxy,
                                              timeout=timeout)
            if not cont:
                break
    except S3Error as e:
        if e.status == 404 or "NoSuchBucket" in str(e):
            return {"ok": True, "deleted": deleted,
                     "note": "bucket existierte nicht (OK)"}
        return {"ok": False, "deleted": deleted,
                 "error": f"list/delete-objects: {e}"}
    try:
        s3_delete_bucket(iam_key, iam_secret, region, bucket, proxy=proxy,
                          timeout=timeout)
    except S3Error as e:
        if e.status == 404 or "NoSuchBucket" in str(e):
            return {"ok": True, "deleted": deleted,
                     "note": "bucket bereits weg (OK)"}
        return {"ok": False, "deleted": deleted,
                 "error": f"delete_bucket: {e}"}
    return {"ok": True, "deleted": deleted}


def s3_ping(iam_key: str, iam_secret: str, region: str, bucket: str,
             proxy: str = "") -> dict:
    """Auth+Bucket-Test — schreibt+löscht 1-Byte Objekt."""
    import secrets
    key = f".mailer-ping-{secrets.token_hex(6)}"
    try:
        url = s3_upload_object(iam_key, iam_secret, region, bucket, key,
                                b".", content_type="text/plain",
                                public=False, proxy=proxy)
        return {"ok": True, "url": url}
    except S3Error as e:
        return {"ok": False, "error": str(e), "status": e.status}


def parse_buckets_field(text: str) -> list:
    out = []
    for chunk in (text or "").replace("\n", ",").split(","):
        c = chunk.strip()
        if not c:
            continue
        if ":" in c:
            b, r = c.split(":", 1)
            out.append((b.strip(), r.strip() or "us-east-1"))
        else:
            out.append((c, "us-east-1"))
    return out
