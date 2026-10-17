"""Mailings page — persistent campaigns with edit, start, stop, resend."""
import json
import time
import logging
import threading
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger("bulk.mailings")

router = APIRouter()
_cores = {}
_threads = {}
_speed = {}


@router.get("/mailings", response_class=HTMLResponse)
async def mailings_page(request: Request):
    db = request.app.state.db
    tpl = request.app.state.templates
    mailings = _build_mailing_list(db)
    return tpl.TemplateResponse(request, "mailings.html", {
        "active": "mailings", "mailings": mailings,
        "brands": db.get_brands(), "domains": db.get_domains(),
        "lists": db.get_lists(), "smtps": db.get_smtps(),
        "templates": db.get_templates(), "db": db,
    })


def _build_mailing_list(db):
    rows = db.get_mailings()
    mailings = []
    for r in rows:
        m = dict(r)
        total = m.get("total_leads", 0) or 0
        sent = m.get("sent", 0) or 0
        failed = m.get("failed", 0) or 0
        m["pct"] = int((sent + failed) / total * 100) if total > 0 else 0
        m["running"] = m["id"] in _cores
        m["speed"] = _speed.get(m["id"], 0)
        m["remaining"] = max(0, total - sent - failed)
        try:
            sm = db._conn().execute("SELECT name FROM smtp_presets WHERE id=?",
                                     (m["smtp_preset_id"],)).fetchone()
            m["smtp_name"] = sm["name"] if sm else "—"
        except Exception:
            m["smtp_name"] = "—"
        try:
            dm = db._conn().execute("SELECT domain FROM domains WHERE id=?",
                                     (m["domain_id"],)).fetchone()
            m["domain_name"] = dm["domain"] if dm else "—"
        except Exception:
            m["domain_name"] = "—"
        mailings.append(m)
    return mailings


@router.get("/mailings/table", response_class=HTMLResponse)
async def mailings_table(request: Request):
    """HTMX polling endpoint for the campaigns table."""
    db = request.app.state.db
    mailings = _build_mailing_list(db)
    html = '<div class="card-header"><h3>Campaigns <span class="count">' + str(len(mailings)) + '</span></h3></div>'
    html += '<table><thead><tr><th>Name</th><th>Domain</th><th>SMTP</th><th>Progress</th><th>Total</th><th>Sent</th><th>Failed</th><th>Remaining</th><th>Speed</th><th></th></tr></thead><tbody>'
    for m in mailings:
        bg = ' style="background:var(--green-light)"' if m["running"] else ''
        pct = m["pct"]
        speed = f'{m["speed"]:,}/h' if m["speed"] else "—"
        btn = (f'<form method="post" action="/mailings/{m["id"]}/stop" style="display:inline">'
               f'<button class="btn btn-danger btn-xs">Stop</button></form>') if m["running"] else \
              (f'<form method="post" action="/mailings/{m["id"]}/start" style="display:inline">'
               f'<button class="btn btn-success btn-xs">Start</button></form>')
        html += (f'<tr{bg}>'
                 f'<td style="font-weight:500">{m["name"] or ("#" + str(m["id"]))}</td>'
                 f'<td style="font-size:12px">{m["domain_name"]}</td>'
                 f'<td style="font-size:12px">{m["smtp_name"]}</td>'
                 f'<td style="min-width:120px"><div class="progress"><div class="progress-bar" style="width:{pct}%">{pct}%</div></div></td>'
                 f'<td>{m.get("total_leads",0) or 0:,}</td>'
                 f'<td style="color:var(--green);font-weight:500">{m.get("sent",0) or 0:,}</td>'
                 f'<td style="color:var(--red)">{m.get("failed",0) or 0:,}</td>'
                 f'<td>{m["remaining"]:,}</td>'
                 f'<td>{speed}</td>'
                 f'<td><div class="btn-group">{btn}</div></td></tr>')
    html += '</tbody></table>'
    return HTMLResponse(html)


@router.post("/mailings/add")
async def add_mailing(request: Request,
                       name: str = Form(""), brand_id: int = Form(0),
                       domain_id: int = Form(0), list_id: int = Form(0),
                       smtp_preset_id: int = Form(0), template_id: int = Form(0),
                       daily_limit: int = Form(0), exclude_domains: str = Form(""),
                       test_email: str = Form(""), test_interval: int = Form(0),
                       schedule_time: str = Form("")):
    db = request.app.state.db
    excludes = [d.strip() for d in exclude_domains.split(",") if d.strip()]
    db.create_mailing(
        name=name or f"Mailing {time.strftime('%Y-%m-%d %H:%M')}",
        brand_id=brand_id, domain_id=domain_id, list_id=list_id,
        smtp_preset_id=smtp_preset_id, template_id=template_id,
        daily_limit=daily_limit, exclude_domains_json=json.dumps(excludes),
        total_leads=db.get_list_lead_count(list_id),
        test_email=test_email, test_interval=test_interval,
        schedule_time=schedule_time,
    )
    return RedirectResponse("/mailings", status_code=303)


@router.post("/mailings/{mid}/save")
async def save_mailing(request: Request, mid: int,
                       name: str = Form(""), brand_id: int = Form(0),
                       domain_id: int = Form(0), list_id: int = Form(0),
                       smtp_preset_id: int = Form(0), template_id: int = Form(0),
                       daily_limit: int = Form(0), exclude_domains: str = Form(""),
                       test_email: str = Form(""), test_interval: int = Form(0),
                       schedule_time: str = Form("")):
    db = request.app.state.db
    excludes = [d.strip() for d in exclude_domains.split(",") if d.strip()]
    c = db._conn()
    c.execute("UPDATE mailings SET name=?,brand_id=?,domain_id=?,list_id=?,"
              "smtp_preset_id=?,template_id=?,daily_limit=?,exclude_domains_json=?,"
              "test_email=?,test_interval=?,schedule_time=?,total_leads=? WHERE id=?",
              (name, brand_id, domain_id, list_id, smtp_preset_id, template_id,
               daily_limit, json.dumps(excludes), test_email, test_interval,
               schedule_time, db.get_list_lead_count(list_id), mid))
    c.commit()
    return RedirectResponse("/mailings", status_code=303)


