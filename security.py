"""Sign-ins, passwords, CSRF protection, roles, agent API keys and rate limits.

The FastAPI "dependencies" here are added to routes to protect them:
  - current_viewer     anyone signed in: an admin, a student or a guest
  - require_member     admins and students only (guests get 403)
  - require_admin      admins only
  - checked_viewer / checked_member / checked_admin
                       the same, for actions that change something (POST):
                       these also check the CSRF token (see below)
  - require_agent      the agent API: finds the agent by its X-API-Key

Secrets (registration keys, KEY_ENCRYPTION_SECRET...) are read from
environment variables each time, so changing them only needs a restart.
"""

import base64
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import quote, urlparse

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from sqlmodel import Session, delete, select

from db import Agent, LoginSession, User, as_utc, get_session, utc_now


def secret_matches(given: str | None, expected: str | None) -> bool:
    """Safely compare a value someone sent with the real secret.

    Returns False if the real secret is missing or empty, so an unset
    environment variable can never be "matched" by an empty value.
    """
    if not expected or not given:
        return False
    # compare_digest takes the same time whether the first or last character
    # differs, so attackers can't guess a secret one character at a time.
    return secrets.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


# ---------------------------------------------------------------------------
# Passwords (hashed with the standard library's scrypt)
# ---------------------------------------------------------------------------

# scrypt is deliberately slow and memory-hungry, so someone with a copy of the
# database can't try millions of guesses per second. n=2**14, r=8 uses 16 MB.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
MIN_PASSWORD_LENGTH = 8


def hash_password(password: str) -> str:
    """Turn a password into "scrypt$n$r$p$salt$hash" text for the database.

    A random salt means two people with the same password get different hashes.
    """
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def password_matches(password: str, stored: str) -> bool:
    """True if `password` is the one that produced the stored hash."""
    try:
        _, n, r, p, salt, expected = stored.split("$")
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


def temporary_password() -> str:
    """A new random password, e.g. for an admin's "Reset password"."""
    return secrets.token_urlsafe(9)  # 12 characters


# ---------------------------------------------------------------------------
# Sign-ins: one LoginSession row per signed-in device
# ---------------------------------------------------------------------------
#
# How it works:
#   1. On /login (or "Continue as guest"), the site makes a long random token,
#      stores its fingerprint in the LoginSession table, and gives the token to
#      that browser in a cookie.
#   2. Every page must bring that cookie. Other devices don't have it, so they
#      have to sign in themselves.
# The cookie can't be read by page scripts (HttpOnly), isn't sent by other
# websites' requests (SameSite=Lax), and only travels over https on the
# deployed site (Secure).

SESSION_COOKIE = "seminar_session"
MEMBER_LIFETIME = timedelta(days=7)
GUEST_LIFETIME = timedelta(days=1)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "testserver"}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def start_session(db: Session, user: User | None) -> tuple[str, timedelta]:
    """Sign a device in (user=None for a guest). Returns (cookie token, lifetime).

    Also tidies away sign-ins that have already expired.
    """
    now = utc_now()
    db.exec(delete(LoginSession).where(LoginSession.expires_at < now))
    lifetime = GUEST_LIFETIME if user is None else MEMBER_LIFETIME
    token = secrets.token_urlsafe(32)
    db.add(LoginSession(
        token_hash=token_hash(token),
        user_id=user.id if user else None,
        is_guest=user is None,
        expires_at=now + lifetime,
        csrf_token=secrets.token_urlsafe(32),
    ))
    db.commit()
    return token, lifetime


def find_session(db: Session, token: str | None) -> LoginSession | None:
    """The still-valid sign-in this cookie token belongs to, or None."""
    if not token:
        return None
    login = db.exec(select(LoginSession).where(LoginSession.token_hash == token_hash(token))).first()
    if login is None or as_utc(login.expires_at) <= utc_now():
        return None
    return login


def end_session(db: Session, token: str | None) -> None:
    """Sign this device out."""
    if token:
        db.exec(delete(LoginSession).where(LoginSession.token_hash == token_hash(token)))
        db.commit()


def end_all_sessions(db: Session, user_id: int, keep_token: str | None = None) -> None:
    """Sign out every device of one member (except, optionally, this one)."""
    statement = delete(LoginSession).where(LoginSession.user_id == user_id)
    if keep_token:
        statement = statement.where(LoginSession.token_hash != token_hash(keep_token))
    db.exec(statement)
    db.commit()


def cookie_is_secure(request: Request) -> bool:
    """Send the cookie over https only, except on this computer (http://localhost)."""
    return request.url.hostname not in LOCAL_HOSTS


