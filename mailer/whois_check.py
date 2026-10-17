"""Lightweight WHOIS / RDAP availability check.

Used as a sanity-fallback when Dynadot's `search` returns Available=no but
we suspect that's wrong (notably .de domains in DENIC-Hold). Tries RDAP
(HTTPS, JSON) first because TCP/43 WHOIS is often firewalled or rate
limited — falls back to WHOIS on TCP/43 if RDAP isn't available.

Returns one of: "available" | "taken" | "unknown".
"""
import socket
import logging

logger = logging.getLogger("mailer.whois")

# RDAP endpoints per TLD. Most use https://rdap.{registry}/domain/{name}.
# 404 -> available, 200 -> taken, anything else -> unknown.
_RDAP_SERVERS = {
    "de":   "https://rdap.denic.de/domain/{}",
    "com":  "https://rdap.verisign.com/com/v1/domain/{}",
    "net":  "https://rdap.verisign.com/net/v1/domain/{}",
    "org":  "https://rdap.publicinterestregistry.org/rdap/domain/{}",
    "info": "https://rdap.identitydigital.services/rdap/domain/{}",
    "biz":  "https://rdap.nic.biz/domain/{}",
    "io":   "https://rdap.identitydigital.services/rdap/domain/{}",
    "co":   "https://rdap.nic.co/domain/{}",
    "me":   "https://rdap.nic.me/domain/{}",
    "xyz":  "https://rdap.centralnic.com/xyz/domain/{}",
    "shop": "https://rdap.nic.shop/domain/{}",
    "store":"https://rdap.centralnic.com/store/domain/{}",
    "online":"https://rdap.centralnic.com/online/domain/{}",
    "site": "https://rdap.centralnic.com/site/domain/{}",
    "eu":   "https://rdap.eu.org/domain/{}",
    "at":   "https://rdap.nic.at/domain/{}",
    "ch":   "https://rdap.nic.ch/domain/{}",
    "nl":   "https://rdap.dns.nl/domain/{}",
    "fr":   "https://rdap.nic.fr/domain/{}",
    "uk":   "https://rdap.nominet.uk/uk/domain/{}",
    "us":   "https://rdap.nic.us/domain/{}",
    "ca":   "https://rdap.cira.ca/rdap/domain/{}",
    "se":   "https://rdap.iis.se/domain/{}",
}


def _rdap_check(domain: str, timeout: float = 8.0) -> tuple:
    """Returns (verdict, raw_text) via RDAP HTTPS."""
    parts = domain.lower().rsplit(".", 1)
    if len(parts) != 2:
        return "unknown", ""
    tld = parts[1]
    template = _RDAP_SERVERS.get(tld)
    if not template:
        return "unknown", ""
    url = template.format(domain.lower())
    try:
        import requests
        r = requests.get(url, timeout=timeout, headers={
            "Accept": "application/rdap+json",
            "User-Agent": "Mozilla/5.0 RDAP-Check",
        })
    except Exception as e:
        logger.warning("RDAP %s failed: %s", url, e)
        return "unknown", ""

    if r.status_code == 404:
        return "available", f"RDAP 404 ({url})"
    if r.status_code == 200:
        return "taken", r.text[:2000]
    if r.status_code in (400, 403, 422):
        # Some registries return 400 for non-existent domains
        body = r.text.lower()
        if "not found" in body or "does not exist" in body or "no such" in body:
            return "available", r.text[:2000]
    return "unknown", f"RDAP HTTP {r.status_code} ({url})\n{r.text[:500]}"

_WHOIS_SERVERS = {
    "de":   "whois.denic.de",
    "com":  "whois.verisign-grs.com",
    "net":  "whois.verisign-grs.com",
    "org":  "whois.publicinterestregistry.org",
    "info": "whois.afilias.net",
    "biz":  "whois.nic.biz",
    "eu":   "whois.eu",
    "io":   "whois.nic.io",
    "co":   "whois.nic.co",
    "me":   "whois.nic.me",
    "xyz":  "whois.nic.xyz",
    "shop": "whois.nic.shop",
    "store":"whois.nic.store",
    "online":"whois.nic.online",
    "site": "whois.nic.site",
    "ch":   "whois.nic.ch",
    "at":   "whois.nic.at",
    "nl":   "whois.domain-registry.nl",
    "fr":   "whois.nic.fr",
    "es":   "whois.nic.es",
    "it":   "whois.nic.it",
    "uk":   "whois.nic.uk",
    "us":   "whois.nic.us",
    "ca":   "whois.cira.ca",
    "pl":   "whois.dns.pl",
    "se":   "whois.iis.se",
}

_FREE_MARKERS = (
    "status: free",            # DENIC
    "no match",
    "no entries found",
    "not found",
    "no data found",
    "no object found",
    "domain not found",
    "no information was found",
    "available",
    "this query returned 0 objects",
    "free",
    "the queried object does not exist",
    "%error:103",              # .at NIC
    "no such domain",
)

_TAKEN_MARKERS = (
    "status: connect",         # DENIC = registered
    "registrant",
    "registry expiry date",
    "creation date:",
    "created on:",
    "domain status",
    "nserver:",
    "name server:",
    "registrar:",
    "sponsoring registrar",
    "registrant contact",
    "tech-c:",
    "admin-c:",
)


def _server_for(domain: str) -> str:
    parts = domain.lower().rsplit(".", 2)
    if len(parts) >= 2:
        last = parts[-1]
        if last in _WHOIS_SERVERS:
            return _WHOIS_SERVERS[last]
        if len(parts) == 3:
            two = ".".join(parts[-2:])
            if two in _WHOIS_SERVERS:
                return _WHOIS_SERVERS[two]
    return ""


def check(domain: str, timeout: float = 6.0) -> tuple:
    """Return (verdict, raw_text). verdict in {available, taken, unknown}.
    Tries RDAP over HTTPS first (faster, not blocked by firewalls),
    falls back to WHOIS/43 if RDAP can't decide."""
    domain = domain.strip().lower()
    verdict, raw = _rdap_check(domain, timeout=timeout)
    if verdict in ("available", "taken"):
        return verdict, raw
    rdap_raw = raw

    server = _server_for(domain)
    if not server:
        return verdict, rdap_raw

    query = (domain + "\r\n").encode()
    if server == "whois.denic.de":
        # DENIC requires -T dn for domain queries
        query = (f"-T dn {domain}\r\n").encode()

    try:
        sock = socket.create_connection((server, 43), timeout=timeout)
    except Exception as e:
        logger.warning("WHOIS connect %s failed: %s", server, e)
        return "unknown", ""

    try:
        sock.settimeout(timeout)
        sock.sendall(query)
        chunks = []
        while True:
            try:
                data = sock.recv(8192)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
            if sum(len(c) for c in chunks) > 65536:
                break
    finally:
        try:
            sock.close()
        except Exception:
            pass

    raw = b"".join(chunks).decode("utf-8", errors="ignore")
    text = raw.lower()

    for m in _FREE_MARKERS:
        if m in text:
            # Defensive: some "available" markers can appear in disclaimers
            if "registrant" in text or "nserver:" in text:
                return "taken", raw
            return "available", raw

    for m in _TAKEN_MARKERS:
        if m in text:
            return "taken", raw

    return "unknown", raw + "\n\n[RDAP]\n" + (rdap_raw or "")
