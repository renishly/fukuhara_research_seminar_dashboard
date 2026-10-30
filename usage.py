"""What is taking up space: breakdowns of file storage and the database for
the admins' Members page, plus the pie charts that show them.

File storage is exact: every stored file has a size, and we know what it
belongs to (an assignment, a submission, an agent...).

The database is exact per table on Postgres (Supabase), which can measure
each table. Breakdowns by class, person or agent are *estimates* made by
adding up the length of the text stored for each, because a database can't
measure single rows. The page says so.
"""

import math
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlmodel import Session, func, select

from db import Agent, Attachment, Course, Event, InboxFile, Monitor, Output, StoredFile, Submission, Task, User, database_size
from permissions import course_name

# Pie slice colours, always used in this order (checked for colour-blind
# readers with the dataviz palette validator). Red, amber and green are left
# out: the site uses them for errors, "Due soon" and "online".
SLICE_COLOURS = ["#2f5bff", "#eb6834", "#1baf7a", "#4a3aa7", "#e87ba4", "#7a7f00"]
OTHER_COLOUR = "#8a94a6"  # the grey "Other" slice for everything smaller
MAX_TABLE_ROWS = 50
LARGEST_FILES = 20

# Friendly names for the database tables.
TABLE_NAMES = {
    "event": "Agent activity lines", "output": "Agent outputs", "submission": "Submissions (text)",
    "task": "Assignments and exams", "user": "Accounts", "loginsession": "Sign-ins (sessions)",
    "agent": "Agents", "command": "Agent commands", "monitor": "Agent monitors",
    "monitornotifysetting": "Notify bells (monitors)",
    "skill": "Agent skills", "skilltree": "Skill trees", "skillcategory": "Skill categories",
    "organizerrun": "Market organizer runs", "storedfile": "File records",
    "course": "Classes", "classvisit": "Class visits (notification marks)",
    "inboxfile": "Inbox records", "notifysetting": "Notify bells", "folder": "Folders",
    "agentfolder": "Folder tags", "gen": "Gens", "courseenrolledgen": "Enrolment (gens)",
    "courseenrolleduser": "Enrolment (students)", "coursehiddengen": "Hidden from (gens)",
    "coursehiddenuser": "Hidden from (students)",
}


@dataclass
class Row:
    label: str
    size: int
    detail: str = ""  # an extra column, e.g. the class of a file
    share: float = 0.0  # of the view's total, 0 to 100
    colour: str | None = None  # set for rows that have their own pie slice


@dataclass
class Slice:
    label: str
    size: int
    share: float
    colour: str
    path: str  # SVG path drawing this slice of the donut


@dataclass
class View:
    """One breakdown: a pie and a table."""

    key: str
    title: str
    rows: list[Row]
    note: str = ""  # e.g. "estimated from text size"
    pie: bool = True
    total: int = 0
    slices: list[Slice] = field(default_factory=list)
    hidden_rows: int = 0  # rows beyond MAX_TABLE_ROWS, not listed


# ---------------------------------------------------------------------------
# The pie (a donut): drawn here in Python as SVG, so no chart library is needed
# ---------------------------------------------------------------------------

CENTRE, OUTER, INNER = 100, 96, 58  # in the SVG's 200 x 200 box


def _point(radius: float, angle: float) -> tuple[float, float]:
    """A point on a circle; angle 0 is straight up, going clockwise."""
    return CENTRE + radius * math.sin(angle), CENTRE - radius * math.cos(angle)


def donut_path(start: float, end: float) -> str:
    """The SVG path of one ring segment from `start` to `end` (radians)."""
    if end - start >= 2 * math.pi - 1e-9:  # a whole ring: two halves (one arc can't be a full circle)
        return donut_path(0, math.pi) + " " + donut_path(math.pi, 2 * math.pi)
    large = 1 if end - start > math.pi else 0
    x1, y1 = _point(OUTER, start)
    x2, y2 = _point(OUTER, end)
    x3, y3 = _point(INNER, end)
    x4, y4 = _point(INNER, start)
    return (f"M {x1:.2f} {y1:.2f} A {OUTER} {OUTER} 0 {large} 1 {x2:.2f} {y2:.2f} "
            f"L {x3:.2f} {y3:.2f} A {INNER} {INNER} 0 {large} 0 {x4:.2f} {y4:.2f} Z")


