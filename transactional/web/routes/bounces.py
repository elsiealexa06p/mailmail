"""Bounce Analytics — error tracking, spam rejection stats, ISP/profile analysis."""
from html import escape
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

router = APIRouter()


@router.get("/bounces", response_class=HTMLResponse)
async def bounces_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    campaigns = [dict(c) for c in db.get_campaigns_with_bounces(uid)]
    stats = db.get_bounce_stats(user_id=uid)
    suppressions = [dict(s) for s in db.get_suppressions(uid, limit=300)]
    suppress_count = db.get_suppression_count(uid)
    return request.app.state.templates.TemplateResponse(request, "bounces.html", {
        "active": "bounces", "campaigns": campaigns, "stats": stats, "db": db,
        "suppressions": suppressions, "suppress_count": suppress_count,
    })


@router.post("/bounces/suppress/add")
async def add_suppression(request: Request, emails: str = Form("")):
    db = request.app.state.db
    uid = request.state.user["id"]
    lines = [e for e in emails.replace(",", "\n").splitlines() if e.strip()]
    db.import_suppressions(lines, user_id=uid, source="manual")
    return RedirectResponse("/bounces", status_code=303)


@router.post("/bounces/suppress/{sid}/delete")
async def delete_suppression(request: Request, sid: int):
    request.app.state.db.delete_suppression(sid)
    return HTMLResponse("", status_code=200)


@router.post("/bounces/suppress/clear")
async def clear_suppressions(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    db.clear_suppressions(uid)
    return RedirectResponse("/bounces", status_code=303)


@router.get("/bounces/suppress/export")
async def export_suppressions(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    rows = db.get_suppressions(uid, limit=1000000)
    body = "\n".join(dict(r)["email"] for r in rows)
    return Response(content=body, media_type="text/plain",
                    headers={"Content-Disposition": "attachment; filename=suppressions.txt"})


@router.post("/bounces/filter", response_class=HTMLResponse)
async def filter_bounces(request: Request,
                         campaign_id: int = Form(0),
                         error_type: str = Form(""),
                         domain: str = Form(""),
                         profile: str = Form("")):
    db = request.app.state.db
    uid = request.state.user["id"]

    stats = db.get_bounce_stats(campaign_id, uid)
    logs = db.get_bounce_log(campaign_id, uid, error_type, domain, profile, 200)
    logs = [dict(l) for l in logs]

    # Stats summary
    html = '<div class="grid-4" style="margin-bottom:16px">'
    html += f'<div class="metric"><div class="value">{stats["total"]}</div><div class="label">Total Bounces</div></div>'
    spam_count = sum(r["cnt"] for r in stats["by_type"] if r["error_type"] == "spam_reject")
    perm_count = sum(r["cnt"] for r in stats["by_type"] if r["error_type"] in ("permanent_reject", "mailbox_not_found"))
    trans_count = sum(r["cnt"] for r in stats["by_type"] if r["error_type"] in ("rate_limit", "timeout", "connection"))
    html += f'<div class="metric"><div class="value" style="color:var(--red)">{spam_count}</div><div class="label">Spam Rejects</div></div>'
    html += f'<div class="metric"><div class="value">{perm_count}</div><div class="label">Permanent</div></div>'
    html += f'<div class="metric"><div class="value">{trans_count}</div><div class="label">Transient</div></div>'
    html += '</div>'

    # By error type
    if stats["by_type"]:
        html += '<div class="grid-2" style="margin-bottom:16px;align-items:start">'
        html += '<div><strong style="font-size:13px">By Error Type</strong>'
        html += '<table style="font-size:12px;margin-top:6px"><thead><tr><th>Type</th><th>Count</th></tr></thead><tbody>'
        for r in stats["by_type"]:
            badge = "badge-failed" if r["error_type"] == "spam_reject" else "badge-draft"
            html += f'<tr><td><span class="badge {badge}">{r["error_type"]}</span></td><td>{r["cnt"]}</td></tr>'
        html += '</tbody></table></div>'

        # By domain
        html += '<div><strong style="font-size:13px">By ISP / Domain</strong>'
        html += '<table style="font-size:12px;margin-top:6px"><thead><tr><th>Domain</th><th>Count</th></tr></thead><tbody>'
        for r in stats["by_domain"][:15]:
            html += f'<tr><td style="font-family:monospace">{escape(r["recipient_domain"])}</td><td>{r["cnt"]}</td></tr>'
        html += '</tbody></table></div>'
        html += '</div>'

    # Spam by domain + profile (the key insight)
    if stats["spam_by_domain"]:
        html += '<div style="margin-bottom:16px"><strong style="font-size:13px">Spam Rejections: ISP vs MIME Profile</strong>'
        html += '<table style="font-size:12px;margin-top:6px"><thead><tr><th>ISP</th><th>Profile</th><th>Spam Rejects</th></tr></thead><tbody>'
        for r in stats["spam_by_domain"]:
            html += (f'<tr><td style="font-family:monospace">{escape(r["recipient_domain"])}</td>'
                     f'<td><span class="badge badge-info">{escape(r["mime_profile"])}</span></td>'
                     f'<td style="color:var(--red);font-weight:600">{r["cnt"]}</td></tr>')
        html += '</tbody></table></div>'

    # Log entries
    if logs:
        html += f'<div><strong style="font-size:13px">Recent Bounces ({len(logs)})</strong>'
        html += '<div style="max-height:400px;overflow-y:auto;margin-top:6px">'
        html += '<table style="font-size:11px"><thead><tr><th>Time</th><th>Email</th><th>ISP</th><th>Type</th><th>Profile</th><th>Error</th></tr></thead><tbody>'
        for l in logs:
            badge = "badge-failed" if l.get("error_type") == "spam_reject" else "badge-draft"
            html += (f'<tr><td style="white-space:nowrap;color:var(--fg2)">{l.get("created_at","")[:16]}</td>'
                     f'<td style="font-family:monospace">{escape(l.get("email","")[:30])}</td>'
                     f'<td>{escape(l.get("recipient_domain",""))}</td>'
                     f'<td><span class="badge {badge}" style="font-size:10px">{l.get("error_type","")}</span></td>'
                     f'<td style="font-size:10px">{escape(l.get("mime_profile",""))}</td>'
                     f'<td style="color:var(--fg2);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" '
                     f'title="{escape(l.get("error_message",""))}">{escape(l.get("error_message","")[:60])}</td></tr>')
        html += '</tbody></table></div></div>'
    elif stats["total"] == 0:
        html += '<div class="empty-state"><p>No bounces recorded yet.</p></div>'

    return HTMLResponse(html)
