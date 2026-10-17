"""DBL Watcher — Domain Blocklist Checks + Telegram-Alerts.

Fragt die klassischen domain-basierten Blocklisten per DNS ab:
  - Spamhaus DBL (public: *.dbl.spamhaus.org  |  DQS: *.<key>.dbl.dq.spamhaus.net)
  - SURBL         (*.multi.surbl.org)
  - URIBL         (*.multi.uribl.com)

Falls dnspython installiert ist, nutzen wir es (ermöglicht Custom-Resolver &
NXDOMAIN sauber zu unterscheiden). Sonst Fallback auf socket.gethostbyname.

Hinweis zu Spamhaus:
  Public-DNS-Queries werden von Spamhaus limitiert / blockiert (Rückgabe
  127.255.255.254). Für zuverlässige Resultate entweder:
    * eigenen rekursiven DNS-Resolver nutzen (nicht 8.8.8.8 / 1.1.1.1 etc.)
    * oder einen DQS-Key eintragen (kostenlose Tier: 100k Queries/Tag)
"""
from __future__ import annotations

import json
import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("bulk.dbl")

try:
    import dns.resolver  # type: ignore
    import dns.exception  # type: ignore
    _HAVE_DNSPY = True
except Exception:
    _HAVE_DNSPY = False


# ---------------------------------------------------------------------
# Spamhaus DBL return codes
#   https://www.spamhaus.org/zen/   (DBL = domain variant)
# 127.0.1.2   spam domain
# 127.0.1.4   phishing
# 127.0.1.5   malware
# 127.0.1.6   botnet C&C
# 127.0.1.102 abused legit — spam
# 127.0.1.103 abused legit — redirector
# 127.0.1.104 abused legit — phish
# 127.0.1.105 abused legit — malware
# 127.0.1.106 abused legit — botnet
# 127.0.1.255 query rejected (public DNS blocked / rate-limited / banned)
# ---------------------------------------------------------------------
DBL_CODES = {
    "127.0.1.2":   "spam domain",
    "127.0.1.4":   "phishing",
    "127.0.1.5":   "malware",
    "127.0.1.6":   "botnet C&C",
    "127.0.1.102": "abused legit (spam)",
    "127.0.1.103": "abused legit (redirector)",
    "127.0.1.104": "abused legit (phish)",
    "127.0.1.105": "abused legit (malware)",
    "127.0.1.106": "abused legit (botnet)",
    "127.0.1.255": "QUERY REFUSED (public DNS blocked)",
}
# SURBL — multi.surbl.org: Bitfield in 127.0.0.X
#   https://surbl.org/lists
SURBL_BITS = {2: "PH (phish)", 4: "MW (malware)", 8: "ABUSE", 16: "CR (cracked)", 64: "AB (abuse-but-ok)"}
# URIBL — multi.uribl.com
#   1 = BLACK, 2 = GREY, 4 = RED, 8 = DBL (abuse-contact), …
URIBL_BITS = {1: "BLACK", 2: "GREY", 4: "RED", 8: "DBL"}


@dataclass
class ListResult:
    name: str                      # "dbl" / "surbl" / "uribl"
    zone: str                      # z.B. "dbl.spamhaus.org"
    listed: bool = False
    codes: list[str] = field(default_factory=list)      # Rohe 127.0.x.y
    reasons: list[str] = field(default_factory=list)    # Menschlich lesbar
    error: str = ""                                     # DNS/Resolver-Fehler

    def to_dict(self):
        return {
            "name": self.name, "zone": self.zone,
            "listed": self.listed, "codes": self.codes,
            "reasons": self.reasons, "error": self.error,
        }


@dataclass
class DomainResult:
    domain: str
    checked_at: float
    lists: list[ListResult] = field(default_factory=list)

    @property
    def listed(self) -> bool:
        return any(l.listed for l in self.lists)

    @property
    def status(self) -> str:
        if any(l.error for l in self.lists) and not any(l.listed for l in self.lists):
            return "error"
        return "listed" if self.listed else "ok"

    def summary(self) -> str:
        parts = []
        for l in self.lists:
            if l.listed:
                r = ", ".join(l.reasons) if l.reasons else ", ".join(l.codes)
                parts.append(f"{l.name.upper()}: {r}")
            elif l.error:
                parts.append(f"{l.name.upper()}: ERR ({l.error})")
            else:
                parts.append(f"{l.name.upper()}: ok")
        return " | ".join(parts) if parts else "–"

    def detail_json(self) -> str:
        return json.dumps({l.name: l.to_dict() for l in self.lists},
                          ensure_ascii=False)


# ---------------------------------------------------------------------
# DNS helpers
# ---------------------------------------------------------------------
def _make_resolver(resolvers: list[str] | None, timeout: float = 4.0):
    """Baut einen dnspython-Resolver (falls verfügbar) oder None."""
    if not _HAVE_DNSPY:
        return None
    r = dns.resolver.Resolver(configure=True)
    if resolvers:
        r.nameservers = resolvers
    r.timeout = timeout
    r.lifetime = timeout
    return r


