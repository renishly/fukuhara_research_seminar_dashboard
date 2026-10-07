"""Helpers shared by all the page routes: templates, times in JST, and replies.

Times: everything is stored in UTC and shown in Japan Standard Time, written
like "2026-10-08 07:10 JST". Japan has no daylight saving time, so a fixed
+9 hours is always right (and needs no extra package, unlike zoneinfo on
Windows).
"""

import hashlib
import html
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from db import as_utc, utc_now
from names import NameMask
from permissions import course_name, gen_label, timespan_label
from security import Viewer

BASE_DIR = Path(__file__).resolve().parent
JST = timezone(timedelta(hours=9), "JST")

# ---------------------------------------------------------------------------
# Times
# ---------------------------------------------------------------------------


def to_jst(moment: datetime) -> datetime:
    return as_utc(moment).astimezone(JST)


def jst(moment: datetime | None, seconds: bool = False) -> str:
    """e.g. "2026-10-08 07:10 JST" (or with seconds)."""
    if moment is None:
        return ""
    return to_jst(moment).strftime("%Y-%m-%d %H:%M:%S JST" if seconds else "%Y-%m-%d %H:%M JST")


def today_jst() -> date:
    return utc_now().astimezone(JST).date()


def parse_jst_input(value: str) -> datetime | None:
    """A date and time typed on a form (in JST, e.g. "2026-10-08T07:10") as UTC.

    None if it can't be read.
    """
    try:
        typed = datetime.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        return None
    if typed.tzinfo is None:
        typed = typed.replace(tzinfo=JST)
    return typed.astimezone(timezone.utc)


def jst_input_value(moment: datetime | None) -> str:
    """A UTC time as the value of a <input type="datetime-local"> (in JST)."""
    return to_jst(moment).strftime("%Y-%m-%dT%H:%M") if moment else ""


def _count(number: int, unit: str) -> str:
    return f"{number} {unit}{'' if number == 1 else 's'}"


