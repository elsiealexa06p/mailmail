"""HTML Exporter — bulk-generate ready-to-send HTML variants as ZIP.

Picks templates the user already has in the HTML Editor, generates N
variants per template using round-robin distribution, replaces the
{Logo} placeholder with the chosen logo mode (cloudinary URL, plain
text, or a custom external-mailer code snippet), and applies the
anti-fingerprint / advanced anti-fingerprint engine on every variant.

Macros ({Name}) and {RedirectLink} are intentionally NOT substituted —
they're left in the HTML for the downstream external mailer to handle.
"""
import json
import logging
import os
import random
import threading
import time
import zipfile
from html import escape

from fastapi import APIRouter, Request, Form
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

logger = logging.getLogger("trans.exporter")
router = APIRouter()

DEFAULT_LOGO_CODE = (
    '<div><img border=0 hspace=0 alt="" '
    'src="Logo[==ORandImage,RndFileName,RndSize=3==].png"></div>'
)

EXPORT_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "exports"
))
os.makedirs(EXPORT_DIR, exist_ok=True)


def _build_afp(cfg: dict, advanced: bool, classes: bool):
    """Instantiate the chosen anti-fingerprint engine, or return None."""
    if not (advanced or classes):
        return None
    if advanced:
        from mailer.advanced_antifingerprint import AdvancedAntiFingerprintEngine
        return AdvancedAntiFingerprintEngine(
            enable_classes=classes,
            structure_variation=float(cfg.get("structure_variation", 0.5)),
        )
    from mailer.antifingerprint import AntiFingerprintEngine
    return AntiFingerprintEngine(enable_classes=classes)


def _replace_logo(html: str, logo_mode: str, logo_code: str,
                  cloudinary_urls: list, logo_text: str,
                  logo_text_color: str) -> str:
    """Replace {Logo} in HTML with the chosen mode. Does not touch macros."""
    if "{Logo}" not in html:
        return html
    if logo_mode == "code":
        return html.replace("{Logo}", logo_code or "")
    if logo_mode == "text":
        return html.replace(
            "{Logo}",
            f'<span style="font-weight:bold;font-size:16px;color:{logo_text_color};">{logo_text}</span>'
        )
    if logo_mode == "cloudinary" and cloudinary_urls:
        return html.replace(
            "{Logo}",
            f'<img src="{random.choice(cloudinary_urls)}" alt="Logo" '
            f'style="display:block;border:0;max-height:50px;width:auto;">'
        )
    return html.replace("{Logo}", "")


def _collect_cloudinary_urls(db, user_id: int) -> list:
    """Walk logo groups and collect Cloudinary URLs from cdn_urls_json."""
    urls = []
    for g in db.get_logo_groups(user_id):
        raw = dict(g).get("cdn_urls_json") or ""
        if not raw:
            continue
        try:
            arr = json.loads(raw)
            if isinstance(arr, list):
                urls.extend([u for u in arr if isinstance(u, str) and u])
        except Exception:
            pass
    return urls


def _run_export(db, job_id: int, user_id: int, template_ids: list,
                total: int, logo_mode: str, logo_code: str, logo_text: str,
                logo_text_color: str, classes: bool, advanced: bool,
                cfg: dict, zip_path: str):
    """Background worker — builds the ZIP and updates the job row."""
    try:
        # Gather source HTML bodies per template (in selection order)
        per_template = []
        for tid in template_ids:
            bodies = db.get_all_template_htmls(user_id=0, template_id=tid)
            t = db.get_template(tid)
            tpl_name = dict(t)["name"] if t else f"tpl{tid}"
            safe_name = "".join(
                c if c.isalnum() or c in "-_" else "_" for c in tpl_name
            ).strip("_") or f"tpl{tid}"
            if bodies:
                per_template.append((safe_name, bodies))
        if not per_template:
            db.update_export_job(job_id, status="FAILED",
                                  error_msg="No template HTMLs found.")
            return

        cloudinary_urls = _collect_cloudinary_urls(db, user_id) if logo_mode == "cloudinary" else []
        afp = _build_afp(cfg, advanced=advanced, classes=classes)

        done = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            tpl_count = len(per_template)
            for i in range(total):
                tpl_idx = i % tpl_count
                safe_name, bodies = per_template[tpl_idx]
                html = random.choice(bodies)

                html = _replace_logo(
                    html, logo_mode, logo_code, cloudinary_urls,
                    logo_text, logo_text_color,
                )
                if afp:
                    try:
                        html = afp.transform(html)
                    except Exception as e:
                        logger.warning("afp transform failed at %d: %s", i, e)

                fname = f"{safe_name}_{i + 1:05d}.html"
                zf.writestr(fname, html)

                done += 1
                if done % 25 == 0 or done == total:
                    db.update_export_job(job_id, done_count=done)

        from datetime import datetime
        db.update_export_job(
            job_id, status="DONE", done_count=total, file_path=zip_path,
            finished_at=datetime.utcnow().isoformat(timespec="seconds"),
        )
    except Exception as e:
        logger.error("Export job %d failed: %s", job_id, e, exc_info=True)
        db.update_export_job(job_id, status="FAILED", error_msg=str(e)[:500])


