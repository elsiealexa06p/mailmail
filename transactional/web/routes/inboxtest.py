"""Inbox Test — send test emails, check IMAP for inbox/spam placement."""
import os
import re
import json
import time
import random
import imaplib
import email as email_lib
import threading
import logging
import secrets
from html import escape
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger("trans.inboxtest")
router = APIRouter()

_test_progress = {"running": False, "phase": "", "done": 0, "total": 0, "log": []}


def _imap_connect(host, port, username, password, proxy=""):
    """Connect to IMAP, optionally via SOCKS5 proxy."""
    if proxy and proxy.strip():
        try:
            import socks
            import socket
            parts = proxy.strip().split(":")
            phost = parts[0]
            pport = int(parts[1]) if len(parts) > 1 else 1080
            puser = parts[2] if len(parts) > 2 else None
            ppass = parts[3] if len(parts) > 3 else None
            sock = socks.socksocket()
            sock.set_proxy(socks.SOCKS5, phost, pport, username=puser, password=ppass)
            sock.connect((host, port))
            ctx = imaplib.IMAP4_SSL_PORT
            mail = imaplib.IMAP4_SSL(host=host, port=port)
            mail.sock = sock
            mail.file = sock.makefile("rb")
        except Exception:
            mail = imaplib.IMAP4_SSL(host, port)
    else:
        mail = imaplib.IMAP4_SSL(host, port)
    mail.login(username, password)
    return mail


def _check_inbox(account, subject_marker, timeout_minutes=5):
    """Check if email arrived in inbox or spam."""
    acct = dict(account)
    proxy = acct.get("proxy", "")
    try:
        mail = _imap_connect(acct["imap_host"], acct.get("imap_port", 993),
                              acct.get("username") or acct["email"],
                              acct["password"], proxy)
    except Exception as e:
        return "ERROR", "", str(e)[:200]

    result = "MISSING"
    folder_found = ""

    folders_to_check = ["INBOX", "Junk", "Spam", "INBOX.spam", "INBOX.Junk",
                        "Bulk Mail", "Bulk", "[Gmail]/Spam"]

    try:
        for folder in folders_to_check:
            try:
                status, _ = mail.select(folder, readonly=True)
                if status != "OK":
                    continue
            except Exception:
                continue

            try:
                _, msg_ids = mail.search(None, f'(SUBJECT "{subject_marker}")')
                if msg_ids[0]:
                    is_spam = folder.lower() in ("junk", "spam", "inbox.spam", "inbox.junk",
                                                  "bulk mail", "bulk", "[gmail]/spam")
                    result = "SPAM" if is_spam else "INBOX"
                    folder_found = folder
                    break
            except Exception:
                continue
    finally:
        try:
            mail.close()
            mail.logout()
        except Exception:
            pass

    return result, folder_found, ""


@router.get("/inboxtest", response_class=HTMLResponse)
async def inboxtest_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    accounts = [dict(a) for a in db.get_inbox_accounts(uid)]
    tests = [dict(t) for t in db.get_inbox_tests(uid)]
    smtp_lists = [dict(sl) for sl in db.get_smtp_lists(uid)]
    templates = [dict(t) for t in db.get_templates(uid)]

    for t in tests:
        results = [dict(r) for r in db.get_inbox_results(t["id"])]
        t["results"] = results
        t["inbox"] = sum(1 for r in results if r["result"] == "INBOX")
        t["spam"] = sum(1 for r in results if r["result"] == "SPAM")
        t["missing"] = sum(1 for r in results if r["result"] == "MISSING")
        t["pending"] = sum(1 for r in results if r["result"] == "PENDING")
        t["errors"] = sum(1 for r in results if r["result"] == "ERROR")
        t["total"] = len(results)

    return request.app.state.templates.TemplateResponse(request, "inboxtest.html", {
        "active": "inboxtest", "accounts": accounts, "tests": tests,
        "smtp_lists": smtp_lists, "templates": templates,
        "progress": _test_progress, "db": db,
    })


