"""Auto-Refresh-Controller für Logo-Assets während laufender Kampagnen.

Alle N Sends wird parallel refresht:
  * CID-Varianten (lokale Files, kein Netz)
  * Cloudinary-Uploads
  * S3-Uploads (round-robin über alle Buckets aller S3-Accounts)

Health-Tracking pro Provider (CID / Cloudinary / S3): consecutive
failures → weight reduziert für Meta-Rotation. Wenn Cloudinary+S3
beide dead → CID-only. Wenn CID auch nix → alte Variants als
last resort.
"""
from __future__ import annotations
import io
import os
import random
import secrets
import time
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

logger = logging.getLogger("mailer.auto_refresh")


class ProviderHealth:
    """Sliding-Window-Health pro Provider. In-Memory pro Kampagne.
    _lock ist RLock damit to_dict() intern self.health lesen darf ohne
    sich selbst zu deadlocken (non-reentrant Lock hätte hier reingesegnet)."""
    def __init__(self, name: str):
        self.name = name
        self.success = 0
        self.fail = 0
        self.consecutive_fails = 0
        self.last_success = 0.0
        self.last_fail = 0.0
        self._lock = threading.RLock()

    def record(self, ok: bool):
        with self._lock:
            if ok:
                self.success += 1
                self.consecutive_fails = 0
                self.last_success = time.time()
            else:
                self.fail += 1
                self.consecutive_fails += 1
                self.last_fail = time.time()

    @property
    def health(self) -> float:
        """0.0-1.0 Score. 1.0 = perfekt, 0.0 = tot.
        Beta(alpha=success+2, beta=fail+2)-mean für stabiles smoothing bei
        wenigen Datenpunkten. Nach 5 consecutive fails aggressive Penalty."""
        with self._lock:
            base = (self.success + 2) / (self.success + self.fail + 4)
            if self.consecutive_fails >= 5:
                base *= 0.1
            elif self.consecutive_fails >= 3:
                base *= 0.4
            return max(0.0, min(1.0, base))

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "success": self.success,
                "fail": self.fail,
                "consecutive_fails": self.consecutive_fails,
                "health": round(self.health, 3),
            }


