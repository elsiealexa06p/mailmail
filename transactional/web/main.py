"""Transactional Mailer Web — FastAPI + HTMX + Jinja2."""
import sys
import os
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from contextlib import asynccontextmanager
from starlette.middleware.base import BaseHTTPMiddleware

# python-multipart limitiert Form-Felder auf 1MB by default. Für große
# Macros/Templates/Snippets/Lead-Blocks brauchen wir mehr.
_MAX_FIELD = 50 * 1024 * 1024        # 50 MB
_MAX_FILE  = 200 * 1024 * 1024       # 200 MB

# 1) Class-Attribute (funktioniert für FormParser bei urlencoded und
#    für MultiPartParser wenn kein max_part_size explicit übergeben wird).
try:
    from starlette.formparsers import FormParser as _FormParser, MultiPartParser as _MultiPartParser
    _FormParser.max_field_size = _MAX_FIELD
    _MultiPartParser.max_field_size = _MAX_FIELD
    _MultiPartParser.max_file_size  = _MAX_FILE
except Exception:
    pass

# 2) Monkey-patch Request.form — Starlette >= 0.40 nimmt max_part_size
#    als Kwarg und Default ist 1MB, was das Class-Attribute übersteuert.
#    Wir setzen den Default hoch damit Form()-Dependencies auch klappen.
try:
    import inspect as _inspect
    from starlette.requests import Request as _Req
    _orig_form = _Req.form
    _accepts_max_part = ("max_part_size" in
                          _inspect.signature(_orig_form).parameters)
    if _accepts_max_part:
        async def _bigger_form(self, *args, **kwargs):
            kwargs.setdefault("max_part_size", _MAX_FIELD)
            return await _orig_form(self, *args, **kwargs)
        _Req.form = _bigger_form
except Exception:
    pass

from .db import TransDB
from .routes import campaigns, smtps, leads, templates, auth, logos, redirects, macros, proxies, pools, config as config_route, settings, ai, admin, testlab, bounces, htmlgen, inboxtest, exporter, cloudinary, smtp_check, antibot_config, snippets, s3_cdn, jobs, pool

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
db = TransDB(os.path.join(_project_root, "trans.db"))


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        request.state.user = None

        if path.startswith("/static") or path in ("/login", "/setup"):
            return await call_next(request)

        if db.user_count() == 0:
            return RedirectResponse("/login", status_code=303)

        user = auth.get_current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        if not user.get("is_active", 1):
            return RedirectResponse("/login", status_code=303)

        if not auth.check_permission(user, path):
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse("Access denied", status_code=403)

        request.state.user = user
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    db.close()

app = FastAPI(title="Transactional Mailer", lifespan=lifespan)
app.state.db = db

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import traceback
    tb = traceback.format_exc()
    logger = logging.getLogger("trans.error")
    logger.error("Unhandled: %s\n%s", exc, tb)
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(f"Error: {exc}\n\n{tb}", status_code=500)

app.add_middleware(AuthMiddleware)

_static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# Reuse bulk mailer CSS. Wenn die Ziel-style.css jemand anderem gehört
# (typisch nach einem git pull als root) → nicht crashen. Die alte CSS
# reicht dann für diesen Boot.
_bulk_css = os.path.join(_project_root, "bulk", "web", "static", "style.css")
if os.path.isfile(_bulk_css):
    import shutil
    try:
        shutil.copy2(_bulk_css, os.path.join(_static_dir, "style.css"))
    except (PermissionError, OSError) as _css_err:
        logging.warning("could not sync bulk style.css (ownership issue?): %s",
                         _css_err)

tpl_dir = os.path.join(os.path.dirname(__file__), "templates")
jinja_templates = Jinja2Templates(directory=tpl_dir)
app.state.templates = jinja_templates

app.include_router(auth.router)
app.include_router(campaigns.router)
app.include_router(smtps.router)
app.include_router(leads.router)
app.include_router(templates.router)
app.include_router(macros.router)
app.include_router(snippets.router)
app.include_router(logos.router)
app.include_router(redirects.router)
app.include_router(proxies.router)
app.include_router(pools.router)
app.include_router(ai.router)
app.include_router(config_route.router)
app.include_router(settings.router)
app.include_router(testlab.router)
app.include_router(bounces.router)
app.include_router(admin.router)
app.include_router(htmlgen.router)
app.include_router(inboxtest.router)
app.include_router(exporter.router)
app.include_router(cloudinary.router)
app.include_router(s3_cdn.router)
app.include_router(smtp_check.router)
app.include_router(antibot_config.router)
app.include_router(jobs.router)
app.include_router(pool.router)


@app.get("/")
async def index(request: Request):
    return RedirectResponse("/campaigns", status_code=303)
