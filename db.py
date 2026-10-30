"""Database setup and table definitions.

Every table is a Python class below (SQLModel turns each into a database
table). Times are always stored in UTC; pages show them in Japan time (JST).

The main groups of tables:
  - People:     Gen, User, LoginSession
  - Classes:    Course (a "class"), the four Course...Gen/User link tables,
                Task (an assignment or exam) and Submission
  - Files:      StoredFile (the file itself lives in file storage, see storage.py)
  - Agents:     Agent, Event, Command, Output, Folder, AgentFolder,
                NotifySetting and InboxFile

Agents and the website never talk directly. An agent writes Events (its
activity) and Outputs through the API, and reads the Commands (Start, Pause,
messages) its owner sends from the website.
"""

import os
from datetime import date, datetime, timezone
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import JSON, Column, Text, UniqueConstraint, inspect, text
from sqlmodel import Field, Session, SQLModel, create_engine, select

# Load secrets from the .env file into environment variables. This lives here
# because db.py is the first of our modules to need them (DATABASE_URL below).
# The tests set SEMINAR_TESTING=1 so they never read your real .env file.
if os.getenv("SEMINAR_TESTING") != "1":
    load_dotenv()


def utc_now() -> datetime:
    """Current time in UTC. Used as the default for created_at columns."""
    return datetime.now(timezone.utc)