class AutoRefreshController:
    """Ein Controller pro laufender Kampagne. State-in-Memory only.

    Semantik:
      * report_send(): pro erfolgreichem Send inkrementieren. Löst Refresh
        aus wenn Zähler N erreicht.
      * refresh(): synchron oder in eigenem Thread — CID + Cloud parallel.
      * weights_for_modes(): gibt Meta-Rotation die aktuellen Gewichte
        basierend auf Provider-Health.
    """

    def __init__(self, refresh_every: int = 100_000,
                  variants_per_refresh: int = 1000,
                  cid_base_weight: float = 3.0):
        self.refresh_every = max(0, int(refresh_every or 0))
        self.variants_per_refresh = max(1, int(variants_per_refresh or 1))
        self.cid_base_weight = float(cid_base_weight)
        self.sent_since_refresh = 0
        self.refresh_running = False
        self.refresh_count = 0
        self.last_refresh_at = 0.0
        self.last_refresh_report: Optional[dict] = None
        self.health_cid = ProviderHealth("cid")
        self.health_cloudinary = ProviderHealth("cloudinary")
        self.health_s3 = ProviderHealth("s3")
        self._lock = threading.Lock()

    def report_send(self) -> bool:
        """Rückgabe: True wenn ein Refresh JETZT getriggert werden soll."""
        if self.refresh_every <= 0:
            return False
        with self._lock:
            if self.refresh_running:
                return False
            self.sent_since_refresh += 1
            if self.sent_since_refresh >= self.refresh_every:
                self.sent_since_refresh = 0
                self.refresh_running = True
                return True
            return False

    def weight_for_mode(self, mode: str) -> float:
        """Weights für weighted-random-choice in Meta-Rotation.

        cid   → base_weight (garantierter Grundanteil, egal was)
        cdn   → mean(cloudinary, s3) * base_weight  — sinkt bei Ausfall
        andere → 1.0 (linear, wenn Config den Modus überhaupt hat)
        """
        if mode == "cid":
            return max(0.1, self.cid_base_weight)
        if mode in ("cdn", "cloudinary"):
            hc = self.health_cloudinary.health
            hs = self.health_s3.health
            # Wenn nur eins konfiguriert ist, ist das andere health=1.0
            # (kein fail) — deshalb hier auch max nehmen falls einer 0 ist.
            best = max(hc, hs)
            return round(self.cid_base_weight * best, 3)
        return 1.0

    def choose_mode(self, active_modes: list) -> str:
        """Weighted-random-choice aus den vom User aktivierten Modi.
        Rückgabe: exakt einer der active_modes. Wenn Liste leer → 'cid'."""
        if not active_modes:
            return "cid"
        weights = [self.weight_for_mode(m) for m in active_modes]
        # Wenn alle Gewichte ~0 → uniform choice als Notfall
        if sum(weights) < 0.01:
            return random.choice(active_modes)
        return random.choices(active_modes, weights=weights, k=1)[0]

    def snapshot(self) -> dict:
        return {
            "refresh_every": self.refresh_every,
            "variants_per_refresh": self.variants_per_refresh,
            "sent_since_refresh": self.sent_since_refresh,
            "refresh_count": self.refresh_count,
            "last_refresh_at": self.last_refresh_at,
            "last_refresh_report": self.last_refresh_report,
            "health": {
                "cid": self.health_cid.to_dict(),
                "cloudinary": self.health_cloudinary.to_dict(),
                "s3": self.health_s3.to_dict(),
            },
            "weights": {
                "cid": self.weight_for_mode("cid"),
                "cdn": self.weight_for_mode("cdn"),
            },
        }

    # ── Der Refresh-Vorgang ─────────────────────────────────

    def run_refresh(self, source_logo_paths: list,
                     variant_dir: str,
                     cloudinary_config: Optional[dict] = None,
                     s3_accounts: Optional[list] = None,
                     s3_proxy: str = "",
                     cloudinary_proxy_dict: Optional[dict] = None,
                     on_cdn_url_added=None,
                     tweak_bytes_fn=None) -> dict:
        """Parallel: CID (lokal), Cloudinary, S3-Uploads.

        source_logo_paths: [(name, path), ...] — die base-logos
        variant_dir: wo neue CID-Varianten als Files landen sollen
        cloudinary_config: {cloud_name, api_key, api_secret} oder None
        s3_accounts: [dict(access_key, secret_key, buckets), ...] oder []
        s3_proxy, cloudinary_proxy_dict: für requests
        on_cdn_url_added(url): callback pro erfolgreichem Cloud-Upload
        tweak_bytes_fn: Funktion (src_path, seed) → bytes für pixel-tweak

        Return: {cid: (ok, fail), cloudinary: (ok, fail), s3: (ok, fail), duration_sec}
        """
        t0 = time.time()
        variants_needed = self.variants_per_refresh
        report = {"cid": (0, 0), "cloudinary": (0, 0), "s3": (0, 0),
                  "duration_sec": 0}

        if not source_logo_paths:
            logger.warning("auto_refresh: no source logos, skipping")
            self.refresh_running = False
            return report

        try:
            # Parallel-Jobs: 3 Provider gleichzeitig
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {}
                if variant_dir:
                    futures[pool.submit(self._refresh_cid,
                                          source_logo_paths, variant_dir,
                                          variants_needed, tweak_bytes_fn)] = "cid"
                if cloudinary_config and cloudinary_config.get("cloud_name"):
                    futures[pool.submit(self._refresh_cloudinary,
                                          source_logo_paths, cloudinary_config,
                                          variants_needed, cloudinary_proxy_dict,
                                          tweak_bytes_fn, on_cdn_url_added)] = "cloudinary"
                if s3_accounts:
                    futures[pool.submit(self._refresh_s3,
                                          source_logo_paths, s3_accounts,
                                          variants_needed, s3_proxy,
                                          tweak_bytes_fn, on_cdn_url_added)] = "s3"
                for fut in as_completed(futures):
                    name = futures[fut]
                    try:
                        ok, fail = fut.result()
                        report[name] = (ok, fail)
                        # Health aggregieren pro Refresh
                        health_obj = getattr(self, f"health_{name}", None)
                        if health_obj:
                            for _ in range(ok):
                                health_obj.record(True)
                            for _ in range(fail):
                                health_obj.record(False)
                    except Exception as e:
                        logger.error("auto_refresh %s crashed: %s", name, e, exc_info=True)
                        report[name] = (0, variants_needed)
                        getattr(self, f"health_{name}").record(False)
        except Exception as e:
            logger.error("auto_refresh outer failure: %s", e, exc_info=True)
        finally:
            report["duration_sec"] = round(time.time() - t0, 2)
            with self._lock:
                self.refresh_count += 1
                self.last_refresh_at = time.time()
                self.last_refresh_report = report
                self.refresh_running = False
            logger.info("auto_refresh done: %s", report)
        return report

    # ── Provider-spezifische Jobs ───────────────────────────

    def _refresh_cid(self, sources, variant_dir, count, tweak_bytes_fn) -> tuple:
        ok = 0
        fail = 0
        try:
            os.makedirs(variant_dir, exist_ok=True)
        except Exception:
            return 0, count
        per_src = max(1, count // len(sources))
        for src_name, src_path in sources:
            base = os.path.splitext(os.path.basename(src_path))[0] or "logo"
            ext = os.path.splitext(src_path)[1].lower() or ".png"
            for i in range(per_src):
                out = os.path.join(variant_dir,
                                    f"ar_{secrets.token_hex(4)}_{int(time.time())}{ext}")
                try:
                    if tweak_bytes_fn:
                        body = tweak_bytes_fn(src_path, seed=random.randint(0, 999999))
                    else:
                        with open(src_path, "rb") as fh:
                            body = fh.read()
                    with open(out, "wb") as fh:
                        fh.write(body)
                    ok += 1
                except Exception as e:
                    logger.warning("cid refresh #%d failed: %s", i, e)
                    fail += 1
        return ok, fail

    def _refresh_cloudinary(self, sources, cfg, count, proxies,
                              tweak_bytes_fn, on_url_added) -> tuple:
        ok = 0
        fail = 0
        try:
            from transactional.web.routes.cloudinary import _cloudinary_upload
        except Exception:
            return 0, count
        per_src = max(1, count // len(sources))
        for src_name, src_path in sources:
            base = "".join(c for c in os.path.splitext(src_name)[0]
                            if c.isalnum() or c in "-_") or "logo"
            for i in range(per_src):
                public_id = f"{base}_{secrets.token_hex(3)}"
                try:
                    body = (tweak_bytes_fn(src_path, seed=random.randint(0, 999999))
                             if tweak_bytes_fn else open(src_path, "rb").read())
                    resp = _cloudinary_upload(
                        cfg["cloud_name"], cfg["api_key"], cfg["api_secret"],
                        body=body, filename=src_name,
                        public_id=public_id, folder=cfg.get("folder", ""),
                        proxies=proxies or {})
                    url = resp.get("secure_url", "")
                    if url:
                        ok += 1
                        if on_url_added:
                            try:
                                on_url_added(url)
                            except Exception:
                                pass
                    else:
                        fail += 1
                except Exception as e:
                    logger.warning("cloudinary refresh #%d failed: %s", i, str(e)[:150])
                    fail += 1
        return ok, fail

    def _refresh_s3(self, sources, accounts, count, proxy,
                     tweak_bytes_fn, on_url_added) -> tuple:
        ok = 0
        fail = 0
        try:
            from mailer.s3_uploader import s3_upload_object, parse_buckets_field
        except Exception:
            return 0, count
        # Alle Buckets aller Accounts in eine flat-Liste zusammenführen
        # als (account, bucket, region)
        bucket_pool = []
        for acc in accounts:
            for b, r in parse_buckets_field(acc.get("buckets", "")):
                bucket_pool.append((acc, b, r))
        if not bucket_pool:
            return 0, count
        per_src = max(1, count // len(sources))
        for src_name, src_path in sources:
            base = "".join(c for c in os.path.splitext(src_name)[0]
                            if c.isalnum() or c in "-_") or "logo"
            ext = os.path.splitext(src_name)[1].lower() or ".png"
            import mimetypes
            ctype = mimetypes.guess_type(src_name)[0] or "image/png"
            for i in range(per_src):
                acc, bucket, region = bucket_pool[i % len(bucket_pool)]
                key = f"{base}/{secrets.token_hex(6)}{ext}"
                try:
                    body = (tweak_bytes_fn(src_path, seed=random.randint(0, 999999))
                             if tweak_bytes_fn else open(src_path, "rb").read())
                    url = s3_upload_object(
                        acc["access_key"], acc["secret_key"], region,
                        bucket, key, body, content_type=ctype,
                        public=True, proxy=proxy, timeout=45)
                    ok += 1
                    if on_url_added:
                        try:
                            on_url_added(url, bucket, key,
                                         account_id=acc.get("id"))
                        except Exception:
                            pass
                except Exception as e:
                    logger.warning("s3 refresh #%d failed: %s", i, str(e)[:150])
                    fail += 1
        return ok, fail
