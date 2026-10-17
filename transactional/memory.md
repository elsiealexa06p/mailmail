# Transactional Web UI — Status

## Implemented
- [x] Campaign: create (SMTP list + lead list), start/stop, live stats (speed, elapsed, ETA), auto-reset
- [x] Campaign: test send, interval test, proxy check, blacklist check, pre-gen links
- [x] Campaign runner: multi-threaded, macros, spintax, antifingerprint, proxy from config
- [x] Campaign cleanup: logo variants cleared after FINISHED
- [x] HTML Editor: templates as groups, bulk upload HTMLs into template, inline editor
- [x] HTML Editor: preview raw/processed/AF/MIME/text-ratio, rotation config
- [x] Macros: add/edit/delete, .txt file import (filename=macro name), built-in ref
- [x] SMTP Lists: create named lists, bulk import (host,port,user,pass NO proxy)
- [x] SMTP Checker: threaded, proxy selection from saved configs, test send option
- [x] Lead Lists: create, paste import, file upload, bulk upload (file=list name)
- [x] Lead Lists: browse with search, state counts
- [x] Logos: source upload, variant generation to separate dir, count, clear
- [x] Redirects: Google Share generation, manual/bulk add, pool table
- [x] Proxies: separate tab, save named configs (single/pool), activate, rotate
- [x] Config: ALL settings from config.ini (sender, sending, AF, image, redirect, test, cloudinary, mxtoolbox)
- [x] Auth: login/setup, PBKDF2 sessions
- [x] Logs: SMTP error log viewer

## Architecture
- SMTP lists (no proxy in import)
- Lead lists (independent of campaigns)
- Macros (replaces content pools + spintax files)
- Proxies as saved configs, activated system-wide
- Templates as groups with multiple HTML files
- Config: single-row JSON with all settings + defaults
- Campaign references smtp_list_id + lead_list_id
- Variants in /logo_variants/, auto-cleaned after mailing