@router.post("/mailings/{mid}/reset")
async def reset_mailing(request: Request, mid: int):
    """Reset all leads to PENDING so mailing can be re-sent."""
    if mid in _cores:
        return RedirectResponse("/mailings", status_code=303)
    db = request.app.state.db
    m = db._conn().execute("SELECT list_id FROM mailings WHERE id=?", (mid,)).fetchone()
    if m:
        c = db._conn()
        c.execute("UPDATE leads SET state='PENDING' WHERE list_id=? AND state IN ('SENT','FAILED','EXCLUDED')",
                  (m["list_id"],))
        c.execute("UPDATE mailings SET sent=0,failed=0,excluded=0,status='DRAFT',"
                  "started_at=NULL,finished_at=NULL WHERE id=?", (mid,))
        c.commit()
    return RedirectResponse("/mailings", status_code=303)


@router.post("/mailings/{mid}/start")
async def start_mailing(request: Request, mid: int):
    db = request.app.state.db
    if mid in _cores:
        return RedirectResponse("/mailings", status_code=303)

    from bulk.mailer.bulk_core import BulkMailerCore
    core = BulkMailerCore(db, mid)
    _cores[mid] = core
    _speed[mid] = 0

    def run():
        try:
            row = db._conn().execute("SELECT schedule_time FROM mailings WHERE id=?", (mid,)).fetchone()
            schedule = row["schedule_time"] if row else ""
            if schedule:
                from datetime import datetime, timedelta
                try:
                    target_time = datetime.strptime(schedule, "%H:%M").time()
                    now = datetime.now()
                    target = datetime.combine(now.date(), target_time)
                    if target <= now:
                        target += timedelta(days=1)
                    wait = (target - now).total_seconds()
                    while wait > 0 and mid in _cores:
                        time.sleep(min(wait, 5))
                        wait = (target - datetime.now()).total_seconds()
                except ValueError:
                    pass

            def speed_tracker():
                last_check = time.monotonic()
                last_sent = 0
                while mid in _cores:
                    time.sleep(5)
                    try:
                        m = db._conn().execute("SELECT sent FROM mailings WHERE id=?", (mid,)).fetchone()
                        now_sent = m["sent"] if m else 0
                        now_t = time.monotonic()
                        elapsed = now_t - last_check
                        if elapsed > 0:
                            rate = (now_sent - last_sent) / elapsed * 3600
                            _speed[mid] = int(rate)
                        last_check = now_t
                        last_sent = now_sent
                    except Exception:
                        pass

            st = threading.Thread(target=speed_tracker, daemon=True)
            st.start()
            logger.info("Mailing %d thread: calling core.run()", mid)
            core.run()
            logger.info("Mailing %d thread: core.run() returned", mid)
        except Exception as e:
            logger.error("Mailing %d thread EXCEPTION: %s", mid, e, exc_info=True)
        finally:
            _cores.pop(mid, None)
            _speed.pop(mid, None)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    _threads[mid] = t
    return RedirectResponse("/mailings", status_code=303)


@router.post("/mailings/{mid}/stop")
async def stop_mailing(request: Request, mid: int):
    core = _cores.get(mid)
    if core:
        core.stop()
        _cores.pop(mid, None)
    return RedirectResponse("/mailings", status_code=303)


@router.post("/mailings/{mid}/delete")
async def delete_mailing(request: Request, mid: int):
    if mid in _cores:
        return RedirectResponse("/mailings", status_code=303)
    db = request.app.state.db
    c = db._conn()
    c.execute("DELETE FROM mailings WHERE id=?", (mid,))
    c.commit()
    return RedirectResponse("/mailings", status_code=303)


@router.get("/mailings/{mid}/stats", response_class=HTMLResponse)
async def mailing_stats(request: Request, mid: int):
    db = request.app.state.db
    m = db._conn().execute("SELECT * FROM mailings WHERE id=?", (mid,)).fetchone()
    if not m:
        return HTMLResponse("<div>Not found</div>")
    md = dict(m)
    total = md.get("total_leads", 0) or 0
    sent = md.get("sent", 0) or 0
    failed = md.get("failed", 0) or 0
    remaining = max(0, total - sent - failed)
    pct = int((sent + failed) / total * 100) if total > 0 else 0
    running = mid in _cores
    speed = _speed.get(mid, 0)
    speed_str = f"{speed:,}/h" if speed else "—"
    status = md.get("status", "DRAFT")

    return HTMLResponse(f"""
    <div class="grid-4" style="margin-bottom:12px">
        <div class="metric"><div class="value" style="color:var(--green)">{sent:,}</div><div class="label">Sent</div></div>
        <div class="metric"><div class="value" style="color:var(--red)">{failed:,}</div><div class="label">Failed</div></div>
        <div class="metric"><div class="value">{remaining:,}</div><div class="label">Remaining</div></div>
        <div class="metric"><div class="value">{speed_str}</div><div class="label">Speed</div></div>
    </div>
    <div class="progress"><div class="progress-bar" style="width:{pct}%">{pct}%</div></div>
    <p style="margin-top:8px;font-size:12px;color:var(--fg2)">
        Status: <span class="badge badge-{'running' if running else status.lower()}">{status}</span>
    </p>
    """)
