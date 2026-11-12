"""The OB/OG directory (Kenichi): graduates who agreed to be contacted for
visits, with their contact details, and a log of who asked whom for a visit
(so nobody is messaged twice).

Members only, never guests: it holds personal contact details. Any member
can add someone, but only with the "they agreed to be listed" box ticked;
the member who added an entry, and admins, can change or remove it.
"""

import re
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, delete, func, select

from db import Alumnus, AlumnusContact, User, as_utc, get_session, utc_now
from routes.common import not_found
from security import Viewer, checked_member, require_member
from web import JST, clean_line, render

router = APIRouter()

PREFERS = {"email": "Email", "phone": "Phone", "either": "Either"}
RECENT = timedelta(days=30)  # a visit request this recent is flagged to the next member
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE = re.compile(r"^[0-9+()\- ]{6,20}$")


def can_change(viewer: Viewer, alumnus: Alumnus) -> bool:
    return viewer.is_admin or alumnus.added_by == viewer.id


def read_form(name, year, company, industry, email, phone, prefers, notes, consent) -> tuple[dict, str | None]:
    values = {"name": clean_line(name)[:100], "year": clean_line(year)[:20], "company": clean_line(company)[:100],
              "industry": clean_line(industry)[:60], "email": clean_line(email)[:200], "phone": clean_line(phone)[:20],
              "prefers": prefers if prefers in PREFERS else "either", "notes": (notes or "").strip()[:1000],
              "consent": consent}
    if not values["name"]:
        return values, "The name is required."
    if not consent:
        return values, "Only list someone who agreed to it: tick the consent box."
    if not values["email"] and not values["phone"]:
        return values, "Give at least an email address or a phone number."
    if values["email"] and not EMAIL.match(values["email"]):
        return values, "That email address doesn't look right."
    if values["phone"] and not PHONE.match(values["phone"]):
        return values, "Phone numbers can use digits, spaces, +, - and brackets."
    return values, None


def page(request: Request, db: Session, viewer: Viewer, q: str = "", industry: str = "", error: str | None = None,
         values: dict | None = None, status_code: int = 200):
    alumni = list(db.exec(select(Alumnus).order_by(func.lower(Alumnus.name))).all())
    industries = sorted({a.industry for a in alumni if a.industry}, key=str.lower)
    words = q.strip().lower()
    shown = [a for a in alumni
             if (not industry or a.industry == industry)
             and (not words or words in f"{a.name} {a.company} {a.industry} {a.year}".lower())]
    # Each one's latest visit request, with who made it.
    latest = {}
    for contact in db.exec(select(AlumnusContact).order_by(AlumnusContact.at)).all():
        latest[contact.alumnus_id] = contact
    members = {u.id: u for u in db.exec(select(User)).all()}
    now = utc_now()
    month_start = now.astimezone(JST).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    visits_this_month = db.exec(select(func.count(AlumnusContact.id)).where(AlumnusContact.at >= month_start)).one()
    rows = [{"alumnus": a, "last": latest.get(a.id), "last_by": members.get(latest[a.id].member_id) if a.id in latest else None,
             "recent": a.id in latest and now - as_utc(latest[a.id].at) < RECENT, "can_change": can_change(viewer, a)}
            for a in shown]
    return render(request, db, viewer, "alumni.html", status_code=status_code, rows=rows, total=len(alumni),
                  industries=industries, industry=industry, q=q, visits_this_month=visits_this_month,
                  prefers=PREFERS, error=error, values=values or {"prefers": "either"})


@router.get("/alumni")
def alumni_page(request: Request, q: str = "", industry: str = "", viewer: Viewer = Depends(require_member),
                db: Session = Depends(get_session)):
    return page(request, db, viewer, q, industry)


@router.post("/alumni")
def add_alumnus(request: Request, name: str = Form(""), year: str = Form(""), company: str = Form(""),
                industry: str = Form(""), email: str = Form(""), phone: str = Form(""), prefers: str = Form("either"),
                notes: str = Form(""), consent: bool = Form(False), viewer: Viewer = Depends(checked_member),
                db: Session = Depends(get_session)):
    values, error = read_form(name, year, company, industry, email, phone, prefers, notes, consent)
    if error:
        return page(request, db, viewer, error=error, values=values, status_code=422)
    db.add(Alumnus(**values, added_by=viewer.id))
    db.commit()
    return RedirectResponse("/alumni", status_code=303)


def own_alumnus(db: Session, viewer: Viewer, alumnus_id: int) -> Alumnus:
    alumnus = db.get(Alumnus, alumnus_id)
    if alumnus is None:
        raise not_found("Not found")
    return alumnus


@router.post("/alumni/{alumnus_id}/edit")
def edit_alumnus(request: Request, alumnus_id: int, name: str = Form(""), year: str = Form(""), company: str = Form(""),
                 industry: str = Form(""), email: str = Form(""), phone: str = Form(""), prefers: str = Form("either"),
                 notes: str = Form(""), consent: bool = Form(False), viewer: Viewer = Depends(checked_member),
                 db: Session = Depends(get_session)):
    alumnus = own_alumnus(db, viewer, alumnus_id)
    if not can_change(viewer, alumnus):
        return page(request, db, viewer, error="Only the member who added them, or an admin, can change this entry.",
                    status_code=403)
    values, error = read_form(name, year, company, industry, email, phone, prefers, notes, consent)
    if error:
        return page(request, db, viewer, error=error, status_code=422)
    for key, value in values.items():
        setattr(alumnus, key, value)
    alumnus.updated_at = utc_now()
    db.add(alumnus)
    db.commit()
    return RedirectResponse("/alumni", status_code=303)


@router.post("/alumni/{alumnus_id}/delete")
def delete_alumnus(request: Request, alumnus_id: int, viewer: Viewer = Depends(checked_member),
                   db: Session = Depends(get_session)):
    alumnus = own_alumnus(db, viewer, alumnus_id)
    if not can_change(viewer, alumnus):
        return page(request, db, viewer, error="Only the member who added them, or an admin, can remove this entry.",
                    status_code=403)
    db.exec(delete(AlumnusContact).where(AlumnusContact.alumnus_id == alumnus.id))
    db.delete(alumnus)
    db.commit()
    return RedirectResponse("/alumni", status_code=303)


@router.post("/alumni/{alumnus_id}/contact")
def log_visit_request(alumnus_id: int, viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    """Note that this member asked them for a visit (shown to everyone, so nobody asks twice)."""
    own_alumnus(db, viewer, alumnus_id)
    db.add(AlumnusContact(alumnus_id=alumnus_id, member_id=viewer.id))
    db.commit()
    return RedirectResponse("/alumni", status_code=303)
