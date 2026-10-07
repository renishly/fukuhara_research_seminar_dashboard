"""Signing in and out, registering, guests, and the Account page."""

import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from db import Gen, User, get_session, utc_now
from names import MAX_OTHER_NAMES, assign_guest_number, split_other_names
from security import (
    MIN_PASSWORD_LENGTH,
    SESSION_COOKIE,
    Viewer,
    checked_member,
    checked_viewer,
    clear_session_cookie,
    end_all_sessions,
    end_session,
    find_viewer,
    hash_password,
    password_matches,
    record_failure,
    registration_key_matches,
    registration_open,
    require_member,
    safe_next,
    set_session_cookie,
    start_session,
    too_many_failures,
)
from web import render

router = APIRouter()

LOGIN_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{3,30}")
MAX_FULL_NAME = 100
LOGIN_FAILED = "Login ID or password is incorrect."
TOO_MANY = "Too many attempts from your network. Please wait 15 minutes and try again."


# ---------------------------------------------------------------------------
# Checking what people type
# ---------------------------------------------------------------------------


def login_id_error(login_id: str) -> str | None:
    if not LOGIN_ID_PATTERN.fullmatch(login_id):
        return "The Login ID must be 3 to 30 characters: letters, digits, \".\", \"_\" or \"-\"."
    return None


def full_name_error(full_name: str) -> str | None:
    if not 1 <= len(full_name) <= MAX_FULL_NAME:
        return f"The full name must be 1 to {MAX_FULL_NAME} characters."
    return None


def password_error(password: str, again: str) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"The password must be at least {MIN_PASSWORD_LENGTH} characters."
    if password != again:
        return "The two passwords don't match."
    return None


def other_names_error(raw: str) -> str | None:
    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) > MAX_OTHER_NAMES:
        return f"You can list up to {MAX_OTHER_NAMES} other names."
    if any(len(line.strip()) > MAX_FULL_NAME for line in lines):
        return f"Each other name can be at most {MAX_FULL_NAME} characters."
    return None


def all_gens(db: Session) -> list[Gen]:
    """Every gen, newest first."""
    return list(db.exec(select(Gen).order_by(Gen.number.desc())).all())


def signed_in_response(request: Request, db: Session, user: User | None, next_page: str) -> RedirectResponse:
    """Sign this device in (a member, or a guest when user is None) and go on."""
    token, lifetime = start_session(db, user)
    response = RedirectResponse(safe_next(next_page), status_code=303)
    set_session_cookie(response, request, token, lifetime)
    return response


# ---------------------------------------------------------------------------
# Login, guest, logout
# ---------------------------------------------------------------------------


@router.get("/")
def home(request: Request, db: Session = Depends(get_session)):
    """Signed in (as a member or a guest): the classes. Otherwise: the login page."""
    return RedirectResponse("/classes" if find_viewer(request, db) else "/login", status_code=303)


@router.get("/login")
def login_page(request: Request, next: str | None = None, signed_out: bool = False,
               db: Session = Depends(get_session)):
    if find_viewer(request, db):
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, db, None, "login.html", next=safe_next(next), signed_out=signed_out, error=None)


@router.post("/login")
def login(
    request: Request,
    login_id: str = Form(""),
    password: str = Form(""),
    next: str = Form("/classes"),
    db: Session = Depends(get_session),
):
    """Check the Login ID and password, then sign this device in for 7 days."""
    def failed(message: str, status_code: int):
        return render(request, db, None, "login.html", status_code=status_code,
                      next=safe_next(next), signed_out=False, login_id=login_id, error=message)

    if too_many_failures(request):
        return failed(TOO_MANY, 429)
    user = db.exec(select(User).where(User.login_id == login_id.strip().lower())).first()
    # The same message whether the Login ID or the password was wrong, so
    # nobody can find out which Login IDs exist.
    if user is None or not password_matches(password, user.password_hash):
        record_failure(request)
        return failed(LOGIN_FAILED, 401)
    return signed_in_response(request, db, user, next)


@router.post("/guest")
def continue_as_guest(request: Request, next: str = Form("/classes"), db: Session = Depends(get_session)):
    """Look around without an account: read-only, limited, for a day."""
    return signed_in_response(request, db, None, next)


@router.post("/logout")
def logout(request: Request, viewer: Viewer = Depends(checked_viewer), db: Session = Depends(get_session)):
    """Sign this device out."""
    end_session(db, request.cookies.get(SESSION_COOKIE))
    response = RedirectResponse("/login?signed_out=true", status_code=303)
    clear_session_cookie(response, request)
    return response


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_page(request: Request, db: Session, status_code: int = 200, **form):
    return render(
        request, db, None, "register.html", status_code=status_code,
        gens=all_gens(db),
        student_open=registration_open("student"),
        admin_open=registration_open("admin"),
        form=form,
    )


