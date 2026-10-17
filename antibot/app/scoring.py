"""Scoring engine — collects signals + rolls them into a 0..100 risk score.

Signals per request:

  Network
    - IP → ASN lookup (cached in DB, via ipinfo.io Lite)
    - Hosting/datacenter ASN → +40
    - Country rule → configurable delta
    - Rate limit (per session bucket) → hits over budget → +30 / block
  Header
    - UA missing / obviously synthetic → +30
    - UA claims Chrome N < cutoff → +15 (old)
  Client (from JS challenge form)
    - navigator.webdriver === true → +40
    - No plugins, no webgl vendor, no canvas hash → +15 each
    - Honeypot field filled → +100 (instant block)
    - Submit timing < 500ms → +50
  Persistence
    - Valid verification cookie → allow shortcut (score forced to 0)

Verdicts:
  score >= threshold_block   → block  (or drop, or honeypot)
  score >= threshold_allow   → challenge (silent PoW + JS test)
  score <  threshold_allow   → allow (302 to target)
"""
import time
import logging
import threading
import requests as _req
from collections import defaultdict, deque

logger = logging.getLogger("antibot.scoring")

# Hosting / cloud ASN slop — grow as we see traffic. Casefold match on org name.
HOSTING_ORG_SNIPPETS = [
    "hetzner", "ovh", "digitalocean", "linode", "vultr", "amazon", "google",
    "microsoft", "azure", "cloudflare", "leaseweb", "contabo", "netcup",
    "worldstream", "hostkey", "servperso", "psychz", "choopa", "quadranet",
    "aws", "gcp", "oracle cloud", "alibaba", "tencent", "hostwinds", "m247",
    "colocrossing", "singlehop", "namecheap hosting", "hostinger",
]

# Very rough "chrome version too old" cutoff — update yearly.
CHROME_MIN_MAJOR = 110


class RateLimiter:
    """Per-bucket sliding window counter (in-memory, per process)."""

    def __init__(self):
        self._buckets = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, bucket: str, window_seconds: int = 60) -> int:
        """Record a hit; return current count in window."""
        now = time.time()
        cutoff = now - window_seconds
        with self._lock:
            dq = self._buckets[bucket]
            while dq and dq[0] < cutoff:
                dq.popleft()
            dq.append(now)
            return len(dq)


_rate_limiter = RateLimiter()


def _looks_like_hosting(org: str) -> bool:
    o = (org or "").casefold()
    return any(s in o for s in HOSTING_ORG_SNIPPETS)


def _asn_lookup(db, ip: str) -> dict:
    """Cached IP → ASN. Uses ipinfo.io free tier (no key)."""
    if not ip:
        return {"asn": "", "org": "", "country": "", "is_hosting": False}
    cached = db.get_asn_cached(ip)
    if cached:
        return {"asn": cached["asn"], "org": cached["org"],
                "country": cached["country"], "is_hosting": bool(cached["is_hosting"])}
    org, asn, cc = "", "", ""
    try:
        r = _req.get(f"https://ipinfo.io/{ip}/json", timeout=3)
        if r.status_code == 200:
            data = r.json()
            org_raw = data.get("org", "")
            # ipinfo returns "AS15169 Google LLC" — split off the ASN
            if org_raw.upper().startswith("AS") and " " in org_raw:
                asn, org = org_raw.split(" ", 1)
            else:
                org = org_raw
            cc = data.get("country", "")
    except Exception as e:
        logger.warning("ASN lookup failed for %s: %s", ip, e)
    hosting = _looks_like_hosting(org)
    db.cache_asn(ip, asn, org, cc, hosting)
    return {"asn": asn, "org": org, "country": cc, "is_hosting": hosting}


def _ua_age_penalty(ua: str) -> int:
    """Very rough age heuristic — old Chrome major → +15."""
    if not ua:
        return 30
    import re
    m = re.search(r"Chrome/(\d+)", ua)
    if not m:
        return 0
    try:
        major = int(m.group(1))
    except ValueError:
        return 0
    if major < CHROME_MIN_MAJOR - 20:
        return 25
    if major < CHROME_MIN_MAJOR:
        return 15
    return 0


