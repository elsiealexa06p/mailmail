"""SQLite backing store — auto-migrates on first open."""
import os
import json
import sqlite3
import secrets
import threading
import hashlib
import time
from typing import Optional


class DB:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._migrate()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn.execute("PRAGMA busy_timeout=5000")
        return self._local.conn

    def close(self):
        if hasattr(self._local, "conn"):
            self._local.conn.close()
            del self._local.conn

    def _migrate(self):
        c = self._conn()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS gates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hostname TEXT NOT NULL UNIQUE,
                active INTEGER DEFAULT 1,
                mode TEXT DEFAULT 'medium',
                target_url TEXT DEFAULT '',
                logo_path TEXT DEFAULT '',
                brand_text TEXT DEFAULT '',
                brand_color TEXT DEFAULT '#005eb8',
                turnstile_site_key TEXT DEFAULT '',
                turnstile_secret_key TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS gate_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gate_id INTEGER NOT NULL REFERENCES gates(id) ON DELETE CASCADE,
                slug TEXT NOT NULL,
                target_override TEXT DEFAULT '',
                label TEXT DEFAULT '',
                hits INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(gate_id, slug)
            );
            CREATE INDEX IF NOT EXISTS idx_glink_slug ON gate_links(slug);
            CREATE INDEX IF NOT EXISTS idx_glink_gate ON gate_links(gate_id);
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                admin_id INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                ip TEXT DEFAULT '',
                asn TEXT DEFAULT '',
                country TEXT DEFAULT '',
                user_agent TEXT DEFAULT '',
                target TEXT DEFAULT '',
                verdict TEXT NOT NULL,
                score INTEGER DEFAULT 0,
                signals_json TEXT DEFAULT '{}',
                token_valid INTEGER DEFAULT 0,
                dry_run INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_dec_ts ON decisions(ts DESC);
            CREATE INDEX IF NOT EXISTS idx_dec_ip ON decisions(ip);
            CREATE INDEX IF NOT EXISTS idx_dec_asn ON decisions(asn);
            CREATE TABLE IF NOT EXISTS asn_rules (
                asn TEXT PRIMARY KEY,
                verdict TEXT NOT NULL,          -- allow | block | score:<int>
                note TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS ip_rules (
                ip TEXT PRIMARY KEY,
                verdict TEXT NOT NULL,          -- allow | block
                note TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS country_rules (
                cc TEXT PRIMARY KEY,
                score_delta INTEGER DEFAULT 0,
                note TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS asn_cache (
                ip TEXT PRIMARY KEY,
                asn TEXT DEFAULT '',
                org TEXT DEFAULT '',
                country TEXT DEFAULT '',
                is_hosting INTEGER DEFAULT 0,
                fetched_at INTEGER NOT NULL
            );
        """)

    # ── config ─────────────────────────────────────────────
    _DEFAULTS = {
        "hmac_secret": "",
        "cookie_secret": "",
        "default_target": "",
        "logo_path": "",
        "brand_text": "Sicherheitsprüfung läuft …",
        "brand_color": "#005eb8",
        "threshold_allow": "40",
        "threshold_block": "70",
        "dry_run": "1",
        "verification_ttl_hours": "6",
        "rate_limit_per_min": "60",
        "turnstile_site_key": "",
        "turnstile_secret_key": "",
        "maxmind_license_key": "",
        "webhook_url": "",
        "webhook_min_score": "70",
        "pow_difficulty": "5",             # leading nibbles → 5 = 20 zero bits
        "wait_seconds": "0",               # extra delay before verify (0 = off)
        "setup_done": "0",
        "dynadot_api_key": "",
        "dynadot_api_secret": "",          # some Dynadot endpoints require it
        "cloudflare_api_token": "",        # Bearer token (Zone-scoped)
        "cloudflare_global_api_key": "",   # optional — full-account global key
        "cloudflare_auth_email": "",       # required when global key is used
        "cloudflare_account_id": "",       # needed only to create new zones
        "buy_currency": "USD",
        "server_public_ip": "",            # auto-detected; used for CF A-records
        "panel_hostname": "",              # gesetzt vom install.sh — für tls-check
    }

    # ── Gate CRUD ──────────────────────────────────────────
    def add_gate(self, hostname: str, **kw) -> int:
        kw["hostname"] = hostname.lower()
        cols = list(kw.keys())
        vals = list(kw.values())
        ph = ",".join("?" for _ in vals)
        c = self._conn()
        c.execute(f"INSERT INTO gates ({','.join(cols)}) VALUES ({ph})", vals)
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def update_gate(self, gate_id: int, **kw):
        if not kw:
            return
        c = self._conn()
        sets = ", ".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE gates SET {sets} WHERE id=?",
                  list(kw.values()) + [gate_id])

    def delete_gate(self, gate_id: int):
        self._conn().execute("DELETE FROM gates WHERE id=?", (gate_id,))

    def list_gates(self) -> list:
        return [dict(r) for r in self._conn().execute(
            "SELECT * FROM gates ORDER BY id").fetchall()]

    def get_gate(self, gate_id: int):
        r = self._conn().execute(
            "SELECT * FROM gates WHERE id=?", (gate_id,)).fetchone()
        return dict(r) if r else None

    def get_gate_by_host(self, hostname: str):
        r = self._conn().execute(
            "SELECT * FROM gates WHERE hostname=? AND active=1",
            (hostname.lower(),)).fetchone()
        return dict(r) if r else None

    # ── Gate-Link CRUD ────────────────────────────────────
    def add_gate_link(self, gate_id: int, slug: str, target_override: str = "",
                      label: str = "") -> int:
        c = self._conn()
        c.execute("INSERT INTO gate_links (gate_id, slug, target_override, label) "
                  "VALUES (?,?,?,?)", (gate_id, slug, target_override, label))
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_gate_link(self, gate_id: int, slug: str):
        r = self._conn().execute(
            "SELECT * FROM gate_links WHERE gate_id=? AND slug=?",
            (gate_id, slug)).fetchone()
        return dict(r) if r else None

    def list_gate_links(self, gate_id: int, limit: int = 500) -> list:
        return [dict(r) for r in self._conn().execute(
            "SELECT * FROM gate_links WHERE gate_id=? ORDER BY id DESC LIMIT ?",
            (gate_id, limit)).fetchall()]

    def bump_gate_link_hits(self, link_id: int):
        self._conn().execute(
            "UPDATE gate_links SET hits=hits+1 WHERE id=?", (link_id,))

    def delete_gate_link(self, link_id: int):
        self._conn().execute("DELETE FROM gate_links WHERE id=?", (link_id,))

    def delete_all_gate_links(self, gate_id: int):
        self._conn().execute("DELETE FROM gate_links WHERE gate_id=?", (gate_id,))

    def get_config(self) -> dict:
        rows = self._conn().execute("SELECT key, value FROM config").fetchall()
        cfg = dict(self._DEFAULTS)
        for r in rows:
            cfg[r["key"]] = r["value"]
        return cfg

    def set_config(self, **kw):
        c = self._conn()
        for k, v in kw.items():
            c.execute("INSERT INTO config (key, value) VALUES (?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (k, str(v)))

    def ensure_secrets(self):
        """Generate HMAC + cookie secrets on first run if missing."""
        cfg = self.get_config()
        updates = {}
        if not cfg.get("hmac_secret"):
            updates["hmac_secret"] = secrets.token_urlsafe(48)
        if not cfg.get("cookie_secret"):
            updates["cookie_secret"] = secrets.token_urlsafe(48)
        if updates:
            self.set_config(**updates)

    # ── admins ─────────────────────────────────────────────
    def add_admin(self, username: str, password: str) -> int:
        h = self._hash_pw(password)
        c = self._conn()
        c.execute("INSERT INTO admins (username, password_hash) VALUES (?, ?)",
                  (username, h))
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def verify_admin(self, username: str, password: str) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT * FROM admins WHERE username=?", (username,)).fetchone()
        if not row:
            return None
        if self._hash_pw(password, row["password_hash"].split("$")[1]) != row["password_hash"]:
            return None
        return dict(row)

    def admin_count(self) -> int:
        return self._conn().execute("SELECT COUNT(*) FROM admins").fetchone()[0]

    @staticmethod
    def _hash_pw(pw: str, salt: str = "") -> str:
        salt = salt or secrets.token_hex(16)
        h = hashlib.scrypt(pw.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=32)
        return f"scrypt${salt}${h.hex()}"

    # ── sessions ───────────────────────────────────────────
    def create_session(self, admin_id: int, ttl_seconds: int = 86400 * 7) -> str:
        tok = secrets.token_urlsafe(32)
        self._conn().execute(
            "INSERT INTO sessions (token, admin_id, expires_at) VALUES (?,?,?)",
            (tok, admin_id, int(time.time()) + ttl_seconds))
        return tok

    def get_session(self, token: str) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT s.admin_id, s.expires_at, a.username "
            "FROM sessions s JOIN admins a ON s.admin_id = a.id "
            "WHERE s.token=? AND s.expires_at > ?",
            (token, int(time.time()))).fetchone()
        return dict(row) if row else None

    def drop_session(self, token: str):
        self._conn().execute("DELETE FROM sessions WHERE token=?", (token,))

    # ── decisions log ──────────────────────────────────────
    def log_decision(self, **kw) -> int:
        kw.setdefault("ts", int(time.time()))
        cols = list(kw.keys())
        vals = [kw[k] if not isinstance(kw[k], (dict, list)) else json.dumps(kw[k])
                for k in cols]
        ph = ",".join("?" for _ in vals)
        c = self._conn()
        c.execute(f"INSERT INTO decisions ({','.join(cols)}) VALUES ({ph})", vals)
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def recent_decisions(self, limit: int = 200, verdict: str = "", asn: str = "",
                          ip: str = "") -> list:
        where = ["1=1"]
        args = []
        if verdict:
            where.append("verdict=?")
            args.append(verdict)
        if asn:
            where.append("asn LIKE ?")
            args.append(f"%{asn}%")
        if ip:
            where.append("ip=?")
            args.append(ip)
        args.append(limit)
        rows = self._conn().execute(
            f"SELECT * FROM decisions WHERE {' AND '.join(where)} "
            f"ORDER BY id DESC LIMIT ?", args).fetchall()
        return [dict(r) for r in rows]

    def counts_since(self, cutoff_ts: int) -> dict:
        rows = self._conn().execute(
            "SELECT verdict, COUNT(*) AS n FROM decisions WHERE ts >= ? GROUP BY verdict",
            (cutoff_ts,)).fetchall()
        return {r["verdict"]: r["n"] for r in rows}

    def top_blocked_asns(self, cutoff_ts: int, limit: int = 10) -> list:
        rows = self._conn().execute(
            "SELECT asn, COUNT(*) AS n FROM decisions "
            "WHERE ts >= ? AND verdict IN ('block','challenge') AND asn != '' "
            "GROUP BY asn ORDER BY n DESC LIMIT ?", (cutoff_ts, limit)).fetchall()
        return [dict(r) for r in rows]

    def prune_decisions(self, keep_days: int = 30):
        cutoff = int(time.time()) - keep_days * 86400
        self._conn().execute("DELETE FROM decisions WHERE ts < ?", (cutoff,))

    # ── rules (asn / ip / country) ─────────────────────────
    def upsert_asn_rule(self, asn: str, verdict: str, note: str = ""):
        self._conn().execute(
            "INSERT INTO asn_rules (asn, verdict, note) VALUES (?,?,?) "
            "ON CONFLICT(asn) DO UPDATE SET verdict=excluded.verdict, note=excluded.note",
            (asn, verdict, note))

    def delete_asn_rule(self, asn: str):
        self._conn().execute("DELETE FROM asn_rules WHERE asn=?", (asn,))

    def get_asn_rules(self) -> list:
        return [dict(r) for r in self._conn().execute(
            "SELECT * FROM asn_rules ORDER BY asn").fetchall()]

    def get_asn_rule(self, asn: str) -> Optional[dict]:
        r = self._conn().execute("SELECT * FROM asn_rules WHERE asn=?", (asn,)).fetchone()
        return dict(r) if r else None

    def upsert_ip_rule(self, ip: str, verdict: str, note: str = ""):
        self._conn().execute(
            "INSERT INTO ip_rules (ip, verdict, note) VALUES (?,?,?) "
            "ON CONFLICT(ip) DO UPDATE SET verdict=excluded.verdict, note=excluded.note",
            (ip, verdict, note))

    def delete_ip_rule(self, ip: str):
        self._conn().execute("DELETE FROM ip_rules WHERE ip=?", (ip,))

    def get_ip_rules(self) -> list:
        return [dict(r) for r in self._conn().execute(
            "SELECT * FROM ip_rules ORDER BY ip").fetchall()]

    def get_ip_rule(self, ip: str) -> Optional[dict]:
        r = self._conn().execute("SELECT * FROM ip_rules WHERE ip=?", (ip,)).fetchone()
        return dict(r) if r else None

    def upsert_country_rule(self, cc: str, delta: int, note: str = ""):
        self._conn().execute(
            "INSERT INTO country_rules (cc, score_delta, note) VALUES (?,?,?) "
            "ON CONFLICT(cc) DO UPDATE SET score_delta=excluded.score_delta, note=excluded.note",
            (cc.upper(), int(delta), note))

    def delete_country_rule(self, cc: str):
        self._conn().execute("DELETE FROM country_rules WHERE cc=?", (cc.upper(),))

    def get_country_rules(self) -> list:
        return [dict(r) for r in self._conn().execute(
            "SELECT * FROM country_rules ORDER BY cc").fetchall()]

    def get_country_rule(self, cc: str) -> Optional[dict]:
        r = self._conn().execute(
            "SELECT * FROM country_rules WHERE cc=?", (cc.upper(),)).fetchone()
        return dict(r) if r else None

    # ── ASN cache ──────────────────────────────────────────
    def get_asn_cached(self, ip: str, max_age_seconds: int = 86400 * 7) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT * FROM asn_cache WHERE ip=? AND fetched_at >= ?",
            (ip, int(time.time()) - max_age_seconds)).fetchone()
        return dict(row) if row else None

    def cache_asn(self, ip: str, asn: str, org: str, country: str, is_hosting: bool):
        self._conn().execute(
            "INSERT INTO asn_cache (ip, asn, org, country, is_hosting, fetched_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(ip) DO UPDATE SET "
            "asn=excluded.asn, org=excluded.org, country=excluded.country, "
            "is_hosting=excluded.is_hosting, fetched_at=excluded.fetched_at",
            (ip, asn, org, country, 1 if is_hosting else 0, int(time.time())))
