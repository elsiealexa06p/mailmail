"""Preview — Build MIME message and display headers + raw source."""
import json
from email.utils import formatdate
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse

router = APIRouter()

PROVIDERS = ["generic", "ses", "sendgrid", "mailgun", "postmark"]


@router.get("/preview", response_class=HTMLResponse)
async def preview_page(request: Request):
    db = request.app.state.db
    tpl = request.app.state.templates
    templates = [dict(t) for t in db.get_templates()]
    return tpl.TemplateResponse(request, "preview.html", {
        "active": "preview", "db": db,
        "providers": PROVIDERS, "templates": templates,
        "result": None,
    })


@router.post("/preview/build", response_class=HTMLResponse)
async def preview_build(request: Request,
                        from_name: str = Form("Newsletter"),
                        from_email: str = Form("newsletter@example.com"),
                        to_email: str = Form("preview@example.com"),
                        subject: str = Form("Test Subject"),
                        provider_type: str = Form("generic"),
                        template_id: int = Form(0)):
    db = request.app.state.db
    tpl = request.app.state.templates

    from bulk.mailer.bulk_mime_builder import BulkMIMEBuilder
    from bulk.mailer.content_engine import BulkContentEngine

    macros = {r["name"]: json.loads(r["values_json"]) for r in db.get_macros()}
    engine = BulkContentEngine(macros)

    email = to_email.strip() or "preview@example.com"
    f_email = from_email.strip() or "newsletter@example.com"
    f_name = engine.process(from_name.strip(), email)
    subj = engine.process(subject.strip(), email)
    domain = f_email.split("@")[1] if "@" in f_email else "example.com"

    # Load HTML from template or use default
    html_src = "<html><body><p>Hello {email_user},</p><p>This is a test newsletter.</p><p>Best regards</p></body></html>"
    if template_id:
        import os
        row = db._conn().execute(
            "SELECT * FROM message_templates WHERE id=?", (template_id,)).fetchone()
        if row:
            files = json.loads(row["html_files_json"] or "[]")
            if files and os.path.isfile(files[0]):
                with open(files[0], "r", encoding="utf-8") as fh:
                    html_src = fh.read()

    html = engine.process(html_src, email)
    plain = BulkContentEngine.html_to_plaintext(html)

    error = None
    headers_text = ""
    full_mime = ""
    try:
        raw, envelope, tag = BulkMIMEBuilder.build_email(
            from_name=f_name, from_email=f_email,
            reply_to_name="", reply_to_email="",
            to_email=email, subject=subj,
            html_body=html, plain_body=plain,
            list_id_token=f"nl.{domain}", list_id_name="Newsletter",
            unsubscribe_url=f"https://unsub.{domain}/u/test",
            unsubscribe_mailto=f"unsub-test@{domain}",
            feedback_id=f"preview:test:p1:{domain.replace('.', '-')[:15]}",
            provider_type=provider_type,
        )
        raw = f"Date: {formatdate(localtime=True)}\r\n" + raw
        headers_text = raw.split("\r\n\r\n")[0]
        full_mime = raw

        # Compute text ratio
        text_bytes = len(plain.encode("utf-8"))
        total_bytes = len(raw.encode("utf-8"))
        text_ratio = int(text_bytes / total_bytes * 100) if total_bytes else 0
    except Exception as e:
        error = str(e)
        text_ratio = 0

    templates_list = [dict(t) for t in db.get_templates()]
    return tpl.TemplateResponse(request, "preview.html", {
        "active": "preview", "db": db,
        "providers": PROVIDERS, "templates": templates_list,
        "result": {
            "headers": headers_text,
            "full_mime": full_mime[:20000],
            "html_preview": html,
            "envelope": envelope if not error else "",
            "text_ratio": text_ratio,
            "error": error,
        },
        "form": {
            "from_name": from_name, "from_email": from_email,
            "to_email": to_email, "subject": subject,
            "provider_type": provider_type, "template_id": template_id,
        },
    })
