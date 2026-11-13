"""Linked dashboards: the places outside the dashboard where a student's work
lives (their own site, a Claude artifact, a Signals profile, a repo, a
document), listed on their agent dashboard and on the agent they belong to.

Each link says who can open it (Public / Keio login / Owner only), as the
owner sets it. The dashboard checks each link when it's saved and then once a
day: the HTTP status, the Last-Modified header, and a
<meta name="zemi:updated" content="..."> tag. It never logs in to anything,
never copies the page, and never changes the access tag itself: a "Public"
link that answers with a login is only flagged for the owner. Two failed
checks in a row show the link as Broken; it stays listed until the owner
archives it.

The check fetches addresses that people type in, from the server, so it
only talks to the public internet:
  - http(s) on the usual ports (80, 443) only, no user:password@ in the address;
  - every address the host name resolves to must be a public one, and the
    address actually connected to is checked again (so a name that resolves
    differently the second time can't reach this server's own network);
  - at most MAX_REDIRECTS redirects, each checked the same way;
  - short timeouts, and at most MAX_READ bytes of the page are read;
  - no proxies or credentials from the environment.
"""

import ipaddress
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from sqlmodel import Session, or_, select
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

from db import ExternalLink, as_utc, utc_now
from web import templates

KINDS = [("site", "Site"), ("artifact", "Artifact"), ("market", "Market profile"), ("repo", "Repo"),
         ("document", "Document")]
ACCESS = [("public", "Public"), ("keio", "Keio login"), ("owner", "Owner only")]
MAX_LABEL = 120
MAX_LINKS = 30  # per student per class, archived ones included
CHECK_EVERY = timedelta(days=1)
RECHECK_AFTER = timedelta(minutes=1)  # the owner's "Check now" at most this often
CHECKS_PER_RUN = 40  # links checked per scheduled run (every few minutes)
BROKEN_AFTER = 2  # failed checks in a row

ALLOWED_PORTS = {80, 443}
MAX_REDIRECTS = 5
MAX_READ = 64 * 1024
TIMEOUT = (4, 8)  # seconds: to connect, to read
USER_AGENT = "SeminarDashboardLinkCheck/1.0"
LOGIN_WORDS = re.compile(r"login|log-in|signin|sign-in|sso|oauth|saml|shibboleth|/auth|idp\.|accounts\.google", re.I)
PASSWORD_FIELD = re.compile(rb"<input[^>]*type\s*=\s*[\"']?password", re.I)
META_UPDATED = re.compile(rb"<meta[^>]+name\s*=\s*[\"']zemi:updated[\"'][^>]*>", re.I)
META_CONTENT = re.compile(rb"content\s*=\s*[\"']([^\"']{1,40})[\"']", re.I)


class Blocked(OSError):
    """An address the check may not connect to."""


