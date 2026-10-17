"""Transactional Mailer Web DB — Lists-based architecture."""
import os
import json
import sqlite3
import threading
from typing import Optional


class TransDB:
    def __init__(self, db_path: str = "trans.db"):
        self._db_path = db_path
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False
        self._ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_path, timeout=60)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA busy_timeout=60000")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn.execute("PRAGMA wal_autocheckpoint=1000")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _ensure_schema(self):
        with self._init_lock:
            if self._initialized:
                return
            c = self._conn()
            c.executescript("""
                CREATE TABLE IF NOT EXISTS trans_config (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    settings_json TEXT DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS trans_smtp_lists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trans_smtps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    list_id INTEGER NOT NULL REFERENCES trans_smtp_lists(id) ON DELETE CASCADE,
                    host TEXT NOT NULL,
                    port INTEGER DEFAULT 587,
                    username TEXT NOT NULL,
                    password TEXT NOT NULL,
                    is_dead INTEGER DEFAULT 0,
                    last_error TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_ts_list ON trans_smtps(list_id);
                CREATE TABLE IF NOT EXISTS trans_lead_lists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    file_origin TEXT DEFAULT '',
                    lead_count INTEGER DEFAULT 0,
                    is_pool INTEGER DEFAULT 0,
                    user_id INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trans_leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    list_id INTEGER NOT NULL REFERENCES trans_lead_lists(id) ON DELETE CASCADE,
                    email TEXT NOT NULL,
                    state TEXT DEFAULT 'PENDING',
                    error_msg TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_tlead_state ON trans_leads(state);
                CREATE INDEX IF NOT EXISTS idx_tlead_list ON trans_leads(list_id);
                CREATE INDEX IF NOT EXISTS idx_tlead_email ON trans_leads(list_id, email);
                CREATE TABLE IF NOT EXISTS trans_macros (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    values_text TEXT DEFAULT '',
                    rotate_every INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trans_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    html_content TEXT DEFAULT '',
                    rotate_every INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trans_template_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    template_id INTEGER NOT NULL REFERENCES trans_templates(id) ON DELETE CASCADE,
                    filename TEXT DEFAULT '',
                    html_content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trans_logos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    group_id INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trans_logo_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    user_id INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trans_redirect_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    short_url TEXT NOT NULL,
                    target_url TEXT DEFAULT '',
                    pool_id INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trans_redirect_pools (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    user_id INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trans_campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    status TEXT DEFAULT 'DRAFT',
                    smtp_list_id INTEGER REFERENCES trans_smtp_lists(id),
                    lead_list_id INTEGER REFERENCES trans_lead_lists(id),
                    schedule_time TEXT DEFAULT '',
                    total_leads INTEGER DEFAULT 0,
                    sent INTEGER DEFAULT 0,
                    failed INTEGER DEFAULT 0,
                    started_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trans_proxies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    proxy_type TEXT DEFAULT 'single',
                    value TEXT DEFAULT '',
                    rotate_every INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trans_bounce_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER DEFAULT 0,
                    lead_id INTEGER DEFAULT 0,
                    email TEXT DEFAULT '',
                    recipient_domain TEXT DEFAULT '',
                    error_code INTEGER DEFAULT 0,
                    error_type TEXT DEFAULT '',
                    error_message TEXT DEFAULT '',
                    mime_profile TEXT DEFAULT '',
                    smtp_host TEXT DEFAULT '',
                    smtp_user TEXT DEFAULT '',
                    user_id INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_tbl_camp ON trans_bounce_log(campaign_id);
                CREATE INDEX IF NOT EXISTS idx_tbl_type ON trans_bounce_log(error_type);
                CREATE INDEX IF NOT EXISTS idx_tbl_domain ON trans_bounce_log(recipient_domain);
                CREATE TABLE IF NOT EXISTS trans_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    display_name TEXT DEFAULT '',
                    logo_path TEXT DEFAULT '',
                    role TEXT DEFAULT 'user',
                    permissions_json TEXT DEFAULT '{}',
                    is_active INTEGER DEFAULT 1,
                    created_by INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trans_app_config (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    login_logo TEXT DEFAULT '',
                    app_name TEXT DEFAULT 'Transactional Mailer'
                );
            """)
            c.commit()
            self._migrate(c)
            self._initialized = True

    def _migrate(self, c):
        """Auto-add missing tables and columns so DB never needs deletion."""
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "trans_template_files" not in tables:
            c.execute("""CREATE TABLE IF NOT EXISTS trans_template_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id INTEGER NOT NULL REFERENCES trans_templates(id) ON DELETE CASCADE,
                filename TEXT DEFAULT '', html_content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        if "trans_proxies" not in tables:
            c.execute("""CREATE TABLE IF NOT EXISTS trans_proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                proxy_type TEXT DEFAULT 'single', value TEXT DEFAULT '',
                rotate_every INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        if "trans_redirect_links" not in tables:
            c.execute("""CREATE TABLE IF NOT EXISTS trans_redirect_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT, short_url TEXT NOT NULL,
                target_url TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        if "trans_macros" not in tables:
            c.execute("""CREATE TABLE IF NOT EXISTS trans_macros (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
                values_text TEXT DEFAULT '', rotate_every INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        if "trans_logos" not in tables:
            c.execute("""CREATE TABLE IF NOT EXISTS trans_logos (
                id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL,
                file_path TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        ll_cols = {r[1] for r in c.execute("PRAGMA table_info(trans_lead_lists)").fetchall()} if "trans_lead_lists" in tables else set()
        if "is_pool" not in ll_cols and "trans_lead_lists" in tables:
            c.execute("ALTER TABLE trans_lead_lists ADD COLUMN is_pool INTEGER DEFAULT 0")
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_tlead_email ON trans_leads(list_id, email)")
        except Exception:
            pass
        if "trans_bounce_log" not in tables:
            c.execute("""CREATE TABLE IF NOT EXISTS trans_bounce_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER DEFAULT 0,
                lead_id INTEGER DEFAULT 0, email TEXT DEFAULT '', recipient_domain TEXT DEFAULT '',
                error_code INTEGER DEFAULT 0, error_type TEXT DEFAULT '',
                error_message TEXT DEFAULT '', mime_profile TEXT DEFAULT '',
                smtp_host TEXT DEFAULT '', smtp_user TEXT DEFAULT '',
                user_id INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tbl_camp ON trans_bounce_log(campaign_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tbl_type ON trans_bounce_log(error_type)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tbl_domain ON trans_bounce_log(recipient_domain)")
        if "trans_app_config" not in tables:
            c.execute("""CREATE TABLE IF NOT EXISTS trans_app_config (
                id INTEGER PRIMARY KEY CHECK(id=1),
                login_logo TEXT DEFAULT '', app_name TEXT DEFAULT 'Transactional Mailer')""")
        # trans_s3_accounts existiert bereits für Redirects (Zeile 474)
        # mit access_key/secret_key. Wir hängen nur `buckets` an falls
        # nicht vorhanden — damit ein bestehender Account gleichzeitig für
        # Redirects UND CDN-Logos nutzbar ist (ein IAM-Key für alles).
        if "trans_s3_accounts" in tables:
            _cols = {r[1] for r in c.execute("PRAGMA table_info(trans_s3_accounts)").fetchall()}
            if "buckets" not in _cols:
                c.execute("ALTER TABLE trans_s3_accounts ADD COLUMN buckets TEXT NOT NULL DEFAULT ''")
        if "trans_s3_uploads" not in tables:
            c.execute("""CREATE TABLE trans_s3_uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER DEFAULT 0,
                filename TEXT NOT NULL DEFAULT '',
                variant_count INTEGER DEFAULT 0,
                user_id INTEGER DEFAULT 0,
                group_id INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        else:
            _cols = {r[1] for r in c.execute("PRAGMA table_info(trans_s3_uploads)").fetchall()}
            for col_name, col_def in [
                ("account_id",    "INTEGER DEFAULT 0"),
                ("filename",      "TEXT NOT NULL DEFAULT ''"),
                ("variant_count", "INTEGER DEFAULT 0"),
                ("user_id",       "INTEGER DEFAULT 0"),
                ("group_id",      "INTEGER DEFAULT 0"),
            ]:
                if col_name not in _cols:
                    c.execute(f"ALTER TABLE trans_s3_uploads ADD COLUMN {col_name} {col_def}")
        if "trans_s3_links" not in tables:
            c.execute("""CREATE TABLE trans_s3_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                upload_id INTEGER NOT NULL,
                url TEXT NOT NULL DEFAULT '',
                bucket TEXT DEFAULT '',
                s3_key TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_ts3l_upload ON trans_s3_links(upload_id)")
        else:
            _cols = {r[1] for r in c.execute("PRAGMA table_info(trans_s3_links)").fetchall()}
            for col_name, col_def in [
                ("upload_id", "INTEGER NOT NULL DEFAULT 0"),
                ("url",       "TEXT NOT NULL DEFAULT ''"),
                ("bucket",    "TEXT DEFAULT ''"),
                ("s3_key",    "TEXT DEFAULT ''"),
            ]:
                if col_name not in _cols:
                    c.execute(f"ALTER TABLE trans_s3_links ADD COLUMN {col_name} {col_def}")

        # Cloudinary uploads: group_id nachträglich adden (für Multi-Pool)
        if "trans_cloudinary_uploads" in tables:
            _cols = {r[1] for r in c.execute(
                "PRAGMA table_info(trans_cloudinary_uploads)").fetchall()}
            if "group_id" not in _cols:
                c.execute("ALTER TABLE trans_cloudinary_uploads "
                          "ADD COLUMN group_id INTEGER DEFAULT 0")

        # Assembly-Mode Snippets — pro Slot ein Pool, live pro Send
        # zufällig kombiniert. Optionales Feature pro Kampagne.
        if "trans_snippets" not in tables:
            c.execute("""CREATE TABLE trans_snippets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot TEXT NOT NULL,
                label TEXT DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                is_active INTEGER DEFAULT 1,
                user_id INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tsnip_slot ON trans_snippets(slot, is_active)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tsnip_user ON trans_snippets(user_id, slot)")

        # SES-API-Provider für trans_smtps
        if "trans_smtps" in tables:
            scols = {r[1] for r in c.execute("PRAGMA table_info(trans_smtps)").fetchall()}
            if "provider_type" not in scols:
                c.execute("ALTER TABLE trans_smtps ADD COLUMN provider_type TEXT DEFAULT 'smtp'")
            if "ses_region" not in scols:
                c.execute("ALTER TABLE trans_smtps ADD COLUMN ses_region TEXT DEFAULT ''")
            if "ses_config_set" not in scols:
                c.execute("ALTER TABLE trans_smtps ADD COLUMN ses_config_set TEXT DEFAULT ''")
        for tbl in ["trans_smtp_lists", "trans_lead_lists", "trans_macros",
                     "trans_templates", "trans_logos", "trans_redirect_links",
                     "trans_campaigns", "trans_proxies"]:
            if tbl in tables:
                tcols = {r[1] for r in c.execute(f"PRAGMA table_info({tbl})").fetchall()}
                if "user_id" not in tcols:
                    c.execute(f"ALTER TABLE {tbl} ADD COLUMN user_id INTEGER DEFAULT 0")
        ucols = {r[1] for r in c.execute("PRAGMA table_info(trans_users)").fetchall()} if "trans_users" in tables else set()
        for col, default in [("role", "'user'"), ("permissions_json", "'{}'"),
                              ("is_active", "1"), ("created_by", "0"),
                              ("logo_path", "''")]:
            if col not in ucols and "trans_users" in tables:
                c.execute(f"ALTER TABLE trans_users ADD COLUMN {col} DEFAULT {default}")
        for tbl, col, default in [
            ("trans_templates", "rotate_every", "0"),
            ("trans_campaigns", "started_at", "NULL"),
            ("trans_campaigns", "finished_at", "NULL"),
            ("trans_campaigns", "schedule_time", "''"),
            ("trans_campaigns", "template_id", "0"),
            ("trans_campaigns", "redirect_pool_id", "0"),
            # Multi-Pool: welche Logo-Group liefert Assets (CID+Cloudinary+S3).
            # 0 = "Template-Default nehmen" (backwards-compat). >0 = expliziter
            # Override → Kampagne A und B können parallel mit A/B laufen.
            ("trans_campaigns", "logo_group_id", "0"),
            # (Auto-Mode/Watchdog/Bandit entfernt — Live-HTML-Gen macht
            # das Fingerprint-Problem obsolet; siehe Meta-Rotation unten)
            # Assembly-Mode + Anti-FP-Rates pro Kampagne
            ("trans_campaigns", "assembly_mode_enabled", "0"),
            ("trans_campaigns", "antifp_passthrough_rate", "0.02"),
            ("trans_campaigns", "antifp_light_rate", "0.10"),
            # Live-HTML-Gen: pro Send frisches HTML aus htmlgen-Engine
            ("trans_campaigns", "live_html_gen_enabled", "0"),
            # Extra-Randomize on top of Live-HTML-Gen: mal Referenz raus,
            # mal Hinweis raus, mal Footer2 leer, mal FristText1<->2 swap.
            # Alles per random.random() pro Send — kein Muster.
            ("trans_campaigns", "htmlgen_extra_randomize", "0"),
            # Optionale Farb-Overrides für den Live-Gen. Leer = random
            # aus htmlgen/config.yaml Pool. Gesetzt = alle Mails der
            # Kampagne nutzen diese Brand-Farbe konsistent.
            ("trans_campaigns", "live_primary_color", "''"),
            ("trans_campaigns", "live_accent_color", "''"),
            # V1 Meta-Rotation: pro Send unabhängig gewürfelt
            ("trans_campaigns", "rotate_subject_pools", "''"),   # Textarea, Pools durch Leerzeile
            ("trans_campaigns", "rotate_from_name_pools", "''"), # dito für From-Name
            ("trans_campaigns", "rotate_image_modes", "''"),     # CSV: cid,cloudinary,url,static_url,text
            ("trans_campaigns", "rotate_link_ref_styles", "''"), # CSV: none,base64,random,email,utm
            # Auto-Refresh: alle N Sends neue Logo-Varianten parallel
            # zu CID + Cloudinary + S3 hochladen. Health-Tracker adjustiert
            # dann automatisch die Weights der Meta-Rotation.
            ("trans_campaigns", "auto_refresh_enabled", "0"),
            ("trans_campaigns", "auto_refresh_every", "100000"),
            ("trans_campaigns", "auto_refresh_variants", "1000"),
            ("trans_campaigns", "auto_refresh_cid_weight", "3.0"),
            ("trans_templates", "logo_group_id", "0"),
            ("trans_logos", "group_id", "0"),
            ("trans_logos", "cdn_url", "''"),
            ("trans_logo_groups", "cdn_urls_json", "''"),
            ("trans_redirect_links", "pool_id", "0"),
            ("trans_macros", "preset_name", "''"),
            ("trans_macros", "is_active", "1"),
            # Sticky: derselbe Wert für alle Vorkommen INNERHALB einer Mail.
            # Für {Name} in From + Signatur konsistent zu halten.
            ("trans_macros", "sticky", "0"),
        ]:
            if tbl in tables:
                cols = {r[1] for r in c.execute(f"PRAGMA table_info({tbl})").fetchall()}
                if col not in cols:
                    c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} DEFAULT {default}")
        # Remove UNIQUE constraint on trans_macros.name (need multiple presets per name)
        if "trans_macros" in tables:
            idx_info = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='trans_macros'").fetchone()
            if idx_info and "UNIQUE" in (idx_info[0] or ""):
                c.execute("""CREATE TABLE trans_macros_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    values_text TEXT DEFAULT '',
                    rotate_every INTEGER DEFAULT 0,
                    user_id INTEGER DEFAULT 0,
                    preset_name TEXT DEFAULT '',
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
                c.execute("""INSERT INTO trans_macros_new
                    (id, name, values_text, rotate_every, user_id, preset_name, is_active, created_at)
                    SELECT id, name, values_text, rotate_every,
                           COALESCE(user_id, 0),
                           COALESCE(preset_name, 'Default'),
                           COALESCE(is_active, 1),
                           created_at
                    FROM trans_macros""")
                c.execute("DROP TABLE trans_macros")
                c.execute("ALTER TABLE trans_macros_new RENAME TO trans_macros")
                c.commit()

        for new_tbl, create_sql in [
            ("trans_logo_groups", """CREATE TABLE IF NOT EXISTS trans_logo_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                user_id INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""),
            ("trans_redirect_pools", """CREATE TABLE IF NOT EXISTS trans_redirect_pools (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                user_id INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""),
            ("trans_suppressions", """CREATE TABLE IF NOT EXISTS trans_suppressions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                reason TEXT DEFAULT '',
                bounce_code INTEGER DEFAULT 0,
                error_message TEXT DEFAULT '',
                source TEXT DEFAULT 'auto',
                campaign_id INTEGER DEFAULT 0,
                user_id INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""),
            ("trans_inbox_accounts", """CREATE TABLE IF NOT EXISTS trans_inbox_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT DEFAULT '',
                email TEXT NOT NULL,
                imap_host TEXT NOT NULL,
                imap_port INTEGER DEFAULT 993,
                username TEXT DEFAULT '',
                password TEXT DEFAULT '',
                proxy TEXT DEFAULT '',
                user_id INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""),
            ("trans_inbox_tests", """CREATE TABLE IF NOT EXISTS trans_inbox_tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT DEFAULT '',
                smtp_list_id INTEGER DEFAULT 0,
                template_id INTEGER DEFAULT 0,
                mime_profile TEXT DEFAULT 'rotate',
                subject TEXT DEFAULT '',
                from_name TEXT DEFAULT '',
                mail_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'DRAFT',
                user_id INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""),
            ("trans_inbox_results", """CREATE TABLE IF NOT EXISTS trans_inbox_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_id INTEGER DEFAULT 0,
                account_id INTEGER DEFAULT 0,
                account_email TEXT DEFAULT '',
                provider TEXT DEFAULT '',
                smtp_host TEXT DEFAULT '',
                smtp_user TEXT DEFAULT '',
                html_file TEXT DEFAULT '',
                mime_profile TEXT DEFAULT '',
                subject_used TEXT DEFAULT '',
                from_used TEXT DEFAULT '',
                result TEXT DEFAULT 'PENDING',
                folder_found TEXT DEFAULT '',
                error_message TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""),
            ("trans_logo_codes", """CREATE TABLE IF NOT EXISTS trans_logo_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                code TEXT NOT NULL,
                is_active INTEGER DEFAULT 0,
                user_id INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""),
            ("trans_export_jobs", """CREATE TABLE IF NOT EXISTS trans_export_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT DEFAULT '',
                status TEXT DEFAULT 'PENDING',
                total_count INTEGER DEFAULT 0,
                done_count INTEGER DEFAULT 0,
                logo_mode TEXT DEFAULT 'code',
                file_path TEXT DEFAULT '',
                error_msg TEXT DEFAULT '',
                user_id INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP)"""),
            ("trans_s3_accounts", """CREATE TABLE IF NOT EXISTS trans_s3_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                access_key TEXT NOT NULL,
                secret_key TEXT NOT NULL,
                region TEXT DEFAULT 'eu-central-1',
                bucket_prefix TEXT DEFAULT 'lk',
                proxy_id INTEGER DEFAULT 0,
                is_primary INTEGER DEFAULT 0,
                buckets TEXT NOT NULL DEFAULT '',
                user_id INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""),
            ("trans_cloudinary_uploads", """CREATE TABLE IF NOT EXISTS trans_cloudinary_uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_filename TEXT NOT NULL,
                base_public_id TEXT NOT NULL,
                folder TEXT DEFAULT '',
                count INTEGER DEFAULT 1,
                pixel_tweak INTEGER DEFAULT 1,
                proxy_id INTEGER DEFAULT 0,
                user_id INTEGER DEFAULT 0,
                group_id INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""),
            ("trans_cloudinary_links", """CREATE TABLE IF NOT EXISTS trans_cloudinary_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                upload_id INTEGER NOT NULL,
                public_id TEXT NOT NULL,
                secure_url TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""),
            ("trans_smtp_check_jobs", """CREATE TABLE IF NOT EXISTS trans_smtp_check_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT DEFAULT '',
                source_list_id INTEGER DEFAULT 0,
                receiver_account_id INTEGER DEFAULT 0,
                receiver_email TEXT DEFAULT '',
                proxy_id INTEGER DEFAULT 0,
                subject TEXT DEFAULT '',
                body_text TEXT DEFAULT '',
                from_name TEXT DEFAULT '',
                wait_seconds INTEGER DEFAULT 90,
                status TEXT DEFAULT 'RUNNING',
                total INTEGER DEFAULT 0,
                delivered INTEGER DEFAULT 0,
                conn_errors INTEGER DEFAULT 0,
                invalid INTEGER DEFAULT 0,
                user_id INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP)"""),
            ("trans_smtp_check_results", """CREATE TABLE IF NOT EXISTS trans_smtp_check_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                smtp_id INTEGER DEFAULT 0,
                host TEXT DEFAULT '',
                port INTEGER DEFAULT 0,
                username TEXT DEFAULT '',
                password TEXT DEFAULT '',
                marker TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                error TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""),
        ]:
            if new_tbl not in tables:
                c.execute(create_sql)
        # Assign orphaned data (user_id=0) to first admin
        first_admin = c.execute("SELECT id FROM trans_users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
        if first_admin:
            aid = first_admin[0]
            for tbl in ["trans_smtp_lists", "trans_lead_lists", "trans_macros",
                         "trans_templates", "trans_logos", "trans_redirect_links",
                         "trans_campaigns", "trans_proxies"]:
                if tbl in tables:
                    c.execute(f"UPDATE {tbl} SET user_id=? WHERE user_id=0 OR user_id IS NULL", (aid,))
        try:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_suppress_user_email "
                      "ON trans_suppressions(user_id, email)")
        except Exception:
            pass
        c.commit()

    # ── Config (single row JSON) ──────────────────────────
    _DEFAULTS = {
        "threads": 40, "normal_delay": 0.3, "provider_delay": 6.0,
        "warmup_delay": 30.0, "warmup_count": 5, "smtp_timeout": 30,
        "ignore_ssl_errors": True, "schedule_time": "",
        "from_name": "", "from_email": "", "subject": "",
        "test_recipients": "", "test_interval": 0, "interval_recipients": "",
        "antifingerprint_classes": True, "advanced_antifingerprint": True,
        "structure_variation": 0.5,
        "image_enabled": False, "image_mode": "cid", "image_quantize": True,
        "image_downscale": False, "logo_max_colors": 256, "logo_rotate_every": 0,
        "cloudinary_cloud_name": "", "cloudinary_api_key": "", "cloudinary_api_secret": "",
        "redirect_enabled": False, "redirect_target_url": "",
        "redirect_rotate_every": 10, "redirect_gen_threads": 3,
        "redirect_append_ref": False,
        "proxy_mode": "off", "proxy_value": "", "proxy_rotate_every": 0,
        "mxtoolbox_api_key": "",
        "html_rotate_every": 0,
        "llm_api_url": "https://openrouter.ai/api/v1/chat/completions",
        "llm_api_key": "",
        "llm_model": "anthropic/claude-sonnet-4-20250514",
        "spam_checker": "both",
        "spam_checker_url": "http://127.0.0.1:11333/checkv2",
        "mime_profile": "rotate",
        "auto_retry_failed": True,
        "auto_suppress_hard_bounces": False,
        "logo_static_url": "",
        "gradual_send_enabled": False,
        "gradual_send_ramp_minutes": 180,
        "gradual_send_start_factor": 0.05,
        "aws_access_key": "", "aws_secret_key": "",
        "aws_region": "eu-central-1", "s3_bucket_prefix": "lk",
        "aws_proxy_id": 0,
        "freshness_every_n_mails": 0,
        "freshness_reset_html": False,
        "freshness_reset_logos": False,
        "freshness_html_count": 25,
        "freshness_logo_count": 25,
        "antibot_enabled": False,
        "antibot_base_url": "",
        "antibot_hmac_secret": "",
        "antibot_token_ttl_hours": 168,
    }

    def get_config(self) -> dict:
        c = self._conn()
        r = c.execute("SELECT settings_json FROM trans_config WHERE id=1").fetchone()
        if not r:
            c.execute("INSERT OR IGNORE INTO trans_config (id,settings_json) VALUES (1,?)",
                      (json.dumps(self._DEFAULTS),))
            c.commit()
            return dict(self._DEFAULTS)
        cfg = dict(self._DEFAULTS)
        cfg.update(json.loads(r["settings_json"] or "{}"))
        return cfg

    def save_config(self, settings: dict):
        c = self._conn()
        c.execute("INSERT OR REPLACE INTO trans_config (id,settings_json) VALUES (1,?)",
                  (json.dumps(settings, ensure_ascii=False),))
        c.commit()

    def update_config(self, **kw):
        cfg = self.get_config()
        cfg.update(kw)
        self.save_config(cfg)

    # ── SMTP Lists ────────────────────────────────────────
    def create_smtp_list(self, name: str, user_id: int = 0) -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_smtp_lists (name,user_id) VALUES (?,?)", (name, user_id))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_smtp_lists(self, user_id: int = 0) -> list:
        if user_id:
            return self._conn().execute("SELECT * FROM trans_smtp_lists WHERE user_id=? ORDER BY name", (user_id,)).fetchall()
        return self._conn().execute("SELECT * FROM trans_smtp_lists ORDER BY name").fetchall()

    def delete_smtp_list(self, lid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_smtps WHERE list_id=?", (lid,))
        c.execute("UPDATE trans_campaigns SET smtp_list_id=0 WHERE smtp_list_id=?", (lid,))
        c.execute("DELETE FROM trans_smtp_lists WHERE id=?", (lid,))
        c.commit()

    def import_smtps(self, list_id: int, text: str) -> int:
        """Parst SMTP-Zeilen. Zwei Formate werden automatisch erkannt:

        1) CSV-Standard:   host,port,user,pass[,proxy]
        2) Pipe-Export (SmtpChecker/AtomicMail o.ä. mit vielen Feldern):
             host|port|N|N|N|bool|...|user@x|pass|N|N|...
           Host in Feld 0, Port in Feld 1, User (email) in Feld 10 und
           Pass in Feld 11 sind zuverlässig; falls das nicht passt,
           versuchen wir die erste Email-Adresse + das direkt folgende
           Feld als User/Pass zu greifen.
        """
        import re
        c = self._conn()
        added = 0

        def _parse_pipe(parts):
            # Bevorzugter Positions-Fallback aus dem Export-Beispiel
            if (len(parts) >= 12 and "@" in parts[10] and parts[11]
                    and parts[1].isdigit()):
                return parts[0].strip(), int(parts[1]), parts[10].strip(), parts[11].strip()
            # Fallback: erste Email finden, Passwort ist das Feld danach
            email_re = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
            for i, p in enumerate(parts):
                if email_re.match(p.strip()) and i + 1 < len(parts) and parts[i + 1].strip():
                    if parts[1].strip().isdigit():
                        return parts[0].strip(), int(parts[1]), p.strip(), parts[i + 1].strip()
            return None

        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            row = None
            if "|" in line and line.count("|") >= 4:
                parts = line.split("|")
                row = _parse_pipe(parts)

            if row is None:
                parts = line.split(",")
                if len(parts) < 4:
                    continue
                try:
                    row = (parts[0].strip(), int(parts[1].strip()),
                           parts[2].strip(), parts[3].strip())
                except ValueError:
                    continue

            host, port, user, pw = row
            if not host or not user or not pw or not port:
                continue
            try:
                c.execute("INSERT INTO trans_smtps (list_id,host,port,username,password) VALUES (?,?,?,?,?)",
                          (list_id, host, port, user, pw))
                added += 1
            except Exception:
                pass
        c.commit()
        return added

    def import_ses_accounts(self, list_id: int, text: str,
                              default_region: str = "eu-central-1",
                              default_config_set: str = "") -> int:
        """Zeilenformat: IAM_KEY,IAM_SECRET[,region[,config_set]]
        Auch pipe-getrennt (|) erlaubt für Copy-Paste aus Textblöcken."""
        c = self._conn()
        added = 0
        for line in text.splitlines():
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
            try:
                c.execute(
                    "INSERT INTO trans_smtps "
                    "(list_id,host,port,username,password,provider_type,"
                    " ses_region,ses_config_set) VALUES (?,?,?,?,?,?,?,?)",
                    (list_id, "ses-api", 0, iam_key, iam_secret,
                     "ses_api", region, cfg_set))
                added += 1
            except Exception:
                pass
        c.commit()
        return added

    def get_smtps(self, list_id: int = 0) -> list:
        if list_id:
            return self._conn().execute("SELECT * FROM trans_smtps WHERE list_id=? ORDER BY host",
                                         (list_id,)).fetchall()
        return self._conn().execute("SELECT * FROM trans_smtps ORDER BY host").fetchall()

    def get_smtp_count(self, list_id: int) -> int:
        r = self._conn().execute("SELECT COUNT(*) FROM trans_smtps WHERE list_id=?", (list_id,)).fetchone()
        return r[0] if r else 0

    def delete_smtp(self, sid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_smtps WHERE id=?", (sid,))
        c.commit()

    def update_smtp(self, sid: int, **kw):
        c = self._conn()
        sets = ", ".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE trans_smtps SET {sets} WHERE id=?", list(kw.values()) + [sid])
        c.commit()

    def delete_smtps_by_ids(self, ids: list):
        if not ids:
            return
        c = self._conn()
        ph = ",".join("?" for _ in ids)
        c.execute(f"DELETE FROM trans_smtps WHERE id IN ({ph})", ids)
        c.commit()

    # ── Lead Lists ────────────────────────────────────────
    def create_lead_list(self, name: str, file_origin: str = "", user_id: int = 0) -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_lead_lists (name,file_origin,user_id) VALUES (?,?,?)",
                  (name, file_origin, user_id))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_lead_lists(self, user_id: int = 0) -> list:
        if user_id:
            return self._conn().execute("SELECT * FROM trans_lead_lists WHERE user_id=? ORDER BY name", (user_id,)).fetchall()
        return self._conn().execute("SELECT * FROM trans_lead_lists ORDER BY name").fetchall()

    def delete_lead_list(self, lid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_leads WHERE list_id=?", (lid,))
        c.execute("UPDATE trans_campaigns SET lead_list_id=0 WHERE lead_list_id=?", (lid,))
        c.execute("DELETE FROM trans_lead_lists WHERE id=?", (lid,))
        c.commit()

    def import_leads(self, list_id: int, emails: list) -> int:
        c = self._conn()
        added = 0
        batch = []
        for email in emails:
            email = email.strip()
            if not email or "@" not in email:
                continue
            batch.append((list_id, email))
            if len(batch) >= 500:
                c.executemany("INSERT INTO trans_leads (list_id,email) VALUES (?,?)", batch)
                added += len(batch)
                batch = []
        if batch:
            c.executemany("INSERT INTO trans_leads (list_id,email) VALUES (?,?)", batch)
            added += len(batch)
        c.execute("UPDATE trans_lead_lists SET lead_count=? WHERE id=?", (added, list_id))
        c.commit()
        return added

    def get_lead_count(self, list_id: int) -> int:
        r = self._conn().execute("SELECT COUNT(*) FROM trans_leads WHERE list_id=?",
                                  (list_id,)).fetchone()
        return r[0] if r else 0

    def get_lead_states(self, list_id: int) -> dict:
        rows = self._conn().execute(
            "SELECT state, COUNT(*) FROM trans_leads WHERE list_id=? GROUP BY state",
            (list_id,)).fetchall()
        states = {"PENDING": 0, "SENT": 0, "FAILED": 0, "IN_PROGRESS": 0, "SUPPRESSED": 0}
        for r in rows:
            states[r[0]] = r[1]
        return states

    def fetch_pending(self, list_id: int, batch_size: int = 200):
        return self._conn().execute(
            "SELECT id, email FROM trans_leads WHERE list_id=? AND state='PENDING' ORDER BY id LIMIT ?",
            (list_id, batch_size)).fetchall()

    def mark_sent(self, lead_id: int):
        c = self._conn()
        c.execute("UPDATE trans_leads SET state='SENT' WHERE id=?", (lead_id,))
        c.commit()

    def mark_failed(self, lead_id: int, error: str = ""):
        c = self._conn()
        c.execute("UPDATE trans_leads SET state='FAILED', error_msg=? WHERE id=?",
                  (error[:500], lead_id))
        c.commit()

    def mark_suppressed(self, lead_id: int, error: str = ""):
        # Distinct terminal state so the blanket retry (FAILED->PENDING)
        # never re-attempts a hard-bounced / suppressed address.
        c = self._conn()
        c.execute("UPDATE trans_leads SET state='SUPPRESSED', error_msg=? WHERE id=?",
                  (error[:500], lead_id))
        c.commit()

    def mark_in_progress(self, lead_ids: list):
        if not lead_ids:
            return
        c = self._conn()
        ph = ",".join("?" for _ in lead_ids)
        c.execute(f"UPDATE trans_leads SET state='IN_PROGRESS' WHERE id IN ({ph})", lead_ids)
        c.commit()

    def reset_leads(self, list_id: int):
        c = self._conn()
        c.execute("UPDATE trans_leads SET state='PENDING', error_msg='' WHERE list_id=?",
                  (list_id,))
        c.commit()

    def reset_in_progress(self, list_id: int):
        c = self._conn()
        c.execute("UPDATE trans_leads SET state='PENDING' WHERE list_id=? AND state='IN_PROGRESS'",
                  (list_id,))
        c.commit()

    def get_lead_preview(self, list_id: int, limit: int = 10) -> list:
        return self._conn().execute("SELECT email FROM trans_leads WHERE list_id=? LIMIT ?",
                                     (list_id, limit)).fetchall()

    # ── Lead Pools ─────────────────────────────────────────
    def create_pool(self, name: str, user_id: int = 0) -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_lead_lists (name,is_pool,user_id) VALUES (?,1,?)",
                  (name, user_id))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_pools(self, user_id: int = 0) -> list:
        if user_id:
            return self._conn().execute(
                "SELECT * FROM trans_lead_lists WHERE is_pool=1 AND user_id=? ORDER BY name",
                (user_id,)).fetchall()
        return self._conn().execute(
            "SELECT * FROM trans_lead_lists WHERE is_pool=1 ORDER BY name").fetchall()

    def pool_stats(self, pool_id: int) -> dict:
        c = self._conn()
        total = c.execute("SELECT COUNT(*) FROM trans_leads WHERE list_id=?", (pool_id,)).fetchone()[0]
        pending = c.execute("SELECT COUNT(*) FROM trans_leads WHERE list_id=? AND state='PENDING'", (pool_id,)).fetchone()[0]
        used = c.execute("SELECT COUNT(*) FROM trans_leads WHERE list_id=? AND state='USED'", (pool_id,)).fetchone()[0]
        sent = c.execute("SELECT COUNT(*) FROM trans_leads WHERE list_id=? AND state='SENT'", (pool_id,)).fetchone()[0]
        return {"total": total, "pending": pending, "used": used, "sent": sent}

    def import_pool_leads(self, pool_id: int, emails: list, skip_dedup: bool = False) -> dict:
        """Import leads into pool. Dedup optional for large imports."""
        c = self._conn()
        added = 0
        dupes = 0

        if skip_dedup:
            batch = []
            for email in emails:
                email = email.strip().lower()
                if not email or "@" not in email:
                    continue
                batch.append((pool_id, email))
                added += 1
                if len(batch) >= 5000:
                    c.executemany("INSERT INTO trans_leads (list_id,email,state) VALUES (?,?,'PENDING')", batch)
                    batch.clear()
            if batch:
                c.executemany("INSERT INTO trans_leads (list_id,email,state) VALUES (?,?,'PENDING')", batch)
            c.commit()
        else:
            existing = set()
            for row in c.execute("SELECT email FROM trans_leads WHERE list_id=?", (pool_id,)):
                existing.add(row[0].lower())
            batch = []
            for email in emails:
                email = email.strip().lower()
                if not email or "@" not in email:
                    continue
                if email in existing:
                    dupes += 1
                    continue
                existing.add(email)
                batch.append((pool_id, email))
                added += 1
                if len(batch) >= 5000:
                    c.executemany("INSERT INTO trans_leads (list_id,email,state) VALUES (?,?,'PENDING')", batch)
                    batch.clear()
            if batch:
                c.executemany("INSERT INTO trans_leads (list_id,email,state) VALUES (?,?,'PENDING')", batch)
            c.commit()

        # Shuffle: assign random sort positions to PENDING leads
        # by updating rowids is not possible, but we can use a random-order trick
        # SQLite doesn't support UPDATE with ORDER BY RANDOM, so we re-insert
        # Actually simpler: the fetch_pending already uses ORDER BY id
        # For true shuffle, we assign a random number column
        # But since the user said they pre-shuffle the big list, and new leads
        # just need to be mixed into the PENDING pool, we randomize their position
        # by giving them IDs that interleave with existing pending leads
        if added > 0:
            self._shuffle_pending(pool_id)

        c.execute("UPDATE trans_lead_lists SET lead_count=(SELECT COUNT(*) FROM trans_leads WHERE list_id=?) WHERE id=?",
                  (pool_id, pool_id))
        c.commit()
        return {"added": added, "dupes": dupes}

    def _shuffle_pending(self, pool_id: int):
        """Shuffle pending leads by reassigning them with random order.
        Creates a temp table, copies pending in random order, deletes originals, re-inserts."""
        c = self._conn()
        # Get all pending emails
        rows = c.execute(
            "SELECT email FROM trans_leads WHERE list_id=? AND state='PENDING' ORDER BY RANDOM()",
            (pool_id,)).fetchall()
        if not rows:
            return
        # Delete all pending
        c.execute("DELETE FROM trans_leads WHERE list_id=? AND state='PENDING'", (pool_id,))
        # Re-insert in shuffled order
        batch = [(pool_id, r[0]) for r in rows]
        c.executemany("INSERT OR IGNORE INTO trans_leads (list_id,email,state) VALUES (?,?,'PENDING')", batch)
        c.commit()

    def reserve_pool_leads(self, pool_id: int, count: int) -> int:
        """Mark next N pending leads as USED (reserved for a campaign)."""
        c = self._conn()
        leads = c.execute(
            "SELECT id FROM trans_leads WHERE list_id=? AND state='PENDING' ORDER BY id LIMIT ?",
            (pool_id, count)).fetchall()
        if not leads:
            return 0
        ids = [r[0] for r in leads]
        ph = ",".join("?" for _ in ids)
        c.execute(f"UPDATE trans_leads SET state='USED' WHERE id IN ({ph})", ids)
        c.commit()
        return len(ids)

    def reset_pool(self, pool_id: int):
        """Reset all USED leads back to PENDING."""
        c = self._conn()
        c.execute("UPDATE trans_leads SET state='PENDING' WHERE list_id=? AND state='USED'", (pool_id,))
        c.commit()

    def reset_pool_all(self, pool_id: int):
        """Reset ALL leads (including SENT/FAILED) back to PENDING."""
        c = self._conn()
        c.execute("UPDATE trans_leads SET state='PENDING', error_msg='' WHERE list_id=?", (pool_id,))
        c.commit()

    # ── Macros ────────────────────────────────────────────
    def add_macro(self, name: str, values_text: str = "", rotate_every: int = 0, user_id: int = 0, preset_name: str = "") -> int:
        c = self._conn()
        if not preset_name:
            preset_name = "Default"
        existing = c.execute("SELECT COUNT(*) FROM trans_macros WHERE name=? AND user_id=?",
                              (name, user_id)).fetchone()[0]
        is_active = 1 if existing == 0 else 0
        c.execute("INSERT INTO trans_macros (name,values_text,rotate_every,user_id,preset_name,is_active) VALUES (?,?,?,?,?,?)",
                  (name, values_text, rotate_every, user_id, preset_name, is_active))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_macros(self, user_id: int = 0) -> list:
        if user_id:
            return self._conn().execute("SELECT * FROM trans_macros WHERE user_id=? ORDER BY name, preset_name", (user_id,)).fetchall()
        return self._conn().execute("SELECT * FROM trans_macros ORDER BY name, preset_name").fetchall()

    def get_active_macros(self, user_id: int = 0) -> list:
        if user_id:
            return self._conn().execute("SELECT * FROM trans_macros WHERE user_id=? AND is_active=1 ORDER BY name", (user_id,)).fetchall()
        return self._conn().execute("SELECT * FROM trans_macros WHERE is_active=1 ORDER BY name").fetchall()

    def activate_macro(self, mid: int):
        c = self._conn()
        row = c.execute("SELECT name, user_id FROM trans_macros WHERE id=?", (mid,)).fetchone()
        if row:
            c.execute("UPDATE trans_macros SET is_active=0 WHERE name=? AND user_id=?",
                      (row["name"], row["user_id"]))
            c.execute("UPDATE trans_macros SET is_active=1 WHERE id=?", (mid,))
            c.commit()

    def get_macro(self, name: str) -> str:
        r = self._conn().execute("SELECT values_text FROM trans_macros WHERE name=? AND is_active=1", (name,)).fetchone()
        return r["values_text"] if r else ""

    def update_macro(self, mid: int, values_text: str, rotate_every: int = 0):
        c = self._conn()
        c.execute("UPDATE trans_macros SET values_text=?, rotate_every=? WHERE id=?",
                  (values_text, rotate_every, mid))
        c.commit()

    def delete_macro(self, mid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_macros WHERE id=?", (mid,))
        c.commit()

    # ── Templates ─────────────────────────────────────────
    def add_template(self, name: str, html_content: str = "", user_id: int = 0) -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_templates (name,html_content,user_id) VALUES (?,?,?)",
                  (name, html_content, user_id))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_templates(self, user_id: int = 0) -> list:
        if user_id:
            return self._conn().execute("SELECT * FROM trans_templates WHERE user_id=? ORDER BY name", (user_id,)).fetchall()
        return self._conn().execute("SELECT * FROM trans_templates ORDER BY name").fetchall()

    def get_template(self, tid: int):
        return self._conn().execute("SELECT * FROM trans_templates WHERE id=?", (tid,)).fetchone()

    def update_template(self, tid: int, name: str, html_content: str):
        c = self._conn()
        c.execute("UPDATE trans_templates SET name=?, html_content=? WHERE id=?",
                  (name, html_content, tid))
        c.commit()

    def delete_template(self, tid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_template_files WHERE template_id=?", (tid,))
        c.execute("DELETE FROM trans_templates WHERE id=?", (tid,))
        c.commit()

    def add_template_file(self, template_id: int, filename: str, html_content: str) -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_template_files (template_id,filename,html_content) VALUES (?,?,?)",
                  (template_id, filename, html_content))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_template_files(self, template_id: int) -> list:
        return self._conn().execute(
            "SELECT * FROM trans_template_files WHERE template_id=? ORDER BY filename",
            (template_id,)).fetchall()

    def get_template_file_count(self, template_id: int) -> int:
        r = self._conn().execute("SELECT COUNT(*) FROM trans_template_files WHERE template_id=?",
                                  (template_id,)).fetchone()
        return r[0] if r else 0

    def delete_template_file(self, fid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_template_files WHERE id=?", (fid,))
        c.commit()

    def get_all_template_htmls(self, user_id: int = 0, template_id: int = 0) -> list:
        """Get HTML bodies. If template_id given, only from that template."""
        bodies = []
        if template_id:
            templates = [self.get_template(template_id)]
            templates = [t for t in templates if t]
        else:
            templates = self.get_templates(user_id)
        for t in templates:
            t = dict(t)
            files = self.get_template_files(t["id"])
            if files:
                for f in files:
                    bodies.append(dict(f)["html_content"])
            elif t.get("html_content", "").strip():
                bodies.append(t["html_content"])
        return bodies

    # ── Logo Groups ────────────────────────────────────────
    def add_logo_group(self, name: str, user_id: int = 0) -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_logo_groups (name,user_id) VALUES (?,?)", (name, user_id))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_logo_groups(self, user_id: int = 0) -> list:
        if user_id:
            return self._conn().execute("SELECT * FROM trans_logo_groups WHERE user_id=? ORDER BY name", (user_id,)).fetchall()
        return self._conn().execute("SELECT * FROM trans_logo_groups ORDER BY name").fetchall()

    def delete_logo_group(self, gid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_logos WHERE group_id=?", (gid,))
        c.execute("DELETE FROM trans_logo_groups WHERE id=?", (gid,))
        c.commit()

    # ── Logos ─────────────────────────────────────────────
    def add_logo(self, filename: str, file_path: str, user_id: int = 0, group_id: int = 0) -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_logos (filename,file_path,user_id,group_id) VALUES (?,?,?,?)",
                  (filename, file_path, user_id, group_id))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_logos(self, user_id: int = 0) -> list:
        if user_id:
            return self._conn().execute("SELECT * FROM trans_logos WHERE user_id=? ORDER BY filename", (user_id,)).fetchall()
        return self._conn().execute("SELECT * FROM trans_logos ORDER BY filename").fetchall()

    def get_logos_by_group(self, group_id: int) -> list:
        return self._conn().execute("SELECT * FROM trans_logos WHERE group_id=? ORDER BY filename", (group_id,)).fetchall()

    def delete_logo(self, lid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_logos WHERE id=?", (lid,))
        c.commit()

    # ── Redirect Pools ───────────────────────────────────
    def add_redirect_pool(self, name: str, user_id: int = 0) -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_redirect_pools (name,user_id) VALUES (?,?)", (name, user_id))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_redirect_pools(self, user_id: int = 0) -> list:
        if user_id:
            return self._conn().execute("SELECT * FROM trans_redirect_pools WHERE user_id=? ORDER BY name", (user_id,)).fetchall()
        return self._conn().execute("SELECT * FROM trans_redirect_pools ORDER BY name").fetchall()

    def delete_redirect_pool(self, pid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_redirect_links WHERE pool_id=?", (pid,))
        c.execute("DELETE FROM trans_redirect_pools WHERE id=?", (pid,))
        c.commit()

    def get_redirect_pool_count(self, pool_id: int) -> int:
        r = self._conn().execute("SELECT COUNT(*) FROM trans_redirect_links WHERE pool_id=?", (pool_id,)).fetchone()
        return r[0] if r else 0

    # ── Redirect Links ────────────────────────────────────
    def add_redirect(self, short_url: str, target_url: str = "", user_id: int = 0, pool_id: int = 0) -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_redirect_links (short_url,target_url,user_id,pool_id) VALUES (?,?,?,?)",
                  (short_url, target_url, user_id, pool_id))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_redirects_by_pool(self, pool_id: int) -> list:
        return self._conn().execute("SELECT * FROM trans_redirect_links WHERE pool_id=? ORDER BY created_at DESC", (pool_id,)).fetchall()

    def get_redirects(self, user_id: int = 0) -> list:
        if user_id:
            return self._conn().execute("SELECT * FROM trans_redirect_links WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
        return self._conn().execute("SELECT * FROM trans_redirect_links ORDER BY created_at DESC").fetchall()

    def get_redirect_count(self, user_id: int = 0) -> int:
        if user_id:
            r = self._conn().execute("SELECT COUNT(*) FROM trans_redirect_links WHERE user_id=?", (user_id,)).fetchone()
        else:
            r = self._conn().execute("SELECT COUNT(*) FROM trans_redirect_links").fetchone()
        return r[0] if r else 0

    def clear_redirects(self, user_id: int = 0):
        c = self._conn()
        if user_id:
            c.execute("DELETE FROM trans_redirect_links WHERE user_id=?", (user_id,))
        else:
            c.execute("DELETE FROM trans_redirect_links")
        c.commit()

    def delete_redirect(self, rid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_redirect_links WHERE id=?", (rid,))
        c.commit()

    # ── Snippets (Assembly-Mode) ───────────────────────────
    SNIPPET_SLOTS = ("header", "intro", "body", "outro", "footer")

    def add_snippet(self, slot: str, label: str, content: str,
                     user_id: int = 0) -> int:
        if slot not in self.SNIPPET_SLOTS:
            raise ValueError(f"unknown snippet slot: {slot}")
        c = self._conn()
        c.execute("INSERT INTO trans_snippets (slot,label,content,user_id,is_active) "
                  "VALUES (?,?,?,?,1)",
                  (slot, label.strip(), content, user_id))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_snippets(self, user_id: int = 0, slot: str = "") -> list:
        c = self._conn()
        where = []
        params = []
        if user_id:
            where.append("user_id=?")
            params.append(user_id)
        if slot:
            where.append("slot=?")
            params.append(slot)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return c.execute(
            f"SELECT * FROM trans_snippets {clause} ORDER BY slot, id"
        , params).fetchall()

    def get_active_snippets(self, user_id: int = 0, slot: str = "") -> list:
        c = self._conn()
        where = ["is_active=1"]
        params = []
        if user_id:
            where.append("user_id=?")
            params.append(user_id)
        if slot:
            where.append("slot=?")
            params.append(slot)
        return c.execute(
            f"SELECT * FROM trans_snippets WHERE {' AND '.join(where)} "
            f"ORDER BY slot, id", params).fetchall()

    def update_snippet(self, sid: int, **kw):
        if not kw:
            return
        c = self._conn()
        cols = ", ".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE trans_snippets SET {cols} WHERE id=?",
                  list(kw.values()) + [sid])
        c.commit()

    def delete_snippet(self, sid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_snippets WHERE id=?", (sid,))
        c.commit()

    def toggle_snippet(self, sid: int):
        c = self._conn()
        c.execute("UPDATE trans_snippets SET is_active=1-is_active WHERE id=?",
                  (sid,))
        c.commit()

    # ── Bounce Log ─────────────────────────────────────────
    def log_bounce(self, campaign_id: int, lead_id: int, email: str,
                   error_code: int, error_type: str, error_message: str,
                   mime_profile: str = "", smtp_host: str = "",
                   smtp_user: str = "", user_id: int = 0):
        domain = email.split("@")[1].lower() if "@" in email else ""
        c = self._conn()
        c.execute("INSERT INTO trans_bounce_log "
                  "(campaign_id,lead_id,email,recipient_domain,error_code,"
                  "error_type,error_message,mime_profile,smtp_host,smtp_user,user_id) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (campaign_id, lead_id, email, domain, error_code,
                   error_type, error_message[:500], mime_profile, smtp_host, smtp_user, user_id))
        c.commit()

    # ── Suppression list (dead / hard-bounced addresses) ──
    def add_suppression(self, email: str, reason: str = "", bounce_code: int = 0,
                        error_message: str = "", source: str = "auto",
                        campaign_id: int = 0, user_id: int = 0) -> bool:
        """Add an address to the suppression list. Idempotent — a repeat
        of the same (user_id, email) is ignored. Returns True if newly added."""
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            return False
        c = self._conn()
        cur = c.execute(
            "INSERT OR IGNORE INTO trans_suppressions "
            "(email,reason,bounce_code,error_message,source,campaign_id,user_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (email, reason, bounce_code, error_message[:300], source, campaign_id, user_id))
        c.commit()
        return cur.rowcount > 0

    def load_suppression_set(self, user_id: int = 0) -> set:
        """Return a frozenset of lowercased suppressed emails for fast
        in-memory checks during a send (avoids per-mail DB hits)."""
        if user_id:
            rows = self._conn().execute(
                "SELECT email FROM trans_suppressions WHERE user_id=?", (user_id,)).fetchall()
        else:
            rows = self._conn().execute("SELECT email FROM trans_suppressions").fetchall()
        return {r[0] for r in rows}

    def get_suppressions(self, user_id: int = 0, limit: int = 500) -> list:
        if user_id:
            return self._conn().execute(
                "SELECT * FROM trans_suppressions WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit)).fetchall()
        return self._conn().execute(
            "SELECT * FROM trans_suppressions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

    def get_suppression_count(self, user_id: int = 0) -> int:
        if user_id:
            r = self._conn().execute(
                "SELECT COUNT(*) FROM trans_suppressions WHERE user_id=?", (user_id,)).fetchone()
        else:
            r = self._conn().execute("SELECT COUNT(*) FROM trans_suppressions").fetchone()
        return r[0] if r else 0

    def delete_suppression(self, sid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_suppressions WHERE id=?", (sid,))
        c.commit()

    def clear_suppressions(self, user_id: int = 0):
        c = self._conn()
        if user_id:
            c.execute("DELETE FROM trans_suppressions WHERE user_id=?", (user_id,))
        else:
            c.execute("DELETE FROM trans_suppressions")
        c.commit()

    def import_suppressions(self, emails: list, user_id: int = 0,
                            source: str = "import") -> int:
        c = self._conn()
        added = 0
        batch = []
        for e in emails:
            e = (e or "").strip().lower()
            if not e or "@" not in e:
                continue
            batch.append((e, "manual import", 0, "", source, 0, user_id))
            if len(batch) >= 1000:
                cur = c.executemany(
                    "INSERT OR IGNORE INTO trans_suppressions "
                    "(email,reason,bounce_code,error_message,source,campaign_id,user_id) "
                    "VALUES (?,?,?,?,?,?,?)", batch)
                added += cur.rowcount
                batch = []
        if batch:
            cur = c.executemany(
                "INSERT OR IGNORE INTO trans_suppressions "
                "(email,reason,bounce_code,error_message,source,campaign_id,user_id) "
                "VALUES (?,?,?,?,?,?,?)", batch)
            added += cur.rowcount
        c.commit()
        return added

    def get_bounce_stats(self, campaign_id: int = 0, user_id: int = 0) -> dict:
        c = self._conn()
        where = "WHERE 1=1"
        params = []
        if campaign_id:
            where += " AND campaign_id=?"
            params.append(campaign_id)
        if user_id:
            where += " AND user_id=?"
            params.append(user_id)

        total = c.execute(f"SELECT COUNT(*) FROM trans_bounce_log {where}", params).fetchone()[0]

        by_type = c.execute(
            f"SELECT error_type, COUNT(*) as cnt FROM trans_bounce_log {where} "
            f"GROUP BY error_type ORDER BY cnt DESC", params).fetchall()

        by_domain = c.execute(
            f"SELECT recipient_domain, COUNT(*) as cnt FROM trans_bounce_log {where} "
            f"GROUP BY recipient_domain ORDER BY cnt DESC LIMIT 20", params).fetchall()

        by_profile = c.execute(
            f"SELECT mime_profile, error_type, COUNT(*) as cnt FROM trans_bounce_log {where} "
            f"GROUP BY mime_profile, error_type ORDER BY cnt DESC", params).fetchall()

        spam_by_domain = c.execute(
            f"SELECT recipient_domain, mime_profile, COUNT(*) as cnt FROM trans_bounce_log "
            f"{where} AND error_type='spam_reject' "
            f"GROUP BY recipient_domain, mime_profile ORDER BY cnt DESC LIMIT 30", params).fetchall()

        return {
            "total": total,
            "by_type": [dict(r) for r in by_type],
            "by_domain": [dict(r) for r in by_domain],
            "by_profile": [dict(r) for r in by_profile],
            "spam_by_domain": [dict(r) for r in spam_by_domain],
        }

    def get_bounce_log(self, campaign_id: int = 0, user_id: int = 0,
                       error_type: str = "", domain: str = "",
                       profile: str = "", limit: int = 100) -> list:
        c = self._conn()
        where = "WHERE 1=1"
        params = []
        if campaign_id:
            where += " AND campaign_id=?"
            params.append(campaign_id)
        if user_id:
            where += " AND user_id=?"
            params.append(user_id)
        if error_type:
            where += " AND error_type=?"
            params.append(error_type)
        if domain:
            where += " AND recipient_domain=?"
            params.append(domain)
        if profile:
            where += " AND mime_profile=?"
            params.append(profile)
        params.append(limit)
        return c.execute(
            f"SELECT * FROM trans_bounce_log {where} ORDER BY created_at DESC LIMIT ?",
            params).fetchall()

    def get_campaigns_with_bounces(self, user_id: int = 0) -> list:
        c = self._conn()
        if user_id:
            return c.execute(
                "SELECT DISTINCT b.campaign_id, c.name FROM trans_bounce_log b "
                "LEFT JOIN trans_campaigns c ON b.campaign_id=c.id "
                "WHERE b.user_id=? ORDER BY b.campaign_id DESC", (user_id,)).fetchall()
        return c.execute(
            "SELECT DISTINCT b.campaign_id, c.name FROM trans_bounce_log b "
            "LEFT JOIN trans_campaigns c ON b.campaign_id=c.id "
            "ORDER BY b.campaign_id DESC").fetchall()

    # ── Proxies ────────────────────────────────────────────
    def add_proxy(self, name: str, proxy_type: str = "single",
                  value: str = "", rotate_every: int = 0, user_id: int = 0) -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_proxies (name,proxy_type,value,rotate_every,user_id) VALUES (?,?,?,?,?)",
                  (name, proxy_type, value, rotate_every, user_id))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_proxies(self, user_id: int = 0) -> list:
        if user_id:
            return self._conn().execute("SELECT * FROM trans_proxies WHERE user_id=? ORDER BY name", (user_id,)).fetchall()
        return self._conn().execute("SELECT * FROM trans_proxies ORDER BY name").fetchall()

    def get_proxy(self, pid: int):
        return self._conn().execute("SELECT * FROM trans_proxies WHERE id=?", (pid,)).fetchone()

    def update_proxy(self, pid: int, **kw):
        c = self._conn()
        sets = ", ".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE trans_proxies SET {sets} WHERE id=?", list(kw.values()) + [pid])
        c.commit()

    def delete_proxy(self, pid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_proxies WHERE id=?", (pid,))
        c.commit()

    # ── Campaigns ─────────────────────────────────────────
    def create_campaign(self, **kw) -> int:
        c = self._conn()
        cols = list(kw.keys())
        vals = list(kw.values())
        ph = ",".join("?" for _ in vals)
        c.execute(f"INSERT INTO trans_campaigns ({','.join(cols)}) VALUES ({ph})", vals)
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_campaigns(self, user_id: int = 0) -> list:
        if user_id:
            return self._conn().execute("SELECT * FROM trans_campaigns WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
        return self._conn().execute("SELECT * FROM trans_campaigns ORDER BY created_at DESC").fetchall()

    def get_campaign(self, cid: int):
        return self._conn().execute("SELECT * FROM trans_campaigns WHERE id=?", (cid,)).fetchone()

    def update_campaign(self, cid: int, **kw):
        c = self._conn()
        sets = ", ".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE trans_campaigns SET {sets} WHERE id=?", list(kw.values()) + [cid])
        c.commit()

    def delete_campaign(self, cid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_campaigns WHERE id=?", (cid,))
        c.commit()

    # ── Users ─────────────────────────────────────────────
    def get_user(self, username: str):
        return self._conn().execute("SELECT * FROM trans_users WHERE username=?",
                                     (username,)).fetchone()

    def get_user_by_id(self, uid: int):
        return self._conn().execute("SELECT * FROM trans_users WHERE id=?", (uid,)).fetchone()

    def create_user(self, username: str, password_hash: str, display_name: str = "") -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_users (username,password_hash,display_name) VALUES (?,?,?)",
                  (username, password_hash, display_name))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def user_count(self) -> int:
        r = self._conn().execute("SELECT COUNT(*) FROM trans_users").fetchone()
        return r[0] if r else 0

    def get_all_users(self) -> list:
        return self._conn().execute(
            "SELECT id,username,display_name,logo_path,role,permissions_json,is_active,created_at "
            "FROM trans_users ORDER BY username").fetchall()

    def update_user(self, uid: int, **kw):
        c = self._conn()
        sets = ", ".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE trans_users SET {sets} WHERE id=?", list(kw.values()) + [uid])
        c.commit()

    def delete_user(self, uid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_users WHERE id=?", (uid,))
        c.commit()

    def update_user_password(self, uid: int, password_hash: str):
        c = self._conn()
        c.execute("UPDATE trans_users SET password_hash=? WHERE id=?", (password_hash, uid))
        c.commit()

    # ── App Config (login logo, app name) ─────────────────
    def get_app_config(self) -> dict:
        c = self._conn()
        r = c.execute("SELECT * FROM trans_app_config WHERE id=1").fetchone()
        if not r:
            c.execute("INSERT OR IGNORE INTO trans_app_config (id) VALUES (1)")
            c.commit()
            return {"login_logo": "", "app_name": "Transactional Mailer"}
        return dict(r)

    def save_app_config(self, **kw):
        c = self._conn()
        existing = self.get_app_config()
        existing.update(kw)
        c.execute("INSERT OR REPLACE INTO trans_app_config (id,login_logo,app_name) VALUES (1,?,?)",
                  (existing.get("login_logo", ""), existing.get("app_name", "Transactional Mailer")))
        c.commit()

    def close(self):
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

    # ── Inbox Test ───────────────────────────────────────
    def add_inbox_account(self, provider, email, imap_host, imap_port, username, password, proxy="", user_id=0):
        c = self._conn()
        c.execute("INSERT INTO trans_inbox_accounts (provider,email,imap_host,imap_port,username,password,proxy,user_id) VALUES (?,?,?,?,?,?,?,?)",
                  (provider, email, imap_host, imap_port, username, password, proxy, user_id))
        c.commit()

    def get_inbox_accounts(self, user_id=0):
        if user_id:
            return self._conn().execute("SELECT * FROM trans_inbox_accounts WHERE user_id=? ORDER BY provider", (user_id,)).fetchall()
        return self._conn().execute("SELECT * FROM trans_inbox_accounts ORDER BY provider").fetchall()

    def delete_inbox_account(self, aid):
        self._conn().execute("DELETE FROM trans_inbox_accounts WHERE id=?", (aid,))
        self._conn().commit()

    def create_inbox_test(self, name, smtp_list_id, template_id, mime_profile, subject, from_name, mail_count, user_id=0):
        c = self._conn()
        c.execute("INSERT INTO trans_inbox_tests (name,smtp_list_id,template_id,mime_profile,subject,from_name,mail_count,user_id) VALUES (?,?,?,?,?,?,?,?)",
                  (name, smtp_list_id, template_id, mime_profile, subject, from_name, mail_count, user_id))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_inbox_tests(self, user_id=0):
        if user_id:
            return self._conn().execute("SELECT * FROM trans_inbox_tests WHERE user_id=? ORDER BY id DESC", (user_id,)).fetchall()
        return self._conn().execute("SELECT * FROM trans_inbox_tests ORDER BY id DESC").fetchall()

    def get_inbox_test(self, tid):
        return self._conn().execute("SELECT * FROM trans_inbox_tests WHERE id=?", (tid,)).fetchone()

    def update_inbox_test(self, tid, **kw):
        sets = ", ".join(f"{k}=?" for k in kw)
        self._conn().execute(f"UPDATE trans_inbox_tests SET {sets} WHERE id=?", (*kw.values(), tid))
        self._conn().commit()

    def delete_inbox_test(self, tid):
        c = self._conn()
        c.execute("DELETE FROM trans_inbox_results WHERE test_id=?", (tid,))
        c.execute("DELETE FROM trans_inbox_tests WHERE id=?", (tid,))
        c.commit()

    def add_inbox_result(self, test_id, account_id, account_email, provider, smtp_host, smtp_user, html_file, mime_profile, subject_used, from_used):
        c = self._conn()
        c.execute("INSERT INTO trans_inbox_results (test_id,account_id,account_email,provider,smtp_host,smtp_user,html_file,mime_profile,subject_used,from_used) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (test_id, account_id, account_email, provider, smtp_host, smtp_user, html_file, mime_profile, subject_used, from_used))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def update_inbox_result(self, rid, result, folder_found="", error_message=""):
        self._conn().execute("UPDATE trans_inbox_results SET result=?, folder_found=?, error_message=? WHERE id=?",
                              (result, folder_found, error_message, rid))
        self._conn().commit()

    def get_inbox_results(self, test_id):
        return self._conn().execute("SELECT * FROM trans_inbox_results WHERE test_id=? ORDER BY id", (test_id,)).fetchall()

    # ── Logo Code Presets ────────────────────────────────
    def add_logo_code(self, name: str, code: str, user_id: int = 0) -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_logo_codes (name,code,is_active,user_id) VALUES (?,?,0,?)",
                  (name, code, user_id))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_logo_codes(self, user_id: int = 0) -> list:
        if user_id:
            return self._conn().execute("SELECT * FROM trans_logo_codes WHERE user_id=? ORDER BY name", (user_id,)).fetchall()
        return self._conn().execute("SELECT * FROM trans_logo_codes ORDER BY name").fetchall()

    def get_logo_code(self, cid: int):
        return self._conn().execute("SELECT * FROM trans_logo_codes WHERE id=?", (cid,)).fetchone()

    def update_logo_code(self, cid: int, name: str, code: str):
        c = self._conn()
        c.execute("UPDATE trans_logo_codes SET name=?, code=? WHERE id=?", (name, code, cid))
        c.commit()

    def delete_logo_code(self, cid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_logo_codes WHERE id=?", (cid,))
        c.commit()

    # ── Export Jobs ──────────────────────────────────────
    def create_export_job(self, name: str, total: int, logo_mode: str, user_id: int = 0) -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_export_jobs (name,status,total_count,logo_mode,user_id) VALUES (?,?,?,?,?)",
                  (name, "RUNNING", total, logo_mode, user_id))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_export_jobs(self, user_id: int = 0) -> list:
        if user_id:
            return self._conn().execute("SELECT * FROM trans_export_jobs WHERE user_id=? ORDER BY id DESC", (user_id,)).fetchall()
        return self._conn().execute("SELECT * FROM trans_export_jobs ORDER BY id DESC").fetchall()

    def get_export_job(self, jid: int):
        return self._conn().execute("SELECT * FROM trans_export_jobs WHERE id=?", (jid,)).fetchone()

    def update_export_job(self, jid: int, **kw):
        c = self._conn()
        sets = ", ".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE trans_export_jobs SET {sets} WHERE id=?", list(kw.values()) + [jid])
        c.commit()

    def delete_export_job(self, jid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_export_jobs WHERE id=?", (jid,))
        c.commit()

    # ── S3 Accounts ──────────────────────────────────────
    def add_s3_account(self, name, access_key, secret_key,
                        region="eu-central-1", bucket_prefix="lk",
                        proxy_id=0, user_id=0):
        c = self._conn()
        existing = c.execute("SELECT COUNT(*) FROM trans_s3_accounts WHERE user_id=?",
                              (user_id,)).fetchone()[0]
        is_primary = 1 if existing == 0 else 0
        c.execute("INSERT INTO trans_s3_accounts "
                  "(name,access_key,secret_key,region,bucket_prefix,proxy_id,is_primary,user_id) "
                  "VALUES (?,?,?,?,?,?,?,?)",
                  (name, access_key, secret_key, region, bucket_prefix,
                   proxy_id, is_primary, user_id))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_s3_accounts(self, user_id=0):
        if user_id:
            return self._conn().execute(
                "SELECT * FROM trans_s3_accounts WHERE user_id=? ORDER BY is_primary DESC, name",
                (user_id,)).fetchall()
        return self._conn().execute(
            "SELECT * FROM trans_s3_accounts ORDER BY is_primary DESC, name").fetchall()

    def get_s3_account(self, aid):
        return self._conn().execute(
            "SELECT * FROM trans_s3_accounts WHERE id=?", (aid,)).fetchone()

    def get_primary_s3_account(self, user_id=0):
        if user_id:
            row = self._conn().execute(
                "SELECT * FROM trans_s3_accounts WHERE user_id=? AND is_primary=1 LIMIT 1",
                (user_id,)).fetchone()
            if row:
                return row
            return self._conn().execute(
                "SELECT * FROM trans_s3_accounts WHERE user_id=? ORDER BY id LIMIT 1",
                (user_id,)).fetchone()
        row = self._conn().execute(
            "SELECT * FROM trans_s3_accounts WHERE is_primary=1 LIMIT 1").fetchone()
        if row:
            return row
        return self._conn().execute(
            "SELECT * FROM trans_s3_accounts ORDER BY id LIMIT 1").fetchone()

    def set_primary_s3_account(self, aid):
        c = self._conn()
        row = c.execute("SELECT user_id FROM trans_s3_accounts WHERE id=?", (aid,)).fetchone()
        if not row:
            return
        c.execute("UPDATE trans_s3_accounts SET is_primary=0 WHERE user_id=?", (row["user_id"],))
        c.execute("UPDATE trans_s3_accounts SET is_primary=1 WHERE id=?", (aid,))
        c.commit()

    def update_s3_account(self, aid, **fields):
        if not fields:
            return
        c = self._conn()
        sets = ", ".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE trans_s3_accounts SET {sets} WHERE id=?",
                  list(fields.values()) + [aid])
        c.commit()

    def delete_s3_account(self, aid):
        c = self._conn()
        was_primary = c.execute(
            "SELECT is_primary, user_id FROM trans_s3_accounts WHERE id=?", (aid,)).fetchone()
        c.execute("DELETE FROM trans_s3_accounts WHERE id=?", (aid,))
        if was_primary and was_primary[0]:
            first = c.execute(
                "SELECT id FROM trans_s3_accounts WHERE user_id=? ORDER BY id LIMIT 1",
                (was_primary["user_id"],)).fetchone()
            if first:
                c.execute("UPDATE trans_s3_accounts SET is_primary=1 WHERE id=?", (first[0],))
        c.commit()

    # ── Cloudinary (transactional) ──────────────────────────
    def add_cloudinary_upload(self, source_filename: str, base_public_id: str,
                               folder: str, count: int, pixel_tweak: int,
                               proxy_id: int, user_id: int, group_id: int = 0) -> int:
        c = self._conn()
        c.execute(
            "INSERT INTO trans_cloudinary_uploads "
            "(source_filename, base_public_id, folder, count, pixel_tweak, proxy_id, user_id, group_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (source_filename, base_public_id, folder, count, pixel_tweak,
             proxy_id, user_id, int(group_id or 0)))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def add_cloudinary_link(self, upload_id: int, public_id: str, secure_url: str):
        c = self._conn()
        c.execute(
            "INSERT INTO trans_cloudinary_links (upload_id, public_id, secure_url) VALUES (?,?,?)",
            (upload_id, public_id, secure_url))
        c.commit()

    def get_cloudinary_uploads(self, user_id: int) -> list:
        return self._conn().execute(
            "SELECT * FROM trans_cloudinary_uploads WHERE user_id=? ORDER BY id DESC",
            (user_id,)).fetchall()

    def get_cloudinary_upload(self, upload_id: int, user_id: int):
        return self._conn().execute(
            "SELECT * FROM trans_cloudinary_uploads WHERE id=? AND user_id=?",
            (upload_id, user_id)).fetchone()

    def get_cloudinary_links(self, upload_id: int) -> list:
        return self._conn().execute(
            "SELECT * FROM trans_cloudinary_links WHERE upload_id=? ORDER BY id",
            (upload_id,)).fetchall()

    def delete_cloudinary_upload(self, upload_id: int, user_id: int):
        c = self._conn()
        c.execute("DELETE FROM trans_cloudinary_links WHERE upload_id IN "
                  "(SELECT id FROM trans_cloudinary_uploads WHERE id=? AND user_id=?)",
                  (upload_id, user_id))
        c.execute("DELETE FROM trans_cloudinary_uploads WHERE id=? AND user_id=?",
                  (upload_id, user_id))
        c.commit()

    # ── S3 Logo CDN — nutzt die bestehende trans_s3_accounts-Tabelle
    # (auch von Redirects verwendet). Ein IAM-Key für alles.
    def update_s3_account_buckets(self, aid, buckets, user_id):
        c = self._conn()
        c.execute("UPDATE trans_s3_accounts SET buckets=? WHERE id=? AND user_id=?",
                  (buckets, aid, user_id))
        c.commit()

    def add_s3_upload(self, account_id, filename, user_id, group_id: int = 0) -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_s3_uploads (account_id,filename,user_id,group_id) "
                  "VALUES (?,?,?,?)", (account_id, filename, user_id, int(group_id or 0)))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def add_s3_link(self, upload_id, url, bucket, s3_key):
        c = self._conn()
        c.execute("INSERT INTO trans_s3_links (upload_id,url,bucket,s3_key) "
                  "VALUES (?,?,?,?)", (upload_id, url, bucket, s3_key))
        c.execute("UPDATE trans_s3_uploads SET variant_count=variant_count+1 WHERE id=?",
                  (upload_id,))
        c.commit()

    def get_s3_uploads(self, user_id) -> list:
        return self._conn().execute(
            "SELECT u.*, a.name AS account_name FROM trans_s3_uploads u "
            "LEFT JOIN trans_s3_accounts a ON a.id=u.account_id "
            "WHERE u.user_id=? ORDER BY u.id DESC", (user_id,)).fetchall()

    def get_s3_links(self, upload_id) -> list:
        return self._conn().execute(
            "SELECT * FROM trans_s3_links WHERE upload_id=? ORDER BY id",
            (upload_id,)).fetchall()

    def delete_s3_upload(self, upload_id, user_id):
        c = self._conn()
        c.execute("DELETE FROM trans_s3_uploads WHERE id=? AND user_id=?",
                  (upload_id, user_id))
        c.commit()

    def delete_all_cloudinary_uploads(self, user_id: int,
                                        group_id: Optional[int] = None) -> int:
        """Löscht Cloudinary-Uploads + Links. group_id=None → alle des
        Users; sonst nur die dieser Group. Remote-Assets bei Cloudinary
        bleiben liegen (kann der User dort selber putzen)."""
        c = self._conn()
        if group_id is None:
            c.execute(
                "DELETE FROM trans_cloudinary_links WHERE upload_id IN "
                "(SELECT id FROM trans_cloudinary_uploads WHERE user_id=?)",
                (user_id,))
            cur = c.execute(
                "DELETE FROM trans_cloudinary_uploads WHERE user_id=?", (user_id,))
        else:
            c.execute(
                "DELETE FROM trans_cloudinary_links WHERE upload_id IN "
                "(SELECT id FROM trans_cloudinary_uploads WHERE user_id=? AND group_id=?)",
                (user_id, int(group_id)))
            cur = c.execute(
                "DELETE FROM trans_cloudinary_uploads WHERE user_id=? AND group_id=?",
                (user_id, int(group_id)))
        c.commit()
        return cur.rowcount or 0

    def delete_all_s3_uploads(self, user_id: int,
                                group_id: Optional[int] = None) -> int:
        """Löscht S3-Upload-Referenzen + Links. group_id=None → alle des
        Users; sonst nur die dieser Group. Bucket-Objekte bei AWS bleiben
        liegen (via S3 Logos → Delete-Bucket löschen)."""
        c = self._conn()
        if group_id is None:
            c.execute(
                "DELETE FROM trans_s3_links WHERE upload_id IN "
                "(SELECT id FROM trans_s3_uploads WHERE user_id=?)",
                (user_id,))
            cur = c.execute(
                "DELETE FROM trans_s3_uploads WHERE user_id=?", (user_id,))
        else:
            c.execute(
                "DELETE FROM trans_s3_links WHERE upload_id IN "
                "(SELECT id FROM trans_s3_uploads WHERE user_id=? AND group_id=?)",
                (user_id, int(group_id)))
            cur = c.execute(
                "DELETE FROM trans_s3_uploads WHERE user_id=? AND group_id=?",
                (user_id, int(group_id)))
        c.commit()
        return cur.rowcount or 0

    def get_all_cdn_urls(self, user_id, group_id: Optional[int] = None) -> list:
        """Union aus Cloudinary + S3 — der gemeinsame CDN-Pool den die
        Send-Loop nutzt. group_id=None → alle Uploads des Users
        (backwards-compat); int → nur diese Group (Multi-Pool). Reihenfolge
        irrelevant, wird eh random gepickt."""
        c = self._conn()
        if group_id is None:
            cloud = c.execute(
                "SELECT cl.secure_url AS url FROM trans_cloudinary_links cl "
                "JOIN trans_cloudinary_uploads cu ON cu.id=cl.upload_id "
                "WHERE cu.user_id=?", (user_id,)).fetchall()
            s3 = c.execute(
                "SELECT l.url FROM trans_s3_links l "
                "JOIN trans_s3_uploads u ON u.id=l.upload_id "
                "WHERE u.user_id=?", (user_id,)).fetchall()
        else:
            cloud = c.execute(
                "SELECT cl.secure_url AS url FROM trans_cloudinary_links cl "
                "JOIN trans_cloudinary_uploads cu ON cu.id=cl.upload_id "
                "WHERE cu.user_id=? AND cu.group_id=?",
                (user_id, int(group_id))).fetchall()
            s3 = c.execute(
                "SELECT l.url FROM trans_s3_links l "
                "JOIN trans_s3_uploads u ON u.id=l.upload_id "
                "WHERE u.user_id=? AND u.group_id=?",
                (user_id, int(group_id))).fetchall()
        return [r["url"] for r in cloud] + [r["url"] for r in s3]

    def get_cdn_stats_by_group(self, user_id) -> dict:
        """Für's Pool-Panel: {group_id: {"cloudinary": N, "s3": N}}."""
        c = self._conn()
        rows = c.execute(
            "SELECT cu.group_id AS gid, COUNT(cl.id) AS n "
            "FROM trans_cloudinary_uploads cu "
            "LEFT JOIN trans_cloudinary_links cl ON cl.upload_id=cu.id "
            "WHERE cu.user_id=? GROUP BY cu.group_id",
            (user_id,)).fetchall()
        out = {}
        for r in rows:
            out.setdefault(r["gid"] or 0, {"cloudinary": 0, "s3": 0})["cloudinary"] = r["n"] or 0
        rows = c.execute(
            "SELECT u.group_id AS gid, COUNT(l.id) AS n "
            "FROM trans_s3_uploads u "
            "LEFT JOIN trans_s3_links l ON l.upload_id=u.id "
            "WHERE u.user_id=? GROUP BY u.group_id",
            (user_id,)).fetchall()
        for r in rows:
            out.setdefault(r["gid"] or 0, {"cloudinary": 0, "s3": 0})["s3"] = r["n"] or 0
        return out

    # ── SMTP Check (send-test + IMAP delivery verify) ───────
    def create_smtp_check_job(self, **kw) -> int:
        c = self._conn()
        cols = list(kw.keys())
        vals = list(kw.values())
        ph = ",".join("?" for _ in vals)
        c.execute(f"INSERT INTO trans_smtp_check_jobs ({','.join(cols)}) VALUES ({ph})", vals)
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def update_smtp_check_job(self, job_id: int, **kw):
        if not kw:
            return
        c = self._conn()
        sets = ", ".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE trans_smtp_check_jobs SET {sets} WHERE id=?",
                  list(kw.values()) + [job_id])
        c.commit()

    def get_smtp_check_jobs(self, user_id: int) -> list:
        return self._conn().execute(
            "SELECT * FROM trans_smtp_check_jobs WHERE user_id=? ORDER BY id DESC",
            (user_id,)).fetchall()

    def get_smtp_check_job(self, job_id: int, user_id: int):
        return self._conn().execute(
            "SELECT * FROM trans_smtp_check_jobs WHERE id=? AND user_id=?",
            (job_id, user_id)).fetchone()

    def add_smtp_check_result(self, job_id: int, smtp_id: int, host: str, port: int,
                               username: str, password: str, marker: str) -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_smtp_check_results "
                  "(job_id, smtp_id, host, port, username, password, marker) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (job_id, smtp_id, host, port, username, password, marker))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def update_smtp_check_result(self, rid: int, status: str, error: str = ""):
        c = self._conn()
        c.execute("UPDATE trans_smtp_check_results SET status=?, error=? WHERE id=?",
                  (status, error, rid))
        c.commit()

    def get_smtp_check_results(self, job_id: int) -> list:
        return self._conn().execute(
            "SELECT * FROM trans_smtp_check_results WHERE job_id=? ORDER BY id",
            (job_id,)).fetchall()

    def delete_smtp_check_job(self, job_id: int, user_id: int):
        c = self._conn()
        c.execute("DELETE FROM trans_smtp_check_results WHERE job_id IN "
                  "(SELECT id FROM trans_smtp_check_jobs WHERE id=? AND user_id=?)",
                  (job_id, user_id))
        c.execute("DELETE FROM trans_smtp_check_jobs WHERE id=? AND user_id=?",
                  (job_id, user_id))
        c.commit()
