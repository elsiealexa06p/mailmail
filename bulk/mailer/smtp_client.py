"""Bulk SMTP Client.

Handles connection reuse, SSL, SOCKS5 proxy, and VERP envelope-from.
"""
import smtplib
import ssl
import time
import threading
import logging
from typing import Optional

try:
    import socks
    HAS_SOCKS = True
except ImportError:
    HAS_SOCKS = False

logger = logging.getLogger("bulk.smtp")


def _parse_proxy(proxy_str: str) -> Optional[tuple]:
    if not proxy_str or not proxy_str.strip():
        return None
    p = proxy_str.strip().replace("socks5://", "").replace("socks://", "")
    user, pwd = "", ""
    if "@" in p:
        auth, p = p.rsplit("@", 1)
        if ":" in auth:
            user, pwd = auth.split(":", 1)
    parts = p.split(":")
    if len(parts) >= 4:
        return parts[0], int(parts[1]), parts[2], parts[3]
    if len(parts) >= 2:
        return parts[0], int(parts[1]), user, pwd
    return None


class SMTPClient:
    MAX_SENDS = 50
    MAX_AGE = 300.0

    def __init__(self, host: str, port: int, username: str, password: str,
                 timeout: int = 30, proxy: str = "",
                 proxy_required: bool = False,
                 send_mode: str = "smtp", region: str = "",
                 config_set: str = ""):
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._timeout = timeout
        self._proxy_str = proxy
        self._proxy = _parse_proxy(proxy)
        self._proxy_required = proxy_required
        self._send_mode = (send_mode or "smtp").lower()
        self._region = region or "eu-central-1"
        self._config_set = config_set or ""
        self._local = threading.local()

    @property
    def has_proxy(self) -> bool:
        return self._proxy is not None

    @property
    def is_ses_api(self) -> bool:
        return self._send_mode == "ses_api"

    def test_connection(self) -> tuple:
        """Test SMTP connection oder SES-API auth.
        Returns (success: bool, message: str)."""
        if self.is_ses_api:
            from mailer.ses_api import ses_ping, SESAPIError
            try:
                info = ses_ping(self._username, self._password, self._region)
                quota = info.get("SendQuota") or {}
                max_24h = quota.get("Max24HourSend", "?")
                rate = quota.get("MaxSendRate", "?")
                sent_24h = quota.get("SentLast24Hours", "?")
                prod = info.get("ProductionAccessEnabled", False)
                sandbox = "" if prod else " (Sandbox)"
                if isinstance(max_24h, (int, float)):
                    return True, (f"SES {self._region} OK: {sent_24h:g}/{max_24h:g}"
                                   f"·24h, {rate:g}/s{sandbox}")
                return True, f"SES {self._region} OK{sandbox}"
            except SESAPIError as e:
                return False, str(e)
            except Exception as e:
                return False, str(e)

        try:
            server = self._connect()
            server.quit()
            proxy_info = f" via proxy {self._proxy[0]}:{self._proxy[1]}" if self._proxy else ""
            return True, f"Connected to {self._host}:{self._port}{proxy_info}"
        except Exception as exc:
            return False, str(exc)

    def test_proxy(self) -> tuple:
        """Test proxy connectivity only (without SMTP auth).
        Returns (success: bool, message: str)."""
        if not self._proxy:
            return False, "No proxy configured"
        if not HAS_SOCKS:
            return False, "PySocks not installed (pip install pysocks)"
        try:
            s = self._make_proxy_socket()
            if s:
                s.close()
                return True, f"Proxy {self._proxy[0]}:{self._proxy[1]} reachable"
        except Exception as exc:
            return False, f"Proxy error: {exc}"
        return False, "Proxy connection failed"

    def send(self, envelope_from: str, to_email: str, raw_message: str) -> tuple:
        if self.is_ses_api:
            return self._send_via_ses(envelope_from, to_email, raw_message)

        if self._proxy_required and self._proxy:
            try:
                s = self._make_proxy_socket()
                if s:
                    s.close()
            except Exception as exc:
                logger.error("PROXY DOWN (killswitch): %s", exc)
                return False, f"PROXY_DOWN: {exc}", 0
        server = self._get_conn()
        try:
            if server is None:
                server = self._connect()
                self._store(server)

            server.sendmail(envelope_from, [to_email], raw_message)
            self._bump()
            return True, "", 0

        except smtplib.SMTPAuthenticationError as exc:
            self._drop()
            logger.error("AUTH FAIL %s: %s", self._host, exc)
            return False, str(exc), exc.smtp_code

        except smtplib.SMTPRecipientsRefused as exc:
            codes = [c for c, _ in exc.recipients.values()]
            code = codes[0] if codes else 0
            detail = str(exc.recipients)
            if all(c >= 500 for c in codes):
                return False, f"FATAL:{detail}", code
            self._drop()
            return False, f"TRANSIENT:{detail}", code

        except smtplib.SMTPResponseException as exc:
            self._drop()
            return False, str(exc.smtp_error), exc.smtp_code

        except (ssl.SSLError, OSError) as exc:
            self._drop()
            logger.error("CONN %s: %s", self._host, exc)
            return False, str(exc), 0

    def _make_proxy_socket(self):
        if not self._proxy or not HAS_SOCKS:
            return None
        host, port, user, pwd = self._proxy
        s = socks.socksocket()
        s.set_proxy(socks.SOCKS5, host, port,
                    username=user or None, password=pwd or None)
        s.settimeout(self._timeout)
        s.connect((self._host, self._port))
        return s

    def _connect(self) -> smtplib.SMTP:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")

        proxy_sock = self._make_proxy_socket()
        ehlo_name = self._username.split("@")[1] if "@" in self._username else self._host

        if self._port == 465:
            if proxy_sock:
                wrapped = ctx.wrap_socket(proxy_sock, server_hostname=self._host)
                server = smtplib.SMTP_SSL(context=ctx)
                server.sock = wrapped
                server._host = self._host
                server.file = server.sock.makefile("rb")
                server.getreply()
            else:
                server = smtplib.SMTP_SSL(self._host, self._port,
                                           timeout=self._timeout, context=ctx)
            server.ehlo(ehlo_name)
        else:
            if proxy_sock:
                server = smtplib.SMTP()
                server.sock = proxy_sock
                server._host = self._host
                server.file = server.sock.makefile("rb")
                server.getreply()
            else:
                server = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
            server.ehlo(ehlo_name)
            if server.has_extn("starttls"):
                server.starttls(context=ctx)
                server.ehlo(ehlo_name)

        server.login(self._username, self._password)
        return server

    def _get_conn(self) -> Optional[smtplib.SMTP]:
        lc = self._local
        if not hasattr(lc, "server") or lc.server is None:
            return None
        if lc.sends >= self.MAX_SENDS:
            self._drop()
            return None
        if time.monotonic() - lc.conn_time > self.MAX_AGE:
            self._drop()
            return None
        try:
            code, _ = lc.server.noop()
            if code != 250:
                self._drop()
                return None
            return lc.server
        except Exception:
            self._drop()
            return None

    def _store(self, server):
        lc = self._local
        lc.server = server
        lc.sends = 0
        lc.conn_time = time.monotonic()

    def _bump(self):
        if hasattr(self._local, "sends"):
            self._local.sends += 1

    def _drop(self):
        lc = self._local
        if hasattr(lc, "server") and lc.server:
            try:
                lc.server.quit()
            except Exception:
                try:
                    lc.server.close()
                except Exception:
                    pass
            lc.server = None

    def _send_via_ses(self, envelope_from: str, to_email: str,
                       raw_message) -> tuple:
        """Zerlegt das schon gebaute Raw-MIME in Simple-Felder und schickt
        via SES v2 SendEmail. Attachments + CID-Inline gehen durch.
        Rückgabe wie SMTP.send: (ok, err_or_msg, code)."""
        from email import message_from_string, message_from_bytes
        from mailer.ses_api import (
            ses_send_simple, SESAPIError, classify_ses_error,
        )
        from mailer.smtp_worker import (
            _parse_from_header, _extract_html_text, _extract_ses_attachments,
        )

        if isinstance(raw_message, bytes):
            parsed = message_from_bytes(raw_message)
        else:
            parsed = message_from_string(raw_message)

        subject = parsed.get("Subject", "") or ""
        from_hdr = parsed.get("From", envelope_from) or envelope_from
        display_name, from_email = _parse_from_header(from_hdr,
                                                      fallback=envelope_from)
        reply_to = parsed.get("Reply-To", "") or ""

        html_body, plain_body = _extract_html_text(parsed)
        ses_attachments = _extract_ses_attachments(parsed)

        # Whitelisted Custom-Header aus dem MIME übernehmen
        allowed_prefixes = ("x-",)
        allowed_exact = {
            "list-unsubscribe", "list-unsubscribe-post", "list-help",
            "list-id", "list-owner", "list-archive", "list-subscribe",
            "message-id",
        }
        extra_headers = []
        seen = set()
        for hname, hval in parsed.items():
            low = hname.lower()
            if low in seen:
                continue
            if low in allowed_exact or low.startswith(allowed_prefixes):
                extra_headers.append({"Name": hname, "Value": str(hval)})
                seen.add(low)

        try:
            ses_send_simple(
                iam_key=self._username,
                iam_secret=self._password,
                region=self._region,
                from_addr=from_email,
                from_name=display_name,
                to_addr=to_email,
                subject=subject,
                html_body=html_body,
                plain_body=plain_body,
                extra_headers=extra_headers or None,
                reply_to=reply_to,
                configuration_set=self._config_set or "",
                attachments=ses_attachments or None,
            )
            return True, "", 0
        except SESAPIError as e:
            cat = classify_ses_error(e)
            if cat == "auth":
                logger.error("SES AUTH FAIL %s: %s", self._region, e)
                return False, str(e), e.status or 535
            if cat == "hard":
                return False, f"FATAL:{e}", e.status or 550
            # transient
            return False, f"TRANSIENT:{e}", e.status or 421