def score_request(db, cfg: dict, *, ip: str, user_agent: str,
                  client_signals: dict = None,
                  rate_bucket: str = "") -> dict:
    """Returns {score, signals, network, verdict_hint}."""
    signals = {}
    score = 0
    client_signals = client_signals or {}

    # 1. Network layer
    net = _asn_lookup(db, ip)
    if net["is_hosting"]:
        score += 40
        signals["hosting_asn"] = f"{net['asn']} {net['org']}"

    if net["country"]:
        cc_rule = db.get_country_rule(net["country"])
        if cc_rule and cc_rule["score_delta"]:
            score += int(cc_rule["score_delta"])
            signals["country_delta"] = f"{net['country']}:{cc_rule['score_delta']}"

    # ASN rule overrides
    if net["asn"]:
        asn_rule = db.get_asn_rule(net["asn"])
        if asn_rule:
            if asn_rule["verdict"] == "allow":
                return {"score": 0, "signals": {"asn_allow": net["asn"]},
                        "network": net, "verdict_hint": "allow"}
            if asn_rule["verdict"] == "block":
                return {"score": 100, "signals": {"asn_block": net["asn"]},
                        "network": net, "verdict_hint": "block"}
            if asn_rule["verdict"].startswith("score:"):
                try:
                    score += int(asn_rule["verdict"].split(":", 1)[1])
                    signals["asn_score"] = asn_rule["verdict"]
                except ValueError:
                    pass

    # IP allow/block override
    if ip:
        ip_rule = db.get_ip_rule(ip)
        if ip_rule and ip_rule["verdict"] == "allow":
            return {"score": 0, "signals": {"ip_allow": ip},
                    "network": net, "verdict_hint": "allow"}
        if ip_rule and ip_rule["verdict"] == "block":
            return {"score": 100, "signals": {"ip_block": ip},
                    "network": net, "verdict_hint": "block"}

    # 2. Rate limit
    if rate_bucket:
        n = _rate_limiter.hit(rate_bucket)
        limit = int(cfg.get("rate_limit_per_min", "60"))
        if n > limit:
            score += 30
            signals["rate_over"] = f"{n}/{limit}"
        if n > limit * 3:
            score += 30

    # 3. Header layer
    ua_pen = _ua_age_penalty(user_agent)
    if ua_pen:
        score += ua_pen
        signals["ua_age"] = ua_pen

    # 4. Client-side signals (only present after challenge)
    if client_signals:
        if client_signals.get("honeypot"):
            score += 100
            signals["honeypot"] = "filled"
        if client_signals.get("webdriver"):
            score += 40
            signals["webdriver"] = True
        if client_signals.get("no_plugins"):
            score += 15
            signals["no_plugins"] = True
        if not client_signals.get("webgl_vendor"):
            score += 15
            signals["no_webgl"] = True
        if not client_signals.get("canvas_hash"):
            score += 15
            signals["no_canvas"] = True
        try:
            ms = int(client_signals.get("submit_ms") or 0)
            if 0 < ms < 500:
                score += 50
                signals["fast_submit_ms"] = ms
        except (TypeError, ValueError):
            pass
        # Turnstile-Verdict: True/False = Gate hat Widget, None = kein Widget
        ts = client_signals.get("turnstile_ok")
        if ts is True:
            # CF sagt "human bestätigt" → das ist der stärkste Human-Beweis
            # den wir haben, kompensiert alle anderen Signals kräftig
            score -= 60
            signals["turnstile_pass"] = True
        elif ts is False:
            # Widget da, aber nicht/nicht sauber gelöst
            score += 50
            signals["turnstile_fail"] = True

        # PoW-Fail-Penalty NUR wenn kein Turnstile bestanden (weil Client
        # den PoW dann bewusst überspringt — Turnstile ist Ersatz).
        if client_signals.get("pow_ok") is False and ts is not True:
            score += 60
            signals["pow_failed"] = True

    score = max(0, min(100, score))

    allow_thr = int(cfg.get("threshold_allow", "40"))
    block_thr = int(cfg.get("threshold_block", "70"))
    if score >= block_thr:
        hint = "block"
    elif score >= allow_thr:
        hint = "challenge"
    else:
        hint = "allow"

    return {"score": score, "signals": signals, "network": net,
            "verdict_hint": hint}