def _public(ip: str) -> bool:
    address = ipaddress.ip_address(ip.split("%")[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def _checked_socket(sock):
    peer = sock.getpeername()[0]
    if not _public(peer):
        sock.close()
        raise Blocked(f"not a public address ({peer})")
    return sock


class _GuardedHTTP(HTTPConnection):
    def _new_conn(self):
        return _checked_socket(super()._new_conn())


class _GuardedHTTPS(HTTPSConnection):
    def _new_conn(self):
        return _checked_socket(super()._new_conn())


class _GuardedHTTPPool(HTTPConnectionPool):
    ConnectionCls = _GuardedHTTP


class _GuardedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _GuardedHTTPS


class _GuardedAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": _GuardedHTTPPool, "https": _GuardedHTTPSPool}


def _session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False  # no proxies or .netrc credentials from the environment
    session.mount("http://", _GuardedAdapter(max_retries=0))
    session.mount("https://", _GuardedAdapter(max_retries=0))
    session.headers["User-Agent"] = USER_AGENT
    return session


def allowed(url: str) -> str | None:
    """Why the check may not fetch this address (None if it may)."""
    try:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return "not a valid address"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "only http(s) addresses are checked"
    if parts.username or parts.password:
        return "addresses with a user name or password aren't checked"
    if port not in ALLOWED_PORTS:
        return f"port {port} isn't checked (only 80 and 443)"
    try:
        found = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return "the host name doesn't exist"
    if not found or not all(_public(info[4][0]) for info in found):
        return "not a public address"
    return None


@dataclass
class Result:
    result: str  # "ok", "login" or "failed"
    note: str
    changed_at: datetime | None = None


def _changed_at(response: requests.Response, body: bytes) -> datetime | None:
    meta = META_UPDATED.search(body)
    if meta and (content := META_CONTENT.search(meta.group(0))):
        try:
            moment = datetime.fromisoformat(content.group(1).decode("ascii", "ignore").strip())
            return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    header = response.headers.get("Last-Modified")
    if header:
        try:
            return parsedate_to_datetime(header)
        except (TypeError, ValueError, IndexError):
            pass
    return None


def fetch(url: str) -> Result:
    """Check one address (see the module's notes on what is allowed)."""
    session = _session()
    try:
        for _ in range(MAX_REDIRECTS + 1):
            if reason := allowed(url):
                return Result("failed", reason)
            try:
                response = session.get(url, allow_redirects=False, stream=True, timeout=TIMEOUT)
            except requests.RequestException as error:
                if any(isinstance(arg, Blocked) for arg in _causes(error)):
                    return Result("failed", "not a public address")
                return Result("failed", "couldn't connect" if isinstance(error, requests.ConnectionError)
                              else "no answer in time" if isinstance(error, requests.Timeout) else "couldn't fetch it")
            with response:
                if response.is_redirect and response.headers.get("Location"):
                    target = urljoin(url, response.headers["Location"])
                    if LOGIN_WORDS.search(target):
                        return Result("login", "answers with a login page")
                    url = target
                    continue
                if response.status_code in (401, 403):
                    return Result("login", f"asks for a login ({response.status_code})")
                if response.status_code >= 400:
                    return Result("failed", f"HTTP {response.status_code}")
                body = b""
                if "html" in response.headers.get("Content-Type", ""):
                    for chunk in response.iter_content(8192):
                        body += chunk
                        if len(body) >= MAX_READ:
                            break
                if PASSWORD_FIELD.search(body):
                    return Result("login", "answers with a login page", _changed_at(response, body))
                return Result("ok", f"reachable ({response.status_code})", _changed_at(response, body))
        return Result("failed", "too many redirects")
    finally:
        session.close()


def _causes(error: BaseException):
    """The error and everything inside it (requests wraps urllib3's errors)."""
    seen = set()
    stack = [error]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        stack += [arg for arg in getattr(current, "args", ()) if isinstance(arg, BaseException)]
        for inner in (current.__cause__, current.__context__, getattr(current, "reason", None)):
            if isinstance(inner, BaseException):
                stack.append(inner)


def check(db: Session, link: ExternalLink, now: datetime | None = None) -> ExternalLink:
    """Check a link now and save what was found."""
    found = fetch(link.url)
    link.result = found.result
    link.note = found.note[:200]
    link.failures = link.failures + 1 if found.result == "failed" else 0
    link.checked_at = now or utc_now()
    if found.changed_at:
        link.changed_at = found.changed_at
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


def check_due(db: Session, now: datetime | None = None) -> int:
    """The daily check: links not checked for a day (archived ones are left
    alone), a few at a time. Returns how many were checked."""
    now = now or utc_now()
    due = db.exec(select(ExternalLink).where(ExternalLink.archived_at.is_(None), or_(
        ExternalLink.checked_at.is_(None), ExternalLink.checked_at < now - CHECK_EVERY)
    ).order_by(ExternalLink.checked_at).limit(CHECKS_PER_RUN)).all()
    for link in due:
        try:
            check(db, link, now)
        except Exception as error:  # noqa: BLE001 - one bad link mustn't stop the rest
            db.rollback()
            print(f"Link check {link.id}: {error}")
    return len(due)


def status(link: ExternalLink) -> dict:
    """How to show a link's last check: {"key", "text", "warn"}.
    key: "unchecked", "ok", "login", "failing" (one failure) or "broken"."""
    if link.result is None:
        return {"key": "unchecked", "text": "Not checked yet", "warn": None}
    if link.failures >= BROKEN_AFTER:
        return {"key": "broken", "text": f"Broken: failed {link.failures} checks in a row ({link.note})", "warn": None}
    if link.result == "failed":
        return {"key": "failing", "text": f"Last check failed ({link.note})", "warn": None}
    if link.result == "login":
        warn = "Marked Public, but it asks for a login: change the access tag?" if link.access == "public" else None
        return {"key": "login", "text": "Reachable: login page", "warn": warn}
    return {"key": "ok", "text": "Reachable", "warn": None}


def links_for(db: Session, course_id: int, owner_id: int, agent_id: int | None = None) -> list[ExternalLink]:
    """A student's links in a class (or one agent's), live ones first, then archived; newest first."""
    query = select(ExternalLink).where(ExternalLink.course_id == course_id, ExternalLink.owner_id == owner_id)
    if agent_id is not None:
        query = query.where(ExternalLink.agent_id == agent_id)
    found = db.exec(query.order_by(ExternalLink.created_at.desc(), ExternalLink.id.desc())).all()
    return sorted(found, key=lambda link: link.archived_at is not None)


def recheck_allowed(link: ExternalLink, now: datetime | None = None) -> bool:
    return link.checked_at is None or (now or utc_now()) - as_utc(link.checked_at) >= RECHECK_AFTER


templates.env.globals["link_status"] = status
