"""Brands + Domains page — CRUD + unsub worker status check."""
import threading
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()

@router.get("/brands", response_class=HTMLResponse)
async def brands_page(request: Request):
    db = request.app.state.db
    tpl = request.app.state.templates
    brands = db.get_brands()
    brand_data = []
    for b in brands:
        bd = dict(b)
        bd["domains"] = [dict(d) for d in db.get_domains(b["id"])]
        bd["used"] = len(db.get_used_lists(b["id"]))
        bd["unused"] = len(db.get_unused_lists(b["id"]))
        brand_data.append(bd)
    return tpl.TemplateResponse(request, "brands.html", {"active": "brands", "brands": brand_data})

@router.post("/brands/add")
async def add_brand(request: Request, name: str = Form("")):
    if name.strip():
        request.app.state.db.add_brand(name.strip())
    return RedirectResponse("/brands", status_code=303)

@router.post("/brands/{bid}/add-domain")
async def add_domain(request: Request, bid: int, domain: str = Form("")):
    if domain.strip():
        request.app.state.db.add_domain(bid, domain.strip())
    return RedirectResponse("/brands", status_code=303)

@router.post("/brands/{bid}/delete")
async def delete_brand(request: Request, bid: int):
    request.app.state.db.delete_brand(bid)
    return RedirectResponse("/brands", status_code=303)

@router.post("/domains/{did}/delete")
async def delete_domain(request: Request, did: int):
    request.app.state.db.delete_domain(did)
    return RedirectResponse("/brands", status_code=303)

@router.post("/domains/{did}/save")
async def save_domain(request: Request, did: int,
                       from_name: str = Form(""), from_email: str = Form(""),
                       reply_to_email: str = Form(""), send_subdomain: str = Form("mail"),
                       unsub_domain: str = Form("")):
    c = request.app.state.db._conn()
    c.execute("UPDATE domains SET from_name=?, from_email=?, reply_to_email=?, "
              "bounce_subdomain=?, send_subdomain=?, unsub_domain=? WHERE id=?",
              (from_name, from_email, reply_to_email, send_subdomain, send_subdomain,
               unsub_domain, did))
    c.commit()
    return RedirectResponse("/brands", status_code=303)


@router.post("/domains/{did}/check-unsub", response_class=HTMLResponse)
async def check_unsub_worker(request: Request, did: int):
    """Check if unsub worker is deployed on Cloudflare for this domain."""
    db = request.app.state.db
    domain_row = db._conn().execute("SELECT * FROM domains WHERE id=?", (did,)).fetchone()
    if not domain_row:
        return HTMLResponse('<span style="color:var(--red)">Domain not found</span>')
    domain_row = dict(domain_row)
    domain = domain_row["domain"]
    unsub_domain = domain_row.get("unsub_domain") or f"unsub.{domain}"

    cf_accounts = db.get_cf_accounts()
    if not cf_accounts:
        return HTMLResponse('<span style="color:var(--fg2)">No CF accounts configured</span>')

    import requests as req_lib
    for acct in cf_accounts:
        acct = dict(acct)
        account_id = acct.get("account_id", "")
        if not account_id:
            continue

        if acct.get("global_api_key") and acct.get("auth_email"):
            headers = {"X-Auth-Key": acct["global_api_key"],
                       "X-Auth-Email": acct["auth_email"]}
        else:
            headers = {"Authorization": f"Bearer {acct.get('api_token', '')}"}

        try:
            worker_name = f"unsub-worker"
            resp = req_lib.get(
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/scripts/{worker_name}",
                headers=headers, timeout=10)
            if resp.status_code == 200:
                db.mark_unsub_deployed(did, unsub_domain)
                return HTMLResponse(
                    f'<span class="badge badge-running">Worker found — marked deployed</span>')
        except Exception:
            continue

    try:
        health = req_lib.get(f"https://{unsub_domain}/health", timeout=8)
        if health.status_code == 200:
            data = health.json()
            if data.get("status") == "ok":
                db.mark_unsub_deployed(did, unsub_domain)
                return HTMLResponse(
                    f'<span class="badge badge-running">Health OK — marked deployed</span>')
    except Exception:
        pass

    return HTMLResponse(
        '<span style="color:var(--fg2)">Worker not found. Deploy it from the Cloudflare page.</span>')
