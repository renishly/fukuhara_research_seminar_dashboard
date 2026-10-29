"""Accounts: gens, registration, login, sessions, CSRF, rate limits, account page."""

import html
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from conftest import PASSWORD, register
from db import Gen, LoginSession, User, add_missing_columns, normalize_database_url, utc_now
from main import app
from security import MAX_FAILURES, SESSION_COOKIE, hash_password, password_matches


def gen_id(session, number):
    return session.exec(select(Gen).where(Gen.number == number)).one().id


# --- Gens and the database ------------------------------------------------------


def test_gens_1_to_12_exist_before_anyone_registers(session):
    assert session.exec(select(User)).all() == []
    assert sorted(session.exec(select(Gen.number)).all()) == list(range(1, 13))


def test_register_page_lists_gens_newest_first(client):
    page = client.get("/register").text
    assert page.index("Gen 12") < page.index("Gen 9") < page.index("Gen 1<")


def test_database_url_scheme_is_converted():
    assert normalize_database_url("postgres://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
    assert normalize_database_url("postgresql://u:p@h/db?sslmode=require") == "postgresql+psycopg://u:p@h/db?sslmode=require"
    assert normalize_database_url("sqlite:///./local.db") == "sqlite:///./local.db"


def test_add_missing_columns_adds_new_fields_with_their_defaults(engine):
    # Simulate an older database: a table without the newer "guest_visible" column.
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE "oldtask" (id INTEGER PRIMARY KEY)')
        connection.exec_driver_sql('ALTER TABLE "task" RENAME TO "task_backup"')
        connection.exec_driver_sql(
            'CREATE TABLE "task" (id INTEGER PRIMARY KEY, course_id INTEGER, kind VARCHAR, title VARCHAR, '
            'description VARCHAR, due_at DATETIME, content_text VARCHAR, file_id INTEGER, created_by INTEGER, '
            'created_at DATETIME, edited_at DATETIME)')
        connection.exec_driver_sql(
            "INSERT INTO task (id, course_id, kind, title, due_at, created_at) "
            "VALUES (1, 1, 'assignment', 'Old', '2026-01-01', '2026-01-01')")
    added = add_missing_columns(engine)
    assert "task.guest_visible" in added
    with engine.begin() as connection:
        assert connection.exec_driver_sql("SELECT guest_visible FROM task WHERE id = 1").scalar() in (0, False)
    assert add_missing_columns(engine) == []  # nothing left to add


# --- Passwords --------------------------------------------------------------------


def test_passwords_are_stored_as_salted_scrypt_hashes():
    first, second = hash_password("same-password"), hash_password("same-password")
    assert first.startswith("scrypt$") and first != second  # random salt
    assert password_matches("same-password", first)
    assert not password_matches("other-password", first)


# --- Registration -------------------------------------------------------------------


def test_student_and_admin_register_with_their_own_keys(client, session):
    student = TestClient(app)
    assert register(student, "student", "Taro", "山田 太郎", gen_id(session, 9)).status_code == 303
    admin = TestClient(app)
    assert register(admin, "admin", "boss", "Fukuhara Sensei").status_code == 303
    users = {u.login_id: u for u in session.exec(select(User)).all()}
    assert users["taro"].role == "student" and users["taro"].full_name == "山田 太郎"
    assert users["boss"].role == "admin" and users["boss"].gen_id is None
    assert users["taro"].guest_number == 1


def test_wrong_or_missing_key_is_refused(client, session):
    assert register(client, "student", "a1", "A", gen_id(session, 9), key="wrong").status_code == 403
    assert register(client, "student", "a2", "A", gen_id(session, 9), key="").status_code == 403
    # The student key never registers an admin.
    assert register(client, "admin", "a3", "A", key="test-student-key-0123456789").status_code == 403
    assert session.exec(select(User)).all() == []


def test_unset_key_switches_registration_off(client, session, monkeypatch):
    monkeypatch.setenv("ADMIN_REGISTRATION_KEY", "")
    response = register(client, "admin", "boss", "Boss", key="")
    assert response.status_code == 403 and "switched off" in response.text
    assert session.exec(select(User)).all() == []


def test_identical_keys_are_refused(client, session, monkeypatch):
    monkeypatch.setenv("ADMIN_REGISTRATION_KEY", "the-same-key-for-both-roles")
    monkeypatch.setenv("STUDENT_REGISTRATION_KEY", "the-same-key-for-both-roles")
    assert register(client, "admin", "boss", "Boss", key="the-same-key-for-both-roles").status_code == 403


def test_student_must_choose_a_gen(client, session):
    response = register(client, "student", "taro", "Taro", gen_id=None)
    assert response.status_code == 400 and "Choose your gen" in response.text


def test_login_id_rules_and_duplicates_in_any_case(client, session):
    assert register(client, "student", "ab", "A", gen_id(session, 9)).status_code == 400  # too short
    assert register(client, "student", "bad id!", "A", gen_id(session, 9)).status_code == 400
    assert register(TestClient(app), "student", "Taro.Y", "A", gen_id(session, 9)).status_code == 303
    response = register(TestClient(app), "student", "TARO.y", "B", gen_id(session, 9))
    assert response.status_code == 400 and "already taken" in response.text


def test_password_rules(client, session):
    assert register(client, "student", "taro", "T", gen_id(session, 9), password="short").status_code == 400
    response = register(client, "student", "taro", "T", gen_id(session, 9), again="different-password")
    assert response.status_code == 400 and "don't match" in html.unescape(response.text)


# --- Login and sessions ---------------------------------------------------------------


def test_login_needs_no_key_and_uses_the_login_id(client, session, make_person):
    make_person("student", "Taro", "Yamada Taro")
    browser = TestClient(app)
    response = browser.post("/login", data={"login_id": "TARO", "password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/classes"
    assert SESSION_COOKIE in response.cookies
    page = browser.get("/classes")
    assert page.status_code == 200 and "Yamada Taro" in page.text


def test_login_failure_never_says_which_part_was_wrong(client, make_person):
    make_person("student", "taro")
    wrong_password = client.post("/login", data={"login_id": "taro", "password": "nope-nope-nope"})
    wrong_id = client.post("/login", data={"login_id": "nobody", "password": PASSWORD})
    for response in (wrong_password, wrong_id):
        assert response.status_code == 401
        assert "Login ID or password is incorrect" in response.text


def test_signed_out_browser_goes_to_login_and_comes_back(client):
    response = client.get("/classes/3?tab=exams", headers={"accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fclasses%2F3%3Ftab%3Dexams"
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_root_goes_to_classes_when_signed_in(make_person):
    guest = make_person("guest")
    assert guest.get("/", follow_redirects=False).headers["location"] == "/classes"


def test_pages_are_never_stored_by_browsers(make_person):
    student = make_person("student")
    assert student.get("/classes").headers["cache-control"] == "no-store"


def test_cookie_is_httponly_and_lax(client, make_person):
    make_person("student", "taro")
    response = client.post("/login", data={"login_id": "taro", "password": PASSWORD}, follow_redirects=False)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=604800" in cookie


def test_logout_ends_only_this_device(client, make_person):
    student = make_person("student", "taro")
    other = TestClient(app)
    other.post("/login", data={"login_id": "taro", "password": PASSWORD})
    student.post("/logout", follow_redirects=False)
    assert student.get("/classes", follow_redirects=False).status_code in (303, 401)
    assert other.get("/classes").status_code == 200


def test_sign_out_all_devices(client, make_person):
    student = make_person("student", "taro")
    other = TestClient(app)
    other.post("/login", data={"login_id": "taro", "password": PASSWORD})
    student.post("/account/signout-all", follow_redirects=False)
    assert other.get("/classes", follow_redirects=False).status_code in (303, 401)
    assert student.get("/classes", follow_redirects=False).status_code in (303, 401)


def test_expired_sign_in_no_longer_works(engine, make_person):
    student = make_person("student", "taro")
    with Session(engine) as db:
        for login in db.exec(select(LoginSession)).all():
            login.expires_at = utc_now() - timedelta(seconds=1)
            db.add(login)
        db.commit()
    assert student.get("/classes", follow_redirects=False).status_code in (303, 401)


def test_guest_visit_lasts_one_day(client):
    response = client.post("/guest", follow_redirects=False)
    assert "max-age=86400" in response.headers["set-cookie"].lower()


def test_password_change_signs_other_devices_out(client, make_person):
    student = make_person("student", "taro")
    other = TestClient(app)
    other.post("/login", data={"login_id": "taro", "password": PASSWORD})
    wrong = student.post("/account/password", data={"current_password": "wrong-one", "new_password": "a-new-password",
                                                    "new_password_again": "a-new-password"})
    assert wrong.status_code == 400 and "incorrect" in wrong.text
    response = student.post("/account/password", data={"current_password": PASSWORD, "new_password": "a-new-password",
                                                       "new_password_again": "a-new-password"})
    assert response.status_code == 200 and "Password changed" in response.text
    assert student.get("/classes").status_code == 200  # this device stays signed in
    assert other.get("/classes", follow_redirects=False).status_code in (303, 401)
    fresh = TestClient(app)
    assert fresh.post("/login", data={"login_id": "taro", "password": "a-new-password"},
                      follow_redirects=False).status_code == 303


def test_rate_limit_after_ten_failures(client, make_person):
    make_person("student", "taro")
    for _ in range(MAX_FAILURES):
        assert client.post("/login", data={"login_id": "taro", "password": "wrong-password"}).status_code == 401
    # Even the right password is refused now, from this address...
    refused = client.post("/login", data={"login_id": "taro", "password": PASSWORD})
    assert refused.status_code == 429 and "Too many attempts" in refused.text
    # ...and registration too.
    assert register(client, "admin", "boss", "Boss").status_code == 429
    # Another address (Render passes it in X-Forwarded-For) is unaffected.
    other = client.post("/login", data={"login_id": "taro", "password": PASSWORD},
                        headers={"X-Forwarded-For": "203.0.113.9"}, follow_redirects=False)
    assert other.status_code == 303


def test_wrong_registration_keys_count_towards_the_rate_limit(client, session):
    for _ in range(MAX_FAILURES):
        register(client, "student", "taro", "T", gen_id(session, 9), key="guess")
    assert register(client, "student", "taro", "T", gen_id(session, 9)).status_code == 429


# --- CSRF ---------------------------------------------------------------------------------


def test_post_without_csrf_token_gets_403(make_person):
    student = make_person("student", "taro")
    del student.client.headers["X-CSRF-Token"]
    response = student.post("/account/other-names", data={"other_names": "Taro"})
    assert response.status_code == 403
    wrong = student.post("/account/other-names", data={"other_names": "Taro", "csrf_token": "not-it"})
    assert wrong.status_code == 403
    assert student.post("/logout").status_code == 403


def test_csrf_token_in_a_plain_form_field_works(engine, make_person):
    student = make_person("student", "taro")
    token = student.client.headers.pop("X-CSRF-Token")
    response = student.post("/account/other-names", data={"other_names": "Taro-kun", "csrf_token": token})
    assert response.status_code == 200 and "Saved" in response.text


# --- Account page ----------------------------------------------------------------------------


def test_account_page_is_for_members_only(make_person):
    assert make_person("guest").get("/account").status_code == 403
    student = make_person("student", "taro", "Yamada Taro")
    page = student.get("/account").text
    assert "Yamada Taro" in page and "taro" in page


def test_other_names_are_limited_to_ten(make_person):
    student = make_person("student", "taro")
    response = student.post("/account/other-names", data={"other_names": "\n".join(f"name{i}" for i in range(11))})
    assert response.status_code == 400


# --- Friendly messages instead of "Internal Server Error" -------------------------------------


@pytest.fixture
def broken_database(monkeypatch):
    """Make every sign-in fail the way a full (read-only) Supabase database does."""
    import routes.auth
    from sqlalchemy.exc import InternalError

    def read_only(*args, **kwargs):
        raise InternalError("INSERT INTO loginsession ...", {}, Exception("cannot execute INSERT in a read-only transaction"))

    monkeypatch.setattr(routes.auth, "start_session", read_only)


def test_full_database_gives_a_friendly_page_and_toast(client, broken_database):
    browser = TestClient(app, raise_server_exceptions=False)
    page = browser.post("/guest", headers={"accept": "text/html"})
    assert page.status_code == 503 and "database is full" in page.text and "Internal Server Error" not in page.text
    toast = browser.post("/guest", headers={"HX-Request": "true"})
    assert toast.status_code == 503 and "database is full" in toast.text and toast.headers["HX-Retarget"] == "#toasts"


def test_unexpected_errors_get_a_plain_message(client, monkeypatch):
    import routes.auth

    def boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(routes.auth, "start_session", boom)
    page = TestClient(app, raise_server_exceptions=False).post("/guest")
    assert page.status_code == 500 and "Something went wrong" in page.text


def test_agent_api_errors_are_503_so_the_connector_retries(client, monkeypatch):
    import security
    from sqlalchemy.exc import OperationalError

    def read_only(*args, **kwargs):
        raise OperationalError("UPDATE agent ...", {}, Exception("read-only transaction"))

    monkeypatch.setattr(security, "hash_api_key", read_only)
    reply = TestClient(app, raise_server_exceptions=False).post("/api/events", json={"message": "x"},
                                                                 headers={"X-API-Key": "some-key-0123456789-abc"})
    assert reply.status_code == 503 and "database is full" in reply.json()["detail"]


# --- Supabase's "RLS disabled" warnings ----------------------------------------------------------


def test_row_level_security_is_switched_on_for_owned_tables_only():
    from contextlib import contextmanager
    from db import enable_row_level_security

    class FakeConnection:
        def __init__(self):
            self.sent = []

        def execute(self, statement):
            self.sent.append(str(statement))

            class Rows:
                def all(self_inner):
                    # (table, RLS already on?, owned by this connection?)
                    return [("user", False, True), ("course", True, True), ("agent", False, False),
                            ("not_ours", False, True)]
            return Rows()

    class FakeEngine:
        class dialect:
            name = "postgresql"

        def __init__(self):
            self.connection = FakeConnection()

        @contextmanager
        def begin(self):
            yield self.connection

    fake = FakeEngine()
    assert enable_row_level_security(fake) == ["user"]
    alters = [s for s in fake.connection.sent if s.startswith("ALTER")]
    # Only the site's own table that this connection owns and that hasn't got RLS yet.
    assert alters == ['ALTER TABLE public."user" ENABLE ROW LEVEL SECURITY']


def test_row_level_security_does_nothing_on_sqlite(engine):
    from db import enable_row_level_security

    assert enable_row_level_security(engine) == []


# --- A lighter site: compression and caching -------------------------------------------------------


def test_pages_and_stylesheets_are_compressed_and_cached(client, make_person):
    from web import static_url

    css = client.get(static_url("tailwind.css"), headers={"Accept-Encoding": "gzip"})
    assert css.headers["content-encoding"] == "gzip"
    assert css.headers["cache-control"] == "public, max-age=31536000, immutable"  # its address has a fingerprint
    page = make_person("student").get("/classes", headers={"Accept-Encoding": "gzip"})
    assert page.headers["content-encoding"] == "gzip" and page.headers["cache-control"] == "no-store"


def test_downloads_are_not_compressed_again(engine, make_person, create_class):
    from sqlmodel import select as pick
    from db import StoredFile
    from test_coursework import submit

    course = create_class(enrolled_gens=[9])
    student = make_person("student", "taro", gen=9)
    submit(student, course, file=("big.pdf", b"%PDF-1.4 " + b"x" * 5000, "application/pdf"))
    with Session(engine) as db:
        stored = db.exec(pick(StoredFile)).one()
    reply = student.get(f"/files/{stored.id}", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in reply.headers
