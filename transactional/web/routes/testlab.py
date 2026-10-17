"""Test Lab — send test mails with specific profiles, view raw source."""
import re
import random
import logging
from html import escape
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger("trans.testlab")
router = APIRouter()


@router.get("/testlab", response_class=HTMLResponse)
async def testlab_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    cfg = db.get_config()
    smtp_lists = [dict(sl, count=db.get_smtp_count(sl["id"])) for sl in db.get_smtp_lists(uid)]
    templates = [dict(t) for t in db.get_templates(uid)]
    from mailer.mime_profiles import get_profile_names
    profiles = get_profile_names()
    return request.app.state.templates.TemplateResponse(request, "testlab.html", {
        "active": "testlab", "cfg": cfg, "smtp_lists": smtp_lists,
        "templates": templates, "profiles": profiles, "db": db,
    })


@router.post("/testlab/send", response_class=HTMLResponse)
async def send_test(request: Request,
                    to_email: str = Form(""),
                    smtp_list_id: int = Form(0),
                    template_id: int = Form(0),
                    mime_profile: str = Form("default"),
                    from_name: str = Form(""),
                    from_email_override: str = Form(""),
                    subject: str = Form("")):
    """Send a test mail with specific profile and show raw source."""
    db = request.app.state.db
    uid = request.state.user["id"]
    cfg = db.get_config()

    if not to_email.strip():
        return HTMLResponse('<div class="alert alert-warning">Enter recipient email.</div>')

    # Get SMTP
    smtps = [dict(s) for s in db.get_smtps(smtp_list_id)] if smtp_list_id else []
    if not smtps:
        return HTMLResponse('<div class="alert alert-danger">No SMTPs in selected list.</div>')
    smtp = random.choice(smtps)

    # Get template HTML
    html_body = "<p>Test Lab Email - {email_user}</p>"
    if template_id:
        tpl = db.get_template(template_id)
        if tpl:
            files = db.get_template_files(template_id)
            if files:
                html_body = dict(files[0])["html_content"]
            elif tpl["html_content"]:
                html_body = tpl["html_content"]

    # Process variables
    email = to_email.strip()
    user = email.split("@")[0] if "@" in email else email
    domain = email.split("@")[1] if "@" in email else ""
    html_body = html_body.replace("{email}", email).replace("{email_user}", user).replace("{domain}", domain)
    html_body = html_body.replace("{Logo}", "").replace("{RedirectLink}", "https://example.com")

    # Resolve macros
    for m in db.get_macros(uid):
        md = dict(m)
        lines = [l.strip() for l in (md.get("values_text") or "").splitlines() if l.strip()]
        if lines:
            html_body = html_body.replace(f"{{{md['name']}}}", random.choice(lines))

    plain = re.sub(r"<[^>]+>", "", html_body).strip()

    # Build sender info
    cur_from_name = from_name.strip() or cfg.get("from_name", "") or "Test"
    cur_from_email = from_email_override.strip() or cfg.get("from_email", "") or smtp["username"]
    cur_subject = subject.strip() or cfg.get("subject", "") or "Test Lab"
    cur_subject = cur_subject.replace("{email_user}", user)

    # Build MIME
    try:
        from mailer.mime_builder import MIMEBuilder
        from mailer.mime_profiles import apply_profile
        raw_msg = MIMEBuilder.build_email(
            from_name=cur_from_name, from_email=cur_from_email,
            to_email=email, subject=f"[TEST] {cur_subject}",
            html_body=html_body, plain_body=plain)

        if mime_profile != "default":
            raw_msg = apply_profile(raw_msg, mime_profile, cur_from_email)
    except Exception as e:
        return HTMLResponse(f'<div class="alert alert-danger">Build error: {escape(str(e))}</div>')

    # Extract headers for display
    if "\r\n\r\n" in raw_msg:
        headers_part = raw_msg.split("\r\n\r\n")[0]
    else:
        headers_part = raw_msg[:500]

    # Send via proxy
    proxy_str = ""
    pv = cfg.get("proxy_value", "").strip()
    if pv:
        proxy_str = pv.splitlines()[0].strip()

    from transactional.web.routes.smtps import _connect_smtp
    send_result = "pending"
    send_error = ""
    try:
        server, error, _ = _connect_smtp(smtp["host"], smtp["port"],
                                          smtp["username"], smtp["password"], proxy_str)
        if server:
            server.sendmail(cur_from_email, [email], raw_msg)
            server.quit()
            send_result = "sent"
        else:
            send_result = "failed"
            send_error = error or "Connection failed"
    except Exception as e:
        send_result = "failed"
        send_error = str(e)

    # Build response
    status_html = ""
    if send_result == "sent":
        status_html = f'<div class="alert alert-success">Sent to {escape(email)} via {escape(smtp["host"])} ({escape(mime_profile)} profile)</div>'
    else:
        status_html = f'<div class="alert alert-danger">Failed: {escape(send_error)}</div>'

    headers_html = (f'<div style="margin-top:12px"><strong>MIME Headers ({mime_profile})</strong></div>'
                    f'<pre style="white-space:pre-wrap;font-size:11px;background:var(--bg);'
                    f'padding:12px;border-radius:var(--radius);max-height:400px;overflow:auto;margin-top:8px">'
                    f'{escape(headers_part)}</pre>')

    preview_html = (f'<div style="margin-top:12px"><strong>HTML Preview</strong></div>'
                    f'<iframe srcdoc="{escape(html_body)}" style="width:100%;height:300px;'
                    f'border:1px solid var(--border);border-radius:var(--radius);background:#fff;margin-top:8px"></iframe>')

    # Spam check on the built MIME (with profile applied)
    spam_html = ""
    try:
        from .spam_check import check_spam, format_result_html
        results = check_spam(raw_msg, "both", cfg.get("spam_checker_url", "http://127.0.0.1:11333/checkv2"))
        spam_html = f'<div style="margin-top:12px"><strong>Spam Score Analysis</strong></div>{format_result_html(results)}'
    except Exception as e:
        spam_html = f'<div style="margin-top:12px;color:var(--fg2)">Spam check skipped: {escape(str(e))}</div>'

    return HTMLResponse(status_html + headers_html + spam_html + preview_html)


