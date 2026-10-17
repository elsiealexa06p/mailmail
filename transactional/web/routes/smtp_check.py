"""SMTP Check — send a plain-text probe from every SMTP in a list through
a proxy, then IMAP-poll the receiver mailbox to see which probes actually
arrived. Categorises SMTPs into:

  * delivered           — probe arrived at the receiver
  * connection_error    — never got to send (proxy, timeout, auth, TLS…)
  * not_delivered       — send returned success but nothing showed up

Delivered SMTPs get dumped into a new 'validated' list; the other two
buckets are exposed as plain-text downloads.
"""
import re
import time
import secrets
import imaplib
import logging
import threading
from html import escape
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import formataddr

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .smtps import _connect_smtp
from .inboxtest import _imap_connect

logger = logging.getLogger("trans.smtpcheck")
router = APIRouter()

# in-process progress (one active job per user is enforced)
_progress: dict = {}


def _prog(uid: int) -> dict:
    p = _progress.get(uid)
    if not p:
        p = {"running": False, "phase": "", "job_id": 0,
             "total": 0, "sent": 0, "conn_errors": 0,
             "delivered": 0, "not_delivered": 0,
             "log": []}
        _progress[uid] = p
    return p


IMAP_FOLDERS = ["INBOX", "Junk", "Spam", "INBOX.spam", "INBOX.Junk",
                "Bulk Mail", "Bulk", "[Gmail]/Spam"]


def _classify_send_error(error: str, error_type: str) -> str:
    """Map (_connect_smtp | send) error into a bucket."""
    if not error:
        return "connection_error"
    e = (error or "").lower()
    if "proxy" in e or error.startswith("Proxy:"):
        return "proxy_error"
    if error_type == "auth":
        return "auth_error"
    if error_type in ("timeout", "connection"):
        return "connection_error"
    return "connection_error"


def _resolve_proxy_value(db, proxy_id: int, cfg: dict) -> str:
    """Pick a proxy line: explicit proxy_id → its first line, else the
    global active proxy config, else empty."""
    if proxy_id:
        row = db.get_proxy(proxy_id)
        if row:
            return (dict(row).get("value") or "").splitlines()[0].strip() \
                if (dict(row).get("value") or "").strip() else ""
    if cfg.get("proxy_mode", "off") != "off":
        pv = (cfg.get("proxy_value") or "").strip()
        if pv:
            return pv.splitlines()[0].strip()
    return ""


@router.get("/smtp-check", response_class=HTMLResponse)
async def smtp_check_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    smtp_lists = []
    for sl in db.get_smtp_lists(uid):
        d = dict(sl)
        d["count"] = db.get_smtp_count(d["id"])
        smtp_lists.append(d)
    inbox_accounts = [dict(a) for a in db.get_inbox_accounts(uid)]
    proxies = [dict(p) for p in db.get_proxies(uid)]
    jobs = [dict(j) for j in db.get_smtp_check_jobs(uid)]
    for j in jobs:
        # find human labels for the FKs
        j["source_list_name"] = next(
            (l["name"] for l in smtp_lists if l["id"] == j.get("source_list_id")), "")
        j["proxy_name"] = next(
            (p["name"] for p in proxies if p["id"] == j.get("proxy_id")), "")
    return request.app.state.templates.TemplateResponse(request, "smtp_check.html", {
        "active": "smtp_check",
        "smtp_lists": smtp_lists,
        "inbox_accounts": inbox_accounts,
        "proxies": proxies,
        "jobs": jobs,
        "progress": _prog(uid),
    })