def relative_time(moment: datetime | None, now: datetime | None = None) -> str:
    """How far away a time is, in words: "in 2 days", "3 hours ago", "just now"."""
    if moment is None:
        return ""
    now = now or utc_now()
    seconds = (as_utc(moment) - now).total_seconds()
    distance = abs(seconds)
    if distance < 60:
        return "just now"
    for size, unit in ((86400 * 365, "year"), (86400 * 30, "month"), (86400, "day"), (3600, "hour"), (60, "minute")):
        if distance >= size:
            words = _count(int(distance // size), unit)
            break
    return f"in {words}" if seconds > 0 else f"{words} ago"


DUE_SOON = timedelta(hours=72)


def due_status(due_at: datetime, now: datetime | None = None) -> str | None:
    """ "past" once the due time has passed, "soon" within 72 hours of it, else None."""
    now = now or utc_now()
    due = as_utc(due_at)
    if due <= now:
        return "past"
    if due - now <= DUE_SOON:
        return "soon"
    return None


def is_late(submission, task) -> bool:
    """Submitted (or last edited) after the linked task's due time."""
    if task is None:
        return False
    when = submission.edited_at or submission.created_at
    return as_utc(when) > as_utc(task.due_at)


def human_size(size: int | None) -> str:
    """Bytes in words: "820 B", "14.2 KB", "3.5 MB"."""
    size = size or 0
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB"


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.filters["jst"] = jst
templates.env.filters["relative"] = relative_time
templates.env.filters["human_size"] = human_size
templates.env.globals["course_name"] = course_name
templates.env.globals["timespan_label"] = timespan_label
templates.env.globals["gen_label"] = gen_label
templates.env.globals["due_status"] = due_status
templates.env.globals["is_late"] = is_late


def static_url(name: str) -> str:
    """A static file's address plus a fingerprint of its contents, e.g.
    /static/theme.css?v=3f2a9c01de. Browsers fetch it again whenever it
    changes (instead of using an old copy they kept), but never otherwise."""
    digest = hashlib.sha256((BASE_DIR / "static" / name).read_bytes()).hexdigest()[:10]
    return f"/static/{name}?v={digest}"


templates.env.globals["static_url"] = static_url


def page_context(db: Session, viewer: Viewer | None) -> dict:
    """What every page and fragment needs to know about who is looking.

    - viewer: the admin, student or guest (None on the login page)
    - mask(text): text as this viewer may see it (names hidden from guests)
    - who(user):  a person's name as this viewer may see it
    """
    if viewer is None:
        return {"viewer": None, "mask": lambda text: "" if text is None else str(text), "who": lambda u: ""}
    names = NameMask(db, viewer)
    return {"viewer": viewer, "mask": names.text, "who": names.name, "csrf": viewer.csrf_token}


def render(
    request: Request,
    db: Session,
    viewer: Viewer | None,
    template: str,
    status_code: int = 200,
    **context,
) -> HTMLResponse:
    """A whole page or a fragment, with page_context() included."""
    return templates.TemplateResponse(
        request, template, page_context(db, viewer) | context, status_code=status_code
    )


def render_text(db: Session, viewer: Viewer, template: str, **context) -> str:
    """A template rendered to HTML text (for joining several fragments)."""
    return templates.get_template(template).render(**(page_context(db, viewer) | context))


# ---------------------------------------------------------------------------
# Replies
# ---------------------------------------------------------------------------


def poll_reply(content: str, seen: str | None, seen_id: str) -> Response:
    """The reply to a section that refreshes itself every few seconds.

    If the section's content is exactly what the page already shows, answer
    "204 No Content": HTMX then leaves the page alone, so nothing is redrawn.
    Otherwise send the new content plus a hidden field holding its
    fingerprint, which the page sends back as `seen` next time.
    """
    fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    if seen == fingerprint:
        return Response(status_code=204)
    return HTMLResponse(content + f'<input type="hidden" id="{seen_id}" name="seen" value="{fingerprint}">')


def alert_html(kind: str, text: str) -> str:
    """A small pop-up message ("toast") that removes itself after 3 seconds.

    `kind` is a daisyUI alert style: "success", "warning" or "error".
    The text is escaped, so anything typed by a user is shown as plain text.
    """
    return (
        f'<div role="alert" class="alert alert-{kind}" x-data x-init="setTimeout(() => $el.remove(), 3000)">'
        f"<span>{html.escape(text)}</span></div>"
    )


def toast(kind: str, text: str, status_code: int = 200) -> HTMLResponse:
    """Reply to an HTMX button with a pop-up message (in the #toasts corner)."""
    return HTMLResponse(
        alert_html(kind, text),
        status_code=status_code,
        headers={"HX-Retarget": "#toasts", "HX-Reswap": "beforeend"},
    )


def form_error(text: str) -> HTMLResponse:
    """A short red message, shown inside a pop-up form."""
    return HTMLResponse(f'<p class="text-error text-sm" role="alert">{html.escape(text)}</p>')


def hx_redirect(url: str) -> HTMLResponse:
    """Tell HTMX to load another page (after a successful form in a pop-up)."""
    return HTMLResponse("", headers={"HX-Redirect": url})


def clean_text(raw: str | None, max_length: int) -> tuple[str | None, str | None]:
    """Tidy multi-line text: returns (text or None if empty, error or None).

    Line breaks are kept, but spaces at the ends and Windows line endings
    are tidied.
    """
    text = "\n".join(line.rstrip() for line in (raw or "").replace("\r\n", "\n").strip().split("\n"))
    if len(text) > max_length:
        return text, f"That's too long: at most {max_length:,} characters."
    return text or None, None


def clean_line(raw: str | None) -> str:
    """One line of text, trimmed, with repeated spaces collapsed."""
    return " ".join((raw or "").split())