def set_session_cookie(response, request: Request, token: str, lifetime: timedelta) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(lifetime.total_seconds()),
        httponly=True,  # page scripts can't read it
        samesite="lax",  # other websites' requests don't carry it
        secure=cookie_is_secure(request),  # https only, except on this computer
        path="/",
    )


def clear_session_cookie(response, request: Request) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax",
                           secure=cookie_is_secure(request))


# ---------------------------------------------------------------------------
# Who is looking: the Viewer
# ---------------------------------------------------------------------------


@dataclass
class Viewer:
    """The person behind a request: a member (admin or student) or a guest."""

    session: LoginSession
    user: User | None  # None for guests

    @property
    def is_guest(self) -> bool:
        return self.user is None

    @property
    def is_member(self) -> bool:
        return self.user is not None

    @property
    def is_admin(self) -> bool:
        return self.user is not None and self.user.role == "admin"

    @property
    def is_student(self) -> bool:
        return self.user is not None and self.user.role == "student"

    @property
    def id(self) -> int | None:
        return self.user.id if self.user else None

    @property
    def csrf_token(self) -> str:
        return self.session.csrf_token


def safe_next(next_page: str | None) -> str:
    """Where to go after signing in: only a page on this site (never another website)."""
    if next_page and next_page.startswith("/") and not next_page.startswith("//") and "\\" not in next_page:
        return next_page
    return "/classes"


def _not_signed_in(request: Request) -> HTTPException:
    """The reply for a request without a valid sign-in.

    - A page opened in the browser: go to the login page, then come back.
    - An HTMX request (a button, or part of a page refreshing itself):
      tell HTMX to load the login page (HX-Redirect).
    - Anything else (e.g. a script): a plain 401.
    """
    if request.headers.get("HX-Request"):
        current = urlparse(request.headers.get("HX-Current-URL", ""))
        page = current.path + (f"?{current.query}" if current.query else "")
        return HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in",
                             headers={"HX-Redirect": f"/login?next={quote(safe_next(page), safe='')}"})
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        page = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return HTTPException(status.HTTP_303_SEE_OTHER, "Not signed in",
                             headers={"Location": f"/login?next={quote(page, safe='')}"})
    return HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")


def find_viewer(request: Request, db: Session) -> Viewer | None:
    """The viewer for this request's cookie, or None if not signed in."""
    login = find_session(db, request.cookies.get(SESSION_COOKIE))
    if login is None:
        return None
    if login.is_guest:
        return Viewer(session=login, user=None)
    user = db.get(User, login.user_id) if login.user_id else None
    if user is None:  # the account was deleted
        return None
    return Viewer(session=login, user=user)


def current_viewer(request: Request, db: Session = Depends(get_session)) -> Viewer:
    """Allow the request only from a signed-in browser (a member or a guest)."""
    viewer = find_viewer(request, db)
    if viewer is None:
        raise _not_signed_in(request)
    return viewer


def forbidden(message: str = "You don't have permission to do that.") -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, message)


def require_member(viewer: Viewer = Depends(current_viewer)) -> Viewer:
    """Admins and students only. Guests are only looking around."""
    if not viewer.is_member:
        raise forbidden("Guests can only look around. Sign in as a member to do that.")
    return viewer


def require_admin(viewer: Viewer = Depends(current_viewer)) -> Viewer:
    if not viewer.is_admin:
        raise forbidden("Only admins can do that.")
    return viewer


# ---------------------------------------------------------------------------
# CSRF protection
#
# CSRF ("cross-site request forgery") is when another website you visit makes
# your browser send a request to this site. The browser would helpfully attach
# your sign-in cookie, so without protection that request would work.
#
# The fix: each sign-in has its own random CSRF token, which is written into
# every page. Every POST must send it back: HTMX sends it in an X-CSRF-Token
# header (set once on the page's <body>), plain forms in a hidden csrf_token
# field. Another website can make your browser send a request, but it can't
# read our pages, so it can't know the token.
# ---------------------------------------------------------------------------


async def checked_viewer(request: Request, viewer: Viewer = Depends(current_viewer)) -> Viewer:
    """A signed-in viewer whose request carries the right CSRF token (else 403)."""
    token = request.headers.get("X-CSRF-Token")
    if token is None:
        form = await request.form()  # FastAPI reuses this for the route's Form(...) fields
        token = form.get("csrf_token")
    if not secret_matches(str(token) if token is not None else None, viewer.csrf_token):
        raise forbidden("Missing or invalid CSRF token. Reload the page and try again.")
    return viewer