def finish(view: View) -> View:
    """Sort a view's rows, work out shares, and give the biggest rows pie slices.

    The six biggest rows get their own colour; everything smaller shares one
    grey "Other" slice, so the pie never needs more than seven colours.
    """
    view.rows = sorted((row for row in view.rows if row.size > 0), key=lambda row: -row.size)
    view.total = sum(row.size for row in view.rows)
    if view.total:
        for row in view.rows:
            row.share = row.size * 100 / view.total
    if view.pie and view.total:
        pieces = [(row.label, row.size, SLICE_COLOURS[i]) for i, row in enumerate(view.rows[: len(SLICE_COLOURS)])]
        for row, (_, _, colour) in zip(view.rows, pieces):
            row.colour = colour
        rest = sum(row.size for row in view.rows[len(SLICE_COLOURS):])
        if rest:
            pieces.append((f"Other ({len(view.rows) - len(SLICE_COLOURS)})", rest, OTHER_COLOUR))
            for row in view.rows[len(SLICE_COLOURS):]:
                row.colour = OTHER_COLOUR
        angle = 0.0
        for label, size, colour in pieces:
            sweep = size / view.total * 2 * math.pi
            view.slices.append(Slice(label, size, size * 100 / view.total, colour, donut_path(angle, angle + sweep)))
            angle += sweep
    view.hidden_rows = max(0, len(view.rows) - MAX_TABLE_ROWS)
    view.rows = view.rows[:MAX_TABLE_ROWS]
    return view


def _add(totals: dict, key, size: int) -> None:
    totals[key] = totals.get(key, 0) + (size or 0)


# ---------------------------------------------------------------------------
# File storage (exact)
# ---------------------------------------------------------------------------


def file_breakdown(db: Session) -> list[View]:
    """File storage by type, class, person and agent, plus the largest files."""
    files = {f.id: f for f in db.exec(select(StoredFile)).all()}
    courses = {c.id: c for c in db.exec(select(Course)).all()}
    users = {u.id: u for u in db.exec(select(User)).all()}
    agents = {a.id: a for a in db.exec(select(Agent)).all()}

    # What each file belongs to: (type, class id, person id, agent id).
    owners: dict[int, tuple[str, int | None, int | None, int | None]] = {}
    tasks = {t.id: t for t in db.exec(select(Task)).all()}
    submissions = {s.id: s for s in db.exec(select(Submission)).all()}
    for item in db.exec(select(Attachment)).all():
        task, sub = tasks.get(item.task_id), submissions.get(item.submission_id)
        if task is not None and item.file_id in files:
            owners[item.file_id] = ("Assignment & exam files", task.course_id, files[item.file_id].uploaded_by, None)
        elif sub is not None:
            owners[item.file_id] = ("Submission files", sub.course_id, sub.student_id, None)
    for output in db.exec(select(Output).where(Output.file_id.is_not(None))).all():
        agent = agents.get(output.agent_id)
        owners[output.file_id] = ("Agent output files", agent and agent.course_id, agent and agent.owner_id, output.agent_id)
    for monitor in db.exec(select(Monitor).where(Monitor.file_id.is_not(None))).all():
        agent = agents.get(monitor.agent_id)
        owners[monitor.file_id] = ("Monitor files", agent and agent.course_id, agent and agent.owner_id, monitor.agent_id)
    for item in db.exec(select(InboxFile)).all():
        agent = agents.get(item.agent_id)
        owners[item.file_id] = ("Inbox files (waiting)", agent and agent.course_id, agent and agent.owner_id, item.agent_id)

    by_type, by_class, by_person, by_agent = {}, {}, {}, {}
    for file_id, stored in files.items():
        kind, course_id, person_id, agent_id = owners.get(file_id, ("Not attached to anything", None, None, None))
        _add(by_type, kind, stored.size)
        _add(by_class, course_id, stored.size)
        _add(by_person, person_id, stored.size)
        if agent_id is not None:
            _add(by_agent, agent_id, stored.size)

    largest = []
    for stored in sorted(files.values(), key=lambda f: -f.size)[:LARGEST_FILES]:
        kind, course_id, person_id, _ = owners.get(stored.id, ("Not attached to anything", None, None, None))
        where = f"{kind} · {class_label(courses, course_id)} · {person_label(users, person_id)}"
        largest.append(Row(stored.filename, stored.size, detail=where))

    return [
        finish(View("files-type", "By type", [Row(k, v) for k, v in by_type.items()])),
        finish(View("files-class", "By class", [Row(class_label(courses, k), v) for k, v in by_class.items()])),
        finish(View("files-person", "By person", [Row(person_label(users, k), v) for k, v in by_person.items()])),
        finish(View("files-agent", "By agent", [Row(agent_label(agents, users, k), v) for k, v in by_agent.items()],
                    note="Agent output files, monitor files and waiting Inbox files.")),
        finish(View("files-largest", "Largest files", largest, pie=False)),
    ]


