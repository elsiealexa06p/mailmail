"""Hoster / Expurgate filter for email lists.

Given a list of email addresses, performs an MX lookup per unique domain
and optionally a reverse-DNS lookup on the MX A-records, then matches the
hostnames against a static set of provider keywords. Returns three lists:
clean addresses, filtered addresses (per provider), and DNS-failures.

The provider set is hard-coded — this filter exists specifically to remove
recipients sitting behind Expurgate (Eleven/Cyren) spam gateways or on
hosters that wrap their inbound mail through it.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable

logger = logging.getLogger("mailer.expurgate")

DEFAULT_TIMEOUT = 5

# Name -> (substring,) tuple. Match runs against MX hostnames and, if
# deep_lookup is enabled, against PTR (reverse-DNS) of MX A-records.
PROVIDERS: dict[str, tuple[str, ...]] = {
    "strato":             ("rzone.de", "strato.de"),
    "mittwald":           ("agenturserver.de", "mittwald.de"),
    "variomedia":         ("variomedia.de", "vrmd.de"),
    "secure_mailservice": ("secure-mailservice.de",),
    "aruba":              ("aruba.it", "aruba.com"),
    "t_online":           ("t-online.de", "magenta.de"),
    "vodafone":           ("vodafonemail.de", "vodafone.de", "arcor.de", "kabelmail.de"),
    "expurgate":          ("expurgate.net", "expurgate.com", "eleven.de"),
    "ionos":              ("kundenserver.de", "ionos.de", "1and1.com", "1und1.de"),
}

STATUS_CLEAN = "clean"
STATUS_ERROR = "error"


def _match(text: str) -> str | None:
    for name, keywords in PROVIDERS.items():
        if any(kw in text for kw in keywords):
            return name
    return None


def _detect_provider(domain: str, deep_lookup: bool, cache: dict,
                      lock, timeout: float) -> str:
    """Cached per-domain provider detection. Returns provider name,
    'clean', or 'error'."""
    with lock:
        if domain in cache:
            return cache[domain]

    import dns.resolver
    import dns.reversename

    result: str

    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=timeout)
    except Exception:
        result = STATUS_ERROR
        with lock:
            cache[domain] = result
        return result

    mx_hosts = [str(r.exchange).rstrip(".").lower() for r in answers]
    hit = _match(" ".join(mx_hosts))

    if not hit and deep_lookup:
        for mx in mx_hosts:
            try:
                a_records = dns.resolver.resolve(mx, "A", lifetime=timeout)
            except Exception:
                continue
            for a in a_records:
                try:
                    rev = dns.reversename.from_address(str(a))
                    ptr = dns.resolver.resolve(rev, "PTR", lifetime=timeout)
                    rev_name = str(ptr[0]).rstrip(".").lower()
                except Exception:
                    continue
                hit = _match(rev_name)
                if hit:
                    break
            if hit:
                break

    result = hit or STATUS_CLEAN
    with lock:
        cache[domain] = result
    return result


def classify_list(
    emails: Iterable[str],
    *,
    deep_lookup: bool = True,
    threads: int = 50,
    timeout: float = DEFAULT_TIMEOUT,
    progress_cb: Callable[[int, int], None] | None = None,
) -> dict[str, list[str]]:
    """Run provider detection over an email list. Returns a dict
    keyed by 'clean', 'error', and each provider name in PROVIDERS,
    with values being the input order preserved per bucket."""
    import threading

    emails = [e.strip() for e in emails if e and e.strip()]
    order = {e: i for i, e in enumerate(emails)}

    buckets: dict[str, list[str]] = {STATUS_CLEAN: [], STATUS_ERROR: []}
    for name in PROVIDERS:
        buckets[name] = []

    cache: dict[str, str] = {}
    lock = threading.Lock()

    def classify_one(mail: str) -> tuple[str, str]:
        if "@" not in mail:
            return mail, STATUS_ERROR
        domain = mail.rsplit("@", 1)[1].lower()
        try:
            status = _detect_provider(domain, deep_lookup, cache, lock, timeout)
        except Exception as e:
            logger.warning("detect failed for %s: %s", domain, e)
            status = STATUS_ERROR
        return mail, status

    total = len(emails)
    done = 0

    with ThreadPoolExecutor(max_workers=max(1, threads)) as ex:
        futures = [ex.submit(classify_one, m) for m in emails]
        for fut in as_completed(futures):
            mail, status = fut.result()
            buckets.setdefault(status, []).append(mail)
            done += 1
            if progress_cb and (done % 25 == 0 or done == total):
                try:
                    progress_cb(done, total)
                except Exception:
                    pass

    # Preserve original ordering within each bucket
    for lst in buckets.values():
        lst.sort(key=lambda m: order.get(m, 0))

    return buckets


def summarise(buckets: dict[str, list[str]]) -> dict:
    """Compact summary suitable for a progress / status panel."""
    total = sum(len(v) for v in buckets.values())
    clean = len(buckets.get(STATUS_CLEAN, []))
    errors = len(buckets.get(STATUS_ERROR, []))
    per_provider = {
        name: len(buckets.get(name, []))
        for name in PROVIDERS if buckets.get(name)
    }
    filtered = sum(per_provider.values())
    return {
        "total": total,
        "clean": clean,
        "errors": errors,
        "filtered": filtered,
        "per_provider": per_provider,
    }
