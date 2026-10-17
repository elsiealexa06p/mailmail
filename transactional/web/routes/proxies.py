"""Proxy Management — saved proxy configs, single or pool."""
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()


@router.get("/proxies", response_class=HTMLResponse)
async def proxies_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    proxies = [dict(p) for p in db.get_proxies(uid)]
    cfg = db.get_config()
    return request.app.state.templates.TemplateResponse(request, "proxies.html", {
        "active": "proxies", "proxies": proxies, "cfg": cfg, "db": db,
    })


@router.post("/proxies/add")
async def add_proxy(request: Request,
                    name: str = Form(""),
                    proxy_type: str = Form("single"),
                    value: str = Form(""),
                    rotate_every: int = Form(0)):
    if name.strip() and value.strip():
        uid = request.state.user['id']
        request.app.state.db.add_proxy(name.strip(), proxy_type, value.strip(), rotate_every, uid)
    return RedirectResponse("/proxies", status_code=303)


@router.post("/proxies/{pid}/save")
async def save_proxy(request: Request, pid: int,
                     name: str = Form(""),
                     proxy_type: str = Form("single"),
                     value: str = Form(""),
                     rotate_every: int = Form(0)):
    request.app.state.db.update_proxy(pid, name=name.strip(), proxy_type=proxy_type,
                                       value=value.strip(), rotate_every=rotate_every)
    return RedirectResponse("/proxies", status_code=303)


@router.post("/proxies/{pid}/delete")
async def delete_proxy(request: Request, pid: int):
    request.app.state.db.delete_proxy(pid)
    return RedirectResponse("/proxies", status_code=303)


@router.post("/proxies/{pid}/activate")
async def activate_proxy(request: Request, pid: int):
    """Set this proxy config as the active one in global config."""
    db = request.app.state.db
    proxy = db.get_proxy(pid)
    if proxy:
        p = dict(proxy)
        db.update_config(
            proxy_mode=p["proxy_type"],
            proxy_value=p["value"],
            proxy_rotate_every=p["rotate_every"])
    return RedirectResponse("/proxies", status_code=303)