def _query_a(qname: str, resolver) -> tuple[list[str], str]:
    """Query A records for qname. Returns (ips, error_msg).
    error_msg leer bei success/NXDOMAIN; NXDOMAIN liefert ips=[]."""
    if resolver is not None:
        try:
            ans = resolver.resolve(qname, "A", search=False)
            return [r.address for r in ans], ""
        except dns.resolver.NXDOMAIN:
            return [], ""
        except dns.resolver.NoAnswer:
            return [], ""
        except dns.exception.Timeout:
            return [], "timeout"
        except Exception as e:
            return [], f"{type(e).__name__}: {e}"
    # Fallback: socket. Kann NXDOMAIN nicht sauber unterscheiden —
    # wir werten gaierror als "nicht gelistet".
    try:
        ips = socket.gethostbyname_ex(qname)[2]
        return ips, ""
    except socket.gaierror:
        return [], ""
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------
def check_domain(
    domain: str,
    *,
    check_dbl: bool = True,
    check_surbl: bool = False,
    check_uribl: bool = False,
    dqs_key: str = "",
    resolvers: list[str] | None = None,
    timeout: float = 4.0,
) -> DomainResult:
    """Prüft `domain` gegen die aktivierten Blocklists.

    `dqs_key` setzt die Spamhaus DBL-Zone auf `<key>.dbl.dq.spamhaus.net`.
    `resolvers` ist eine Liste IP-Adressen; leer → System-Default.
    """
    domain = (domain or "").strip().strip(".").lower()
    if not domain or "." not in domain:
        res = DomainResult(domain=domain, checked_at=time.time())
        res.lists.append(ListResult(name="input", zone="", error="invalid domain"))
        return res

    resolver = _make_resolver(resolvers, timeout=timeout)
    out = DomainResult(domain=domain, checked_at=time.time())

    if check_dbl:
        zone = f"{dqs_key}.dbl.dq.spamhaus.net" if dqs_key else "dbl.spamhaus.org"
        qname = f"{domain}.{zone}"
        ips, err = _query_a(qname, resolver)
        lr = ListResult(name="dbl", zone=zone)
        if err:
            lr.error = err
        elif ips:
            lr.listed = True
            lr.codes = ips
            for ip in ips:
                reason = DBL_CODES.get(ip, f"unknown code {ip}")
                lr.reasons.append(reason)
            # 127.0.1.255 ist KEIN echter Hit — Query-Rejection
            if lr.codes == ["127.0.1.255"]:
                lr.listed = False
                lr.error = "DBL query refused (use own resolver or DQS key)"
        out.lists.append(lr)

    if check_surbl:
        zone = "multi.surbl.org"
        qname = f"{domain}.{zone}"
        ips, err = _query_a(qname, resolver)
        lr = ListResult(name="surbl", zone=zone)
        if err:
            lr.error = err
        elif ips:
            lr.listed = True
            lr.codes = ips
            for ip in ips:
                try:
                    bit = int(ip.split(".")[-1])
                    hits = [n for b, n in SURBL_BITS.items() if bit & b]
                    lr.reasons.append(", ".join(hits) if hits else f"code {ip}")
                except Exception:
                    lr.reasons.append(f"code {ip}")
        out.lists.append(lr)

    if check_uribl:
        zone = "multi.uribl.com"
        qname = f"{domain}.{zone}"
        ips, err = _query_a(qname, resolver)
        lr = ListResult(name="uribl", zone=zone)
        if err:
            lr.error = err
        elif ips:
            lr.listed = True
            lr.codes = ips
            for ip in ips:
                try:
                    bit = int(ip.split(".")[-1])
                    hits = [n for b, n in URIBL_BITS.items() if bit & b]
                    lr.reasons.append(", ".join(hits) if hits else f"code {ip}")
                except Exception:
                    lr.reasons.append(f"code {ip}")
        out.lists.append(lr)

    return out


# ---------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------
def send_telegram(token: str, chat_id: str, text: str,
                  parse_mode: str = "HTML", timeout: float = 10.0) -> tuple[bool, str]:
    """Schickt eine Telegram-Nachricht. Rückgabe (ok, error_or_response)."""
    import requests
    token = (token or "").strip()
    chat_id = (chat_id or "").strip()
    if not token or not chat_id:
        return False, "token/chat_id leer"
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": "true",
        }, timeout=timeout)
        if resp.status_code == 200 and resp.json().get("ok"):
            return True, "ok"
        return False, f"http {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def format_alert(results: list[DomainResult], recovered: list[DomainResult] = None) -> str:
    """Erzeugt eine HTML-formatierte Telegram-Nachricht."""
    from html import escape as esc
    lines = []
    if results:
        lines.append("<b>🚨 DBL-Alert — Domains gelistet</b>")
        for r in results:
            lines.append(f"\n<b>{esc(r.domain)}</b>")
            for l in r.lists:
                if l.listed:
                    reasons = ", ".join(l.reasons) if l.reasons else ", ".join(l.codes)
                    lines.append(f"  • {l.name.upper()}: <code>{esc(reasons)}</code>")
                elif l.error:
                    lines.append(f"  • {l.name.upper()}: <i>err: {esc(l.error)}</i>")
    if recovered:
        if lines:
            lines.append("")
        lines.append("<b>✅ Wieder clean</b>")
        for r in recovered:
            lines.append(f"  • <code>{esc(r.domain)}</code>")
    return "\n".join(lines)
