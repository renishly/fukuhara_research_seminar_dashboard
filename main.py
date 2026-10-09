"""The Fukuhara Research Seminar Dashboard: the FastAPI app.

Run locally with:   fastapi dev main.py
Then open:          http://127.0.0.1:8000

This file only sets the app up. The pages and the API live in routes/:
  routes/auth.py        login, register, guest, logout, account
  routes/classes.py     the timeline, classes, and the class page with its tabs
  routes/coursework.py  assignments, exams, submissions and file downloads
  routes/agents.py      students' agent dashboards and agent pages
  routes/agent_api.py   the API that agents talk to (with their X-API-Key)
  routes/members.py     the admins' Members page
"""

import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError
from fastapi.staticfiles import StaticFiles

# Importing db also loads your .env file (see the top of db.py).
import storage
from db import create_db_and_tables
from routes import agent_api, agents, auth, classes, coursework, members
from web import BASE_DIR, alert_html, templates


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs once when the server starts: create tables and Gen 1-12 if missing,
    and say in the log where uploaded files are kept (and whether that works)."""
    create_db_and_tables()
    status = storage.check()
    print(f"File storage: {status['where']}. {'OK' if status['ok'] else 'PROBLEM'}: {status['message']}")
    yield


app = FastAPI(title="Fukuhara Research Seminar Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# The order matters where addresses look alike: e.g. POST /classes/1/agents
# (agents.py) must be found before POST /classes/1/{assignments or exams}
# (coursework.py), so coursework comes last.
for module in (auth, classes, agents, agent_api, members, coursework):
    app.include_router(module.router)


@app.middleware("http")
async def never_store_private_pages(request: Request, call_next):
    """Tell browsers (and anything in between) never to keep a copy of a page.

    Otherwise a restored tab or the Back button could show a signed-in page
    without asking the server, and so without checking the sign-in. Replies
    that set their own caching (static files, downloads) keep it.
    """
    response = await call_next(request)
    if not request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def _wants_html_page(request: Request) -> bool:
    return request.method == "GET" and "text/html" in request.headers.get("accept", "")


@app.exception_handler(HTTPException)
async def friendly_errors(request: Request, error: HTTPException):
    """Show errors in a way that fits where they happened.

    - "Go to the login page" redirects (303) are just followed.
    - A button or form using HTMX gets a pop-up message (a "toast").
    - A page opened in the browser gets a simple error page.
    - Anything else (the agent API, scripts) gets the usual JSON.
    """
    headers = getattr(error, "headers", None) or {}
    if error.status_code == 303 and "Location" in headers:
        return RedirectResponse(headers["Location"], status_code=303)
    message = error.detail if isinstance(error.detail, str) else "Something went wrong."
    if request.headers.get("HX-Request") and "HX-Redirect" not in headers:
        return HTMLResponse(
            alert_html("error", message),
            status_code=error.status_code,
            headers={"HX-Retarget": "#toasts", "HX-Reswap": "beforeend", **headers},
        )
    if _wants_html_page(request) and not request.url.path.startswith("/api/"):
        return templates.TemplateResponse(
            request, "error.html",
            {"status": error.status_code, "message": message, "viewer": None},
            status_code=error.status_code,
        )
    return await http_exception_handler(request, error)


@app.exception_handler(RequestValidationError)
async def friendly_validation_errors(request: Request, error: RequestValidationError):
    """A form or address the site can't read: a toast for HTMX, JSON otherwise."""
    if request.headers.get("HX-Request"):
        return HTMLResponse(
            alert_html("error", "Please check the form and try again."),
            status_code=422,
            headers={"HX-Retarget": "#toasts", "HX-Reswap": "beforeend"},
        )
    return await request_validation_exception_handler(request, error)


DATABASE_FULL = (
    "The database is full, so the site can't save anything right now (pages can still be read). "
    "Please tell an admin: they can free space by deleting old classes or agents."
)
SOMETHING_WENT_WRONG = "Something went wrong on the server. Please try again; if it keeps happening, tell an admin."


def database_is_read_only(error: Exception) -> bool:
    """True for the error Postgres gives when Supabase has made a full database read-only."""
    message = str(error).lower()
    return isinstance(error, SQLAlchemyError) and ("read-only" in message or "read only" in message)


@app.exception_handler(Exception)
async def friendly_server_errors(request: Request, error: Exception):
    """Any unexpected error: a plain explanation instead of "Internal Server Error".

    The full details still go to the server's log (Render's "Logs" tab).
    Agents get 503, which the connector treats as "try again later": their
    outputs wait in its offline buffer instead of being lost.
    """
    traceback.print_exception(error)
    full = database_is_read_only(error)
    message = DATABASE_FULL if full else SOMETHING_WENT_WRONG
    status_code = 503 if full else 500
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": message}, status_code=503)
    if request.headers.get("HX-Request"):
        return HTMLResponse(alert_html("error", message), status_code=status_code,
                            headers={"HX-Retarget": "#toasts", "HX-Reswap": "beforeend"})
    return templates.TemplateResponse(
        request, "error.html", {"status": status_code, "message": message, "viewer": None},
        status_code=status_code,
    )
