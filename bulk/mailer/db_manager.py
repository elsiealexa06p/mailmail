"""Bulk Mailer Database Manager.

SQLite schema for brands, domains, SMTP presets, mailing lists,
macros, message templates, and mailing state.
"""
import os
import sqlite3
import threading
import json
from typing import List, Tuple, Optional, Dict


class BulkDBManager:
    def __init__(self, db_path: str = "bulk.db"):
        self._db_path = db_path
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False
        self._ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_path, timeout=30)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA busy_timeout=10000")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _ensure_schema(self):
        with self._init_lock:
            if self._initialized:
                return
            c = self._conn()
            c.executescript("""
                CREATE TABLE IF NOT EXISTS brands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS domains (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
                    domain TEXT NOT NULL,
                    from_name TEXT DEFAULT '',
                    from_email TEXT DEFAULT '',
                    reply_to_email TEXT DEFAULT '',
                    bounce_subdomain TEXT DEFAULT 'bounce',
                    send_subdomain TEXT DEFAULT 'mail',
                    unsub_worker_deployed INTEGER DEFAULT 0,
                    unsub_domain TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(brand_id, domain)
                );

                CREATE TABLE IF NOT EXISTS smtp_presets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER DEFAULT 587,
                    username TEXT NOT NULL,
                    password TEXT NOT NULL,
                    provider_type TEXT DEFAULT 'generic',
                    daily_limit INTEGER DEFAULT 0,
                    sent_today INTEGER DEFAULT 0,
                    last_reset_date TEXT DEFAULT '',
                    proxy TEXT DEFAULT '',
                    proxy_required INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS cf_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    auth_type TEXT DEFAULT 'token',
                    api_token TEXT DEFAULT '',
                    global_api_key TEXT DEFAULT '',
                    auth_email TEXT DEFAULT '',
                    account_id TEXT DEFAULT '',
                    r2_access_key TEXT DEFAULT '',
                    r2_secret_key TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS lead_lists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    file_origin TEXT DEFAULT '',
                    lead_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    list_id INTEGER NOT NULL REFERENCES lead_lists(id) ON DELETE CASCADE,
                    email TEXT NOT NULL,
                    state TEXT DEFAULT 'PENDING',
                    error_msg TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_leads_state ON leads(state);
                CREATE INDEX IF NOT EXISTS idx_leads_list ON leads(list_id);
                CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);

                CREATE TABLE IF NOT EXISTS brand_list_usage (
                    brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
                    list_id INTEGER NOT NULL REFERENCES lead_lists(id) ON DELETE CASCADE,
                    mailing_id INTEGER,
                    used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (brand_id, list_id)
                );

                CREATE TABLE IF NOT EXISTS macros (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    values_json TEXT NOT NULL DEFAULT '[]',
                    rotate_every INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS message_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    html_files_json TEXT DEFAULT '[]',
                    html_rotate_every INTEGER DEFAULT 0,
                    pdf_path TEXT DEFAULT '',
                    pdf_macro_enabled INTEGER DEFAULT 0,
                    subject_macro TEXT DEFAULT '',
                    sender_rotate_json TEXT DEFAULT '[]',
                    sender_rotate_every INTEGER DEFAULT 0,
                    settings_json TEXT DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS mailings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT DEFAULT '',
                    brand_id INTEGER REFERENCES brands(id),
                    domain_id INTEGER REFERENCES domains(id),
                    smtp_preset_id INTEGER REFERENCES smtp_presets(id),
                    list_id INTEGER REFERENCES lead_lists(id),
                    template_id INTEGER REFERENCES message_templates(id),
                    status TEXT DEFAULT 'DRAFT',
                    total_leads INTEGER DEFAULT 0,
                    sent INTEGER DEFAULT 0,
                    failed INTEGER DEFAULT 0,
                    excluded INTEGER DEFAULT 0,
                    daily_limit INTEGER DEFAULT 0,
                    exclude_domains_json TEXT DEFAULT '[]',
                    test_email TEXT DEFAULT '',
                    test_interval INTEGER DEFAULT 0,
                    schedule_time TEXT DEFAULT '',
                    started_at TIMESTAMP,
                    paused_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    display_name TEXT DEFAULT '',
                    logo_path TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS llm_config (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    api_url TEXT DEFAULT 'https://api.openai.com/v1/chat/completions',
                    api_key TEXT DEFAULT '',
                    model TEXT DEFAULT 'gpt-4o-mini',
                    language TEXT DEFAULT 'de'
                );
                CREATE TABLE IF NOT EXISTS dynadot_config (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    api_key TEXT DEFAULT '',
                    secret TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS dynadot_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    api_key TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS purchased_domains (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT NOT NULL UNIQUE,
                    registrar TEXT DEFAULT 'dynadot',
                    cf_zone_id TEXT DEFAULT '',
                    cf_ns1 TEXT DEFAULT '',
                    cf_ns2 TEXT DEFAULT '',
                    ns_set INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'registered',
                    purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS warmup_seeds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE,
                    password TEXT NOT NULL,
                    imap_host TEXT NOT NULL,
                    imap_port INTEGER DEFAULT 993,
                    smtp_host TEXT DEFAULT '',
                    smtp_port INTEGER DEFAULT 587,
                    proxy TEXT DEFAULT '',
                    user_agent TEXT DEFAULT '',
                    open_rate REAL DEFAULT 0.7,
                    click_rate REAL DEFAULT 0.2,
                    reply_rate REAL DEFAULT 0.03,
                    active_start INTEGER DEFAULT 7,
                    active_end INTEGER DEFAULT 22,
                    is_active INTEGER DEFAULT 1,
                    last_checked TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS warmup_campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sending_domain TEXT NOT NULL,
                    smtp_preset_id INTEGER REFERENCES smtp_presets(id),
                    template_id INTEGER REFERENCES message_templates(id),
                    from_email TEXT DEFAULT '',
                    from_name TEXT DEFAULT '',
                    status TEXT DEFAULT 'IDLE',
                    curve_type TEXT DEFAULT 'turbo',
                    start_date TEXT DEFAULT '',
                    current_day INTEGER DEFAULT 0,
                    seed_pct INTEGER DEFAULT 100,
                    daily_target INTEGER DEFAULT 50,
                    sent_today INTEGER DEFAULT 0,
                    last_send_date TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS warmup_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER REFERENCES warmup_campaigns(id) ON DELETE CASCADE,
                    seed_id INTEGER REFERENCES warmup_seeds(id) ON DELETE CASCADE,
                    message_id TEXT DEFAULT '',
                    action_type TEXT NOT NULL,
                    scheduled_at TIMESTAMP NOT NULL,
                    executed_at TIMESTAMP,
                    result TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_warmup_actions_sched ON warmup_actions(scheduled_at);
                CREATE INDEX IF NOT EXISTS idx_warmup_actions_camp ON warmup_actions(campaign_id);
            """)
            c.commit()
            self._migrate(c)
            self._initialized = True

    def _migrate(self, c):
        """Add columns that may be missing in older databases."""
        existing = {r[1] for r in c.execute("PRAGMA table_info(smtp_presets)").fetchall()}
        if "proxy_required" not in existing:
            c.execute("ALTER TABLE smtp_presets ADD COLUMN proxy_required INTEGER DEFAULT 0")
        if "threads_per_smtp" not in existing:
            c.execute("ALTER TABLE smtp_presets ADD COLUMN threads_per_smtp INTEGER DEFAULT 1")
        # SES-API-Adapter: send_mode = 'smtp' (default) oder 'ses_api'.
        # Bei ses_api ist username=IAM_KEY, password=IAM_SECRET, host/port ignoriert.
        if "send_mode" not in existing:
            c.execute("ALTER TABLE smtp_presets ADD COLUMN send_mode TEXT DEFAULT 'smtp'")
        if "ses_region" not in existing:
            c.execute("ALTER TABLE smtp_presets ADD COLUMN ses_region TEXT DEFAULT ''")
        if "ses_config_set" not in existing:
            c.execute("ALTER TABLE smtp_presets ADD COLUMN ses_config_set TEXT DEFAULT ''")
        existing = {r[1] for r in c.execute("PRAGMA table_info(domains)").fetchall()}
        if "unsub_domain" not in existing:
            c.execute("ALTER TABLE domains ADD COLUMN unsub_domain TEXT DEFAULT ''")
        if "unsub_worker_deployed" not in existing:
            c.execute("ALTER TABLE domains ADD COLUMN unsub_worker_deployed INTEGER DEFAULT 0")
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "dynadot_accounts" not in tables:
            c.execute("""CREATE TABLE IF NOT EXISTS dynadot_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                api_key TEXT DEFAULT '',
                secret TEXT DEFAULT '',
                send_currency INTEGER DEFAULT 0,
                is_primary INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        else:
            da_cols = {r[1] for r in c.execute("PRAGMA table_info(dynadot_accounts)").fetchall()}
            if "is_primary" not in da_cols:
                c.execute("ALTER TABLE dynadot_accounts ADD COLUMN is_primary INTEGER DEFAULT 0")
                first = c.execute("SELECT id FROM dynadot_accounts ORDER BY id LIMIT 1").fetchone()
                if first:
                    c.execute("UPDATE dynadot_accounts SET is_primary=1 WHERE id=?", (first[0],))
            if "secret" not in da_cols:
                c.execute("ALTER TABLE dynadot_accounts ADD COLUMN secret TEXT DEFAULT ''")
            if "send_currency" not in da_cols:
                c.execute("ALTER TABLE dynadot_accounts ADD COLUMN send_currency INTEGER DEFAULT 0")
        # Cloudinary
        if "bulk_cloudinary_config" not in tables:
            c.execute("""CREATE TABLE bulk_cloudinary_config (
                id INTEGER PRIMARY KEY CHECK(id=1),
                cloud_name TEXT DEFAULT '',
                api_key TEXT DEFAULT '',
                api_secret TEXT DEFAULT ''
            )""")
        if "bulk_cloudinary_uploads" not in tables:
            c.execute("""CREATE TABLE bulk_cloudinary_uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_filename TEXT NOT NULL,
                base_public_id TEXT NOT NULL,
                folder TEXT DEFAULT '',
                count INTEGER DEFAULT 1,
                pixel_tweak INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        # Warmup — neue Campaign-Spalten
        wc_cols = {r[1] for r in c.execute("PRAGMA table_info(warmup_campaigns)").fetchall()}
        if "daily_fixed_target" not in wc_cols:
            c.execute("ALTER TABLE warmup_campaigns ADD COLUMN daily_fixed_target INTEGER DEFAULT 0")
        if "pdf_attach_pct" not in wc_cols:
            c.execute("ALTER TABLE warmup_campaigns ADD COLUMN pdf_attach_pct INTEGER DEFAULT 0")
        if "reply_mode" not in wc_cols:
            c.execute("ALTER TABLE warmup_campaigns ADD COLUMN reply_mode TEXT DEFAULT 'template'")
        # Warmup PDF-Pool (uploaded PDFs die random attached werden)
        if "warmup_pdfs" not in tables:
            c.execute("""CREATE TABLE warmup_pdfs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                file_path TEXT NOT NULL,
                size INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")

        # Spaceship Registrar Accounts (parallel zu Dynadot)
        if "spaceship_accounts" not in tables:
            c.execute("""CREATE TABLE spaceship_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                api_key TEXT DEFAULT '',
                api_secret TEXT DEFAULT '',
                is_primary INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")

        # Porkbun Registrar Accounts
        if "porkbun_accounts" not in tables:
            c.execute("""CREATE TABLE porkbun_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                api_key TEXT DEFAULT '',
                api_secret TEXT DEFAULT '',
                is_primary INTEGER DEFAULT 0,
                proxy TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        else:
            pb_cols = {r[1] for r in c.execute("PRAGMA table_info(porkbun_accounts)").fetchall()}
            if "proxy" not in pb_cols:
                c.execute("ALTER TABLE porkbun_accounts ADD COLUMN proxy TEXT DEFAULT ''")

        # Registrar-Proxy nachrüsten für Dynadot + Spaceship (analog Porkbun)
        for _tbl in ("dynadot_accounts", "spaceship_accounts"):
            _cols = {r[1] for r in c.execute(f"PRAGMA table_info({_tbl})").fetchall()}
            if _cols and "proxy" not in _cols:
                c.execute(f"ALTER TABLE {_tbl} ADD COLUMN proxy TEXT DEFAULT ''")

        # PDF Variator pools (kv-store, ein Eintrag pro Pool-Typ)
        if "pdf_variator_pools" not in tables:
            c.execute("""CREATE TABLE pdf_variator_pools (
                pool_key TEXT PRIMARY KEY,
                content TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        if "bulk_cloudinary_links" not in tables:
            c.execute("""CREATE TABLE bulk_cloudinary_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                upload_id INTEGER REFERENCES bulk_cloudinary_uploads(id) ON DELETE CASCADE,
                public_id TEXT NOT NULL,
                secure_url TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")

        # AWS S3 Logo Uploader — jeder Batch landet in einem NEUEN Bucket
        # (globally unique), um Spam-Reputation pro Bucket zu isolieren.
        if "bulk_s3_config" not in tables:
            c.execute("""CREATE TABLE bulk_s3_config (
                id INTEGER PRIMARY KEY CHECK(id=1),
                access_key TEXT DEFAULT '',
                secret_key TEXT DEFAULT '',
                region TEXT DEFAULT 'us-east-1',
                bucket_prefix TEXT DEFAULT ''
            )""")
            c.execute("INSERT OR IGNORE INTO bulk_s3_config (id) VALUES (1)")
        if "bulk_s3_uploads" not in tables:
            c.execute("""CREATE TABLE bulk_s3_uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_filename TEXT NOT NULL,
                base_name TEXT NOT NULL,
                bucket_name TEXT NOT NULL,
                region TEXT DEFAULT 'us-east-1',
                count INTEGER DEFAULT 1,
                pixel_tweak INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        if "bulk_s3_links" not in tables:
            c.execute("""CREATE TABLE bulk_s3_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                upload_id INTEGER REFERENCES bulk_s3_uploads(id) ON DELETE CASCADE,
                object_key TEXT NOT NULL,
                public_url TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")

        # DBL Watcher — domains auf Spamhaus DBL / SURBL / URIBL beobachten,
        # periodisch per DNS prüfen, Telegram-Alert bei neuen Listings.
        if "dbl_watch" not in tables:
            c.execute("""CREATE TABLE dbl_watch (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL UNIQUE,
                enabled INTEGER DEFAULT 1,
                status TEXT DEFAULT 'pending',      -- pending | ok | listed | error
                detail TEXT DEFAULT '',             -- JSON: {"dbl": "127.0.1.2", "surbl": null, "uribl": null, "error": ""}
                last_checked_at TIMESTAMP,
                listed_since TIMESTAMP,
                last_notified_at TIMESTAMP,
                note TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        if "dbl_settings" not in tables:
            c.execute("""CREATE TABLE dbl_settings (
                id INTEGER PRIMARY KEY CHECK(id=1),
                enabled INTEGER DEFAULT 1,
                check_interval_minutes INTEGER DEFAULT 60,
                telegram_bot_token TEXT DEFAULT '',
                telegram_chat_id TEXT DEFAULT '',
                resolvers TEXT DEFAULT '',          -- komma-separiert, leer = System-Default
                dqs_key TEXT DEFAULT '',            -- optional: Spamhaus Data Query Service key
                check_dbl INTEGER DEFAULT 1,
                check_surbl INTEGER DEFAULT 0,
                check_uribl INTEGER DEFAULT 0,
                renotify_hours INTEGER DEFAULT 24,  -- bei anhaltendem Listing erneut benachrichtigen
                last_run_at TIMESTAMP,
                last_run_summary TEXT DEFAULT ''
            )""")
            c.execute("INSERT OR IGNORE INTO dbl_settings (id) VALUES (1)")
        c.commit()

    # --- Users / Auth ---
    def get_user(self, username: str):
        return self._conn().execute(
            "SELECT * FROM users WHERE username=?", (username,)).fetchone()

    def get_user_by_id(self, uid: int):
        return self._conn().execute(
            "SELECT * FROM users WHERE id=?", (uid,)).fetchone()

    def create_user(self, username: str, password_hash: str,
                    display_name: str = "") -> int:
        c = self._conn()
        c.execute("INSERT INTO users (username, password_hash, display_name) "
                  "VALUES (?,?,?)", (username, password_hash, display_name))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def update_user_profile(self, uid: int, display_name: str = "",
                            logo_path: str = ""):
        c = self._conn()
        c.execute("UPDATE users SET display_name=?, logo_path=? WHERE id=?",
                  (display_name, logo_path, uid))
        c.commit()

    def update_user_password(self, uid: int, password_hash: str):
        c = self._conn()
        c.execute("UPDATE users SET password_hash=? WHERE id=?",
                  (password_hash, uid))
        c.commit()

    def get_all_users(self) -> list:
        return self._conn().execute(
            "SELECT id, username, display_name, logo_path, created_at "
            "FROM users ORDER BY username").fetchall()

    def delete_user(self, uid: int):
        c = self._conn()
        c.execute("DELETE FROM users WHERE id=?", (uid,))
        c.commit()

    def user_count(self) -> int:
        r = self._conn().execute("SELECT COUNT(*) FROM users").fetchone()
        return r[0] if r else 0

    # --- Brands ---
    def add_brand(self, name: str) -> int:
        c = self._conn()
        c.execute("INSERT INTO brands (name) VALUES (?)", (name,))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_brands(self) -> list:
        return self._conn().execute("SELECT * FROM brands ORDER BY name").fetchall()

    def delete_brand(self, brand_id: int):
        c = self._conn()
        c.execute("DELETE FROM brands WHERE id=?", (brand_id,))
        c.commit()

    # --- Domains ---
    def add_domain(self, brand_id: int, domain: str, **kw) -> int:
        c = self._conn()
        cols = ["brand_id", "domain"] + list(kw.keys())
        vals = [brand_id, domain] + list(kw.values())
        ph = ",".join("?" for _ in vals)
        c.execute(f"INSERT INTO domains ({','.join(cols)}) VALUES ({ph})", vals)
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_domains(self, brand_id: int = None) -> list:
        if brand_id:
            return self._conn().execute("SELECT * FROM domains WHERE brand_id=?", (brand_id,)).fetchall()
        return self._conn().execute("SELECT * FROM domains ORDER BY domain").fetchall()

    def delete_domain(self, domain_id: int):
        c = self._conn()
        c.execute("DELETE FROM domains WHERE id=?", (domain_id,))
        c.commit()

    # --- SMTP Presets ---
    def add_smtp(self, name: str, host: str, port: int, username: str,
                 password: str, provider_type: str = "generic",
                 daily_limit: int = 0, proxy: str = "",
                 proxy_required: int = 0) -> int:
        c = self._conn()
        c.execute("INSERT INTO smtp_presets (name,host,port,username,password,provider_type,"
                  "daily_limit,proxy,proxy_required) VALUES (?,?,?,?,?,?,?,?,?)",
                  (name, host, port, username, password, provider_type,
                   daily_limit, proxy, proxy_required))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_smtps(self) -> list:
        return self._conn().execute("SELECT * FROM smtp_presets ORDER BY name").fetchall()

    def add_ses_smtp(self, name: str, iam_key: str, iam_secret: str,
                      region: str = "eu-central-1", config_set: str = "",
                      daily_limit: int = 0) -> int:
        """SES-API-Preset. username=IAM_KEY, password=IAM_SECRET,
        host='ses-api' als Marker."""
        c = self._conn()
        c.execute(
            "INSERT INTO smtp_presets "
            "(name,host,port,username,password,provider_type,daily_limit,"
            " proxy,proxy_required,send_mode,ses_region,ses_config_set) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, "ses-api", 0, iam_key, iam_secret, "amazon_ses",
             daily_limit, "", 0, "ses_api", region, config_set))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def import_ses_smtps(self, text: str,
                          default_region: str = "eu-central-1",
                          default_config_set: str = "",
                          default_name_prefix: str = "SES-",
                          daily_limit: int = 0) -> int:
        """Zeilenformat: IAM_KEY,IAM_SECRET[,region[,config_set[,name]]]
        Pipe (|) statt Komma auch erlaubt."""
        added = 0
        for i, line in enumerate(text.splitlines()):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            sep = "|" if "|" in line else ","
            parts = [p.strip() for p in line.split(sep)]
            if len(parts) < 2:
                continue
            iam_key, iam_secret = parts[0], parts[1]
            if not iam_key or not iam_secret:
                continue
            region = parts[2] if len(parts) >= 3 and parts[2] else default_region
            cfg_set = parts[3] if len(parts) >= 4 else default_config_set
            name = parts[4] if len(parts) >= 5 and parts[4] \
                   else f"{default_name_prefix}{iam_key[:8]}"
            try:
                self.add_ses_smtp(name, iam_key, iam_secret,
                                   region=region, config_set=cfg_set,
                                   daily_limit=daily_limit)
                added += 1
            except Exception:
                pass
        return added

    def delete_smtp(self, smtp_id: int):
        c = self._conn()
        c.execute("DELETE FROM smtp_presets WHERE id=?", (smtp_id,))
        c.commit()

    def reset_daily_counts(self):
        import datetime
        today = datetime.date.today().isoformat()
        c = self._conn()
        c.execute("UPDATE smtp_presets SET sent_today=0, last_reset_date=? "
                  "WHERE last_reset_date != ?", (today, today))
        c.commit()

    def increment_smtp_sent(self, smtp_id: int):
        c = self._conn()
        c.execute("UPDATE smtp_presets SET sent_today=sent_today+1 WHERE id=?", (smtp_id,))
        c.commit()

    def get_smtp_remaining(self, smtp_id: int) -> int:
        r = self._conn().execute("SELECT daily_limit, sent_today FROM smtp_presets WHERE id=?",
                                  (smtp_id,)).fetchone()
        if not r or r["daily_limit"] == 0:
            return 999999
        return max(0, r["daily_limit"] - r["sent_today"])

    # --- Lead Lists ---
    def create_list(self, name: str, file_origin: str = "") -> int:
        c = self._conn()
        c.execute("INSERT INTO lead_lists (name, file_origin) VALUES (?,?)", (name, file_origin))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def import_leads(self, list_id: int, emails: List[str], exclude_domains: List[str] = None) -> int:
        c = self._conn()
        added = 0
        batch = []
        for email in emails:
            email = email.strip().lower()
            if not email or "@" not in email:
                continue
            if exclude_domains:
                domain = email.split("@")[1]
                if domain in exclude_domains:
                    continue
            batch.append((list_id, email))
            if len(batch) >= 500:
                c.executemany("INSERT INTO leads (list_id, email) VALUES (?,?)", batch)
                added += len(batch)
                batch = []
        if batch:
            c.executemany("INSERT INTO leads (list_id, email) VALUES (?,?)", batch)
            added += len(batch)
        c.execute("UPDATE lead_lists SET lead_count=? WHERE id=?", (added, list_id))
        c.commit()
        return added

    def get_lists(self) -> list:
        return self._conn().execute("SELECT * FROM lead_lists ORDER BY name").fetchall()

    def delete_list(self, list_id: int):
        c = self._conn()
        c.execute("DELETE FROM lead_lists WHERE id=?", (list_id,))
        c.commit()

    def search_leads(self, list_id: int, query: str) -> list:
        return self._conn().execute(
            "SELECT * FROM leads WHERE list_id=? AND email LIKE ? LIMIT 500",
            (list_id, f"%{query}%")).fetchall()

    def delete_leads_by_domain(self, list_id: int, domain: str) -> int:
        c = self._conn()
        c.execute("DELETE FROM leads WHERE list_id=? AND email LIKE ?",
                  (list_id, f"%@{domain}"))
        deleted = c.execute("SELECT changes()").fetchone()[0]
        c.execute("UPDATE lead_lists SET lead_count=(SELECT COUNT(*) FROM leads WHERE list_id=?) WHERE id=?",
                  (list_id, list_id))
        c.commit()
        return deleted

    def delete_leads_by_ids(self, lead_ids: List[int]):
        c = self._conn()
        ph = ",".join("?" for _ in lead_ids)
        c.execute(f"DELETE FROM leads WHERE id IN ({ph})", lead_ids)
        c.commit()

    def get_list_lead_count(self, list_id: int) -> int:
        r = self._conn().execute("SELECT COUNT(*) FROM leads WHERE list_id=?", (list_id,)).fetchone()
        return r[0] if r else 0

    def fetch_pending(self, list_id: int, exclude_domains: List[str] = None,
                      batch_size: int = 200) -> List[Tuple[int, str]]:
        c = self._conn()
        rows = c.execute(
            "SELECT id, email FROM leads WHERE list_id=? AND state='PENDING' ORDER BY id LIMIT ?",
            (list_id, batch_size)).fetchall()
        if not exclude_domains:
            return [(r["id"], r["email"]) for r in rows]
        return [(r["id"], r["email"]) for r in rows
                if r["email"].split("@")[1] not in exclude_domains]

    def mark_sent(self, lead_id: int):
        c = self._conn()
        c.execute("UPDATE leads SET state='SENT', updated_at=CURRENT_TIMESTAMP WHERE id=?", (lead_id,))
        c.commit()

    def mark_failed(self, lead_id: int, error: str = ""):
        c = self._conn()
        c.execute("UPDATE leads SET state='FAILED', error_msg=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                  (error[:500], lead_id))
        c.commit()

    def mark_excluded(self, lead_id: int):
        c = self._conn()
        c.execute("UPDATE leads SET state='EXCLUDED', updated_at=CURRENT_TIMESTAMP WHERE id=?", (lead_id,))
        c.commit()

    def mark_in_progress(self, lead_ids: List[int]):
        if not lead_ids:
            return
        c = self._conn()
        ph = ",".join("?" for _ in lead_ids)
        c.execute(f"UPDATE leads SET state='IN_PROGRESS', updated_at=CURRENT_TIMESTAMP WHERE id IN ({ph})", lead_ids)
        c.commit()

    def reset_in_progress(self, list_id: int = None):
        c = self._conn()
        if list_id:
            c.execute("UPDATE leads SET state='PENDING' WHERE state='IN_PROGRESS' AND list_id=?", (list_id,))
        else:
            c.execute("UPDATE leads SET state='PENDING' WHERE state='IN_PROGRESS'")
        c.commit()

    def mailing_stats(self, list_id: int) -> dict:
        c = self._conn()
        rows = c.execute("SELECT state, COUNT(*) FROM leads WHERE list_id=? GROUP BY state",
                         (list_id,)).fetchall()
        stats = {"PENDING": 0, "SENT": 0, "FAILED": 0, "EXCLUDED": 0, "IN_PROGRESS": 0, "total": 0}
        for r in rows:
            stats[r[0]] = r[1]
            stats["total"] += r[1]
        return stats

    # --- Brand List Usage ---
    def mark_list_used(self, brand_id: int, list_id: int, mailing_id: int = None):
        c = self._conn()
        c.execute("INSERT OR REPLACE INTO brand_list_usage (brand_id, list_id, mailing_id) VALUES (?,?,?)",
                  (brand_id, list_id, mailing_id))
        c.commit()

    def get_used_lists(self, brand_id: int) -> list:
        return self._conn().execute(
            "SELECT l.* FROM lead_lists l JOIN brand_list_usage u ON l.id=u.list_id WHERE u.brand_id=?",
            (brand_id,)).fetchall()

    def get_unused_lists(self, brand_id: int) -> list:
        return self._conn().execute(
            "SELECT * FROM lead_lists WHERE id NOT IN "
            "(SELECT list_id FROM brand_list_usage WHERE brand_id=?)", (brand_id,)).fetchall()

    # --- Macros ---
    def add_macro(self, name: str, values: list) -> int:
        c = self._conn()
        c.execute("INSERT INTO macros (name, values_json) VALUES (?,?)",
                  (name, json.dumps(values, ensure_ascii=False)))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_macros(self) -> list:
        return self._conn().execute("SELECT * FROM macros ORDER BY name").fetchall()

    def get_macro_values(self, name: str) -> list:
        r = self._conn().execute("SELECT values_json FROM macros WHERE name=?", (name,)).fetchone()
        return json.loads(r["values_json"]) if r else []

    def update_macro(self, macro_id: int, values: list):
        c = self._conn()
        c.execute("UPDATE macros SET values_json=? WHERE id=?",
                  (json.dumps(values, ensure_ascii=False), macro_id))
        c.commit()

    def delete_macro(self, macro_id: int):
        c = self._conn()
        c.execute("DELETE FROM macros WHERE id=?", (macro_id,))
        c.commit()

    def export_macros(self) -> str:
        rows = self.get_macros()
        data = {r["name"]: json.loads(r["values_json"]) for r in rows}
        return json.dumps(data, indent=2, ensure_ascii=False)

    def import_macros(self, json_str: str) -> int:
        data = json.loads(json_str)
        c = self._conn()
        added = 0
        for name, values in data.items():
            c.execute("INSERT OR REPLACE INTO macros (name, values_json) VALUES (?,?)",
                      (name, json.dumps(values, ensure_ascii=False)))
            added += 1
        c.commit()
        return added

    # --- Message Templates ---
    def add_template(self, name: str, **kw) -> int:
        c = self._conn()
        cols = ["name"] + [k for k in kw if k in (
            "html_files_json", "html_rotate_every", "pdf_path", "pdf_macro_enabled",
            "subject_macro", "sender_rotate_json", "sender_rotate_every", "settings_json")]
        vals = [name] + [kw[k] for k in cols[1:]]
        ph = ",".join("?" for _ in vals)
        c.execute(f"INSERT INTO message_templates ({','.join(cols)}) VALUES ({ph})", vals)
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_templates(self) -> list:
        return self._conn().execute("SELECT * FROM message_templates ORDER BY name").fetchall()

    def delete_template(self, template_id: int):
        c = self._conn()
        c.execute("DELETE FROM message_templates WHERE id=?", (template_id,))
        c.commit()

    # --- Mailings ---
    def create_mailing(self, **kw) -> int:
        c = self._conn()
        cols = [k for k in kw]
        vals = [kw[k] for k in cols]
        ph = ",".join("?" for _ in vals)
        c.execute(f"INSERT INTO mailings ({','.join(cols)}) VALUES ({ph})", vals)
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_mailings(self) -> list:
        return self._conn().execute("SELECT * FROM mailings ORDER BY created_at DESC").fetchall()

    def update_mailing_status(self, mailing_id: int, status: str):
        c = self._conn()
        ts_col = {"RUNNING": "started_at", "PAUSED": "paused_at", "FINISHED": "finished_at"}.get(status)
        if ts_col:
            c.execute(f"UPDATE mailings SET status=?, {ts_col}=CURRENT_TIMESTAMP WHERE id=?",
                      (status, mailing_id))
        else:
            c.execute("UPDATE mailings SET status=? WHERE id=?", (status, mailing_id))
        c.commit()

    def update_mailing_counts(self, mailing_id: int, sent: int, failed: int, excluded: int):
        c = self._conn()
        c.execute("UPDATE mailings SET sent=?, failed=?, excluded=? WHERE id=?",
                  (sent, failed, excluded, mailing_id))
        c.commit()

    # --- Backup ---
    def export_all(self) -> str:
        data = {
            "brands": [dict(r) for r in self.get_brands()],
            "domains": [dict(r) for r in self.get_domains()],
            "smtp_presets": [dict(r) for r in self.get_smtps()],
            "macros": {r["name"]: json.loads(r["values_json"]) for r in self.get_macros()},
            "templates": [dict(r) for r in self.get_templates()],
            "lead_lists": [dict(r) for r in self.get_lists()],
        }
        return json.dumps(data, indent=2, ensure_ascii=False, default=str)

    # --- Cloudflare Accounts ---
    def add_cf_account(self, name: str, auth_type: str = "token",
                       api_token: str = "", global_api_key: str = "",
                       auth_email: str = "", account_id: str = "",
                       r2_access_key: str = "", r2_secret_key: str = "") -> int:
        c = self._conn()
        c.execute("INSERT INTO cf_accounts (name,auth_type,api_token,global_api_key,"
                  "auth_email,account_id,r2_access_key,r2_secret_key) "
                  "VALUES (?,?,?,?,?,?,?,?)",
                  (name, auth_type, api_token, global_api_key, auth_email,
                   account_id, r2_access_key, r2_secret_key))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_cf_accounts(self) -> list:
        return self._conn().execute("SELECT * FROM cf_accounts ORDER BY name").fetchall()

    def delete_cf_account(self, cf_id: int):
        c = self._conn()
        c.execute("DELETE FROM cf_accounts WHERE id=?", (cf_id,))
        c.commit()

    def update_cf_account(self, cf_id: int, **kw):
        c = self._conn()
        sets = ", ".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE cf_accounts SET {sets} WHERE id=?", list(kw.values()) + [cf_id])
        c.commit()

    # --- Domain Unsub Status ---
    def mark_unsub_deployed(self, domain_id: int, unsub_domain: str):
        c = self._conn()
        c.execute("UPDATE domains SET unsub_worker_deployed=1, unsub_domain=? WHERE id=?",
                  (unsub_domain, domain_id))
        c.commit()

    def is_unsub_deployed(self, domain_id: int) -> bool:
        r = self._conn().execute("SELECT unsub_worker_deployed FROM domains WHERE id=?",
                                  (domain_id,)).fetchone()
        return bool(r and r[0])

    def get_unsub_domain(self, domain_id: int) -> str:
        r = self._conn().execute("SELECT unsub_domain FROM domains WHERE id=?",
                                  (domain_id,)).fetchone()
        return r["unsub_domain"] if r and r["unsub_domain"] else ""

    # --- List-ID Generation ---
    @staticmethod
    def generate_list_id(domain: str, campaign_id: int = 0) -> str:
        import secrets
        short = secrets.token_hex(4)
        return f"c{campaign_id}-{short}.{domain}"

    # --- Warmup Seeds ---
    def add_seed(self, provider: str, email: str, password: str,
                 imap_host: str, imap_port: int = 993,
                 smtp_host: str = "", smtp_port: int = 587,
                 proxy: str = "", **kw) -> int:
        c = self._conn()
        cols = ["provider", "email", "password", "imap_host", "imap_port",
                "smtp_host", "smtp_port", "proxy"]
        vals = [provider, email, password, imap_host, imap_port,
                smtp_host, smtp_port, proxy]
        for k in ("user_agent", "open_rate", "click_rate", "reply_rate",
                   "active_start", "active_end"):
            if k in kw:
                cols.append(k)
                vals.append(kw[k])
        ph = ",".join("?" for _ in vals)
        c.execute(f"INSERT OR IGNORE INTO warmup_seeds ({','.join(cols)}) VALUES ({ph})", vals)
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_seeds(self, active_only: bool = False) -> list:
        sql = "SELECT * FROM warmup_seeds"
        if active_only:
            sql += " WHERE is_active=1"
        return self._conn().execute(sql + " ORDER BY provider, email").fetchall()

    def update_seed(self, seed_id: int, **kw):
        c = self._conn()
        sets = ", ".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE warmup_seeds SET {sets} WHERE id=?",
                  list(kw.values()) + [seed_id])
        c.commit()

    def delete_seed(self, seed_id: int):
        c = self._conn()
        c.execute("DELETE FROM warmup_seeds WHERE id=?", (seed_id,))
        c.commit()

    # --- Warmup Campaigns ---
    def create_warmup_campaign(self, **kw) -> int:
        c = self._conn()
        cols = list(kw.keys())
        vals = list(kw.values())
        ph = ",".join("?" for _ in vals)
        c.execute(f"INSERT INTO warmup_campaigns ({','.join(cols)}) VALUES ({ph})", vals)
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_warmup_campaigns(self) -> list:
        return self._conn().execute(
            "SELECT * FROM warmup_campaigns ORDER BY created_at DESC").fetchall()

    def get_warmup_campaign(self, cid: int):
        return self._conn().execute(
            "SELECT * FROM warmup_campaigns WHERE id=?", (cid,)).fetchone()

    def update_warmup_campaign(self, cid: int, **kw):
        c = self._conn()
        sets = ", ".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE warmup_campaigns SET {sets} WHERE id=?",
                  list(kw.values()) + [cid])
        c.commit()

    def delete_warmup_campaign(self, cid: int):
        c = self._conn()
        c.execute("DELETE FROM warmup_campaigns WHERE id=?", (cid,))
        c.commit()

    # --- Warmup Actions ---
    def schedule_warmup_action(self, campaign_id: int, seed_id: int,
                                action_type: str, scheduled_at: str,
                                message_id: str = "") -> int:
        c = self._conn()
        c.execute("INSERT INTO warmup_actions (campaign_id,seed_id,action_type,"
                  "scheduled_at,message_id) VALUES (?,?,?,?,?)",
                  (campaign_id, seed_id, action_type, scheduled_at, message_id))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_pending_actions(self, limit: int = 50) -> list:
        return self._conn().execute(
            "SELECT a.*, s.email, s.password, s.imap_host, s.imap_port, "
            "s.smtp_host, s.smtp_port, s.proxy, s.provider, s.user_agent "
            "FROM warmup_actions a JOIN warmup_seeds s ON a.seed_id=s.id "
            "WHERE a.executed_at IS NULL AND a.scheduled_at <= datetime('now') "
            "ORDER BY a.scheduled_at LIMIT ?", (limit,)).fetchall()

    def mark_action_done(self, action_id: int, result: str = "ok"):
        c = self._conn()
        c.execute("UPDATE warmup_actions SET executed_at=CURRENT_TIMESTAMP, "
                  "result=? WHERE id=?", (result, action_id))
        c.commit()

    def get_warmup_log(self, campaign_id: int = 0, limit: int = 100) -> list:
        if campaign_id:
            return self._conn().execute(
                "SELECT a.*, s.email FROM warmup_actions a "
                "JOIN warmup_seeds s ON a.seed_id=s.id "
                "WHERE a.campaign_id=? ORDER BY a.scheduled_at DESC LIMIT ?",
                (campaign_id, limit)).fetchall()
        return self._conn().execute(
            "SELECT a.*, s.email FROM warmup_actions a "
            "JOIN warmup_seeds s ON a.seed_id=s.id "
            "ORDER BY a.scheduled_at DESC LIMIT ?", (limit,)).fetchall()

    # --- LLM ---
    def get_llm_config(self) -> dict:
        c = self._conn()
        r = c.execute("SELECT * FROM llm_config WHERE id=1").fetchone()
        if not r:
            c.execute("INSERT OR IGNORE INTO llm_config (id) VALUES (1)")
            c.commit()
            return {"api_url": "https://api.openai.com/v1/chat/completions",
                    "api_key": "", "model": "gpt-4o-mini", "language": "de"}
        return dict(r)

    def save_llm_config(self, api_url: str, api_key: str, model: str, language: str = "de"):
        c = self._conn()
        c.execute("INSERT OR REPLACE INTO llm_config (id, api_url, api_key, model, language) "
                  "VALUES (1,?,?,?,?)", (api_url, api_key, model, language))
        c.commit()

    # --- Dynadot ---
    # --- Dynadot Accounts ---
    def add_dynadot_account(self, name: str, api_key: str,
                            secret: str = "", send_currency: int = 0) -> int:
        c = self._conn()
        # First account becomes primary automatically
        existing = c.execute("SELECT COUNT(*) FROM dynadot_accounts").fetchone()[0]
        is_primary = 1 if existing == 0 else 0
        c.execute("INSERT INTO dynadot_accounts (name, api_key, secret, send_currency, is_primary) VALUES (?,?,?,?,?)",
                  (name, api_key, secret, send_currency, is_primary))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def update_dynadot_account(self, aid: int, **fields):
        if not fields:
            return
        c = self._conn()
        sets = ", ".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE dynadot_accounts SET {sets} WHERE id=?",
                  list(fields.values()) + [aid])
        c.commit()

    def get_dynadot_accounts(self) -> list:
        return self._conn().execute(
            "SELECT * FROM dynadot_accounts ORDER BY is_primary DESC, name").fetchall()

    def get_dynadot_account(self, aid: int):
        return self._conn().execute(
            "SELECT * FROM dynadot_accounts WHERE id=?", (aid,)).fetchone()

    def get_primary_dynadot_account(self):
        row = self._conn().execute(
            "SELECT * FROM dynadot_accounts WHERE is_primary=1 LIMIT 1").fetchone()
        if row:
            return row
        return self._conn().execute(
            "SELECT * FROM dynadot_accounts ORDER BY id LIMIT 1").fetchone()

    def set_primary_dynadot_account(self, aid: int):
        c = self._conn()
        c.execute("UPDATE dynadot_accounts SET is_primary=0")
        c.execute("UPDATE dynadot_accounts SET is_primary=1 WHERE id=?", (aid,))
        c.commit()

    def delete_dynadot_account(self, aid: int):
        c = self._conn()
        was_primary = c.execute(
            "SELECT is_primary FROM dynadot_accounts WHERE id=?", (aid,)).fetchone()
        c.execute("DELETE FROM dynadot_accounts WHERE id=?", (aid,))
        if was_primary and was_primary[0]:
            first = c.execute("SELECT id FROM dynadot_accounts ORDER BY id LIMIT 1").fetchone()
            if first:
                c.execute("UPDATE dynadot_accounts SET is_primary=1 WHERE id=?", (first[0],))
        c.commit()

    def get_dynadot_config(self) -> dict:
        c = self._conn()
        r = c.execute("SELECT * FROM dynadot_config WHERE id=1").fetchone()
        if not r:
            c.execute("INSERT OR IGNORE INTO dynadot_config (id, api_key, secret) VALUES (1,'','')")
            c.commit()
            return {"api_key": "", "secret": ""}
        return dict(r)

    def save_dynadot_config(self, api_key: str, secret: str):
        c = self._conn()
        c.execute("INSERT OR REPLACE INTO dynadot_config (id, api_key, secret) VALUES (1,?,?)",
                  (api_key, secret))
        c.commit()

    def add_purchased_domain(self, domain: str, cf_zone_id: str = "",
                              cf_ns1: str = "", cf_ns2: str = "") -> int:
        c = self._conn()
        c.execute("INSERT OR IGNORE INTO purchased_domains "
                  "(domain, cf_zone_id, cf_ns1, cf_ns2) VALUES (?,?,?,?)",
                  (domain, cf_zone_id, cf_ns1, cf_ns2))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_purchased_domains(self) -> list:
        return self._conn().execute(
            "SELECT * FROM purchased_domains ORDER BY purchased_at DESC").fetchall()

    def update_purchased_domain(self, domain: str, **kw):
        c = self._conn()
        sets = ", ".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE purchased_domains SET {sets} WHERE domain=?",
                  list(kw.values()) + [domain])
        c.commit()

    def close(self):
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

    # ── Cloudinary (bulk) ────────────────────────────────
    def get_cloudinary_config(self) -> dict:
        c = self._conn()
        r = c.execute("SELECT * FROM bulk_cloudinary_config WHERE id=1").fetchone()
        if not r:
            c.execute("INSERT OR IGNORE INTO bulk_cloudinary_config (id) VALUES (1)")
            c.commit()
            return {"cloud_name": "", "api_key": "", "api_secret": ""}
        return dict(r)

    def save_cloudinary_config(self, cloud_name: str, api_key: str, api_secret: str):
        c = self._conn()
        c.execute(
            "INSERT OR REPLACE INTO bulk_cloudinary_config (id, cloud_name, api_key, api_secret) "
            "VALUES (1, ?, ?, ?)",
            (cloud_name, api_key, api_secret))
        c.commit()

    def add_cloudinary_upload(self, source_filename: str, base_public_id: str,
                               folder: str, count: int, pixel_tweak: int) -> int:
        c = self._conn()
        c.execute(
            "INSERT INTO bulk_cloudinary_uploads "
            "(source_filename, base_public_id, folder, count, pixel_tweak) "
            "VALUES (?,?,?,?,?)",
            (source_filename, base_public_id, folder, count, pixel_tweak))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def add_cloudinary_link(self, upload_id: int, public_id: str, secure_url: str):
        c = self._conn()
        c.execute(
            "INSERT INTO bulk_cloudinary_links (upload_id, public_id, secure_url) VALUES (?,?,?)",
            (upload_id, public_id, secure_url))
        c.commit()

    def get_cloudinary_uploads(self) -> list:
        return self._conn().execute(
            "SELECT * FROM bulk_cloudinary_uploads ORDER BY id DESC").fetchall()

    def get_cloudinary_links(self, upload_id: int) -> list:
        return self._conn().execute(
            "SELECT * FROM bulk_cloudinary_links WHERE upload_id=? ORDER BY id",
            (upload_id,)).fetchall()

    def delete_cloudinary_upload(self, upload_id: int):
        c = self._conn()
        c.execute("DELETE FROM bulk_cloudinary_links WHERE upload_id=?", (upload_id,))
        c.execute("DELETE FROM bulk_cloudinary_uploads WHERE id=?", (upload_id,))
        c.commit()

    # ── AWS S3 Logo Uploader (ein neuer Bucket pro Batch) ─────────
    def get_s3_config(self) -> dict:
        c = self._conn()
        r = c.execute("SELECT * FROM bulk_s3_config WHERE id=1").fetchone()
        if not r:
            c.execute("INSERT OR IGNORE INTO bulk_s3_config (id) VALUES (1)")
            c.commit()
            return {"access_key": "", "secret_key": "", "region": "us-east-1", "bucket_prefix": ""}
        return dict(r)

    def save_s3_config(self, access_key: str, secret_key: str,
                       region: str = "us-east-1", bucket_prefix: str = ""):
        c = self._conn()
        c.execute(
            "INSERT OR REPLACE INTO bulk_s3_config "
            "(id, access_key, secret_key, region, bucket_prefix) VALUES (1, ?, ?, ?, ?)",
            (access_key, secret_key, region, bucket_prefix))
        c.commit()

    def add_s3_upload(self, source_filename: str, base_name: str,
                      bucket_name: str, region: str,
                      count: int, pixel_tweak: int) -> int:
        c = self._conn()
        c.execute(
            "INSERT INTO bulk_s3_uploads "
            "(source_filename, base_name, bucket_name, region, count, pixel_tweak) "
            "VALUES (?,?,?,?,?,?)",
            (source_filename, base_name, bucket_name, region, count, pixel_tweak))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def add_s3_link(self, upload_id: int, object_key: str, public_url: str):
        c = self._conn()
        c.execute(
            "INSERT INTO bulk_s3_links (upload_id, object_key, public_url) VALUES (?,?,?)",
            (upload_id, object_key, public_url))
        c.commit()

    def get_s3_uploads(self) -> list:
        return self._conn().execute(
            "SELECT * FROM bulk_s3_uploads ORDER BY id DESC").fetchall()

    def get_s3_upload(self, upload_id: int):
        return self._conn().execute(
            "SELECT * FROM bulk_s3_uploads WHERE id=?", (upload_id,)).fetchone()

    def get_s3_links(self, upload_id: int) -> list:
        return self._conn().execute(
            "SELECT * FROM bulk_s3_links WHERE upload_id=? ORDER BY id",
            (upload_id,)).fetchall()

    def delete_s3_upload(self, upload_id: int):
        c = self._conn()
        c.execute("DELETE FROM bulk_s3_links WHERE upload_id=?", (upload_id,))
        c.execute("DELETE FROM bulk_s3_uploads WHERE id=?", (upload_id,))
        c.commit()

    # ── PDF Variator Pools (persistent per-Panel) ──────────
    def get_variator_pools(self) -> dict:
        """Alle gespeicherten Pools als {pool_key: content_string}."""
        rows = self._conn().execute(
            "SELECT pool_key, content FROM pdf_variator_pools").fetchall()
        return {r["pool_key"]: r["content"] for r in rows}

    def save_variator_pools(self, pools: dict):
        """Bulk-Upsert: übergibt {pool_key: content} und schreibt alles."""
        c = self._conn()
        for k, v in pools.items():
            c.execute(
                "INSERT INTO pdf_variator_pools (pool_key, content, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(pool_key) DO UPDATE SET "
                "content=excluded.content, updated_at=CURRENT_TIMESTAMP",
                (k, v))
        c.commit()

    def reset_variator_pools(self):
        self._conn().execute("DELETE FROM pdf_variator_pools")
        self._conn().commit()

    # ── Warmup PDF-Pool ─────────────────────────────────────
    def add_warmup_pdf(self, filename: str, file_path: str, size: int) -> int:
        c = self._conn()
        c.execute("INSERT INTO warmup_pdfs (filename, file_path, size) VALUES (?,?,?)",
                  (filename, file_path, size))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def list_warmup_pdfs(self) -> list:
        return [dict(r) for r in self._conn().execute(
            "SELECT * FROM warmup_pdfs ORDER BY id DESC").fetchall()]

    def get_warmup_pdf(self, pid: int):
        r = self._conn().execute("SELECT * FROM warmup_pdfs WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None

    def delete_warmup_pdf(self, pid: int):
        c = self._conn()
        c.execute("DELETE FROM warmup_pdfs WHERE id=?", (pid,))
        c.commit()

    def random_warmup_pdf(self):
        r = self._conn().execute(
            "SELECT * FROM warmup_pdfs ORDER BY RANDOM() LIMIT 1").fetchone()
        return dict(r) if r else None

    # ── Spaceship Registrar-Accounts ────────────────────────
    def add_spaceship_account(self, name: str, api_key: str,
                                api_secret: str = "") -> int:
        c = self._conn()
        existing = c.execute("SELECT COUNT(*) FROM spaceship_accounts").fetchone()[0]
        is_primary = 1 if existing == 0 else 0
        c.execute("INSERT INTO spaceship_accounts (name, api_key, api_secret, is_primary) "
                  "VALUES (?,?,?,?)",
                  (name, api_key, api_secret, is_primary))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def update_spaceship_account(self, aid: int, **fields):
        if not fields:
            return
        c = self._conn()
        sets = ", ".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE spaceship_accounts SET {sets} WHERE id=?",
                  list(fields.values()) + [aid])
        c.commit()

    def get_spaceship_accounts(self) -> list:
        return self._conn().execute(
            "SELECT * FROM spaceship_accounts ORDER BY is_primary DESC, name").fetchall()

    def get_spaceship_account(self, aid: int):
        return self._conn().execute(
            "SELECT * FROM spaceship_accounts WHERE id=?", (aid,)).fetchone()

    def get_primary_spaceship_account(self):
        row = self._conn().execute(
            "SELECT * FROM spaceship_accounts WHERE is_primary=1 LIMIT 1").fetchone()
        if row:
            return row
        return self._conn().execute(
            "SELECT * FROM spaceship_accounts ORDER BY id LIMIT 1").fetchone()

    def set_primary_spaceship_account(self, aid: int):
        c = self._conn()
        c.execute("UPDATE spaceship_accounts SET is_primary=0")
        c.execute("UPDATE spaceship_accounts SET is_primary=1 WHERE id=?", (aid,))
        c.commit()

    def delete_spaceship_account(self, aid: int):
        c = self._conn()
        was_primary = c.execute(
            "SELECT is_primary FROM spaceship_accounts WHERE id=?", (aid,)).fetchone()
        c.execute("DELETE FROM spaceship_accounts WHERE id=?", (aid,))
        if was_primary and was_primary[0]:
            first = c.execute(
                "SELECT id FROM spaceship_accounts ORDER BY id LIMIT 1").fetchone()
            if first:
                c.execute("UPDATE spaceship_accounts SET is_primary=1 WHERE id=?", (first[0],))
        c.commit()

    # ── Porkbun Registrar-Accounts ─────────────────────────
    def add_porkbun_account(self, name: str, api_key: str,
                              api_secret: str = "") -> int:
        c = self._conn()
        existing = c.execute("SELECT COUNT(*) FROM porkbun_accounts").fetchone()[0]
        is_primary = 1 if existing == 0 else 0
        c.execute("INSERT INTO porkbun_accounts (name, api_key, api_secret, is_primary) "
                  "VALUES (?,?,?,?)",
                  (name, api_key, api_secret, is_primary))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def update_porkbun_account(self, aid: int, **fields):
        if not fields:
            return
        c = self._conn()
        sets = ", ".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE porkbun_accounts SET {sets} WHERE id=?",
                  list(fields.values()) + [aid])
        c.commit()

    def get_porkbun_accounts(self) -> list:
        return self._conn().execute(
            "SELECT * FROM porkbun_accounts ORDER BY is_primary DESC, name").fetchall()

    def get_porkbun_account(self, aid: int):
        return self._conn().execute(
            "SELECT * FROM porkbun_accounts WHERE id=?", (aid,)).fetchone()

    def get_primary_porkbun_account(self):
        row = self._conn().execute(
            "SELECT * FROM porkbun_accounts WHERE is_primary=1 LIMIT 1").fetchone()
        if row:
            return row
        return self._conn().execute(
            "SELECT * FROM porkbun_accounts ORDER BY id LIMIT 1").fetchone()

    def set_primary_porkbun_account(self, aid: int):
        c = self._conn()
        c.execute("UPDATE porkbun_accounts SET is_primary=0")
        c.execute("UPDATE porkbun_accounts SET is_primary=1 WHERE id=?", (aid,))
        c.commit()

    def delete_porkbun_account(self, aid: int):
        c = self._conn()
        was_primary = c.execute(
            "SELECT is_primary FROM porkbun_accounts WHERE id=?", (aid,)).fetchone()
        c.execute("DELETE FROM porkbun_accounts WHERE id=?", (aid,))
        if was_primary and was_primary[0]:
            first = c.execute(
                "SELECT id FROM porkbun_accounts ORDER BY id LIMIT 1").fetchone()
            if first:
                c.execute("UPDATE porkbun_accounts SET is_primary=1 WHERE id=?", (first[0],))
        c.commit()
