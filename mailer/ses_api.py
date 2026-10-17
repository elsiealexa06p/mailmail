"""AWS SES v2 SendEmail (Simple content) via HTTPS with hand-rolled SigV4.

Kein boto3 nötig — SigV4 sind ~40 Zeilen mit hashlib+hmac. Wir sprechen
den Simple-Endpoint an: SES baut das MIME selbst nach ihrem
zustelloptimierten Schema (der "Fingerprint"), wir liefern nur
strukturierte Felder + optionale Custom-Header (List-Unsubscribe,
List-Unsubscribe-Post, Reply-To sind whitelisted).
"""
from __future__ import annotations
import datetime
import hashlib
import hmac
import json
import logging
from typing import Optional

logger = logging.getLogger("mailer.ses_api")

SERVICE = "ses"
ALGO = "AWS4-HMAC-SHA256"


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signature_key(secret: str, date_stamp: str, region: str,
                    service: str) -> bytes:
    k_date = _sign(("AWS4" + secret).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "aws4_request")


def _sigv4_headers(method: str, host: str, path: str, region: str,
                    body: str, iam_key: str, iam_secret: str,
                    content_type: str = "application/json") -> dict:
    """Build headers with SigV4 v4 signature."""
    now = datetime.datetime.utcnow()
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    payload_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    # Canonical request
    canonical_uri = path
    canonical_querystring = ""
    canonical_headers = (
        f"content-type:{content_type}\n"
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "content-type;host;x-amz-content-sha256;x-amz-date"

    canonical_request = (
        f"{method}\n{canonical_uri}\n{canonical_querystring}\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )

    credential_scope = f"{date_stamp}/{region}/{SERVICE}/aws4_request"
    string_to_sign = (
        f"{ALGO}\n{amz_date}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )

    signing_key = _signature_key(iam_secret, date_stamp, region, SERVICE)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()

    auth = (
        f"{ALGO} Credential={iam_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Content-Type": content_type,
        "X-Amz-Date": amz_date,
        "X-Amz-Content-Sha256": payload_hash,
        "Authorization": auth,
        "Host": host,
    }


class SESAPIError(Exception):
    """Fehler mit AWS-Response-Code für die Klassifizierung."""
    def __init__(self, msg: str, status: int = 0, code: str = ""):
        super().__init__(msg)
        self.status = status
        self.code = code


def _ses_call(iam_key: str, iam_secret: str, region: str,
               method: str, path: str, body: dict = None,
               timeout: int = 20) -> dict:
    import requests
    host = f"email.{region}.amazonaws.com"
    body_str = json.dumps(body or {}, separators=(",", ":"))
    headers = _sigv4_headers(method, host, path, region, body_str,
                              iam_key, iam_secret)
    url = f"https://{host}{path}"
    r = requests.request(method, url, headers=headers, data=body_str,
                          timeout=timeout)
    try:
        data = r.json() if r.text else {}
    except Exception:
        data = {"_raw": r.text[:500]}
    if r.status_code >= 400:
        # AWS liefert bei Fehlern {message: "...", __type: "SES/AccessDenied"}
        code = (data.get("__type") or data.get("code") or "").split("#")[-1]
        msg = data.get("message") or data.get("Message") or data.get("_raw") \
              or f"HTTP {r.status_code}"
        raise SESAPIError(f"[{r.status_code}] {code}: {msg}",
                          status=r.status_code, code=code)
    return data


def ses_send_simple(iam_key: str, iam_secret: str, region: str,
                     from_addr: str, from_name: str,
                     to_addr: str, subject: str,
                     html_body: str, plain_body: str = "",
                     extra_headers: Optional[list] = None,
                     reply_to: str = "",
                     configuration_set: str = "",
                     attachments: Optional[list] = None) -> dict:
    """SES v2 SendEmail mit Content.Simple.

    extra_headers: [{"Name": "...", "Value": "..."}] — SES whitelistet nur
    bestimmte Header-Namen (List-Unsubscribe, List-Unsubscribe-Post,
    List-Help, List-Id, Reply-To, Message-ID sind erlaubt).

    attachments (seit SES-API-Erweiterung 2025 in Simple erlaubt):
      [{
        "FileName": "katalog.pdf", "ContentType": "application/pdf",
        "RawContent": "<base64 str>",           # base64 vom Datei-Byte-Inhalt
        "ContentDisposition": "ATTACHMENT" | "INLINE",  # optional
        "ContentId": "logo@mail",               # nur INLINE, für cid:...
        "ContentTransferEncoding": "BASE64"     # optional
      }]

    Returns {MessageId: ...}. Wirft SESAPIError bei Fehler."""
    display_from = (f"{from_name} <{from_addr}>" if from_name else from_addr)

    body_obj = {"Html": {"Data": html_body, "Charset": "UTF-8"}}
    if plain_body:
        body_obj["Text"] = {"Data": plain_body, "Charset": "UTF-8"}

    simple = {
        "Subject": {"Data": subject, "Charset": "UTF-8"},
        "Body": body_obj,
    }
    if extra_headers:
        simple["Headers"] = extra_headers
    if attachments:
        simple["Attachments"] = attachments

    payload = {
        "FromEmailAddress": display_from,
        "Destination": {"ToAddresses": [to_addr]},
        "Content": {"Simple": simple},
    }
    if reply_to:
        payload["ReplyToAddresses"] = [reply_to]
    if configuration_set:
        payload["ConfigurationSetName"] = configuration_set

    return _ses_call(iam_key, iam_secret, region,
                     "POST", "/v2/email/outbound-emails", body=payload)


def ses_ping(iam_key: str, iam_secret: str, region: str) -> dict:
    """GetAccount als billiger Auth-Test. Liefert Send-Quota + Reputation."""
    return _ses_call(iam_key, iam_secret, region,
                     "GET", "/v2/email/account", body={})


# ── Fehler-Klassifizierung analog SMTP ────────────────────

# hart: Empfänger tot / dauerhaft
HARD_CODES = {"MessageRejected", "MailFromDomainNotVerified",
              "SendingPausedException", "AccountSuspendedException"}

# transient: kurzfristig retryen (Rate-Limit, Throttling)
TRANSIENT_CODES = {"TooManyRequestsException", "ThrottlingException",
                   "LimitExceededException", "RequestTimeout",
                   "ServiceUnavailable", "InternalFailure"}

# fatal für den Account: IAM permissions weg → Account rausschmeißen
FATAL_CODES = {"AccessDenied", "AccessDeniedException",
               "InvalidClientTokenId", "SignatureDoesNotMatch",
               "IncompleteSignature", "TokenRefreshRequired"}


def classify_ses_error(err: SESAPIError) -> str:
    if err.code in FATAL_CODES:
        return "auth"       # SMTP-Äquivalent
    if err.code in TRANSIENT_CODES:
        return "transient"
    if err.code in HARD_CODES:
        return "hard"
    if 500 <= err.status < 600:
        return "transient"
    if err.status == 400:
        return "hard"
    return "transient"