@router.get("/exporter", response_class=HTMLResponse)
async def exporter_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    templates = [
        dict(t, file_count=db.get_template_file_count(t["id"]))
        for t in db.get_templates(uid)
    ]
    logo_codes = [dict(c) for c in db.get_logo_codes(uid)]
    jobs = [dict(j) for j in db.get_export_jobs(uid)]
    cfg = db.get_config()
    cloudinary_urls = _collect_cloudinary_urls(db, uid)
    return request.app.state.templates.TemplateResponse(request, "exporter.html", {
        "active": "exporter",
        "templates": templates,
        "logo_codes": logo_codes,
        "jobs": jobs,
        "cfg": cfg,
        "cloudinary_count": len(cloudinary_urls),
        "default_logo_code": DEFAULT_LOGO_CODE,
    })


@router.post("/exporter/logo-code/add")
async def add_logo_code(request: Request,
                         name: str = Form(""),
                         code: str = Form("")):
    uid = request.state.user["id"]
    name = name.strip() or "Untitled"
    if code.strip():
        request.app.state.db.add_logo_code(name, code, uid)
    return RedirectResponse("/exporter", status_code=303)


@router.post("/exporter/logo-code/{cid}/update")
async def update_logo_code(request: Request, cid: int,
                            name: str = Form(""),
                            code: str = Form("")):
    if name.strip() and code.strip():
        request.app.state.db.update_logo_code(cid, name.strip(), code)
    return RedirectResponse("/exporter", status_code=303)


@router.post("/exporter/logo-code/{cid}/delete")
async def delete_logo_code(request: Request, cid: int):
    request.app.state.db.delete_logo_code(cid)
    return RedirectResponse("/exporter", status_code=303)