def as_utc(moment: datetime) -> datetime:
    """The same moment in UTC. SQLite forgets time zones, and we only ever
    store UTC, so a time without a zone is taken to be UTC already."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


class Gen(SQLModel, table=True):
    """A seminar generation ("Gen 9"). Every student belongs to one."""

    id: int | None = Field(default=None, primary_key=True)
    number: int = Field(unique=True, index=True)
    # The next guest placeholder number in this gen ("Gen 9 Student 7").
    # It only ever goes up, so a number is never given to a second student.
    next_guest_number: int = 1
    created_at: datetime = Field(default_factory=utc_now)


class User(SQLModel, table=True):
    """An admin or student account."""

    id: int | None = Field(default=None, primary_key=True)
    login_id: str = Field(unique=True, index=True)  # always stored lower-case
    full_name: str  # display name only (two people can share one)
    role: str  # "admin" or "student"
    gen_id: int | None = Field(default=None, foreign_key="gen.id")  # students only
    password_hash: str
    # When the password last changed (set at registration too).
    credentials_changed_at: datetime = Field(default_factory=utc_now)
    # Guests see "Gen 9 Student 7" instead of a student's name (see names.py).
    guest_number: int | None = None
    # Other ways this student's name is written (romaji, a nickname...), one
    # per line. Only used to hide names from guests; never shown to others.
    other_names: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class LoginSession(SQLModel, table=True):
    """One signed-in device (or one guest visit).

    The browser keeps a long random token in a cookie; only its fingerprint
    (SHA-256) is stored here, so a copied database can't be used to sign in.
    Deleting the row signs that device out straight away.
    """

    id: int | None = Field(default=None, primary_key=True)
    token_hash: str = Field(unique=True, index=True)
    user_id: int | None = Field(default=None, foreign_key="user.id", index=True)  # None for guests
    is_guest: bool = False
    expires_at: datetime
    # Every form and button on the site sends this back (see security.py).
    csrf_token: str
    created_at: datetime = Field(default_factory=utc_now)


# ---------------------------------------------------------------------------
# Classes and coursework
# ---------------------------------------------------------------------------


class Course(SQLModel, table=True):
    """A class of the seminar. (Named Course because `class` is a Python keyword.)"""

    id: int | None = Field(default=None, primary_key=True)
    title: str | None = None  # optional; see course_name() in permissions.py
    description: str
    start_month: date  # always the 1st day of the month
    end_month: date  # always the 1st day of the month, never before start_month
    hidden_from_guests: bool = False
    # Which tabs the class has besides Assignments (which every class has).
    # Classes from before these existed get True, i.e. all three tabs.
    has_exams: bool = True
    has_agents: bool = True
    created_by: int | None = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=utc_now)


# Who is enrolled in, or hidden from, a class: whole gens or single students.
# Each row says "this gen/student is enrolled in/hidden from this class".


class CourseEnrolledGen(SQLModel, table=True):
    course_id: int = Field(foreign_key="course.id", primary_key=True)
    gen_id: int = Field(foreign_key="gen.id", primary_key=True)


class CourseEnrolledUser(SQLModel, table=True):
    course_id: int = Field(foreign_key="course.id", primary_key=True)
    user_id: int = Field(foreign_key="user.id", primary_key=True)


class CourseHiddenGen(SQLModel, table=True):
    course_id: int = Field(foreign_key="course.id", primary_key=True)
    gen_id: int = Field(foreign_key="gen.id", primary_key=True)


class CourseHiddenUser(SQLModel, table=True):
    course_id: int = Field(foreign_key="course.id", primary_key=True)
    user_id: int = Field(foreign_key="user.id", primary_key=True)


class ClassVisit(SQLModel, table=True):
    """When a member last opened a class's page.

    Submissions made after this (by someone else) are "new" for that member,
    and the class gets a notification mark on the timeline until they open it.
    """

    user_id: int = Field(foreign_key="user.id", primary_key=True)
    course_id: int = Field(foreign_key="course.id", primary_key=True)
    seen_at: datetime = Field(default_factory=utc_now)


class StoredFile(SQLModel, table=True):
    """A file kept in file storage (Supabase Storage or ./local_files/).

    Only these details are in the database; the bytes are in file storage,
    because the free database is small and turns read-only when full.
    """

    id: int | None = Field(default=None, primary_key=True)
    storage_path: str = Field(unique=True)  # where storage.py keeps it
    filename: str  # the name it was uploaded with
    content_type: str
    size: int  # bytes
    uploaded_by: int | None = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=utc_now)


class Task(SQLModel, table=True):
    """An assignment or an exam, posted by an admin."""

    id: int | None = Field(default=None, primary_key=True)
    course_id: int = Field(foreign_key="course.id", index=True)
    kind: str  # "assignment" or "exam"
    title: str
    description: str | None = None
    due_at: datetime  # UTC (entered in JST on the form)
    # The content: text OR files (in Attachment), never both.
    content_text: str | None = None
    file_id: int | None = Field(default=None, foreign_key="storedfile.id")  # legacy: moved to Attachment
    # Unticked: guests don't see it at all.
    guest_visible: bool = False
    created_by: int | None = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=utc_now)
    edited_at: datetime | None = None


class Submission(SQLModel, table=True):
    """A student's submission in the Assignments or Exams tab of a class."""

    id: int | None = Field(default=None, primary_key=True)
    course_id: int = Field(foreign_key="course.id", index=True)
    kind: str  # "assignment" or "exam"
    student_id: int = Field(foreign_key="user.id", index=True)
    title: str | None = None
    description: str | None = None
    # Optional link to a Task of the same class and kind.
    task_id: int | None = Field(default=None, foreign_key="task.id")
    content_text: str | None = None  # text OR files (in Attachment), never both
    file_id: int | None = Field(default=None, foreign_key="storedfile.id")  # legacy: moved to Attachment
    guest_visible: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    edited_at: datetime | None = None


class Attachment(SQLModel, table=True):
    """One file of an assignment, exam (task_id) or submission (submission_id).
    Each can have several; position keeps them in the order they were added."""

    id: int | None = Field(default=None, primary_key=True)
    task_id: int | None = Field(default=None, foreign_key="task.id", index=True)
    submission_id: int | None = Field(default=None, foreign_key="submission.id", index=True)
    file_id: int = Field(foreign_key="storedfile.id", unique=True)
    position: int = 0


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


