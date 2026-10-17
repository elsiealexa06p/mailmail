import json
import random
import sqlite3
import threading
import logging
import time
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

logger = logging.getLogger("mailer.redirect")

API_URL = (
    "https://www.google.com/httpservice/retry/"
    "SearchApiService/GetShortenedKpSharingUrl"
)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Accept": "*/*",
    "Accept-Language": "de,en-US;q=0.7,en;q=0.3",
    "Referer": "https://www.google.com/",
}


def _normalize_proxy(raw: str) -> Optional[str]:
    """Nimmt einen Proxy-String und liefert eine URL für requests.proxies.
    Akzeptiert alle üblichen Formate:
      * host:port
      * host:port:user:pass
      * scheme://host:port
      * scheme://user:pass@host:port
      * scheme://host:port:user:pass   ← häufiges Colon-Auth-Format
    Default-Schema: socks5h (DNS auch durch Proxy)."""
    s = (raw or "").strip()
    if not s:
        return None
    scheme = "socks5h"
    rest = s
    if "://" in s:
        scheme, rest = s.split("://", 1)
    # user:pass@host:port bereits sauber
    if "@" in rest:
        return f"{scheme}://{rest}"
    parts = rest.split(":")
    if len(parts) == 2:
        return f"{scheme}://{parts[0]}:{parts[1]}"
    if len(parts) == 4:
        host, port, user, pw = parts
        return f"{scheme}://{user}:{pw}@{host}:{port}"
    return None


class RedirectManager:
    def __init__(self, target_url: str = "", db_path: str = "redirects.db",
                 enabled: bool = False, rotate_every: int = 10,
                 gen_threads: int = 3):
        self._target_url = target_url
        self._db_path = db_path
        self._enabled = enabled and bool(target_url)
        self._links: List[str] = []
        self._lock = threading.Lock()
        self._gen_thread: Optional[threading.Thread] = None
        self._rotate_every = max(1, rotate_every)
        self._gen_threads = max(1, gen_threads)
        if self._enabled:
            self._ensure_schema()
            self._load_from_db()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def pool_size(self) -> int:
        with self._lock:
            return len(self._links)

    def _ensure_schema(self):
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("""CREATE TABLE IF NOT EXISTS redirect_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            short_url TEXT NOT NULL,
            target_url TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.commit()
        conn.close()

    def _load_from_db(self):
        conn = sqlite3.connect(self._db_path, timeout=10)
        rows = conn.execute("SELECT short_url FROM redirect_links ORDER BY id").fetchall()
        conn.close()
        with self._lock:
            self._links = [r[0] for r in rows]

    def _save_link(self, short_url: str, target: str = ""):
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("INSERT INTO redirect_links (short_url, target_url) VALUES (?, ?)",
                     (short_url, target or self._target_url))
        conn.commit()
        conn.close()

    def prepare(self, lead_count: int) -> None:
        if not self._enabled:
            return
        if not HAS_REQUESTS:
            logger.error("requests not installed")
            return
        needed = max(1, lead_count // self._rotate_every)
        current = self.pool_size
        if current >= needed:
            return
        missing = needed - current
        self._gen_thread = threading.Thread(target=self._generate_batch,
                                            args=(missing,), daemon=True)
        self._gen_thread.start()

    def wait_ready(self):
        if self._gen_thread and self._gen_thread.is_alive():
            self._gen_thread.join()

    def get_link(self, send_index: int) -> str:
        with self._lock:
            if not self._links:
                return self._target_url
            group = send_index // self._rotate_every
            return self._links[group % len(self._links)]

    def _generate_batch(self, count: int):
        print(f"  Redirect links: generating {count} ...")
        generated = 0
        for i in range(count):
            url = self._generate_one(self._target_url)
            if url:
                with self._lock:
                    self._links.append(url)
                self._save_link(url)
                generated += 1
            if (i + 1) % 10 == 0 or (i + 1) == count:
                print(f"    [{i + 1}/{count}] ({generated} ok)")
            time.sleep(0.5)
        print(f"  Redirect pool: {self.pool_size} links")

    @staticmethod
    def _generate_one(target_url: str, proxy: str = "") -> Optional[str]:
        """Generate a share.google link. If `proxy` is set (SOCKS5 or
        HTTP URL like `socks5://user:pass@host:port` or `http://host:port`),
        the API call goes through it."""
        reqpld = json.dumps([[[target_url], 1, None, None, None, None, 35]])
        params = {
            "sca_esv": "2f77f72a12157cd0",
            "client": "firefox-b-d",
            "hs": "VrYp",
            "reqpld": reqpld,
            "msc": "gwsrpc",
            "opi": "89978449",
        }
        kwargs = {"params": params, "headers": HEADERS, "timeout": 20}
        if proxy and proxy.strip():
            p = _normalize_proxy(proxy.strip())
            if p:
                kwargs["proxies"] = {"http": p, "https": p}
        try:
            resp = _requests.get(API_URL, **kwargs)
            resp.raise_for_status()
            raw = resp.text
            if raw.startswith(")]}'"):
                raw = raw[4:].strip()
            data = json.loads(raw)
            return data[0][0][0]
        except Exception as exc:
            logger.error("Redirect API error: %s", exc)
            return None

    @staticmethod
    def _generate_one_goto_pw(target_url: str, proxy: str = "",
                                debug: bool = False,
                                _browser=None):
        """Playwright-based /goto generator. Startet Chromium headless,
        navigiert zur SERP, wartet bis JS gerendert hat, extrahiert
        den /goto-Link. Robust gegen Google's Anti-Scraping (weil es
        ein echter Browser ist).

        Nutzt einen persistenten Browser wenn _browser übergeben wird
        (für Batch-Jobs — einmal starten, viele Contexts drin).
        """
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        except ImportError:
            if debug:
                return (None,
                        "playwright not installed — pip install playwright && "
                        "playwright install chromium", "")
            logger.error("playwright not installed")
            return None
        import re as _re

        pw_proxy = None
        proxy_skipped_reason = ""
        proxy_config_dbg = "no proxy"
        if proxy and proxy.strip():
            p = _normalize_proxy(proxy.strip())
            if p:
                from urllib.parse import urlparse
                pr = urlparse(p)
                is_socks = pr.scheme.startswith("socks")
                has_auth = bool(pr.username or pr.password)
                if is_socks and has_auth:
                    proxy_skipped_reason = (
                        f"chromium supports socks5 without auth only "
                        f"(dieser proxy hat user/pass) — ohne proxy weiter"
                    )
                    proxy_config_dbg = f"SKIPPED ({pr.scheme}://***@{pr.hostname}:{pr.port})"
                    logger.warning("Playwright /goto: %s", proxy_skipped_reason)
                else:
                    pw_proxy = {"server": f"{pr.scheme}://{pr.hostname}:{pr.port}"}
                    if pr.username:
                        pw_proxy["username"] = pr.username
                    if pr.password:
                        pw_proxy["password"] = pr.password
                    proxy_config_dbg = (
                        f"{pr.scheme}://{'user:***@' if has_auth else ''}"
                        f"{pr.hostname}:{pr.port}")

        def _extract(page) -> str:
            try:
                anchors = page.eval_on_selector_all(
                    "a[href*='/goto?url=']",
                    "els => els.map(e => e.href)")
                for href in anchors:
                    m = _re.search(r'/goto\?url=([A-Za-z0-9_\-]+={0,3})', href)
                    if m:
                        return f"https://www.google.com/goto?url={m.group(1)}"
            except Exception:
                pass
            html = page.content()
            m = _re.search(r'/goto\?url=([A-Za-z0-9_\-]+={0,3})', html)
            if m:
                return f"https://www.google.com/goto?url={m.group(1)}"
            return ""

        def _run(pw):
            own_browser = _browser is None
            browser = _browser or pw.chromium.launch(
                headless=True, proxy=pw_proxy,
                args=["--disable-blink-features=AutomationControlled"])
            context = None
            try:
                context = browser.new_context(
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/130.0.0.0 Safari/537.36"),
                    locale="de-DE",
                    viewport={"width": 1280, "height": 900},
                    extra_http_headers={"Accept-Language": "de-DE,de;q=0.9,en;q=0.7"},
                )
                context.add_cookies([
                    {"name": "CONSENT",
                     "value": "YES+cb.20240101-08-p0.de+FX+123",
                     "domain": ".google.com", "path": "/"},
                    {"name": "SOCS", "value": "CAESHAgBEhIaAB",
                     "domain": ".google.com", "path": "/"},
                ])
                page = context.new_page()
                from urllib.parse import quote as _quote
                url = (f"https://www.google.com/search?q={_quote(target_url, safe='')}"
                       f"&hl=de&gl=de&pws=0")
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                # Consent-Modal manchmal trotz Cookies
                for label in ("Alle ablehnen", "Alle akzeptieren"):
                    try:
                        page.locator(f"button:has-text('{label}')").first.click(timeout=1500)
                        break
                    except Exception:
                        continue
                try:
                    page.wait_for_selector("a[href*='/goto?url=']", timeout=10000)
                except PWTimeout:
                    pass
                found = _extract(page)
                if not found:
                    if debug:
                        note = f" [{proxy_skipped_reason}]" if proxy_skipped_reason else ""
                        return (None,
                                f"no /goto in rendered DOM (final={page.url}){note} "
                                f"[proxy: {proxy_config_dbg}]",
                                page.content()[:400])
                    return None
                if debug:
                    note = f" [{proxy_skipped_reason}]" if proxy_skipped_reason else ""
                    return (found, f"ok (playwright){note} [proxy: {proxy_config_dbg}]", "")
                return found
            finally:
                if context:
                    try: context.close()
                    except Exception: pass
                if own_browser:
                    try: browser.close()
                    except Exception: pass

        try:
            with sync_playwright() as pw:
                return _run(pw)
        except Exception as exc:
            if debug:
                return (None,
                        f"playwright exception: {exc} [proxy: {proxy_config_dbg}]",
                        "")
            logger.error("playwright /goto error: %s", exc)
            return None


    @staticmethod
    def _generate_one_goto(target_url: str, proxy: str = "",
                            debug: bool = False):
        """Google `/goto?url=<token>` — signed open-redirect. Generiert
        indem die Ziel-URL als Suchanfrage geschickt wird und der resultierende
        goto-Link aus der SERP-HTML extrahiert wird.

        Gültigkeit laut Beobachtung: 1-3 Tage. TTL steckt im Token, nicht
        beeinflussbar von uns — kurz vor Send generieren.

        Nutzt gbv=1 (Google Basic View) um die JS-only SERP zu umgehen —
        sonst kommt nur ein <noscript>-Skeleton zurück und der Regex
        findet natürlich nichts.

        debug=True → returned tuple (url_or_None, status, html_snippet)
        statt nur der URL. Für den Test-Endpoint.
        """
        import re as _re
        from urllib.parse import quote as _quote
        headers = dict(HEADERS)
        # Neuerer Chrome-UA — Firefox-124 wird von Google z.T. auf die
        # JS-only SERP gemapped. Chrome-Desktop kriegt zuverlässiger die
        # klassische HTML-SERP wenn kombiniert mit gbv=1.
        headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        headers["Accept"] = ("text/html,application/xhtml+xml,application/xml;"
                              "q=0.9,image/webp,*/*;q=0.8")
        headers["Accept-Language"] = "de-DE,de;q=0.9,en;q=0.7"
        headers["Sec-Fetch-Dest"] = "document"
        headers["Sec-Fetch-Mode"] = "navigate"
        headers["Sec-Fetch-Site"] = "none"
        headers["Sec-Ch-Ua"] = ('"Chromium";v="130", "Google Chrome";v="130", '
                                  '"Not-A.Brand";v="99"')
        headers["Sec-Ch-Ua-Mobile"] = "?0"
        headers["Sec-Ch-Ua-Platform"] = '"Windows"'
        headers["Upgrade-Insecure-Requests"] = "1"
        cookies = {
            "CONSENT": "YES+cb.20240101-08-p0.de+FX+123",
            "SOCS": "CAESHAgBEhIaAB",
            "NID": "511=abc" + _re.sub(r"\D", "",
                                       str(hash(target_url))[-15:]),
        }
        # gbv=1 = Google Basic View → klassische HTML-SERP OHNE JS-Rendering.
        # Genau die Ansicht die den /goto-Link inline im HTML einbaut.
        url = (f"https://www.google.com/search?"
               f"q={_quote(target_url, safe='')}&hl=de&gl=de&pws=0&gbv=1")
        kwargs = {"headers": headers, "cookies": cookies,
                  "timeout": 20, "allow_redirects": True}
        if proxy and proxy.strip():
            p = _normalize_proxy(proxy.strip())
            if p:
                kwargs["proxies"] = {"http": p, "https": p}
        try:
            resp = _requests.get(url, **kwargs)
            html = resp.text or ""
            status = resp.status_code
            final_url = resp.url
            if "consent.google.com" in final_url or "consent.google" in html[:2000]:
                if debug:
                    return (None, f"consent-wall (final={final_url})",
                            html[:400])
                logger.warning("Google /goto: consent-wall für %s (final=%s)",
                                target_url[:80], final_url)
                return None
            if "unusual traffic" in html[:5000].lower() or "/sorry/" in final_url:
                if debug:
                    return (None, f"rate-limited/captcha (final={final_url})",
                            html[:400])
                logger.warning("Google /goto: rate-limit für %s",
                                target_url[:80])
                return None
            # JS-only skeleton? enablejs marker im first 3KB
            if "/httpservice/retry/enablejs" in html[:3000]:
                if debug:
                    return (None, f"js-only skeleton (gbv=1 ignoriert, "
                                   f"status={status})", html[:400])
                logger.warning("Google /goto: JS-only für %s trotz gbv=1",
                                target_url[:80])
                return None
            patterns = [
                r'/goto\?url=([A-Za-z0-9_\-]+={0,3})',
                r'\\x2fgoto\\x3furl\\x3d([A-Za-z0-9_\-]+={0,3})',
                r'"(https://www\.google\.com/goto\?url=[A-Za-z0-9_\-]+={0,3})"',
                r'&#47;goto&#63;url&#61;([A-Za-z0-9_\-]+={0,3})',
                # gbv=1 nutzt manchmal /url?q= statt /goto — auch akzeptieren
                r'/url\?q=(https?[^&"\'<> ]+)&amp;',
                r'/url\?q=(https?[^&"\'<> ]+)&sa=',
            ]
            for pat in patterns:
                m = _re.search(pat, html)
                if m:
                    tok = m.group(1)
                    if tok.startswith("https://www.google.com/goto"):
                        found = tok
                    elif tok.startswith("http"):
                        # /url?q=<target> — nicht was wir wollen, aber besser als nichts
                        # (das ist der klassische Google-URL-Rewrite, kein signed token)
                        from urllib.parse import unquote as _unquote
                        # Wenn der User /url?q= wirklich haben will, würde er
                        # den alten Generator nutzen. Für /goto ignorieren wir das.
                        continue
                    else:
                        found = f"https://www.google.com/goto?url={tok}"
                    if debug:
                        return (found, f"ok (status={status})", "")
                    return found
            has_url_q = "/url?q=" in html
            has_ping = "ping=" in html
            if debug:
                snippet = html[:400]
                return (None,
                        f"no /goto token (status={status}, has_url_q={has_url_q}, "
                        f"has_ping={has_ping}, len={len(html)})",
                        snippet)
            logger.warning("Google /goto: kein Token in SERP für %s "
                            "(status=%s, len=%d, /url?q=%s)",
                            target_url[:80], status, len(html), has_url_q)
            return None
        except Exception as exc:
            if debug:
                return (None, f"exception: {exc}", "")
            logger.error("Google /goto error: %s", exc)
            return None

    @staticmethod
    def generate_batch_threaded(target_url: str, count: int, threads: int = 5,
                                 callback=None) -> List[str]:
        results: List[str] = []
        lock = threading.Lock()
        done = [0]

        def worker():
            url = RedirectManager._generate_one(target_url)
            with lock:
                if url:
                    results.append(url)
                done[0] += 1
                if callback:
                    callback(done[0], count, url)

        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = [executor.submit(worker) for _ in range(count)]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception:
                    pass
                time.sleep(0.3)
        return results