@router.post("/inboxtest/add-account")
async def add_account(request: Request, provider: str = Form(""),
                      account_email: str = Form(""), imap_host: str = Form(""),
                      imap_port: int = Form(993), username: str = Form(""),
                      password: str = Form(""), proxy: str = Form("")):
    if account_email.strip() and imap_host.strip():
        uid = request.state.user["id"]
        request.app.state.db.add_inbox_account(
            provider.strip(), account_email.strip(), imap_host.strip(),
            imap_port, username.strip() or account_email.strip(),
            password.strip(), proxy.strip(), uid)
    return RedirectResponse("/inboxtest", status_code=303)


@router.post("/inboxtest/account/{aid}/delete")
async def delete_account(request: Request, aid: int):
    request.app.state.db.delete_inbox_account(aid)
    return RedirectResponse("/inboxtest", status_code=303)


@router.post("/inboxtest/run", response_class=HTMLResponse)
async def run_test(request: Request,
                   test_name: str = Form(""),
                   smtp_list_id: int = Form(0),
                   template_id: int = Form(0),
                   mime_profile: str = Form("rotate"),
                   subject: str = Form(""),
                   from_name: str = Form("")):
    if _test_progress["running"]:
        return HTMLResponse('<div class="alert alert-warning">Test already running.</div>')

    db = request.app.state.db
    uid = request.state.user["id"]
    accounts = [dict(a) for a in db.get_inbox_accounts(uid)]
    if not accounts:
        return HTMLResponse('<div class="alert alert-danger">No test accounts configured.</div>')

    smtps = [dict(s) for s in db.get_smtps(smtp_list_id)]
    if not smtps:
        return HTMLResponse('<div class="alert alert-danger">No SMTPs in selected list.</div>')

    html_bodies = db.get_all_template_htmls(uid, template_id=template_id)
    if not html_bodies:
        return HTMLResponse('<div class="alert alert-danger">No HTML templates found.</div>')

    cfg = db.get_config()
    name = test_name.strip() or f"Test {datetime.now().strftime('%d.%m %H:%M')}"
    test_id = db.create_inbox_test(name, smtp_list_id, template_id, mime_profile,
                                    subject or cfg.get("subject", "Test"), from_name or cfg.get("from_name", "Test"),
                                    len(accounts), uid)

    _test_progress.update(running=True, phase="sending", done=0, total=len(accounts), log=[])

    def worker():
        try:
            _run_inbox_test(db, test_id, accounts, smtps, html_bodies, cfg, mime_profile, subject, from_name)
        except Exception as e:
            logger.error("Inbox test error: %s", e, exc_info=True)
            _test_progress["log"].append(f"Error: {e}")
        finally:
            _test_progress["running"] = False
            db.update_inbox_test(test_id, status="DONE")

    threading.Thread(target=worker, daemon=True).start()
    return HTMLResponse(
        f'<div class="alert alert-info">Test "{escape(name)}" started — sending to {len(accounts)} accounts...</div>'
        f'<div hx-get="/inboxtest/progress" hx-trigger="every 2s" hx-swap="innerHTML"></div>'
    )


def _run_inbox_test(db, test_id, accounts, smtps, html_bodies, cfg, mime_profile_mode, subject_override, from_name_override):
    """Send test emails and record what was used."""
    from mailer.mime_builder import MIMEBuilder

    subject_tpl = subject_override.strip() or cfg.get("subject", "Test")
    from_name_tpl = from_name_override.strip() or cfg.get("from_name", "Test")

    macros = {}
    for m in db.get_active_macros():
        md = dict(m)
        lines = [l.strip() for l in (md.get("values_text") or "").splitlines() if l.strip()]
        if lines:
            macros[md["name"]] = lines

    smtp_idx = 0
    for i, acct in enumerate(accounts):
        smtp = smtps[smtp_idx % len(smtps)]
        smtp_idx += 1
        html = html_bodies[i % len(html_bodies)]

        marker = f"IBXT-{test_id}-{secrets.token_hex(4)}"
        cur_subject = f"{_process_text(subject_tpl, acct['email'], macros)} [{marker}]"
        cur_from = _process_text(from_name_tpl, acct["email"], macros)
        from_email = cfg.get("from_email", "") or smtp["username"]
        html_processed = _process_text(html, acct["email"], macros)
        html_processed = html_processed.replace("{Logo}", "").replace("{RedirectLink}", "#")

        plain = re.sub(r"<[^>]+>", "", html_processed).strip()

        profile = mime_profile_mode
        if profile == "rotate":
            from mailer.mime_profiles import get_random_profile
            profile = get_random_profile()

        result_id = db.add_inbox_result(
            test_id, acct["id"], acct["email"], acct.get("provider", ""),
            smtp["host"], smtp["username"],
            f"html_{i % len(html_bodies)}", profile, cur_subject, cur_from)

        try:
            raw_msg = MIMEBuilder.build_email(
                from_name=cur_from, from_email=from_email,
                to_email=acct["email"], subject=cur_subject,
                html_body=html_processed, plain_body=plain)

            if mime_profile_mode != "default":
                from mailer.mime_profiles import apply_profile
                raw_msg = apply_profile(raw_msg, profile, from_email)

            _send_test_mail(smtp, from_email, acct["email"], raw_msg, cfg)
            _test_progress["log"].append(f"Sent to {acct['email']} via {smtp['host']}")
        except Exception as e:
            db.update_inbox_result(result_id, "ERROR", "", str(e)[:200])
            _test_progress["log"].append(f"FAILED {acct['email']}: {e}")

        _test_progress["done"] = i + 1

    _test_progress["phase"] = "waiting"
    _test_progress["log"].append("Waiting 60s for delivery...")
    time.sleep(60)

    _test_progress["phase"] = "checking"
    _test_progress["done"] = 0

    results = [dict(r) for r in db.get_inbox_results(test_id)]
    for i, r in enumerate(results):
        if r["result"] in ("ERROR",):
            _test_progress["done"] = i + 1
            continue

        acct_row = None
        for a in accounts:
            if a["id"] == r["account_id"]:
                acct_row = a
                break
        if not acct_row:
            _test_progress["done"] = i + 1
            continue

        marker = ""
        subj = r.get("subject_used", "")
        m = re.search(r"\[IBXT-\d+-[a-f0-9]+\]", subj)
        if m:
            marker = m.group(0)[1:-1]

        if marker:
            result, folder, err = _check_inbox(acct_row, marker)
            db.update_inbox_result(r["id"], result, folder, err)
            _test_progress["log"].append(f"{acct_row['email']}: {result} ({folder or err or 'n/a'})")
        else:
            db.update_inbox_result(r["id"], "ERROR", "", "No marker in subject")

        _test_progress["done"] = i + 1

    _test_progress["log"].append("Done!")


def _send_test_mail(smtp_row, from_email, to_email, raw_msg, cfg):
    """Send one email via SMTP."""
    import smtplib
    import ssl

    host = smtp_row["host"]
    port = smtp_row["port"]
    user = smtp_row["username"]
    pwd = smtp_row["password"]

    proxy_mode = cfg.get("proxy_mode", "off")
    proxy_value = cfg.get("proxy_value", "")

    if proxy_mode != "off" and proxy_value.strip():
        proxy = proxy_value.strip().splitlines()[0].strip()
        parts = proxy.split(":")
        import socks
        import socket
        sock = socks.socksocket()
        sock.set_proxy(socks.SOCKS5, parts[0], int(parts[1]) if len(parts) > 1 else 1080,
                        username=parts[2] if len(parts) > 2 else None,
                        password=parts[3] if len(parts) > 3 else None)
        sock.connect((host, port))
        if port == 465:
            ctx = ssl.create_default_context()
            if cfg.get("ignore_ssl_errors"):
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
            server = smtplib.SMTP_SSL(host, port)
            server.sock = sock
            server.file = sock.makefile("rb")
            server.getreply()
        else:
            server = smtplib.SMTP(host, port)
            server.sock = sock
            server.file = sock.makefile("rb")
            server.getreply()
            server.ehlo(user.split("@")[1] if "@" in user else host)
            server.starttls()
    else:
        if port == 465:
            ctx = ssl.create_default_context()
            if cfg.get("ignore_ssl_errors"):
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            server = smtplib.SMTP_SSL(host, port, context=ctx)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.ehlo(user.split("@")[1] if "@" in user else host)
            server.starttls()

    server.login(user, pwd)
    server.sendmail(from_email, to_email, raw_msg)
    server.quit()