class Agent(SQLModel, table=True):
    """One AI agent of one student (its owner) in one class."""

    __table_args__ = (UniqueConstraint("owner_id", "course_id", "name"),)

    id: int | None = Field(default=None, primary_key=True)
    owner_id: int = Field(foreign_key="user.id", index=True)
    course_id: int = Field(foreign_key="course.id", index=True)
    name: str  # unique among this owner's agents in this class; can be renamed
    description: str | None = None
    # The agent proves who it is with its own API key, stored twice and never
    # as plain text: a fingerprint (SHA-256) to recognise it, and an encrypted
    # copy so "View key" can show it again (see security.py).
    api_key_hash: str = Field(unique=True, index=True)
    api_key_encrypted: str | None = None
    # How often the agent says it checks in (seconds). None = the default.
    check_in_seconds: int | None = None
    accepts_messages: bool = False  # set by the agent: accept_messages()
    accepts_inbox: bool = False  # set by the agent: accept_inbox()
    paused: bool = False  # set by the owner's Start/Pause buttons
    last_seen_at: datetime | None = None  # None = never connected
    # What's pinned to the grid card: one output category OR one monitor (by name).
    pinned_category: str | None = None
    pinned_monitor_name: str | None = None
    # Legacy: an agent used to have a single monitor, stored in these columns.
    # They are no longer used: at startup, move_legacy_monitors() moves any
    # such monitor into the Monitor table. (Removing columns needs a migration
    # tool such as Alembic, so they stay.)
    monitor_url: str | None = None
    monitor_file_id: int | None = Field(default=None, foreign_key="storedfile.id")
    monitor_filename: str | None = None
    monitor_label: str | None = None
    monitor_updated_at: datetime | None = None
    pinned_monitor: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class Event(SQLModel, table=True):
    """One line of an agent's Activity log."""

    id: int | None = Field(default=None, primary_key=True)
    agent_id: int = Field(foreign_key="agent.id", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    type: str = "log"  # "log", "status", "error", or automatic: "output", "monitor", "delete", "inbox"
    message: str
    # JSON needs an explicit column type so it works on both SQLite and Postgres.
    data: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))