@router.get("/register")
def register_form(request: Request, db: Session = Depends(get_session)):
    viewer = find_viewer(request, db)
    if viewer is not None and viewer.is_member:  # already have an account
        return RedirectResponse("/classes", status_code=303)
    return register_page(request, db, role="student")


@router.post("/register")
def register(
    request: Request,
    role: str = Form("student"),
    login_id: str = Form(""),
    full_name: str = Form(""),
    password: str = Form(""),
    password_again: str = Form(""),
    gen_id: int | None = Form(None),
    registration_key: str = Form(""),
    db: Session = Depends(get_session),
):
    """Create an admin or student account. Each role needs its registration key."""
    role = "admin" if role == "admin" else "student"
    login_id = login_id.strip()
    full_name = " ".join(full_name.split())
    typed = {"role": role, "login_id": login_id, "full_name": full_name, "gen_id": gen_id}

    def refused(message: str, status_code: int = 400):
        return register_page(request, db, status_code=status_code, error=message, **typed)

    if too_many_failures(request):
        return refused(TOO_MANY, 429)
    if not registration_open(role):
        return refused(f"Registration for {role}s is switched off.", 403)
    if not registration_key_matches(role, registration_key):
        record_failure(request)
        return refused(f"The {role} registration key is incorrect.", 403)

    error = login_id_error(login_id) or full_name_error(full_name) or password_error(password, password_again)
    gen = db.get(Gen, gen_id) if (role == "student" and gen_id is not None) else None
    if error is None and role == "student" and gen is None:
        error = "Choose your gen."
    if error is None and db.exec(select(User).where(User.login_id == login_id.lower())).first():
        error = "That Login ID is already taken."
    if error:
        return refused(error)

    user = User(
        login_id=login_id.lower(),
        full_name=full_name,
        role=role,
        gen_id=gen.id if gen else None,
        password_hash=hash_password(password),
    )
    if role == "student":
        assign_guest_number(db, user)
    db.add(user)
    db.commit()
    db.refresh(user)
    return signed_in_response(request, db, user, "/classes")


# ---------------------------------------------------------------------------
# The Account page
# ---------------------------------------------------------------------------


def account_page(request: Request, db: Session, viewer: Viewer, status_code: int = 200, **messages):
    gen = db.get(Gen, viewer.user.gen_id) if viewer.user.gen_id else None
    return render(request, db, viewer, "account.html", status_code=status_code, gen=gen, **messages)


@router.get("/account")
def account(request: Request, viewer: Viewer = Depends(require_member), db: Session = Depends(get_session)):
    return account_page(request, db, viewer)


@router.post("/account/password")
def change_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    new_password_again: str = Form(""),
    viewer: Viewer = Depends(checked_member),
    db: Session = Depends(get_session),
):
    """Change your own password. Every other device is signed out."""
    user = viewer.user
    if not password_matches(current_password, user.password_hash):
        return account_page(request, db, viewer, 400, password_error="Your current password is incorrect.")
    error = password_error(new_password, new_password_again)
    if error:
        return account_page(request, db, viewer, 400, password_error=error)
    user.password_hash = hash_password(new_password)
    user.credentials_changed_at = utc_now()
    db.add(user)
    db.commit()
    end_all_sessions(db, user.id, keep_token=request.cookies.get(SESSION_COOKIE))
    return account_page(request, db, viewer, password_notice="Password changed. Your other devices were signed out.")


@router.post("/account/signout-all")
def sign_out_everywhere(request: Request, viewer: Viewer = Depends(checked_member),
                        db: Session = Depends(get_session)):
    """End every sign-in of this member, including this device."""
    end_all_sessions(db, viewer.user.id)
    response = RedirectResponse("/login?signed_out=true", status_code=303)
    clear_session_cookie(response, request)
    return response


@router.post("/account/other-names")
def save_other_names(
    request: Request,
    other_names: str = Form(""),
    viewer: Viewer = Depends(checked_member),
    db: Session = Depends(get_session),
):
    """A student's other names (romaji, nicknames...), hidden from guests like their name."""
    error = other_names_error(other_names)
    if error:
        return account_page(request, db, viewer, 400, names_error=error)
    viewer.user.other_names = "\n".join(split_other_names(other_names)) or None
    db.add(viewer.user)
    db.commit()
    return account_page(request, db, viewer, names_notice="Saved.")