@router.post("/testlab/preview-headers", response_class=HTMLResponse)
async def preview_headers(request: Request,
                          from_email: str = Form("test@example.com"),
                          mime_profile: str = Form("default")):
    """Show headers + spam scores without sending."""
    db = request.app.state.db
    uid = request.state.user["id"]
    cfg = db.get_config()

    # Build with template if available
    html_body = "<p>Test preview content</p>"
    templates = db.get_all_template_htmls(uid)
    if templates:
        html_body = templates[0]
    html_body = html_body.replace("{email}", "preview@example.com").replace("{email_user}", "preview")
    html_body = html_body.replace("{domain}", "example.com").replace("{Logo}", "").replace("{RedirectLink}", "https://example.com")
    plain = re.sub(r"<[^>]+>", "", html_body).strip()

    fe = from_email.strip() or "test@example.com"
    subject = cfg.get("subject", "") or "Preview"

    try:
        from mailer.mime_builder import MIMEBuilder
        from mailer.mime_profiles import apply_profile
        raw = MIMEBuilder.build_email(
            from_name=cfg.get("from_name", "") or "Test", from_email=fe,
            to_email="preview@example.com", subject=subject,
            html_body=html_body, plain_body=plain)
        if mime_profile != "default":
            raw = apply_profile(raw, mime_profile, fe)
        headers = raw.split("\r\n\r\n")[0] if "\r\n\r\n" in raw else raw[:500]

        headers_html = (f'<pre style="white-space:pre-wrap;font-size:11px;background:var(--bg);'
                        f'padding:12px;border-radius:var(--radius)">{escape(headers)}</pre>')

        # Spam check
        spam_html = ""
        try:
            from .spam_check import check_spam, format_result_html
            results = check_spam(raw, "both", cfg.get("spam_checker_url", "http://127.0.0.1:11333/checkv2"))
            spam_html = f'<div style="margin-top:12px"><strong>Spam Scores</strong></div>{format_result_html(results)}'
        except Exception as e:
            spam_html = f'<div style="margin-top:10px;color:var(--fg2)">Spam check: {escape(str(e))}</div>'

        return HTMLResponse(headers_html + spam_html)
    except Exception as e:
        return HTMLResponse(f'<div class="alert alert-danger">{escape(str(e))}</div>')
