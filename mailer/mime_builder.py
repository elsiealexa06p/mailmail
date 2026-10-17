import mimetypes
import re
import secrets
import time
import base64
from email import quoprimime
from email.utils import formatdate, formataddr, encode_rfc2231
from email.header import Header
from typing import Optional, Tuple, List

# All C0 control chars (0x00-0x1F) + DEL (0x7F) are forbidden in header
# values — strip them to close header-injection / truncation vectors.
_HEADER_FORBIDDEN = dict.fromkeys(list(range(0x00, 0x20)) + [0x7F])
_EOL_RE = re.compile(r"\r\n|\r|\n")


class MIMEBuilder:

    @staticmethod
    def _sanitize(value: str) -> str:
        # Strip CR, LF, NUL, TAB, and all other C0 controls + DEL.
        return value.translate(_HEADER_FORBIDDEN).strip()

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
        normalized = MIMEBuilder._to_crlf(text)
        raw = normalized.encode("utf-8").decode("latin-1")
        return quoprimime.body_encode(raw, maxlinelen=76, eol="\r\n")

    @staticmethod
    def _fold_header(name: str, value: str) -> str:
        # Fold long header values on FWS; ASCII stays readable, non-ASCII
        # becomes folded encoded-words. Accounts for the "Name: " width.
        try:
            value.encode("ascii")
            h = Header(value, "ascii", maxlinelen=78, header_name=name)
        except UnicodeEncodeError:
            h = Header(value, "utf-8", maxlinelen=78, header_name=name)
        # Header.encode() folds with bare LF — normalize to CRLF so the
        # assembled message is uniformly CRLF per RFC 5322 §2.1.
        return MIMEBuilder._to_crlf(h.encode(splitchars=" ;,"))

    @staticmethod
    def _fold_addr(name: str, formatted: str) -> str:
        # Fold an already-formatted address header (output of formataddr).
        # Header folds on FWS inside the display name and leaves the
        # angle-addr intact; an existing encoded-word is not re-encoded.
        return MIMEBuilder._to_crlf(
            Header(formatted, header_name=name, maxlinelen=78).encode(splitchars=" "))

    @staticmethod
    def _quote_param(filename: str) -> str:
        # RFC 2045/2183 quoted-string: escape backslash then quote so a
        # filename containing " or \ can't break out of the parameter.
        return filename.replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def generate_message_id(sender_domain: str) -> str:
        if not sender_domain or "." not in sender_domain:
            raise ValueError(f"Invalid sender domain for Message-ID: {sender_domain!r}")
        ts_hex = format(int(time.time() * 1000), "x")
        rand_hex = secrets.token_hex(16)
        return f"<{ts_hex}.{rand_hex}@{sender_domain}>"

    @staticmethod
    def generate_boundary() -> str:
        ts = int(time.time() * 1000)
        rand_hex = secrets.token_hex(8)
        return f"----=_Part_{ts}_{rand_hex}"

    @classmethod
    def _get_best_encoding(cls, text: str) -> tuple:
        # 7bit is only legal if ASCII AND every line <= 998 octets
        # (RFC 5321 §4.5.3.1). Otherwise quoted-printable with proper
        # 76-char soft wrapping, which satisfies the 998 limit too.
        normalized = cls._to_crlf(text)
        try:
            normalized.encode("ascii")
            if cls._max_line_len(normalized) <= 998:
                return "7bit", normalized
        except UnicodeEncodeError:
            pass
        return "quoted-printable", cls._qp_encode(text)

    @classmethod
    def build_email(
        cls,
        from_name: str,
        from_email: str,
        to_email: str,
        subject: str,
        html_body: str,
        plain_body: str,
        attachment: Optional[Tuple[str, bytes]] = None,
        inline_images: Optional[List[Tuple[bytes, str, str]]] = None,
    ) -> str:
        from_name = cls._sanitize(from_name)
        from_email = cls._sanitize(from_email)
        to_email = cls._sanitize(to_email)
        subject = cls._sanitize(subject)

        sender_domain = from_email.split("@")[1] if "@" in from_email else ""
        if not sender_domain or "." not in sender_domain:
            raise ValueError(f"Cannot extract valid domain from From address: {from_email!r}")
        if "@" not in to_email or "." not in to_email.split("@")[-1]:
            raise ValueError(f"Invalid To address: {to_email!r}")

        message_id = cls.generate_message_id(sender_domain)
        date_str = formatdate(localtime=True)
        # Pass the RAW display name to formataddr with charset — it does
        # the RFC 2047 encoding itself. Pre-encoding then re-passing makes
        # formataddr quote the encoded-word so receivers show it literally.
        from_header = cls._fold_addr("From", formataddr((from_name, from_email), charset="utf-8"))
        subject_encoded = cls._fold_header("Subject", subject)

        has_inline = bool(inline_images)
        has_attach = bool(attachment)

        if has_attach and has_inline:
            return cls._build_mixed_related(
                date_str, from_header, to_email, message_id,
                subject_encoded, html_body, plain_body, attachment, inline_images,
            )
        if has_attach:
            return cls._build_mixed(
                date_str, from_header, to_email, message_id,
                subject_encoded, html_body, plain_body, attachment,
            )
        if has_inline:
            return cls._build_related(
                date_str, from_header, to_email, message_id,
                subject_encoded, html_body, plain_body, inline_images,
            )
        return cls._build_alternative(
            date_str, from_header, to_email, message_id,
            subject_encoded, html_body, plain_body,
        )

    @classmethod
    def _header_block(cls, date_str, from_header, to_email,
                      message_id, subject, content_type) -> list:
        return [
            f"Date: {date_str}",
            f"From: {from_header}",
            f"To: {to_email}",
            f"Message-ID: {message_id}",
            f"Subject: {subject}",
            "Auto-Submitted: auto-generated",
            "MIME-Version: 1.0",
            f"Content-Type: {content_type}",
        ]

    @classmethod
    def _alt_part(cls, boundary, html_body, plain_body) -> list:
        plain_enc, plain_data = cls._get_best_encoding(plain_body)
        html_enc, html_data = cls._get_best_encoding(html_body)
        return [
            f"--{boundary}",
            "Content-Type: text/plain; charset=utf-8",
            f"Content-Transfer-Encoding: {plain_enc}",
            "", plain_data, "",
            f"--{boundary}",
            "Content-Type: text/html; charset=utf-8",
            f"Content-Transfer-Encoding: {html_enc}",
            "", html_data, "",
            f"--{boundary}--",
        ]

    @classmethod
    def _inline_parts(cls, inline_images) -> list:
        lines = []
        for img_data, cid, mime_type in inline_images:
            # cid and mime_type are interpolated into header lines — strip
            # control chars so a stray CR/LF can't inject headers.
            cid = cls._sanitize(cid)
            mime_type = cls._sanitize(mime_type) or "application/octet-stream"
            b64 = base64.b64encode(img_data).decode("ascii")
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
    def _attach_part(cls, att_filename, att_data) -> list:
        mime_type = mimetypes.guess_type(att_filename)[0] or "application/octet-stream"
        try:
            att_filename.encode("ascii")
            # Escape " and \ so a filename can't break out of the quoted
            # parameter and inject extra Content-Type/Disposition params.
            safe = cls._quote_param(att_filename)
            name_p = f'name="{safe}"'
            disp_p = f'filename="{safe}"'
        except UnicodeEncodeError:
            enc = encode_rfc2231(att_filename, "utf-8")
            name_p = f"name*={enc}"
            disp_p = f"filename*={enc}"
        b64 = base64.b64encode(att_data).decode("ascii")
        b64_lines = "\r\n".join(b64[i:i+76] for i in range(0, len(b64), 76))
        return [
            f"Content-Type: {mime_type}; {name_p}",
            f"Content-Disposition: attachment; {disp_p}",
            "Content-Transfer-Encoding: base64",
            "", b64_lines, "",
        ]

    @classmethod
    def _build_alternative(cls, date_str, from_header, to_email,
                           message_id, subject, html_body, plain_body) -> str:
        bnd = cls.generate_boundary()
        headers = cls._header_block(date_str, from_header, to_email, message_id,
                                     subject, f'multipart/alternative; boundary="{bnd}"')
        lines = headers + ["", "This is a multi-part message in MIME format.", ""]
        lines += cls._alt_part(bnd, html_body, plain_body)
        lines.append("")
        return "\r\n".join(lines)

    @classmethod
    def _build_related(cls, date_str, from_header, to_email,
                       message_id, subject, html_body, plain_body, inline_images) -> str:
        rel_bnd = cls.generate_boundary()
        alt_bnd = cls.generate_boundary()
        headers = cls._header_block(date_str, from_header, to_email, message_id,
                                     subject, f'multipart/related; boundary="{rel_bnd}"')
        lines = headers + ["", "This is a multi-part message in MIME format.", ""]
        lines.append(f"--{rel_bnd}")
        lines.append(f'Content-Type: multipart/alternative; boundary="{alt_bnd}"')
        lines.append("")
        lines += cls._alt_part(alt_bnd, html_body, plain_body)
        lines.append("")
        for img_data, cid, mime_type in inline_images:
            lines.append(f"--{rel_bnd}")
            lines += cls._inline_parts([(img_data, cid, mime_type)])
        lines.append(f"--{rel_bnd}--")
        lines.append("")
        return "\r\n".join(lines)

    @classmethod
    def _build_mixed(cls, date_str, from_header, to_email,
                     message_id, subject, html_body, plain_body, attachment) -> str:
        mix_bnd = cls.generate_boundary()
        alt_bnd = cls.generate_boundary()
        att_fn, att_data = attachment
        att_fn = cls._sanitize(att_fn)
        headers = cls._header_block(date_str, from_header, to_email, message_id,
                                     subject, f'multipart/mixed; boundary="{mix_bnd}"')
        lines = headers + ["", "This is a multi-part message in MIME format.", ""]
        lines.append(f"--{mix_bnd}")
        lines.append(f'Content-Type: multipart/alternative; boundary="{alt_bnd}"')
        lines.append("")
        lines += cls._alt_part(alt_bnd, html_body, plain_body)
        lines.append("")
        lines.append(f"--{mix_bnd}")
        lines += cls._attach_part(att_fn, att_data)
        lines.append(f"--{mix_bnd}--")
        lines.append("")
        return "\r\n".join(lines)

    @classmethod
    def _build_mixed_related(cls, date_str, from_header, to_email,
                             message_id, subject, html_body, plain_body,
                             attachment, inline_images) -> str:
        mix_bnd = cls.generate_boundary()
        rel_bnd = cls.generate_boundary()
        alt_bnd = cls.generate_boundary()
        att_fn, att_data = attachment
        att_fn = cls._sanitize(att_fn)
        headers = cls._header_block(date_str, from_header, to_email, message_id,
                                     subject, f'multipart/mixed; boundary="{mix_bnd}"')
        lines = headers + ["", "This is a multi-part message in MIME format.", ""]
        lines.append(f"--{mix_bnd}")
        lines.append(f'Content-Type: multipart/related; boundary="{rel_bnd}"')
        lines.append("")
        lines.append(f"--{rel_bnd}")
        lines.append(f'Content-Type: multipart/alternative; boundary="{alt_bnd}"')
        lines.append("")
        lines += cls._alt_part(alt_bnd, html_body, plain_body)
        lines.append("")
        for img_data, cid, mime_type in inline_images:
            lines.append(f"--{rel_bnd}")
            lines += cls._inline_parts([(img_data, cid, mime_type)])
        lines.append(f"--{rel_bnd}--")
        lines.append("")
        lines.append(f"--{mix_bnd}")
        lines += cls._attach_part(att_fn, att_data)
        lines.append(f"--{mix_bnd}--")
        lines.append("")
        return "\r\n".join(lines)