def class_label(courses: dict, course_id) -> str:
    course = courses.get(course_id)
    return course_name(course) if course else "No class"


def person_label(users: dict, user_id) -> str:
    user = users.get(user_id)
    return user.full_name if user else "Nobody (deleted account)"


def agent_label(agents: dict, users: dict, agent_id) -> str:
    agent = agents.get(agent_id)
    if agent is None:
        return "Deleted agent"
    return f"{agent.name} ({person_label(users, agent.owner_id)})"


# ---------------------------------------------------------------------------
# The database (exact per table on Postgres; estimated by class/person/agent)
# ---------------------------------------------------------------------------


def table_sizes(db: Session) -> tuple[dict[str, int], bool]:
    """Each of the site's tables' size in bytes, and whether it's exact.

    Postgres measures each table with its indexes. SQLite can only if it was
    built with the "dbstat" feature; otherwise this returns ({}, False).
    """
    dialect = db.get_bind().dialect.name
    try:
        if dialect == "postgresql":
            rows = db.exec(text(
                "SELECT relname, pg_total_relation_size(relid) FROM pg_catalog.pg_statio_user_tables "
                "WHERE schemaname = 'public'"
            )).all()
            return {name: int(size) for name, size in rows}, True
        if dialect == "sqlite":
            rows = db.exec(text(
                "SELECT m.tbl_name, SUM(d.pgsize) FROM dbstat AS d JOIN sqlite_master AS m ON d.name = m.name "
                "GROUP BY m.tbl_name"
            )).all()
            return {name: int(size) for name, size in rows}, True
    except Exception:  # noqa: BLE001 - not available here: fall back to estimates
        db.rollback()
    return {}, False


def _length(column):
    return func.coalesce(func.length(column), 0)


def database_breakdown(db: Session) -> list[View]:
    """The database by table, and estimated by class, person and agent."""
    views = []
    sizes, exact = table_sizes(db)
    if exact:
        rows = [Row(TABLE_NAMES.get(name, name), size) for name, size in sizes.items()]
        whole = database_size(db)
        if whole is not None and whole > sum(sizes.values()):
            rows.append(Row("Supabase's own (sign-in system, file bookkeeping, settings)"
                            if db.get_bind().dialect.name == "postgresql" else "Free space and SQLite's own",
                            whole - sum(sizes.values())))
        views.append(finish(View("db-table", "By table", rows, note="Exact sizes, including indexes.")))

    courses = {c.id: c for c in db.exec(select(Course)).all()}
    users = {u.id: u for u in db.exec(select(User)).all()}
    agents = {a.id: a for a in db.exec(select(Agent)).all()}

    # Estimated: the text stored for each agent, submission and task.
    per_agent: dict[int, int] = {}
    for agent_id, size in db.exec(select(Output.agent_id, func.sum(_length(Output.value) + _length(Output.category)))
                                  .group_by(Output.agent_id)).all():
        _add(per_agent, agent_id, int(size or 0))
    for agent_id, size in db.exec(select(Event.agent_id, func.sum(_length(Event.message)))
                                  .group_by(Event.agent_id)).all():
        _add(per_agent, agent_id, int(size or 0))

    by_class, by_person = {}, {}
    for agent_id, size in per_agent.items():
        agent = agents.get(agent_id)
        _add(by_class, agent and agent.course_id, size)
        _add(by_person, agent and agent.owner_id, size)
    submission_text = _length(Submission.content_text) + _length(Submission.title) + _length(Submission.description)
    for course_id, student_id, size in db.exec(
        select(Submission.course_id, Submission.student_id, func.sum(submission_text))
        .group_by(Submission.course_id, Submission.student_id)
    ).all():
        _add(by_class, course_id, int(size or 0))
        _add(by_person, student_id, int(size or 0))
    task_text = _length(Task.content_text) + _length(Task.title) + _length(Task.description)
    for course_id, author_id, size in db.exec(
        select(Task.course_id, Task.created_by, func.sum(task_text)).group_by(Task.course_id, Task.created_by)
    ).all():
        _add(by_class, course_id, int(size or 0))
        _add(by_person, author_id, int(size or 0))

    note = "Estimated from the size of the text stored (outputs, activity, submissions, assignments)."
    views.append(finish(View("db-class", "By class", [Row(class_label(courses, k), v) for k, v in by_class.items()],
                             note=note)))
    views.append(finish(View("db-person", "By person", [Row(person_label(users, k), v) for k, v in by_person.items()],
                             note=note)))
    views.append(finish(View("db-agent", "By agent",
                             [Row(agent_label(agents, users, k), v) for k, v in per_agent.items()],
                             note="Estimated from the text of each agent's outputs and activity lines.")))
    return views
