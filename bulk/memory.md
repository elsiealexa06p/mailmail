# Bulk Mailer Web — Project Memory

## Architecture
- FastAPI + HTMX + Jinja2 (all Python, no React/npm)
- Same backend as PySide6 GUI: `bulk/mailer/*` shared
- SQLite (WAL mode), upgrade to PostgreSQL later
- Entry: `python bulk/web_server.py` → uvicorn on port 8000

## DB Schema (bulk.db)
- brands (id, name)
- domains (id, brand_id, domain, from_name, from_email, reply_to_email, bounce_subdomain, send_subdomain, unsub_worker_deployed, unsub_domain)
- smtp_presets (id, name, host, port, username, password, provider_type, daily_limit, sent_today, last_reset_date, proxy)
- lead_lists (id, name, file_origin, lead_count)
- leads (id, list_id, email, state, error_msg)
- brand_list_usage (brand_id, list_id, mailing_id, used_at)
- macros (id, name, values_json, rotate_every)
- message_templates (id, name, html_files_json, html_rotate_every, pdf_path, pdf_macro_enabled, subject_macro, sender_rotate_json, sender_rotate_every, settings_json)
- mailings (id, name, brand_id, domain_id, smtp_preset_id, list_id, template_id, status, total_leads, sent, failed, excluded, daily_limit, exclude_domains_json, test_email, test_interval, schedule_time, started_at, paused_at, finished_at)
- cf_accounts (id, name, auth_type, api_token, global_api_key, auth_email, account_id, r2_access_key, r2_secret_key)

## Web Pages Needed
1. **Mailings** (main page) — table of all mailings, add/edit/start/stop/delete, live progress, events log
2. **Brands** — tree: brand → domains, domain settings, unsub deploy, list usage
3. **SMTP** — presets table, add/edit/delete, provider type, proxy, daily limit
4. **Lists** — import, search, bulk delete, exclude rules, compare
5. **Composer** — templates: HTML files/paste/folder, subject macro, PDF
6. **Macros** — CRUD, rotate_every, export/import, insert dialog
7. **Preview** — from/to/subject inputs, provider toggle, raw MIME headers
8. **Cloudflare** — accounts, R2 buckets, file upload, worker deploy, domain sync
9. **Logs** — smtp_errors.log tail, mailing history, DB management
10. **Macro Help** — reference page

## MIME Builder Features (bulk/mailer/bulk_mime_builder.py)
- Provider-aware: SES (no Return-Path, X-SES-MESSAGE-TAGS) vs Generic (Return-Path)
- List-Unsubscribe + One-Click (RFC 8058)
- VERP envelope with recipient_id
- Feedback-ID with format validation
- List-Id with quoted phrase
- HTML always QP, plain text 7bit/QP auto
- Inline images (multipart/related)
- Attachments (multipart/mixed)
- Custom headers (blocked for reserved)
- No Date header (set by send layer)

## Mailer Core Features (bulk/mailer/bulk_core.py)
- Rate limiter (daily limit → even distribution with Gaussian jitter)
- PDF macro (fill hidden form field per mail)
- Sender/subject rotation via macros
- HTML rotation
- Unsub auto-insert when domain has worker deployed
- Test interval (sends test mail every N sends)
- Schedule (waits until HH:MM before starting)
- Proxy per SMTP (SOCKS5)

## Key Decisions
- No AntiFingerprint (this is bulk, not transactional)
- No CID logos (external CDN via R2)
- No Spintax file injection (macros from DB instead)
- Sender rotation via macros, not hardcoded list
- List-Id auto-generated as newsletter.{domain}
- Unsub worker on Cloudflare (KV store, auto-deploy per domain)
