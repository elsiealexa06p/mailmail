"""MXToolbox Blacklist Checker.

Checks outgoing IPs (own IP or proxy IPs) against email blacklists
via the MXToolbox API before mailing starts.
"""
import logging
from typing import List, Tuple

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    import socks
    HAS_SOCKS = True
except ImportError:
    HAS_SOCKS = False

logger = logging.getLogger("mailer.blacklist")

API_BASE = "https://mxtoolbox.com/api/v1/lookup"


class BlacklistChecker:
    def __init__(self, api_key: str = ""):
        self._api_key = api_key
        self._enabled = bool(api_key) and HAS_REQUESTS

    @property
    def enabled(self) -> bool:
        return self._enabled

    def check_ip(self, ip: str) -> Tuple[bool, List[dict]]:
        if not self._enabled:
            return True, [{"error": "API key not configured"}]
        url = f"{API_BASE}/blacklist/{ip}"
        headers = {"Authorization": self._api_key}
        try:
            resp = _requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                failures = []
                for entry in data.get("Failed", []):
                    failures.append({
                        "name": entry.get("Name", ""),
                        "info": entry.get("Info", ""),
                    })
                return len(failures) == 0, failures
            logger.error("MXToolbox API %d: %s", resp.status_code, resp.text[:200])
            return True, [{"error": f"API returned {resp.status_code}"}]
        except Exception as exc:
            logger.error("MXToolbox error: %s", exc)
            return True, [{"error": str(exc)}]

    @staticmethod
    def get_own_ip() -> str:
        try:
            return _requests.get("https://api.ipify.org", timeout=10).text.strip()
        except Exception:
            return ""

    @staticmethod
    def get_proxy_ip(proxy_host: str, proxy_port: int,
                     username: str = "", password: str = "") -> str:
        if not HAS_SOCKS:
            return ""
        try:
            import socket
            s = socks.socksocket(socket.AF_INET, socket.SOCK_STREAM)
            s.set_proxy(socks.SOCKS5, proxy_host, proxy_port,
                        username=username or None, password=password or None)
            s.settimeout(10)
            s.connect(("api.ipify.org", 80))
            s.sendall(b"GET / HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n")
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            s.close()
            body = data.decode().split("\r\n\r\n", 1)[-1].strip()
            return body
        except Exception as exc:
            logger.error("Proxy IP lookup failed: %s", exc)
            return ""

    def check_sending_ips(self, proxies: list = None) -> dict:
        results = {}

        if not proxies:
            own_ip = self.get_own_ip()
            if own_ip:
                clean, details = self.check_ip(own_ip)
                results[f"Own IP ({own_ip})"] = {"clean": clean, "details": details, "ip": own_ip}
            return results

        for i, proxy in enumerate(proxies[:5]):
            proxy_ip = self.get_proxy_ip(proxy.host, proxy.port, proxy.username, proxy.password)
            label = f"Proxy {i+1} ({proxy.host}:{proxy.port})"
            if proxy_ip:
                clean, details = self.check_ip(proxy_ip)
                results[f"{label} → {proxy_ip}"] = {"clean": clean, "details": details, "ip": proxy_ip}
            else:
                results[label] = {"clean": True, "details": [{"error": "Could not resolve proxy IP"}], "ip": ""}

        return results