def _process_text(text, email_addr, macros):
    user = email_addr.split("@")[0] if "@" in email_addr else email_addr
    domain = email_addr.split("@")[1] if "@" in email_addr else ""
    text = text.replace("{email}", email_addr).replace("{email_user}", user).replace("{domain}", domain)
    for mname, mlines in macros.items():
        text = text.replace(f"{{{mname}}}", random.choice(mlines))
    def _spintax(m):
        return random.choice(m.group(1).split("|"))
    for _ in range(20):
        new = re.sub(r"\{([^{}]+\|[^{}]+)\}", _spintax, text)
        if new == text:
            break
        text = new
    import string
    def _randstr(m):
        length, charset_name, case = int(m.group(1)), m.group(2), m.group(3)
        cs = {"a-z": string.ascii_lowercase, "A-Z": string.ascii_uppercase,
              "0-9": string.digits, "a-z0-9": string.ascii_lowercase + string.digits}.get(charset_name, string.ascii_lowercase)
        r = "".join(random.choice(cs) for _ in range(length))
        return r.upper() if case == "upper" else r.lower() if case == "lower" else r
    text = re.sub(r"\[RANDSTR:(\d+):([a-zA-Z0-9\-]+):(\w+)\]", _randstr, text)
    return text


@router.get("/inboxtest/progress", response_class=HTMLResponse)
async def test_progress(request: Request):
    p = _test_progress
    if not p["running"] and not p["log"]:
        return HTMLResponse("")

    phase = p["phase"]
    done = p["done"]
    total = p["total"]
    pct = int(done / total * 100) if total > 0 else 0

    html = f'<div class="progress" style="margin-bottom:8px"><div class="progress-bar" style="width:{pct}%">{done}/{total} ({phase})</div></div>'
    if p["log"]:
        html += '<div style="font-family:monospace;font-size:11px;max-height:200px;overflow-y:auto;background:#f8f9fa;padding:8px;border-radius:4px">'
        for line in p["log"][-20:]:
            color = "var(--green)" if "INBOX" in line else "var(--red)" if ("SPAM" in line or "FAILED" in line) else "var(--fg2)"
            html += f'<div style="color:{color}">{escape(line)}</div>'
        html += '</div>'

    if not p["running"]:
        html += '<div class="alert alert-success" style="margin-top:8px">Test complete. <a href="/inboxtest" style="color:var(--accent)">Reload</a></div>'
    else:
        html += '<div hx-get="/inboxtest/progress" hx-trigger="every 2s" hx-swap="outerHTML"></div>'

    return HTMLResponse(html)


