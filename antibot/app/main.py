"""antibot — FastAPI entrypoint."""
import os
import sys
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.db import DB
from app.routes import auth, setup, gate, admin, settings as settings_route, domains, gates

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_db_path = os.environ.get("ANTIBOT_DB", os.path.join(_project_root, "antibot.db"))
db = DB(_db_path)
db.ensure_secrets()


class AuthMiddleware(BaseHTTPMiddleware):
    """Only admin/* and /logout require an admin session; everything else is public."""

    ADMIN_PREFIXES = ("/admin",)
    PUBLIC_EXACT = {"/login", "/setup", "/logout"}

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        request.state.admin = None

        if path.startswith("/static") or path.startswith("/go/") \
           or path in ("/verify", "/api/check", "/health", "/tls-check", "/favicon.ico") \
           or path in self.PUBLIC_EXACT:
            return await call_next(request)

        if any(path == p or path.startswith(p + "/") for p in self.ADMIN_PREFIXES):
            cfg = db.get_config()
            if db.admin_count() == 0 or cfg.get("setup_done") != "1":
                return RedirectResponse("/setup", status_code=303)
            admin_row = auth.get_current_admin(request)
            if not admin_row:
                return RedirectResponse("/login", status_code=303)
            request.state.admin = admin_row
            return await call_next(request)

        # root — kein Admin-Redirect. Wer zufällig auf guard.example.com/
        # landet soll nicht sehen dass da ein Login lauert. Ausnahme:
        # Setup ist noch nicht durchgelaufen, dann muss man dorthin.
        if path == "/":
            cfg = db.get_config()
            if db.admin_count() == 0 or cfg.get("setup_done") != "1":
                return RedirectResponse("/setup", status_code=303)
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse("not found", status_code=404)

        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    db.close()


app = FastAPI(title="antibot", lifespan=lifespan)
app.state.db = db

_static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
os.makedirs(os.path.join(_static_dir, "logo"), exist_ok=True)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
app.state.templates = templates

app.add_middleware(AuthMiddleware)

app.include_router(auth.router)
app.include_router(setup.router)
app.include_router(gate.router)
app.include_router(admin.router)
app.include_router(settings_route.router)
app.include_router(domains.router)
app.include_router(gates.router)


@app.get("/health")
async def health():
    return {"ok": True}