class Command(SQLModel, table=True):
    """Something the owner sent to one agent: "start", "pause" or "message"."""

    id: int | None = Field(default=None, primary_key=True)
    agent_id: int = Field(foreign_key="agent.id", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    command: str
    payload: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    status: str = "pending"  # set to "done" by the agent once handled


class Output(SQLModel, table=True):
    """One result an agent chose to share with output(content, category)."""

    id: int | None = Field(default=None, primary_key=True)
    agent_id: int = Field(foreign_key="agent.id", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    category: str = Field(index=True)
    # What `value` holds: "text", "number", "link", "json" (a list or dict as
    # text), or "file" (value is then the file's name).
    kind: str
    value: str
    file_id: int | None = Field(default=None, foreign_key="storedfile.id")


class Monitor(SQLModel, table=True):
    """One of an agent's monitors: a named datapoint that is *replaced* each
    time the agent sends it (set_monitor(content, name)), unlike an output,
    which is a one-off item that adds to the list.

    Its content is text, a number, a link, a list/dict (as JSON text), a PDF
    (shown inside the page) or another file, just like an output's.
    """

    __table_args__ = (UniqueConstraint("agent_id", "name"),)

    id: int | None = Field(default=None, primary_key=True)
    agent_id: int = Field(foreign_key="agent.id", index=True)
    name: str  # 1 to 50 characters, unique per agent
    # "text", "number", "link", "json", "pdf" or "file" (value is then the file's name)
    kind: str
    value: str
    file_id: int | None = Field(default=None, foreign_key="storedfile.id")
    updated_at: datetime = Field(default_factory=utc_now)  # when the agent made this value
    created_at: datetime = Field(default_factory=utc_now)


class Folder(SQLModel, table=True):
    """A folder (tag) a student uses to group their own agents."""

    __table_args__ = (UniqueConstraint("owner_id", "name"),)

    id: int | None = Field(default=None, primary_key=True)
    owner_id: int = Field(foreign_key="user.id", index=True)
    name: str
    created_at: datetime = Field(default_factory=utc_now)


class AgentFolder(SQLModel, table=True):
    """Says "this agent is in this folder"."""

    agent_id: int = Field(foreign_key="agent.id", primary_key=True)
    folder_id: int = Field(foreign_key="folder.id", primary_key=True)


class NotifySetting(SQLModel, table=True):
    """A member's bell on an output category ("notify me").

    Nothing sends notifications yet; a future Discord bot will read this table.
    """

    user_id: int = Field(foreign_key="user.id", primary_key=True)
    agent_id: int = Field(foreign_key="agent.id", primary_key=True)
    category: str = Field(primary_key=True)


class MonitorNotifySetting(SQLModel, table=True):
    """A member's bell on one monitor ("notify me when it changes").

    Like NotifySetting for outputs: stored now, sent by a future Discord bot.
    """

    user_id: int = Field(foreign_key="user.id", primary_key=True)
    agent_id: int = Field(foreign_key="agent.id", primary_key=True)
    name: str = Field(primary_key=True)


class FeedFilter(SQLModel, table=True):
    """What a member last chose in an agent's Outputs or Monitors Filter, so
    the same choices are back the next time they open that agent's page."""

    user_id: int = Field(foreign_key="user.id", primary_key=True)
    agent_id: int = Field(foreign_key="agent.id", primary_key=True)
    section: str = Field(primary_key=True)  # "outputs" or "monitors"
    selected: list[str] = Field(default_factory=list, sa_column=Column(JSON))  # the ticked names
    show_all: bool = False
    sort: str = "date"
    order: str = "desc"
    updated_at: datetime = Field(default_factory=utc_now)


class InboxFile(SQLModel, table=True):
    """A file the owner uploaded for one agent, waiting until the agent takes it."""

    id: int | None = Field(default=None, primary_key=True)
    agent_id: int = Field(foreign_key="agent.id", index=True)
    file_id: int = Field(foreign_key="storedfile.id")
    note: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


# ---------------------------------------------------------------------------
# The Agent Skills market
# ---------------------------------------------------------------------------


class SkillCategory(SQLModel, table=True):
    """A category of skills by what they do (e.g. "Data retrieval"), with a
    description of what skills in it are expected to do. Written by the market
    organizer (see market.py), which updates them once or twice a day."""

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
    description: str = ""
    position: int = 0  # the order categories are listed in
    updated_at: datetime = Field(default_factory=utc_now)


class Skill(SQLModel, table=True):
    """A skill file (Markdown, e.g. "skills.md") shared on the market.

    Posted by a member directly, or by one of their agents through the API;
    either way it is credited to the member, and also to the agent if an
    agent posted it. Its identifier is "skill-<id>" (see market.py), and
    reading it always gives the latest version.
    """

    id: int | None = Field(default=None, primary_key=True)
    course_id: int = Field(foreign_key="course.id", index=True)  # the class it was posted in
    share: str = "class"  # "class": that class only; "seminar": every class's market
    owner_id: int = Field(foreign_key="user.id", index=True)  # the member it is credited to
    agent_id: int | None = Field(default=None, foreign_key="agent.id", index=True)  # set if an agent posted it
    agent_name: str | None = None  # kept for the credit line, even if the agent is renamed or deleted later
    name: str
    summary: str | None = None  # the author's short description
    content: str = Field(sa_column=Column(Text, nullable=False))
    version: int = 1  # goes up each time the content is replaced
    downloads: int = 0
    # Written by the market organizer:
    category_id: int | None = Field(default=None, foreign_key="skillcategory.id")
    specialty: str | None = None  # what sets this skill apart from others in its category
    organized_version: int | None = None  # which version the organizer last looked at
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SkillTree(SQLModel, table=True):
    """An agent's "skill tree": the big-picture description of where each of
    its skills (shared on the market or not) fits and is used in the agent.
    One per agent; uploading again replaces it."""

    id: int | None = Field(default=None, primary_key=True)
    agent_id: int = Field(foreign_key="agent.id", unique=True)
    owner_id: int = Field(foreign_key="user.id", index=True)
    content: str = Field(sa_column=Column(Text, nullable=False))
    version: int = 1
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class OrganizerRun(SQLModel, table=True):
    """One run of the market organizer, kept to schedule the next ones (at most
    two a day, one at 15:00 JST) and to show admins how the last one went."""

    id: int | None = Field(default=None, primary_key=True)
    jst_date: str = Field(index=True)  # e.g. "2026-10-30", the day in Japan
    trigger: str  # "3 pm", "morning", "manual" or "github"
    method: str  # "ai" (OpenRouter) or "rules" (built-in keyword rules)
    ok: bool = False
    message: str = ""
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None


# ---------------------------------------------------------------------------
# Connecting, creating tables, and the first gens
# ---------------------------------------------------------------------------


def normalize_database_url(url: str) -> str:
    """Make sure a Postgres URL uses the psycopg (version 3) driver.

    Supabase hands out URLs starting with "postgresql://" (some hosts use
    "postgres://"). SQLAlchemy needs "postgresql+psycopg://" to know which
    driver to use.
    """
    for old_prefix in ("postgres://", "postgresql://"):
        if url.startswith(old_prefix):
            return "postgresql+psycopg://" + url[len(old_prefix):]
    return url


def make_engine(url: str):
    """Create a SQLAlchemy engine for the given database URL."""
    if url.startswith("sqlite"):
        # SQLite normally refuses to be used from more than one thread;
        # FastAPI serves requests from several threads, so we allow it.
        return create_engine(url, connect_args={"check_same_thread": False})
    # pool_pre_ping checks a connection still works before using it, which
    # avoids errors after the database has been idle for a while.
    return create_engine(url, pool_pre_ping=True)


DATABASE_URL = normalize_database_url(os.getenv("DATABASE_URL") or "sqlite:///./local.db")
engine = make_engine(DATABASE_URL)

FIRST_GENS = range(1, 13)  # Gen 1 to Gen 12 exist from the very first start


def create_db_and_tables(target_engine=None) -> None:
    """Create missing tables, add missing columns, and make sure Gen 1-12 exist.

    This only *adds* things. Renaming, removing or changing a column needs a
    migration tool such as Alembic (see the README).
    """
    target_engine = target_engine or engine
    SQLModel.metadata.create_all(target_engine)
    add_missing_columns(target_engine)
    enable_row_level_security(target_engine)
    with Session(target_engine) as session:
        seed_gens(session)
        move_legacy_monitors(session)
        move_legacy_attachments(session)


def seed_gens(session: Session) -> None:
    """Add Gen 1 to Gen 12 if they're missing."""
    existing = set(session.exec(select(Gen.number)).all())
    for number in FIRST_GENS:
        if number not in existing:
            session.add(Gen(number=number))
    session.commit()


LEGACY_MONITOR_NAME = "Monitor"  # the name a single, older-style monitor gets


def move_legacy_monitors(session: Session) -> None:
    """Move monitors saved the old way (one per agent, in Agent's monitor_*
    columns) into the Monitor table, and empty those columns. Runs at every
    start, but only does anything once per agent."""
    agents = session.exec(select(Agent).where(
        (Agent.monitor_url.is_not(None)) | (Agent.monitor_file_id.is_not(None)) | (Agent.pinned_monitor == True)  # noqa: E712
    )).all()
    for agent in agents:
        if agent.monitor_url or agent.monitor_file_id:
            exists = session.exec(select(Monitor).where(
                Monitor.agent_id == agent.id, Monitor.name == LEGACY_MONITOR_NAME)).first()
            if exists is None:
                session.add(Monitor(
                    agent_id=agent.id, name=LEGACY_MONITOR_NAME,
                    kind="pdf" if agent.monitor_file_id else "link",
                    value=(agent.monitor_filename or "monitor.pdf") if agent.monitor_file_id else agent.monitor_url,
                    file_id=agent.monitor_file_id, updated_at=agent.monitor_updated_at or utc_now(),
                ))
        if agent.pinned_monitor:
            agent.pinned_monitor_name = LEGACY_MONITOR_NAME
        agent.monitor_url = agent.monitor_file_id = agent.monitor_filename = None
        agent.monitor_label = agent.monitor_updated_at = None
        agent.pinned_monitor = False
        session.add(agent)
    session.commit()


def move_legacy_attachments(session: Session) -> None:
    """Move the single file of assignments, exams and submissions saved the old
    way (in their file_id column) into the Attachment table. Runs at every
    start, but only does anything once per item."""
    for model, key in ((Task, "task_id"), (Submission, "submission_id")):
        for item in session.exec(select(model).where(model.file_id.is_not(None))).all():
            if session.exec(select(Attachment).where(Attachment.file_id == item.file_id)).first() is None:
                session.add(Attachment(file_id=item.file_id, **{key: item.id}))
            item.file_id = None
            session.add(item)
    session.commit()


def enable_row_level_security(target_engine=None) -> list[str]:
    """Switch on Postgres "row level security" (RLS) for every table of the site.

    Why: Supabase also offers every table in the "public" schema through its
    own automatic web API (PostgREST). With RLS on and no rules ("policies"),
    that API can read and change nothing, so the tables are only reachable
    through this site. The site itself is unaffected: it connects as the
    tables' owner, and Postgres doesn't apply RLS to a table's owner.

    Only tables this connection owns are changed (otherwise the site could
    lock itself out); others are reported in the log. SQLite has no RLS, so
    there this does nothing. Returns the names of the tables it switched on.
    """
    target_engine = target_engine or engine
    if target_engine.dialect.name != "postgresql":
        return []
    names = [table.name for table in SQLModel.metadata.sorted_tables]
    switched_on = []
    with target_engine.begin() as connection:
        rows = connection.execute(text(
            "SELECT c.relname, c.relrowsecurity, pg_get_userbyid(c.relowner) = current_user "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r'"
        )).all()
        for name, has_rls, owned in rows:
            if name not in names or has_rls:
                continue
            if not owned:
                print(f"Database: can't switch on row level security for {name}: this connection doesn't own it")
                continue
            connection.execute(text(f'ALTER TABLE public."{name}" ENABLE ROW LEVEL SECURITY'))
            switched_on.append(name)
    if switched_on:
        print(f"Database: switched on row level security for {len(switched_on)} tables")
    return switched_on


def _sql_literal(value) -> str | None:
    """A default value written as SQL, or None if it isn't a simple value."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def add_missing_columns(target_engine=None) -> list[str]:
    """Add columns that exist in the code above but not yet in the database.

    create_all() only creates whole tables, so when a new field is added to a
    table that already exists (e.g. on the live database), its column must be
    added separately. Each new column gets the field's default value, so
    existing rows stay valid. Returns the names of the columns it added.
    """
    target_engine = target_engine or engine
    inspector = inspect(target_engine)
    statements = []
    for mapper in SQLModel._sa_registry.mappers:
        model, table = mapper.class_, mapper.local_table
        if not inspector.has_table(table.name):
            continue
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            column_type = column.type.compile(dialect=target_engine.dialect)
            sql = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}'
            field = model.model_fields.get(column.name)
            default = _sql_literal(field.default) if field is not None else None
            if default is not None:
                sql += f" DEFAULT {default}"
                if not column.nullable:
                    sql += " NOT NULL"
            statements.append((f"{table.name}.{column.name}", sql))
    if statements:
        with target_engine.begin() as connection:
            for name, sql in statements:
                connection.execute(text(sql))
                print(f"Database: added the new column {name}")
    return [name for name, _ in statements]


DATABASE_LIMIT = 500 * 1024 * 1024  # Supabase's free plan: 500 MB, then the database turns read-only


def database_size(session: Session) -> int | None:
    """How many bytes the database takes up, or None if it can't be told."""
    try:
        if session.get_bind().dialect.name == "postgresql":
            return session.exec(text("SELECT pg_database_size(current_database())")).one()[0]
        if session.get_bind().dialect.name == "sqlite":
            pages = session.exec(text("PRAGMA page_count")).one()[0]
            page_size = session.exec(text("PRAGMA page_size")).one()[0]
            return pages * page_size
    except Exception:  # noqa: BLE001 - a size we can't read just isn't shown
        return None
    return None


def get_session():
    """FastAPI dependency: gives each request its own database session."""
    with Session(engine) as session:
        yield session