@router.post("/inboxtest/{tid}/check-imap", response_class=HTMLResponse)
async def recheck_imap(request: Request, tid: int):
    """Re-check IMAP for a test (manual re-scan)."""
    if _test_progress["running"]:
        return HTMLResponse('<div class="alert alert-warning">Test running.</div>')

    db = request.app.state.db
    uid = request.state.user["id"]
    accounts = {a["id"]: dict(a) for a in db.get_inbox_accounts(uid)}
    results = [dict(r) for r in db.get_inbox_results(tid)]

    _test_progress.update(running=True, phase="re-checking", done=0, total=len(results), log=[])

    def worker():
        for i, r in enumerate(results):
            acct = accounts.get(r["account_id"])
            if not acct:
                _test_progress["done"] = i + 1
                continue
            subj = r.get("subject_used", "")
            m = re.search(r"\[IBXT-\d+-[a-f0-9]+\]", subj)
            marker = m.group(0)[1:-1] if m else ""
            if marker:
                res, folder, err = _check_inbox(acct, marker)
                db.update_inbox_result(r["id"], res, folder, err)
                _test_progress["log"].append(f"{acct['email']}: {res}")
            _test_progress["done"] = i + 1
        _test_progress["running"] = False
        _test_progress["log"].append("Re-check done!")

    threading.Thread(target=worker, daemon=True).start()
    return HTMLResponse(
        '<div class="alert alert-info">Re-checking IMAP...</div>'
        '<div hx-get="/inboxtest/progress" hx-trigger="every 2s" hx-swap="innerHTML"></div>'
    )


@router.post("/inboxtest/{tid}/delete")
async def delete_test(request: Request, tid: int):
    request.app.state.db.delete_inbox_test(tid)
    return RedirectResponse("/inboxtest", status_code=303)


@router.post("/inboxtest/compare", response_class=HTMLResponse)
async def compare_tests(request: Request, test_a: int = Form(0), test_b: int = Form(0)):
    """Compare two test runs — show SMTP-level diff."""
    db = request.app.state.db
    if not test_a or not test_b:
        return HTMLResponse('<div class="alert alert-warning">Select two tests to compare.</div>')

    results_a = {r["smtp_user"]: dict(r) for r in db.get_inbox_results(test_a)}
    results_b = {r["smtp_user"]: dict(r) for r in db.get_inbox_results(test_b)}
    all_smtps = sorted(set(list(results_a.keys()) + list(results_b.keys())))

    rows = ""
    improved = 0
    degraded = 0
    for smtp in all_smtps:
        ra = results_a.get(smtp, {})
        rb = results_b.get(smtp, {})
        res_a = ra.get("result", "—")
        res_b = rb.get("result", "—")

        if res_a != "INBOX" and res_b == "INBOX":
            improved += 1
            diff_class = "color:var(--green);font-weight:600"
        elif res_a == "INBOX" and res_b != "INBOX":
            degraded += 1
            diff_class = "color:var(--red);font-weight:600"
        elif res_a == res_b:
            diff_class = "color:var(--fg2)"
        else:
            diff_class = ""

        badge_a = _result_badge(res_a)
        badge_b = _result_badge(res_b)

        rows += (f'<tr><td style="font-family:monospace;font-size:12px">{escape(smtp)}</td>'
                 f'<td>{badge_a}</td><td>{badge_b}</td>'
                 f'<td style="{diff_class}">{_diff_label(res_a, res_b)}</td></tr>')

    header = (f'<div style="margin-bottom:12px">'
              f'<span style="color:var(--green);font-weight:600">{improved} improved</span> · '
              f'<span style="color:var(--red);font-weight:600">{degraded} degraded</span> · '
              f'{len(all_smtps)} total SMTPs</div>')

    html = header + (f'<table><thead><tr><th>SMTP</th><th>Test A</th><th>Test B</th><th>Change</th></tr></thead>'
                     f'<tbody>{rows}</tbody></table>')
    return HTMLResponse(html)


def _result_badge(result):
    colors = {"INBOX": "badge-running", "SPAM": "badge-failed", "MISSING": "badge-draft",
              "PENDING": "badge-draft", "ERROR": "badge-failed"}
    return f'<span class="badge {colors.get(result, "badge-draft")}">{result}</span>'


def _diff_label(a, b):
    if a == b:
        return "="
    return f"{a} → {b}"
