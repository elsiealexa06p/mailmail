"""Admin login / logout — cookie-based session, DB-backed token."""
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import Optional

router = APIRouter()

COOKIE_NAME = "abo_admin"


def get_current_admin(request: Request) -> Optional[dict]:
    tok = request.cookies.get(COOKIE_NAME)
    if not tok:
        return None
    db = request.app.state.db
    return db.get_session(tok)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, e: str = ""):
    return request.app.state.templates.TemplateResponse(request, "login.html", {
        "error": e, "cfg": request.app.state.db.get_config(),
    })


@router.post("/login")
async def login_submit(request: Request,
                        username: str = Form(""),
                        password: str = Form("")):
    db = request.app.state.db
    admin = db.verify_admin(username.strip(), password)
    if not admin:
        return RedirectResponse("/login?e=bad", status_code=303)
    tok = db.create_session(admin["id"])
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(COOKIE_NAME, tok, httponly=True, samesite="lax",
                    secure=True, max_age=86400 * 7, path="/")
    return resp


@router.get("/logout")
async def logout(request: Request):
    tok = request.cookies.get(COOKIE_NAME)
    if tok:
        request.app.state.db.drop_session(tok)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp
