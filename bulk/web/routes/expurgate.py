"""Expurgate / Hoster filter — upload an email list, detect domains on
Expurgate-protected hosters via MX + reverse-DNS, download buckets."""
import io
import logging
import os
import threading
import time
import uuid
import zipfile
from html import escape

from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

logger = logging.getLogger("bulk.expurgate")
router = APIRouter()

JOB_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "exports", "expurgate"
))
os.makedirs(JOB_DIR, exist_ok=True)

# job_id -> dict(status, total, done, summary, files, error)
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _new_job(emails_count: int) -> str:
    job_id = uuid.uuid4().hex[:10]
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "RUNNING",
            "total": emails_count,
            "done": 0,
            "summary": None,
            "files": {},   # bucket name -> path
            "error": "",
            "started": time.time(),
        }
    return job_id


def _update(job_id: str, **kw):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kw)


def _run_job(job_id: str, emails: list, deep_lookup: bool, threads: int):
    try:
        from mailer.expurgate_filter import classify_list, summarise, PROVIDERS

        def progress(done: int, total: int):
            _update(job_id, done=done, total=total)

        buckets = classify_list(
            emails,
            deep_lookup=deep_lookup,
            threads=threads,
            progress_cb=progress,
        )
        summary = summarise(buckets)

        # Write the standard files: clean / expurgate / errors / per-provider
        job_dir = os.path.join(JOB_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)
        files: dict[str, str] = {}

        def _write(name: str, items: list):
            if not items:
                return
            p = os.path.join(job_dir, f"{name}.txt")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("\n".join(items) + "\n")
            files[name] = p

        _write("clean", buckets.get("clean", []))
        _write("errors", buckets.get("error", []))

        # Combined Expurgate-bucket: every provider hit lands here too,
        # because (per user intent) all these hosters proxy via Expurgate.
        all_filtered = []
        for name in PROVIDERS:
            hits = buckets.get(name, [])
            if hits:
                _write(name, hits)
                all_filtered.extend(hits)
        if all_filtered:
            order = {e: i for i, e in enumerate(emails)}
            all_filtered.sort(key=lambda m: order.get(m, 0))
            _write("filtered_all", all_filtered)

        # Bundle ZIP
        zip_path = os.path.join(job_dir, "result.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, path in files.items():
                zf.write(path, arcname=os.path.basename(path))
        files["zip"] = zip_path

        _update(job_id, status="DONE", files=files, summary=summary,
                done=summary["total"], total=summary["total"])
    except Exception as e:
        logger.error("Expurgate job %s failed: %s", job_id, e, exc_info=True)
        _update(job_id, status="FAILED", error=str(e)[:300])


@router.get("/expurgate", response_class=HTMLResponse)
async def expurgate_page(request: Request):
    from mailer.expurgate_filter import PROVIDERS
    recent_jobs = []
    with _jobs_lock:
        for jid, j in sorted(_jobs.items(),
                              key=lambda kv: kv[1]["started"], reverse=True)[:10]:
            recent_jobs.append({"id": jid, **j})
    return request.app.state.templates.TemplateResponse(request, "expurgate.html", {
        "active": "expurgate",
        "providers": PROVIDERS,
        "recent_jobs": recent_jobs,
    })


@router.post("/expurgate/scan", response_class=HTMLResponse)
async def scan(request: Request,
                file: UploadFile = File(None),
                pasted: str = Form(""),
                deep_lookup: str = Form(""),
                threads: int = Form(50)):
    raw = ""
    if file and file.filename:
        try:
            raw = (await file.read()).decode("utf-8", errors="ignore")
        except Exception as e:
            return HTMLResponse(
                f'<div class="alert alert-danger">File read failed: {escape(str(e))}</div>'
            )
    if pasted.strip():
        raw = (raw + "\n" + pasted).strip() if raw else pasted

    emails = [line.strip() for line in raw.splitlines() if line.strip() and "@" in line]
    # Dedupe while preserving order
    seen = set()
    unique = []
    for e in emails:
        low = e.lower()
        if low in seen:
            continue
        seen.add(low)
        unique.append(e)

    if not unique:
        return HTMLResponse(
            '<div class="alert alert-warning">No email addresses found in the input.</div>'
        )

    threads = max(1, min(int(threads or 50), 200))
    deep = bool(deep_lookup)

    job_id = _new_job(len(unique))
    t = threading.Thread(target=_run_job, args=(job_id, unique, deep, threads), daemon=True)
    t.start()

    return HTMLResponse(
        f'<div class="alert alert-info">Job <code>{job_id}</code> started — '
        f'{len(unique)} unique addresses, {threads} threads, '
        f'deep-lookup {"on" if deep else "off"}.</div>'
        f'<div hx-get="/expurgate/job/{job_id}/status" '
        f'hx-trigger="every 2s" hx-swap="outerHTML"></div>'
    )


@router.get("/expurgate/job/{job_id}/status", response_class=HTMLResponse)
async def job_status(request: Request, job_id: str):
    with _jobs_lock:
        job = dict(_jobs.get(job_id) or {})
    if not job:
        return HTMLResponse('<div class="alert alert-danger">Job not found.</div>')

    status = job.get("status", "?")
    total = job.get("total", 0)
    done = job.get("done", 0)
    pct = int(done / total * 100) if total else 0

    if status == "RUNNING":
        return HTMLResponse(
            f'<div hx-get="/expurgate/job/{job_id}/status" hx-trigger="every 2s" hx-swap="outerHTML">'
            f'<div class="progress" style="margin-bottom:6px">'
            f'<div class="progress-bar" style="width:{pct}%">{done}/{total}</div></div>'
            f'<p style="font-size:12px;color:var(--fg2)">Resolving MX records …</p>'
            f'</div>'
        )
    if status == "FAILED":
        return HTMLResponse(
            f'<div><div class="alert alert-danger">Failed: {escape(job.get("error",""))}</div></div>'
        )

    # DONE
    s = job.get("summary") or {}
    files = job.get("files") or {}
    rows = ""
    for name, count in (s.get("per_provider") or {}).items():
        path = files.get(name)
        link = (f'<a href="/expurgate/job/{job_id}/download/{name}" '
                f'class="btn btn-secondary btn-xs">{name}.txt</a>') if path else ""
        rows += f"<tr><td>{escape(name)}</td><td>{count}</td><td>{link}</td></tr>"

    dl_clean = (f'<a href="/expurgate/job/{job_id}/download/clean" '
                 f'class="btn btn-primary btn-sm">clean.txt</a>'
                 if files.get("clean") else "<em>none</em>")
    dl_filtered = (f'<a href="/expurgate/job/{job_id}/download/filtered_all" '
                    f'class="btn btn-secondary btn-sm">filtered_all.txt</a>'
                    if files.get("filtered_all") else "<em>none</em>")
    dl_err = (f'<a href="/expurgate/job/{job_id}/download/errors" '
               f'class="btn btn-secondary btn-sm btn-xs">errors.txt</a>'
               if files.get("errors") else "")
    dl_zip = (f'<a href="/expurgate/job/{job_id}/download/zip" '
               f'class="btn btn-primary btn-sm">ZIP (all files)</a>'
               if files.get("zip") else "")

    return HTMLResponse(
        f'<div class="card" style="margin-top:8px">'
        f'<div class="card-header"><h3>Job <code>{escape(job_id)}</code> — done</h3></div>'
        f'<table style="margin-bottom:10px;font-size:12px">'
        f'<thead><tr><th>Provider</th><th>Hits</th><th>Download</th></tr></thead>'
        f'<tbody>{rows or "<tr><td colspan=3>(no provider hits)</td></tr>"}</tbody>'
        f'</table>'
        f'<p style="font-size:13px">'
        f' &bull; Total: <strong>{s.get("total",0)}</strong>&nbsp;&nbsp;'
        f' Clean: <strong>{s.get("clean",0)}</strong>&nbsp;&nbsp;'
        f' Filtered: <strong>{s.get("filtered",0)}</strong>&nbsp;&nbsp;'
        f' Errors: <strong>{s.get("errors",0)}</strong></p>'
        f'<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">'
        f'  {dl_clean} {dl_filtered} {dl_err} {dl_zip}'
        f'</div></div>'
    )


@router.get("/expurgate/job/{job_id}/download/{bucket}")
async def download(request: Request, job_id: str, bucket: str):
    with _jobs_lock:
        job = dict(_jobs.get(job_id) or {})
    if not job:
        return Response("Job not found", status_code=404)
    path = (job.get("files") or {}).get(bucket)
    if not path or not os.path.isfile(path):
        return Response("File not available", status_code=404)
    if bucket == "zip":
        media = "application/zip"
        fname = f"expurgate_{job_id}.zip"
    else:
        media = "text/plain; charset=utf-8"
        fname = os.path.basename(path)
    with open(path, "rb") as fh:
        data = fh.read()
    return Response(content=data, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.post("/expurgate/job/{job_id}/delete")
async def delete_job(request: Request, job_id: str):
    with _jobs_lock:
        job = _jobs.pop(job_id, None)
    if job:
        import shutil
        d = os.path.join(JOB_DIR, job_id)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    return RedirectResponse("/expurgate", status_code=303)
