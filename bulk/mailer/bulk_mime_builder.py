"""Bulk MIME Builder.

Builds RFC-compliant newsletter/bulk emails with proper List-* headers,
Precedence, Feedback-ID, VERP envelope, and X-Entity-Ref-ID.

NO Anti-Fingerprint. NO Spintax obfuscation.
This is meant to look like legitimate ESP output.
"""
import re
import mimetypes
import secrets
import time
import base64
import uuid
from email import quoprimime
from email.utils import formataddr, encode_rfc2231
from email.header import Header
from typing import Optional, Tuple, List, Dict

# All C0 control chars (0x00-0x1F) + DEL (0x7F) are forbidden in header
# values — strip them to close header-injection / truncation vectors.
_HEADER_FORBIDDEN = dict.fromkeys(list(range(0x00, 0x20)) + [0x7F])
_EOL_RE = re.compile(r"\r\n|\r|\n")
_FEEDBACK_RE = re.compile(r"^[\w\-.:]*:[\w\-.:]*:[\w\-.:]*:[\w\-.]{5,15}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_LIST_ID_RE = re.compile(r"^[\w\-]+(\.[\w\-]+)+$")
_RESERVED_HEADERS = frozenset({
    "from", "to", "subject", "date", "message-id", "mime-version",
    "content-type", "list-unsubscribe", "list-unsubscribe-post",
    "list-id", "precedence", "feedback-id", "x-entity-ref-id", "reply-to",
})


class BulkMIMEBuilder:

    @staticmethod
    def _sanitize(v: str) -> str:
        # Strip CR, LF, NUL, TAB, and all other C0 controls + DEL.
        return v.translate(_HEADER_FORBIDDEN).strip()

    @staticmethod
    def _to_crlf(text: str) -> str:
        # Collapse \r\n, lone \r, lone \n to a single \r\n (no doubling).
        return _EOL_RE.sub("\r\n", text)

    @staticmethod
    def _max_line_len(text: str) -> int:
        # Measured in octets — the 998 SMTP limit is byte-based.
        return max((len(l.encode("utf-8")) for l in text.splitlines()), default=0)

    @staticmethod
    def _qp_encode(text: str) -> str:
        # email.quoprimime.body_encode folds at <=76 and never splits an
        # =XX escape — unlike quopri.encodestring which does no wrapping.
        normalized = BulkMIMEBuilder._to_crlf(text)
        raw = normalized.encode("utf-8").decode("latin-1")
        return quoprimime.body_encode(raw, maxlinelen=76, eol="\r\n")

    @staticmethod
    def _fold_header(name: str, value: str) -> str:
        # Fold long header values on FWS; ASCII stays readable, non-ASCII
        # becomes folded encoded-words. Header.encode() folds with bare LF,
        # so normalize back to CRLF for a uniformly-CRLF message.
        try:
            value.encode("ascii")
            h = Header(value, "ascii", maxlinelen=78, header_name=name)
        except UnicodeEncodeError:
            h = Header(value, "utf-8", maxlinelen=78, header_name=name)
        return BulkMIMEBuilder._to_crlf(h.encode(splitchars=" ;,"))

    @staticmethod
    def _fold_addr(name: str, formatted: str) -> str:
        # Fold an already-formatted address header (output of formataddr).
        # Folds on FWS in the display name, leaves the angle-addr intact,
        # and does not re-encode an existing encoded-word.
        return BulkMIMEBuilder._to_crlf(
            Header(formatted, header_name=name, maxlinelen=78).encode(splitchars=" "))

    @staticmethod
    def _quote_param(filename: str) -> str:
        # RFC 2045/2183 quoted-string: escape backslash then quote so a
        # filename containing " or \ can't break out of the parameter.
        return filename.replace("\\", "\\\\").replace('"', '\\"')

    @classmethod
    def _get_encoding(cls, text: str, force_qp: bool = False) -> tuple:
        # 7bit is only legal if ASCII AND every line <= 998 octets
        # (RFC 5321 §4.5.3.1). Otherwise quoted-printable with proper
        # 76-char soft wrapping, which satisfies the 998 limit too.
        normalized = cls._to_crlf(text)
        if not force_qp:
            try:
                normalized.encode("ascii")
                if cls._max_line_len(normalized) <= 998:
                    return "7bit", normalized
            except UnicodeEncodeError:
                pass
        return "quoted-printable", cls._qp_encode(text)

    @staticmethod
    def _boundary() -> str:
        return f"----=_Part_{int(time.time()*1000)}_{secrets.token_hex(8)}"

    @staticmethod
    def _message_id(domain: str) -> str:
        ts = format(int(time.time() * 1000), "x")
        rnd = secrets.token_hex(16)
        return f"<{ts}.{rnd}@{domain}>"

    @staticmethod
    def _validate_email(email: str, label: str):
        if not _EMAIL_RE.match(email):
            raise ValueError(f"Invalid {label}: {email!r}")

    @classmethod
    def build_email(
        cls,
        from_name: str,
        from_email: str,
        reply_to_name: str,
        reply_to_email: str,
        to_email: str,
        subject: str,
        html_body: str,
        plain_body: str,
        list_id_token: str,
        list_id_name: str = "",
        unsubscribe_url: str = "",
        unsubscribe_mailto: str = "",
        feedback_id: str = "",
        bounce_domain: str = "",
        message_id_domain: str = "",
        recipient_id: str = "",
        attachment: Optional[Tuple[str, bytes]] = None,
        inline_images: Optional[List[Tuple[bytes, str, str]]] = None,
        custom_headers: Optional[Dict[str, str]] = None,
        provider_type: str = "generic",
    ) -> Tuple[str, str, str]:
        """Build a bulk/newsletter email.

        Does NOT include a Date header — the send layer must prepend
        Date: at SMTP handoff time to avoid stale timestamps in queued batches.

        Returns (raw_message, envelope_from, verp_tag).
        """
        from_name = cls._sanitize(from_name)
        from_email = cls._sanitize(from_email)
        reply_to_name = cls._sanitize(reply_to_name)
        reply_to_email = cls._sanitize(reply_to_email)
        to_email = cls._sanitize(to_email)
        subject = cls._sanitize(subject)
        list_id_token = cls._sanitize(list_id_token)
        list_id_name = cls._sanitize(list_id_name)
        unsubscribe_url = cls._sanitize(unsubscribe_url)
        unsubscribe_mailto = cls._sanitize(unsubscribe_mailto)
        feedback_id = cls._sanitize(feedback_id)

        cls._validate_email(from_email, "From")
        cls._validate_email(to_email, "To")
        if reply_to_email:
            cls._validate_email(reply_to_email, "Reply-To")

        domain = from_email.split("@")[1]
        mid_domain = message_id_domain or domain

        if unsubscribe_url and not unsubscribe_url.startswith("https://"):
            raise ValueError("Unsubscribe URL must be HTTPS")

        if feedback_id and not _FEEDBACK_RE.match(feedback_id):
            raise ValueError(f"Invalid Feedback-ID format: {feedback_id!r}")

        if not _LIST_ID_RE.match(list_id_token):
            raise ValueError(f"Invalid list_id_token (use domain-style, no @ or spaces): {list_id_token!r}")

        if list_id_name:
            safe_name = list_id_name.replace("\\", "\\\\").replace('"', '\\"')
            list_id_str = f'"{safe_name}" <{list_id_token}>'
        else:
            list_id_str = f"<{list_id_token}>"

        msg_id = cls._message_id(mid_domain)
        # Pass the RAW display name to formataddr with charset — it does
        # the RFC 2047 encoding itself. Pre-encoding then re-passing makes
        # formataddr quote the encoded-word so receivers show it literally.
        from_header = cls._fold_addr("From", formataddr((from_name, from_email), charset="utf-8"))
        subject_enc = cls._fold_header("Subject", subject)
        entity_ref = str(uuid.uuid4())

        verp_tag = recipient_id or secrets.token_hex(8)
        is_ses = provider_type.lower() in ("ses", "aws", "amazon")

        if is_ses:
            envelope_from = from_email
        else:
            b_domain = bounce_domain or f"bounce.{domain}"
            envelope_from = f"bounce+{verp_tag}@{b_domain}"

        headers = [f"From: {from_header}"]
        if reply_to_email:
            reply_header = cls._fold_addr("Reply-To",
                formataddr((reply_to_name, reply_to_email), charset="utf-8"))
            headers.append(f"Reply-To: {reply_header}")

        if not is_ses:
            headers.append(f"Return-Path: <{envelope_from}>")

        headers += [
            f"To: {to_email}",
            f"Subject: {subject_enc}",
            f"Message-ID: {msg_id}",
            f"List-Id: {list_id_str}",
        ]

        unsub_parts = []
        if unsubscribe_url:
            unsub_parts.append(f"<{unsubscribe_url}>")
        if unsubscribe_mailto:
            unsub_parts.append(f"<mailto:{unsubscribe_mailto}>")
        if unsub_parts:
            headers.append(f"List-Unsubscribe: {', '.join(unsub_parts)}")
            if unsubscribe_url:
                headers.append("List-Unsubscribe-Post: List-Unsubscribe=One-Click")

        headers.append("Precedence: bulk")

        if is_ses:
            headers.append(f"X-SES-MESSAGE-TAGS: campaign={verp_tag}")

        if feedback_id:
            headers.append(f"Feedback-ID: {feedback_id}")

        headers.append(f"X-Entity-Ref-ID: {entity_ref}")

        if custom_headers:
            for k, v in custom_headers.items():
                if k.lower() in _RESERVED_HEADERS:
                    raise ValueError(f"Cannot override reserved header: {k}")
                headers.append(f"{cls._sanitize(k)}: {cls._sanitize(v)}")

        headers.append("MIME-Version: 1.0")

        has_inline = bool(inline_images)
        has_attach = bool(attachment)

        if has_attach and has_inline:
            body = cls._build_mixed_related(headers, html_body, plain_body, attachment, inline_images)
        elif has_attach:
            body = cls._build_mixed(headers, html_body, plain_body, attachment)
        elif has_inline:
            body = cls._build_related(headers, html_body, plain_body, inline_images)
        else:
            body = cls._build_alternative(headers, html_body, plain_body)

        return body, envelope_from, verp_tag

    @classmethod
    def _alt_part(cls, bnd: str, html: str, plain: str) -> list:
        plain_enc, plain_data = cls._get_encoding(plain)
        html_enc, html_data = cls._get_encoding(html, force_qp=True)
        return [
            f"--{bnd}",
            "Content-Type: text/plain; charset=utf-8",
            f"Content-Transfer-Encoding: {plain_enc}",
            "", plain_data, "",
            f"--{bnd}",
            "Content-Type: text/html; charset=utf-8",
            f"Content-Transfer-Encoding: {html_enc}",
            "", html_data, "",
            f"--{bnd}--",
        ]

    @classmethod
    def _inline_parts(cls, images: List[Tuple[bytes, str, str]]) -> list:
        lines = []
        for data, cid, mime_type in images:
            # cid and mime_type land in header lines — strip control chars
            # so a stray CR/LF can't inject headers.
            cid = cls._sanitize(cid)
            mime_type = cls._sanitize(mime_type) or "application/octet-stream"
            b64 = base64.b64encode(data).decode("ascii")
            b64_lines = "\r\n".join(b64[i:i+76] for i in range(0, len(b64), 76))
            lines += [
                f"Content-Type: {mime_type}",
                "Content-Transfer-Encoding: base64",
                f"Content-ID: <{cid}>",
                "Content-Disposition: inline",
                "", b64_lines, "",
            ]
        return lines

    @classmethod
    def _attach_part(cls, filename: str, data: bytes) -> list:
        filename = cls._sanitize(filename)
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            filename.encode("ascii")
            # Escape " and \ so a filename can't break out of the quoted
            # parameter and inject extra Content-Type/Disposition params.
            safe = cls._quote_param(filename)
            name_p = f'name="{safe}"'
            disp_p = f'filename="{safe}"'
        except UnicodeEncodeError:
            enc = encode_rfc2231(filename, "utf-8")
            name_p = f"name*={enc}"
            disp_p = f"filename*={enc}"
        b64 = base64.b64encode(data).decode("ascii")
        b64_lines = "\r\n".join(b64[i:i+76] for i in range(0, len(b64), 76))
        return [
            f"Content-Type: {mime_type}; {name_p}",
            f"Content-Disposition: attachment; {disp_p}",
            "Content-Transfer-Encoding: base64",
            "", b64_lines, "",
        ]

    @classmethod
    def _build_alternative(cls, headers: list, html: str, plain: str) -> str:
        h = list(headers)
        bnd = cls._boundary()
        h.append(f'Content-Type: multipart/alternative; boundary="{bnd}"')
        lines = h + ["", "This is a multi-part message in MIME format.", ""] + cls._alt_part(bnd, html, plain) + [""]
        return "\r\n".join(lines)

    @classmethod
    def _build_related(cls, headers: list, html: str, plain: str,
                       images: list) -> str:
        h = list(headers)
        rel = cls._boundary()
        alt = cls._boundary()
        h.append(f'Content-Type: multipart/related; boundary="{rel}"')
        lines = h + ["", "This is a multi-part message in MIME format.", ""]
        lines.append(f"--{rel}")
        lines.append(f'Content-Type: multipart/alternative; boundary="{alt}"')
        lines.append("")
        lines += cls._alt_part(alt, html, plain)
        lines.append("")
        for data, cid, mime in images:
            lines.append(f"--{rel}")
            lines += cls._inline_parts([(data, cid, mime)])
        lines.append(f"--{rel}--")
        lines.append("")
        return "\r\n".join(lines)

    @classmethod
    def _build_mixed(cls, headers: list, html: str, plain: str,
                     attachment: Tuple[str, bytes]) -> str:
        h = list(headers)
        mix = cls._boundary()
        alt = cls._boundary()
        h.append(f'Content-Type: multipart/mixed; boundary="{mix}"')
        lines = h + ["", "This is a multi-part message in MIME format.", ""]
        lines.append(f"--{mix}")
        lines.append(f'Content-Type: multipart/alternative; boundary="{alt}"')
        lines.append("")
        lines += cls._alt_part(alt, html, plain)
        lines.append("")
        lines.append(f"--{mix}")
        lines += cls._attach_part(attachment[0], attachment[1])
        lines.append(f"--{mix}--")
        lines.append("")
        return "\r\n".join(lines)

    @classmethod
    def _build_mixed_related(cls, headers: list, html: str, plain: str,
                              attachment: Tuple[str, bytes],
                              images: list) -> str:
        h = list(headers)
        mix = cls._boundary()
        rel = cls._boundary()
        alt = cls._boundary()
        h.append(f'Content-Type: multipart/mixed; boundary="{mix}"')
        lines = h + ["", "This is a multi-part message in MIME format.", ""]
        lines.append(f"--{mix}")
        lines.append(f'Content-Type: multipart/related; boundary="{rel}"')
        lines.append("")
        lines.append(f"--{rel}")
        lines.append(f'Content-Type: multipart/alternative; boundary="{alt}"')
        lines.append("")
        lines += cls._alt_part(alt, html, plain)
        lines.append("")
        for data, cid, mime in images:
            lines.append(f"--{rel}")
            lines += cls._inline_parts([(data, cid, mime)])
        lines.append(f"--{rel}--")
        lines.append("")
        lines.append(f"--{mix}")
        lines += cls._attach_part(attachment[0], attachment[1])
        lines.append(f"--{mix}--")
        lines.append("")
        return "\r\n".join(lines)