@router.post("/smtp-check/run", response_class=HTMLResponse)
async def start_job(request: Request,
                    smtp_list_id: int = Form(0),
                    receiver_account_id: int = Form(0),
                    proxy_id: int = Form(0),
                    subject: str = Form("Hallo"),
                    body_text: str = Form("Hallo, das ist ein kurzer Verbindungs-Test. Kein Werbeinhalt."),
                    from_name: str = Form("Test"),
                    wait_seconds: int = Form(90),
                    name: str = Form("")):
    db = request.app.state.db
    uid = request.state.user["id"]
    prog = _prog(uid)

    if prog["running"]:
        return HTMLResponse('<div class="alert alert-warning">A check is already running.</div>')
    if not smtp_list_id:
        return HTMLResponse('<div class="alert alert-warning">Pick an SMTP list.</div>')
    if not receiver_account_id:
        return HTMLResponse('<div class="alert alert-warning">Pick a receiver mailbox.</div>')

    smtps = [dict(s) for s in db.get_smtps(smtp_list_id)]
    if not smtps:
        return HTMLResponse('<div class="alert alert-warning">SMTP list is empty.</div>')

    inbox_row = None
    for a in db.get_inbox_accounts(uid):
        if dict(a)["id"] == receiver_account_id:
            inbox_row = dict(a)
            break
    if not inbox_row:
        return HTMLResponse('<div class="alert alert-danger">Receiver mailbox not found.</div>')

    proxy_row = None
    if proxy_id:
        prow = db.get_proxy(proxy_id)
        if prow and dict(prow).get("user_id") in (uid, 0):
            proxy_row = dict(prow)

    cfg = db.get_config()
    proxy_val = _resolve_proxy_value(db, proxy_id, cfg)
    if not proxy_val:
        return HTMLResponse(
            '<div class="alert alert-danger">A proxy is required (pick one or set '
            'a global proxy on the Proxies page).</div>')

    wait_seconds = max(15, min(int(wait_seconds or 90), 900))
    subject = (subject or "Hallo").strip()[:200]
    body_text = (body_text or "").strip() or "Hallo, das ist ein kurzer Verbindungs-Test."
    from_name = (from_name or "Test").strip()[:80]
    receiver_email = inbox_row["email"]
    job_name = name.strip() or f"SMTP-Check {datetime.now().strftime('%d.%m %H:%M')}"

    job_id = db.create_smtp_check_job(
        name=job_name, source_list_id=smtp_list_id,
        receiver_account_id=receiver_account_id,
        receiver_email=receiver_email,
        proxy_id=proxy_id, subject=subject, body_text=body_text,
        from_name=from_name, wait_seconds=wait_seconds,
        total=len(smtps), status="RUNNING", user_id=uid)

    # Pre-create result rows with unique markers
    prepared = []
    for s in smtps:
        marker = f"SMTPCHK{job_id}X{secrets.token_hex(4)}"
        rid = db.add_smtp_check_result(
            job_id=job_id, smtp_id=s["id"], host=s["host"], port=int(s["port"]),
            username=s["username"], password=s["password"], marker=marker)
        prepared.append({**s, "_rid": rid, "_marker": marker})

    prog.update(running=True, phase="sending", job_id=job_id,
                total=len(smtps), sent=0, conn_errors=0,
                delivered=0, not_delivered=0, log=[])

    def worker():
        try:
            _run_check(db, uid, job_id, prepared, proxy_val,
                       subject, body_text, from_name,
                       receiver_email, inbox_row, wait_seconds, prog)
        except Exception as e:
            logger.error("SMTP check worker crashed: %s", e, exc_info=True)
            prog["log"].append(f"crashed: {e}")
            db.update_smtp_check_job(job_id, status="FAILED",
                                      finished_at=datetime.now().isoformat())
        finally:
            prog["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return HTMLResponse(
        f'<div class="alert alert-info">Job #{job_id} running — sending {len(smtps)} probe(s), '
        f'then waiting {wait_seconds}s before IMAP check.</div>'
        f'<div hx-get="/smtp-check/progress" hx-trigger="every 2s" hx-swap="outerHTML"></div>'
    )


def _run_check(db, uid, job_id, prepared, proxy_val,
                subject_tpl, body_text, from_name,
                receiver_email, inbox_row, wait_seconds, prog):
    # ── phase 1: send ──────────────────────────────────
    prog["phase"] = "sending"
    for i, s in enumerate(prepared):
        rid = s["_rid"]
        marker = s["_marker"]
        subj = f"{subject_tpl} [{marker}]"
        display_from = formataddr((from_name, s["username"]))
        msg = MIMEText(body_text, "plain", "utf-8")
        msg["From"] = display_from
        msg["To"] = receiver_email
        msg["Subject"] = subj

        server, error, etype = _connect_smtp(
            s["host"], int(s["port"]), s["username"], s["password"],
            proxy_val, timeout=25)
        if not server:
            bucket = _classify_send_error(error or "", etype or "")
            db.update_smtp_check_result(rid, bucket, (error or "")[:250])
            prog["conn_errors"] += 1
            prog["log"].append(f"{s['username']}: {bucket} — {(error or '')[:80]}")
        else:
            try:
                server.send_message(msg)
                db.update_smtp_check_result(rid, "sent", "")
                prog["sent"] += 1
            except Exception as e:
                # Login worked, but sending failed. Treat as connection-side.
                db.update_smtp_check_result(rid, "send_error", str(e)[:250])
                prog["conn_errors"] += 1
                prog["log"].append(f"{s['username']}: send_error — {str(e)[:80]}")
            finally:
                try:
                    server.quit()
                except Exception:
                    pass
        # small pacing between hosts so we don't hammer any single MX
        time.sleep(0.4)

    # ── phase 2: wait ──────────────────────────────────
    prog["phase"] = f"waiting {wait_seconds}s"
    prog["log"].append(f"waiting {wait_seconds}s for delivery")
    time.sleep(wait_seconds)

    # ── phase 3: IMAP hunt ─────────────────────────────
    prog["phase"] = "imap-check"
    prog["log"].append(f"connecting IMAP {inbox_row['imap_host']}")
    try:
        mail = _imap_connect(
            inbox_row["imap_host"], int(inbox_row.get("imap_port") or 993),
            inbox_row.get("username") or inbox_row["email"],
            inbox_row["password"], (inbox_row.get("proxy") or "").strip())
    except Exception as e:
        prog["log"].append(f"IMAP connect failed: {e}")
        db.update_smtp_check_job(job_id, status="FAILED",
                                  finished_at=datetime.now().isoformat())
        return

    # Collect every marker that appears in any of the tested folders.
    found_markers = set()
    try:
        for folder in IMAP_FOLDERS:
            try:
                status, _ = mail.select(folder, readonly=True)
                if status != "OK":
                    continue
            except Exception:
                continue
            for s in prepared:
                m = s["_marker"]
                if m in found_markers:
                    continue
                try:
                    _, ids = mail.search(None, f'(SUBJECT "{m}")')
                    if ids and ids[0]:
                        found_markers.add(m)
                except Exception:
                    continue
    finally:
        try:
            mail.close()
        except Exception:
            pass
        try:
            mail.logout()
        except Exception:
            pass

    # ── phase 4: reconcile ─────────────────────────────
    delivered = 0
    not_delivered = 0
    for s in prepared:
        rid = s["_rid"]
        cur = db._conn().execute(
            "SELECT status FROM trans_smtp_check_results WHERE id=?", (rid,)
        ).fetchone()
        if not cur:
            continue
        cur_status = cur["status"]
        # Only re-evaluate rows that actually got sent — connection-side
        # failures keep their bucket so the user sees the real cause.
        if cur_status != "sent":
            continue
        if s["_marker"] in found_markers:
            db.update_smtp_check_result(rid, "delivered", "")
            delivered += 1
        else:
            db.update_smtp_check_result(rid, "not_delivered", "")
            not_delivered += 1

    prog["delivered"] = delivered
    prog["not_delivered"] = not_delivered
    prog["phase"] = "done"
    prog["log"].append(f"done — {delivered} delivered, {not_delivered} not delivered, "
                        f"{prog['conn_errors']} connection issues")
    db.update_smtp_check_job(
        job_id, status="DONE", delivered=delivered,
        conn_errors=prog["conn_errors"], invalid=not_delivered,
        finished_at=datetime.now().isoformat())


@router.get("/smtp-check/progress", response_class=HTMLResponse)
async def progress(request: Request):
    uid = request.state.user["id"]
    p = _prog(uid)
    if not p["running"] and not p["log"]:
        return HTMLResponse("")

    total = p["total"] or 1
    done_send = p["sent"] + p["conn_errors"]
    if p["phase"] == "sending":
        pct = int(done_send / total * 100)
        bar_label = f"{done_send}/{total} sent"
    elif p["phase"].startswith("waiting"):
        pct = 100
        bar_label = p["phase"]
    elif p["phase"] == "imap-check":
        pct = 100
        bar_label = "IMAP check"
    else:
        pct = 100
        bar_label = "done"

    tally = (f'<span style="color:var(--green)">{p["delivered"]} delivered</span> · '
             f'<span style="color:var(--red)">{p["not_delivered"]} not delivered</span> · '
             f'<span style="color:var(--orange,#c88400)">{p["conn_errors"]} connection issues</span>')

    log_html = ""
    if p["log"]:
        rows = "".join(
            f'<div style="color:var(--fg2)">{escape(l)}</div>' for l in p["log"][-15:])
        log_html = (f'<div style="font-family:monospace;font-size:11px;max-height:180px;'
                    f'overflow-y:auto;background:#f8f9fa;padding:8px;border-radius:4px;'
                    f'margin-top:6px">{rows}</div>')

    if p["running"]:
        return HTMLResponse(
            f'<div hx-get="/smtp-check/progress" hx-trigger="every 2s" hx-swap="outerHTML">'
            f'<div style="font-size:12px;margin-bottom:4px">Phase: <strong>{escape(p["phase"])}</strong></div>'
            f'<div class="progress" style="margin-bottom:6px"><div class="progress-bar" '
            f'style="width:{pct}%">{escape(bar_label)}</div></div>'
            f'<p style="font-size:12px">{tally}</p>{log_html}</div>')

    return HTMLResponse(
        f'<div class="alert alert-success">Job #{p["job_id"]} finished — {tally}. '
        f'<a href="/smtp-check/job/{p["job_id"]}" style="color:var(--accent)">Open job</a> · '
        f'<a href="/smtp-check" style="color:var(--accent)">Reload</a></div>{log_html}')