def checked_member(viewer: Viewer = Depends(checked_viewer)) -> Viewer:
    return require_member(viewer)


def checked_admin(viewer: Viewer = Depends(checked_viewer)) -> Viewer:
    return require_admin(viewer)


# ---------------------------------------------------------------------------
# Rate limiting for login and registration
#
# After 10 failures from one address within 15 minutes, further attempts from
# that address are refused until the window has passed. The counts are kept
# in memory, which is enough because Render's free service runs one copy of
# the site. (They reset when the site restarts.)
# ---------------------------------------------------------------------------

MAX_FAILURES = 10
FAILURE_WINDOW_SECONDS = 15 * 60
_failures: dict[str, deque] = defaultdict(deque)


def client_ip(request: Request) -> str:
    """The visitor's address. On Render, the site sits behind Render's proxy,
    which puts the visitor's real address first in X-Forwarded-For."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded.strip():
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _recent_failures(ip: str) -> deque:
    failures = _failures[ip]
    cutoff = time.monotonic() - FAILURE_WINDOW_SECONDS
    while failures and failures[0] < cutoff:
        failures.popleft()
    return failures


def too_many_failures(request: Request) -> bool:
    return len(_recent_failures(client_ip(request))) >= MAX_FAILURES


def record_failure(request: Request) -> None:
    _recent_failures(client_ip(request)).append(time.monotonic())


def reset_rate_limits() -> None:
    """Forget every count (used by the tests)."""
    _failures.clear()


# ---------------------------------------------------------------------------
# Registration keys
# ---------------------------------------------------------------------------


def registration_key(role: str) -> str:
    """The registration key for "admin" or "student" ("" when unset: registration off)."""
    name = "ADMIN_REGISTRATION_KEY" if role == "admin" else "STUDENT_REGISTRATION_KEY"
    return (os.getenv(name) or "").strip()


def registration_open(role: str) -> bool:
    return bool(registration_key(role))


def registration_key_matches(role: str, given: str | None) -> bool:
    """True if `given` is that role's key. The two keys must differ, so a
    student can never register as an admin with the student key."""
    admin_key, student_key = registration_key("admin"), registration_key("student")
    if admin_key and admin_key == student_key:
        return False  # misconfigured: refuse both rather than guess
    return secret_matches((given or "").strip(), registration_key(role))


# ---------------------------------------------------------------------------
# Agent API keys
# ---------------------------------------------------------------------------

# auto_error=False lets us return our own 401 message when the header is missing.
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
MIN_API_KEY_LENGTH = 20


def generate_api_key() -> str:
    """A new random API key (64 hex characters)."""
    return secrets.token_hex(32)


def hash_api_key(api_key: str) -> str:
    """The fingerprint of an API key, used to look agents up by their key.

    SHA-256 is fine here (rather than a slow password hash) because API keys
    are long and random, so they can't be guessed from the fingerprint.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


class KeyEncryptionNotConfigured(Exception):
    """KEY_ENCRYPTION_SECRET is missing, so keys can't be stored or shown."""


def _fernet() -> Fernet:
    """The encrypter/decrypter for API keys, from KEY_ENCRYPTION_SECRET.

    The secret should be a Fernet key (see the README for the command that
    makes one). Any other long random text also works: it's turned into a
    Fernet key with SHA-256.
    """
    secret = (os.getenv("KEY_ENCRYPTION_SECRET") or "").strip()
    if not secret:
        raise KeyEncryptionNotConfigured("KEY_ENCRYPTION_SECRET is not set.")
    try:
        return Fernet(secret.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest()))


def encrypt_api_key(api_key: str) -> str:
    """Scramble a key so it can be stored in the database."""
    return _fernet().encrypt(api_key.encode("utf-8")).decode("ascii")


def decrypt_api_key(encrypted: str) -> str | None:
    """Unscramble a stored key. None if it can't be (e.g. the secret changed)."""
    try:
        return _fernet().decrypt(encrypted.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return None


def require_agent(
    api_key: str | None = Depends(api_key_header),
    db: Session = Depends(get_session),
) -> Agent:
    """Find the agent that owns this X-API-Key, or refuse with 401.

    Also records that the agent was just seen, which keeps it "online":
    any API call counts.
    """
    agent = None
    if api_key:  # an empty or missing key never matches anything
        # Looking up the key's fingerprint means the database compares hashes,
        # never the secret key itself.
        agent = db.exec(select(Agent).where(Agent.api_key_hash == hash_api_key(api_key))).first()
    if agent is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid API key")
    agent.last_seen_at = utc_now()
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent
