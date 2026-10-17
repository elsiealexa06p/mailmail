"""Admin panel — dashboard, decision log, playground."""
import json
import time
import hashlib
from html import escape
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

router = APIRouter()


@router.get("/admin", response_class=HTMLResponse)
async def dashboard(request: Request, welcome: str = ""):
    db = request.app.state.db
    cfg = db.get_config()
    now = int(time.time())
    last_hour = db.counts_since(now - 3600)
    last_day  = db.counts_since(now - 86400)
    top_asns  = db.top_blocked_asns(now - 86400, limit=10)
    recent    = db.recent_decisions(limit=20)
    from .gate import _owner_bypass
    return request.app.state.templates.TemplateResponse(request, "admin_dashboard.html", {
        "cfg": cfg,
        "welcome": bool(welcome),
        "last_hour": last_hour,
        "last_day": last_day,
        "top_asns": top_asns,
        "recent": recent,
        "owner_bypass": _owner_bypass(cfg.get("cookie_secret", "")),
    })


@router.get("/admin/log", response_class=HTMLResponse)
async def log_view(request: Request, verdict: str = "", asn: str = "",
                    ip: str = "", limit: int = 200):
    db = request.app.state.db
    cfg = db.get_config()
    limit = max(1, min(int(limit or 200), 2000))
    rows = db.recent_decisions(limit=limit, verdict=verdict, asn=asn, ip=ip)
    for r in rows:
        try:
            r["signals"] = json.loads(r.get("signals_json") or "{}")
        except Exception:
            r["signals"] = {}
    return request.app.state.templates.TemplateResponse(request, "admin_log.html", {
        "cfg": cfg, "rows": rows,
        "filter_verdict": verdict, "filter_asn": asn, "filter_ip": ip,
        "limit": limit,
    })


@router.get("/admin/log.csv")
async def log_csv(request: Request, verdict: str = "", asn: str = "",
                   ip: str = "", limit: int = 5000):
    db = request.app.state.db
    limit = max(1, min(int(limit or 5000), 20000))
    rows = db.recent_decisions(limit=limit, verdict=verdict, asn=asn, ip=ip)
    header = "ts,verdict,score,ip,asn,country,user_agent,target,token_valid,dry_run\n"
    lines = [header]
    for r in rows:
        lines.append(",".join(str(v).replace(",", " ").replace("\n", " ")
                              for v in (r["ts"], r["verdict"], r["score"], r["ip"],
                                         r["asn"], r["country"], r["user_agent"],
                                         r["target"], r["token_valid"], r["dry_run"])) + "\n")
    return Response("".join(lines), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=decisions.csv"})


@router.get("/admin/policies", response_class=HTMLResponse)
async def policies_view(request: Request):
    db = request.app.state.db
    return request.app.state.templates.TemplateResponse(request, "admin_policies.html", {
        "cfg": db.get_config(),
        "asn_rules": db.get_asn_rules(),
        "ip_rules": db.get_ip_rules(),
        "country_rules": db.get_country_rules(),
    })


@router.post("/admin/policies/asn/add")
async def asn_add(request: Request, asn: str = Form(""), verdict: str = Form(""),
                   note: str = Form("")):
    if asn.strip():
        request.app.state.db.upsert_asn_rule(asn.strip(), verdict.strip() or "score:20", note.strip())
    return RedirectResponse("/admin/policies", status_code=303)


@router.post("/admin/policies/asn/{asn}/delete")
async def asn_del(request: Request, asn: str):
    request.app.state.db.delete_asn_rule(asn)
    return RedirectResponse("/admin/policies", status_code=303)


@router.post("/admin/policies/ip/add")
async def ip_add(request: Request, ip: str = Form(""), verdict: str = Form(""),
                  note: str = Form("")):
    if ip.strip() and verdict in ("allow", "block"):
        request.app.state.db.upsert_ip_rule(ip.strip(), verdict, note.strip())
    return RedirectResponse("/admin/policies", status_code=303)


@router.post("/admin/policies/ip/{ip}/delete")
async def ip_del(request: Request, ip: str):
    request.app.state.db.delete_ip_rule(ip)
    return RedirectResponse("/admin/policies", status_code=303)


@router.post("/admin/policies/country/add")
async def cc_add(request: Request, cc: str = Form(""), delta: int = Form(0),
                  note: str = Form("")):
    if cc.strip():
        request.app.state.db.upsert_country_rule(cc.strip(), delta, note.strip())
    return RedirectResponse("/admin/policies", status_code=303)


@router.post("/admin/policies/country/{cc}/delete")
async def cc_del(request: Request, cc: str):
    request.app.state.db.delete_country_rule(cc)
    return RedirectResponse("/admin/policies", status_code=303)


# ── Playground ─────────────────────────────────────────────
@router.get("/admin/playground", response_class=HTMLResponse)
async def playground_view(request: Request):
    return request.app.state.templates.TemplateResponse(request, "admin_playground.html", {
        "cfg": request.app.state.db.get_config(),
        "result": None,
    })


@router.post("/admin/playground", response_class=HTMLResponse)
async def playground_run(request: Request,
                          ip: str = Form(""), user_agent: str = Form(""),
                          honeypot: str = Form(""), webdriver: str = Form(""),
                          webgl_vendor: str = Form(""), canvas_hash: str = Form(""),
                          submit_ms: int = Form(0)):
    from ..scoring import score_request
    from ..tokens import session_bucket_from_request
    db = request.app.state.db
    cfg = db.get_config()
    bucket = session_bucket_from_request(ip, user_agent, cfg.get("cookie_secret", ""))
    signals = {
        "honeypot": bool(honeypot), "webdriver": webdriver == "true",
        "webgl_vendor": webgl_vendor, "canvas_hash": canvas_hash,
        "no_plugins": False, "submit_ms": submit_ms, "pow_ok": True,
    }
    result = score_request(db, cfg, ip=ip, user_agent=user_agent,
                            client_signals=signals, rate_bucket=bucket)
    return request.app.state.templates.TemplateResponse(request, "admin_playground.html", {
        "cfg": cfg, "result": result,
        "form": {"ip": ip, "user_agent": user_agent, "honeypot": honeypot,
                 "webdriver": webdriver, "webgl_vendor": webgl_vendor,
                 "canvas_hash": canvas_hash, "submit_ms": submit_ms},
    })