@router.get("/smtp-check/job/{jid}", response_class=HTMLResponse)
async def job_detail(request: Request, jid: int):
    db = request.app.state.db
    uid = request.state.user["id"]
    job_row = db.get_smtp_check_job(jid, uid)
    if not job_row:
        return HTMLResponse('<div class="alert alert-danger">Job not found.</div>')
    job = dict(job_row)
    results = [dict(r) for r in db.get_smtp_check_results(jid)]

    buckets = {"delivered": [], "not_delivered": [],
               "connection_error": [], "proxy_error": [],
               "auth_error": [], "send_error": [], "sent": [], "pending": []}
    for r in results:
        buckets.setdefault(r["status"], []).append(r)

    return request.app.state.templates.TemplateResponse(request, "smtp_check_job.html", {
        "active": "smtp_check",
        "job": job,
        "results": results,
        "buckets": buckets,
        "smtp_lists": [dict(sl) for sl in db.get_smtp_lists(uid)],
    })


def _lines_for_bucket(results: list, statuses: set) -> str:
    return "\n".join(
        f'{r["host"]},{r["port"]},{r["username"]},{r["password"]}'
        for r in results if r["status"] in statuses)


BUCKET_STATUSES = {
    "delivered":   {"delivered"},
    "connection":  {"connection_error", "proxy_error", "auth_error", "send_error"},
    "invalid":     {"not_delivered"},
}


@router.get("/smtp-check/job/{jid}/export/{bucket}.txt")
async def export_bucket(request: Request, jid: int, bucket: str):
    db = request.app.state.db
    uid = request.state.user["id"]
    if not db.get_smtp_check_job(jid, uid):
        return Response(content="", media_type="text/plain", status_code=404)
    statuses = BUCKET_STATUSES.get(bucket)
    if not statuses:
        return Response(content="", media_type="text/plain", status_code=400)
    results = [dict(r) for r in db.get_smtp_check_results(jid)]
    body = _lines_for_bucket(results, statuses)
    return Response(content=body, media_type="text/plain",
                    headers={"Content-Disposition": f'attachment; filename=smtpcheck_{jid}_{bucket}.txt'})


@router.post("/smtp-check/job/{jid}/save-list", response_class=HTMLResponse)
async def save_bucket_as_list(request: Request, jid: int,
                               bucket: str = Form("delivered"),
                               list_name: str = Form("")):
    """Dump one bucket into a new trans_smtps list."""
    db = request.app.state.db
    uid = request.state.user["id"]
    job = db.get_smtp_check_job(jid, uid)
    if not job:
        return HTMLResponse('<span style="color:var(--red)">Job not found.</span>')
    statuses = BUCKET_STATUSES.get(bucket)
    if not statuses:
        return HTMLResponse('<span style="color:var(--red)">Unknown bucket.</span>')
    results = [dict(r) for r in db.get_smtp_check_results(jid)
                if r["status"] in statuses]
    if not results:
        return HTMLResponse('<span style="color:var(--fg2)">No SMTPs in this bucket.</span>')

    default_name = {
        "delivered": f"Validated (job {jid})",
        "connection": f"Connection issues (job {jid})",
        "invalid": f"Invalid (job {jid})",
    }[bucket]
    name = (list_name or "").strip() or default_name

    new_id = db.create_smtp_list(name, uid)
    conn = db._conn()
    conn.executemany(
        "INSERT INTO trans_smtps (list_id,host,port,username,password) VALUES (?,?,?,?,?)",
        [(new_id, r["host"], int(r["port"]), r["username"], r["password"]) for r in results])
    conn.commit()
    return HTMLResponse(
        f'<span style="color:var(--green)">Saved {len(results)} SMTP(s) to '
        f'<strong>{escape(name)}</strong>.</span> '
        f'<a href="/smtps" style="color:var(--accent);font-size:12px">Open SMTP Lists</a>'
    )


@router.post("/smtp-check/job/{jid}/delete")
async def delete_job(request: Request, jid: int):
    db = request.app.state.db
    uid = request.state.user["id"]
    db.delete_smtp_check_job(jid, uid)
    return RedirectResponse("/smtp-check", status_code=303)
