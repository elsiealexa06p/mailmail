"""Warmup IMAP Worker — executes engagement actions on seed accounts."""
import imaplib
import email
import ssl
import re
import time
import random
import logging
import smtplib
from email.mime.text import MIMEText
from typing import Optional

from .warmup_providers import get_provider_config

logger = logging.getLogger("bulk.warmup.imap")

try:
    import socks
    HAS_SOCKS = True
except ImportError:
    HAS_SOCKS = False

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


def _parse_proxy(proxy_str: str):
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


def _make_proxy_socket(proxy_str: str, target_host: str, target_port: int):
    """Create a SOCKS5-connected socket."""
    if not HAS_SOCKS:
        return None
    proxy = _parse_proxy(proxy_str)
    if not proxy:
        return None
    host, port, user, pwd = proxy
    s = socks.socksocket()
    s.set_proxy(socks.SOCKS5, host, port,
                username=user or None, password=pwd or None)
    s.settimeout(30)
    s.connect((target_host, target_port))
    return s


class IMAPWorker:
    """Performs IMAP engagement actions on a single seed account."""

    def __init__(self, email_addr: str, password: str,
                 imap_host: str, imap_port: int = 993,
                 smtp_host: str = "", smtp_port: int = 587,
                 proxy: str = "", provider: str = "",
                 user_agent: str = "",
                 llm_config: dict = None):
        self._email = email_addr
        self._password = password
        self._imap_host = imap_host
        self._imap_port = imap_port
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._proxy = proxy
        self._provider = provider
        self._user_agent = user_agent
        self._prov_cfg = get_provider_config(provider)
        self._llm = llm_config or {}
        self._imap = None

    def connect(self) -> bool:
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            proxy_sock = _make_proxy_socket(self._proxy, self._imap_host, self._imap_port)
            if proxy_sock:
                self._imap = imaplib.IMAP4_SSL(host=self._imap_host, ssl_context=ctx)
                self._imap.sock = ctx.wrap_socket(proxy_sock, server_hostname=self._imap_host)
                self._imap.file = self._imap.makefile("rb")
                self._imap.readline()
            else:
                self._imap = imaplib.IMAP4_SSL(self._imap_host, self._imap_port, ssl_context=ctx)

            self._imap.login(self._email, self._password)
            logger.info("IMAP connected: %s (%s)", self._email, self._provider)
            return True
        except Exception as e:
            logger.error("IMAP connect failed %s: %s", self._email, e)
            return False

    def disconnect(self):
        if self._imap:
            try:
                self._imap.logout()
            except Exception:
                pass
            self._imap = None

    def mark_read(self, from_filter: str = "") -> str:
        """Mark matching unread messages as read."""
        try:
            self._imap.select("INBOX")
            criteria = "(UNSEEN)"
            if from_filter:
                criteria = f'(UNSEEN FROM "{from_filter}")'
            _, data = self._imap.search(None, criteria)
            msg_ids = data[0].split()
            if not msg_ids:
                return "no_unread"
            target = msg_ids[-1]
            self._imap.store(target, "+FLAGS", "\\Seen")
            return f"marked_read:{target.decode()}"
        except Exception as e:
            return f"error:{e}"

    def rescue_from_spam(self, from_filter: str = "") -> str:
        """Move messages from spam folder to inbox."""
        spam_folder = self._prov_cfg.get("spam_folder", "Spam")
        try:
            status, _ = self._imap.select(spam_folder)
            if status != "OK":
                return "spam_folder_not_found"
            criteria = "ALL"
            if from_filter:
                criteria = f'(FROM "{from_filter}")'
            _, data = self._imap.search(None, criteria)
            msg_ids = data[0].split()
            if not msg_ids:
                return "not_in_spam"
            for mid in msg_ids:
                self._imap.copy(mid, "INBOX")
                self._imap.store(mid, "+FLAGS", "\\Deleted")
            self._imap.expunge()
            return f"rescued:{len(msg_ids)}"
        except Exception as e:
            return f"error:{e}"

    def flag_important(self, from_filter: str = "") -> str:
        """Flag message as important/starred."""
        try:
            self._imap.select("INBOX")
            criteria = "(UNSEEN)" if not from_filter else f'(FROM "{from_filter}")'
            _, data = self._imap.search(None, criteria)
            msg_ids = data[0].split()
            if not msg_ids:
                return "no_messages"
            target = msg_ids[-1]
            self._imap.store(target, "+FLAGS", "\\Flagged")
            return f"flagged:{target.decode()}"
        except Exception as e:
            return f"error:{e}"

    def click_links(self, from_filter: str = "", max_links: int = 2) -> str:
        """Find tracking links in messages and fetch them."""
        if not HAS_REQUESTS:
            return "no_requests_lib"
        try:
            self._imap.select("INBOX")
            criteria = f'(FROM "{from_filter}")' if from_filter else "ALL"
            _, data = self._imap.search(None, criteria)
            msg_ids = data[0].split()
            if not msg_ids:
                return "no_messages"

            _, msg_data = self._imap.fetch(msg_ids[-1], "(RFC822)")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            html_body = ""
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    html_body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                    break

            urls = re.findall(r'href="(https?://[^"]+)"', html_body)
            urls = [u for u in urls if "unsubscribe" not in u.lower()][:max_links]
            if not urls:
                return "no_links"

            clicked = 0
            headers = {"User-Agent": self._user_agent or
                       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                       "Accept": "text/html", "Accept-Language": "de-DE,de;q=0.9"}
            proxies = None
            if self._proxy:
                proxy = _parse_proxy(self._proxy)
                if proxy:
                    h, p, u, pw = proxy
                    auth = f"{u}:{pw}@" if u else ""
                    proxies = {"https": f"socks5://{auth}{h}:{p}",
                               "http": f"socks5://{auth}{h}:{p}"}

            for url in urls:
                try:
                    _requests.get(url, headers=headers, proxies=proxies,
                                  timeout=10, allow_redirects=True)
                    clicked += 1
                except Exception:
                    pass

            return f"clicked:{clicked}/{len(urls)}"
        except Exception as e:
            return f"error:{e}"

    def send_reply(self, from_filter: str = "") -> str:
        """Send a short reply to the most recent matching message."""
        if not self._smtp_host:
            return "no_smtp"
        try:
            self._imap.select("INBOX")
            criteria = f'(FROM "{from_filter}")' if from_filter else "ALL"
            _, data = self._imap.search(None, criteria)
            msg_ids = data[0].split()
            if not msg_ids:
                return "no_messages"

            _, msg_data = self._imap.fetch(msg_ids[-1], "(RFC822)")
            raw = msg_data[0][1]
            orig = email.message_from_bytes(raw)

            reply_to = orig.get("Reply-To") or orig.get("From", "")
            subject = orig.get("Subject", "")
            if not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"

            from .warmup_ai import generate_reply, _fallback_reply
            if self._llm.get("api_key"):
                body_text = generate_reply(
                    self._llm.get("api_url", ""), self._llm["api_key"],
                    self._llm.get("model", ""), subject,
                    language=self._llm.get("language", "de"))
            else:
                body_text = _fallback_reply(self._llm.get("language", "de"))
            reply = MIMEText(body_text, "plain", "utf-8")
            reply["From"] = self._email
            reply["To"] = reply_to
            reply["Subject"] = subject
            if orig.get("Message-ID"):
                reply["In-Reply-To"] = orig["Message-ID"]
                reply["References"] = orig["Message-ID"]

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            if self._smtp_port == 465:
                server = smtplib.SMTP_SSL(self._smtp_host, self._smtp_port,
                                           context=ctx, timeout=30)
            else:
                server = smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=30)
                server.ehlo()
                if server.has_extn("starttls"):
                    server.starttls(context=ctx)
                    server.ehlo()

            server.login(self._email, self._password)
            server.send_message(reply)
            server.quit()
            return f"replied_to:{reply_to}"
        except Exception as e:
            return f"error:{e}"

    def check_inbox(self, from_filter: str = "") -> dict:
        """Check inbox status: total messages, unread, in spam."""
        result = {"inbox_total": 0, "inbox_unread": 0, "in_spam": 0}
        try:
            self._imap.select("INBOX")
            _, data = self._imap.search(None, "ALL")
            result["inbox_total"] = len(data[0].split()) if data[0] else 0
            if from_filter:
                _, data = self._imap.search(None, f'(UNSEEN FROM "{from_filter}")')
                result["inbox_unread"] = len(data[0].split()) if data[0] else 0

            spam_folder = self._prov_cfg.get("spam_folder", "Spam")
            status, _ = self._imap.select(spam_folder)
            if status == "OK" and from_filter:
                _, data = self._imap.search(None, f'(FROM "{from_filter}")')
                result["in_spam"] = len(data[0].split()) if data[0] else 0
        except Exception:
            pass
        return result
