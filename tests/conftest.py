"""Shared test setup.

The tests use an in-memory database, a temporary folder for files, and test
values for every secret, so they never touch local.db, local_files/ or your
real .env file.
"""

import os
import sys
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

# These must be set BEFORE the app is imported, because db.py reads them then.
os.environ["SEMINAR_TESTING"] = "1"  # don't load the real .env
os.environ["DATABASE_URL"] = "sqlite://"  # in-memory, never local.db
os.environ["ADMIN_REGISTRATION_KEY"] = "test-admin-key-0123456789"
os.environ["STUDENT_REGISTRATION_KEY"] = "test-student-key-0123456789"
os.environ["KEY_ENCRYPTION_SECRET"] = Fernet.generate_key().decode()
os.environ["LOCAL_FILES_DIR"] = tempfile.mkdtemp(prefix="seminar-test-files-")
for name in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_BUCKET"):
    os.environ.pop(name, None)  # always the local (temporary) file folder

# Let the tests import main.py, db.py, ... from the folder above.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import Session, create_engine, select  # noqa: E402

from db import Agent, Gen, LoginSession, User, create_db_and_tables, get_session  # noqa: E402
from main import app  # noqa: E402
from security import SESSION_COOKIE, reset_rate_limits, token_hash  # noqa: E402

ADMIN_KEY = os.environ["ADMIN_REGISTRATION_KEY"]
STUDENT_KEY = os.environ["STUDENT_REGISTRATION_KEY"]
PASSWORD = "correct-horse-battery"


@pytest.fixture
def engine():
    # StaticPool makes every session share one connection; otherwise each
    # connection to "sqlite://" would get its own empty database.
    test_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    create_db_and_tables(test_engine)  # tables, and Gen 1-12
    return test_engine


@pytest.fixture
def session(engine):
    with Session(engine) as db:
        yield db


@pytest.fixture
def client(engine):
    """A browser that isn't signed in. Routes use the test database."""

    def test_session():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_session] = test_session
    reset_rate_limits()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class Person:
    """One signed-in browser (its own cookies) plus the account behind it."""

    def __init__(self, client: TestClient, user: User | None):
        self.client = client
        self.user = user

    @property
    def id(self):
        return self.user.id if self.user else None

    def get(self, url, **kwargs):
        return self.client.get(url, **kwargs)

    def post(self, url, **kwargs):
        return self.client.post(url, **kwargs)


def _gen_id(engine, number: int) -> int:
    with Session(engine) as db:
        return db.exec(select(Gen).where(Gen.number == number)).one().id


def _use_csrf(engine, browser: TestClient) -> None:
    """Send this sign-in's CSRF token with every request, like the pages do."""
    token = browser.cookies.get(SESSION_COOKIE)
    with Session(engine) as db:
        login = db.exec(select(LoginSession).where(LoginSession.token_hash == token_hash(token))).one()
    browser.headers["X-CSRF-Token"] = login.csrf_token


def register(browser: TestClient, role="student", login_id="taro", full_name="Yamada Taro", gen_id=None,
             key=None, password=PASSWORD, again=None):
    data = {"role": role, "login_id": login_id, "full_name": full_name, "password": password,
            "password_again": again or password,
            "registration_key": key if key is not None else (ADMIN_KEY if role == "admin" else STUDENT_KEY)}
    if gen_id is not None:
        data["gen_id"] = gen_id
    return browser.post("/register", data=data, follow_redirects=False)


@pytest.fixture
def make_person(client, engine):
    """make_person(role, login_id, full_name, gen=9) -> a signed-in Person.

    Each person has their own browser (cookies), so tests can act as several
    people at once.
    """
    browsers = []

    def make(role="student", login_id=None, full_name=None, gen=9):
        browser = TestClient(app)
        browsers.append(browser)
        if role == "guest":
            browser.post("/guest", follow_redirects=False)
            _use_csrf(engine, browser)
            return Person(browser, None)
        login_id = login_id or f"{role}{len(browsers)}"
        full_name = full_name or f"{role.title()} Person {len(browsers)}"
        response = register(browser, role, login_id, full_name, _gen_id(engine, gen) if role == "student" else None)
        assert response.status_code == 303, response.text
        _use_csrf(engine, browser)
        with Session(engine) as db:
            user = db.exec(select(User).where(User.login_id == login_id.lower())).one()
        return Person(browser, user)

    yield make
    for browser in browsers:
        browser.close()


@pytest.fixture
def admin(make_person):
    return make_person("admin", "admin", "Fukuhara Admin")


@pytest.fixture
def guest(make_person):
    return make_person("guest")


def class_form(engine, title="Research Methods", description="Line one\nLine two\nLine three",
               start=(4, 2026), end=(9, 2026), enrolled_gens=(), enrolled_users=(), hidden_gens=(),
               hidden_users=(), hidden_from_guests=False, has_exams=True, has_agents=True) -> dict:
    data = {"title": title, "description": description, "start_month": start[0], "start_year": start[1],
            "end_month": end[0], "end_year": end[1],
            "enrolled_gen": [_gen_id(engine, n) for n in enrolled_gens], "enrolled_user": list(enrolled_users),
            "hidden_gen": [_gen_id(engine, n) for n in hidden_gens], "hidden_user": list(hidden_users)}
    if hidden_from_guests:
        data["hidden_from_guests"] = "true"
    # Like the form: a ticked box sends "true", an unticked one sends nothing.
    if has_exams:
        data["has_exams"] = "true"
    if has_agents:
        data["has_agents"] = "true"
    return data


@pytest.fixture
def create_class(engine, admin):
    """create_class(**fields) -> the new class's id (created by the admin, through the form)."""

    def create(**fields):
        response = admin.post("/classes", data=class_form(engine, **fields))
        assert response.status_code == 200 and "HX-Redirect" in response.headers, response.text
        return int(response.headers["HX-Redirect"].rsplit("/", 1)[1])

    return create


def key_for(name: str) -> str:
    """A made-up API key for a test agent (at least 20 characters, no spaces)."""
    return f"test-key-for-{name}-0123456789".replace(" ", "-")


@pytest.fixture
def create_agent(engine):
    """create_agent(student, course_id, name) -> the agent's id (added on the Add Agent card)."""

    def create(student: Person, course_id: int, name="agent-1", api_key=None, description=""):
        response = student.post(f"/classes/{course_id}/agents",
                                data={"name": name, "api_key": api_key or key_for(name), "description": description})
        assert "Agent added" in response.text, response.text
        with Session(engine) as db:
            return db.exec(select(Agent).where(Agent.owner_id == student.id, Agent.course_id == course_id,
                                               Agent.name == name)).one().id

    return create


def api(name: str) -> dict:
    """The headers an agent sends: its own key."""
    return {"X-API-Key": key_for(name)}