@router.post("/exporter/generate", response_class=HTMLResponse)
async def generate_export(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    form = await request.form()

    template_ids = []
    for v in form.getlist("template_ids"):
        try:
            template_ids.append(int(v))
        except (TypeError, ValueError):
            pass
    if not template_ids:
        return HTMLResponse('<div class="alert alert-warning">Select at least one template.</div>')

    try:
        total = max(1, min(int(form.get("total", "100")), 50000))
    except (TypeError, ValueError):
        total = 100

    raw_mode = (form.get("logo_mode", "code:default") or "code:default").strip()
    classes = "afp_classes" in form
    advanced = "afp_advanced" in form

    cfg = db.get_config()
    logo_text = (form.get("logo_text", "") or cfg.get("logo_text", "Logo")).strip() or "Logo"
    logo_text_color = (form.get("logo_text_color", "") or cfg.get("logo_text_color", "#333333")).strip() or "#333333"

    # Resolve logo_mode. The form sends e.g. "code:default", "code:<id>",
    # "cloudinary", "text". We collapse to effective_mode for the engine
    # plus a logo_code string when needed.
    logo_code = ""
    preset_label = ""
    if raw_mode.startswith("code:"):
        effective_mode = "code"
        sel = raw_mode.split(":", 1)[1]
        if sel == "default":
            logo_code = DEFAULT_LOGO_CODE
            preset_label = "Default"
        else:
            try:
                preset = db.get_logo_code(int(sel))
            except (TypeError, ValueError):
                preset = None
            if preset:
                preset = dict(preset)
                logo_code = preset["code"]
                preset_label = preset["name"]
            else:
                logo_code = DEFAULT_LOGO_CODE
                preset_label = "Default (preset missing)"
        override = form.get("logo_code_override", "")
        if override.strip():
            logo_code = override
            preset_label += " + override"
    elif raw_mode == "code":  # backward compat
        effective_mode = "code"
        logo_code = DEFAULT_LOGO_CODE
        preset_label = "Default"
    else:
        effective_mode = raw_mode

    job_logo_mode = (
        f"code: {preset_label}" if effective_mode == "code" else effective_mode
    )
    name = (form.get("name", "") or f"Export {total}").strip()[:80]

    job_id = db.create_export_job(name, total, job_logo_mode, uid)
    fname = f"export_{job_id}_{int(time.time())}.zip"
    zip_path = os.path.join(EXPORT_DIR, fname)

    t = threading.Thread(
        target=_run_export,
        args=(db, job_id, uid, template_ids, total, effective_mode, logo_code,
              logo_text, logo_text_color, classes, advanced, cfg, zip_path),
        daemon=True,
    )
    t.start()

    return HTMLResponse(
        f'<div class="alert alert-info">Job #{job_id} started: {total} HTMLs from '
        f'{len(template_ids)} template(s).</div>'
        f'<div id="job-{job_id}-status" hx-get="/exporter/jobs/{job_id}/status" '
        f'hx-trigger="every 2s" hx-swap="outerHTML"></div>'
    )


@router.get("/exporter/jobs/{jid}/status", response_class=HTMLResponse)
async def job_status(request: Request, jid: int):
    job = request.app.state.db.get_export_job(jid)
    if not job:
        return HTMLResponse('<div class="alert alert-danger">Job not found.</div>')
    j = dict(job)
    status = j.get("status", "?")
    total = j.get("total_count", 0) or 0
    done = j.get("done_count", 0) or 0
    pct = int(done / total * 100) if total else 0

    if status == "RUNNING":
        return HTMLResponse(
            f'<div id="job-{jid}-status" hx-get="/exporter/jobs/{jid}/status" '
            f'hx-trigger="every 2s" hx-swap="outerHTML">'
            f'<div class="progress" style="margin-bottom:6px">'
            f'<div class="progress-bar" style="width:{pct}%">{done}/{total}</div></div>'
            f'<p style="font-size:12px;color:var(--fg2)">Generating...</p></div>'
        )
    if status == "DONE":
        return HTMLResponse(
            f'<div id="job-{jid}-status">'
            f'<div class="alert alert-success">'
            f'Done! {done} HTMLs ready. '
            f'<a href="/exporter/jobs/{jid}/download" class="btn btn-primary btn-sm" '
            f'style="margin-left:8px">Download ZIP</a>'
            f'</div></div>'
        )
    return HTMLResponse(
        f'<div id="job-{jid}-status">'
        f'<div class="alert alert-danger">Failed: {escape(j.get("error_msg", "") or "unknown error")}</div>'
        f'</div>'
    )


@router.get("/exporter/jobs/{jid}/download")
async def download_job(request: Request, jid: int):
    job = request.app.state.db.get_export_job(jid)
    if not job:
        return HTMLResponse('Job not found', status_code=404)
    j = dict(job)
    path = j.get("file_path", "")
    if not path or not os.path.isfile(path):
        return HTMLResponse('File missing', status_code=404)
    return FileResponse(
        path, media_type="application/zip",
        filename=f"{(j.get('name') or 'export').replace(' ', '_')}.zip",
    )


@router.post("/exporter/jobs/{jid}/delete")
async def delete_job(request: Request, jid: int):
    db = request.app.state.db
    job = db.get_export_job(jid)
    if job:
        path = dict(job).get("file_path", "")
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except Exception:
                pass
    db.delete_export_job(jid)
    return RedirectResponse("/exporter", status_code=303)
