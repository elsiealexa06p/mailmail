import threading
import time
from typing import Optional, List

try:
    import dns.resolver
    HAS_DNSPYTHON = True
except ImportError:
    HAS_DNSPYTHON = False


class DNSCache:
    TTL = 600

    def __init__(self):
        self._cache: dict = {}
        self._lock = threading.Lock()

    def resolve_mx(self, domain: str) -> Optional[List[str]]:
        domain = domain.lower().strip()
        with self._lock:
            entry = self._cache.get(("MX", domain))
            if entry and (time.monotonic() - entry[1]) < self.TTL:
                return entry[0]

        records = self._query_mx(domain)
        with self._lock:
            self._cache[("MX", domain)] = (records, time.monotonic())
        return records

    def resolve_a(self, hostname: str) -> Optional[List[str]]:
        hostname = hostname.lower().strip()
        with self._lock:
            entry = self._cache.get(("A", hostname))
            if entry and (time.monotonic() - entry[1]) < self.TTL:
                return entry[0]

        records = self._query_a(hostname)
        with self._lock:
            self._cache[("A", hostname)] = (records, time.monotonic())
        return records

    @staticmethod
    def _query_mx(domain: str) -> Optional[List[str]]:
        if not HAS_DNSPYTHON:
            return None
        try:
            answers = dns.resolver.resolve(domain, "MX")
            results = []
            for rdata in sorted(answers, key=lambda r: r.preference):
                results.append(str(rdata.exchange).rstrip("."))
            return results if results else None
        except Exception:
            return None

    @staticmethod
    def _query_a(hostname: str) -> Optional[List[str]]:
        if not HAS_DNSPYTHON:
            return None
        try:
            answers = dns.resolver.resolve(hostname, "A")
            return [str(rdata) for rdata in answers]
        except Exception:
            return None
