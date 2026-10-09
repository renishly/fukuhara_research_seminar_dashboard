"""The classes timeline, creating/editing/deleting classes, and the class page.

The class page has three tabs: Assignments, Exams and Agents. The active tab
is in the address (?tab=assignments) so it can be linked and survives a
refresh; switching tabs swaps only the content below the tab buttons (HTMX).
"""

from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import or_
from sqlmodel import Session, delete, func, select

from db import (
    Agent,
    ClassVisit,
    Course,
    CourseEnrolledGen,
    CourseEnrolledUser,
    CourseHiddenGen,
    CourseHiddenUser,
    Gen,
    Submission,
    User,
    as_utc,
    get_session,
    utc_now,
)
from permissions import (
    MONTH_NAMES,
    can_manage_classes,
    can_submit,
    course_tabs,
    enrolled_gen_ids,
    enrolled_user_ids,
    hidden_gen_ids,
    hidden_user_ids,
    is_enrolled,
    visible_courses,
)
from routes.agent_tools import is_online
from routes.common import (
    delete_course,
    gens_by_id,
    get_course,
    kind_of,
    not_found,
    student_groups,
    visible_submissions,
    visible_tasks,
)
from security import Viewer, checked_admin, current_viewer, password_matches, require_admin
from timeline import AXIS_HEIGHT, BOX_HEIGHT, DEFAULT_ZOOM, ZOOM_LEVELS, build_timeline, month_number
from web import JST, clean_line, clean_text, form_error, hx_redirect, poll_reply, render, render_text, today_jst

router = APIRouter()

MAX_TITLE = 200
MAX_COURSE_DESCRIPTION = 5000
POLL_SECONDS = 15


# ---------------------------------------------------------------------------
# The timeline
# ---------------------------------------------------------------------------


def first_lines(text: str, count: int = 2) -> str:
    """The first lines of a description, for a timeline box."""
    return "\n".join(text.strip().splitlines()[:count])


def classes_with_new_submissions(db: Session, viewer: Viewer, course_ids: list[int]) -> set[int]:
    """The classes where someone else submitted something since this member
    last opened the class's page (or, if they never have, since they registered).

    Guests have no lasting identity, so they never get notification marks.
    """
    if not viewer.is_member or not course_ids:
        return set()
    # Exam submissions only count in classes whose Exams tab is switched on.
    newest = db.exec(
        select(Submission.course_id, func.max(Submission.created_at))
        .join(Course, Course.id == Submission.course_id)
        .where(Submission.course_id.in_(course_ids), Submission.student_id != viewer.id,
               or_(Submission.kind == "assignment", Course.has_exams))
        .group_by(Submission.course_id)
    ).all()
    visits = {visit.course_id: visit.seen_at
              for visit in db.exec(select(ClassVisit).where(ClassVisit.user_id == viewer.id)).all()}
    registered = viewer.user.created_at
    return {course_id for course_id, latest in newest
            if as_utc(latest) > as_utc(visits.get(course_id, registered))}


def mark_class_seen(db: Session, viewer: Viewer, course: Course) -> None:
    """Remember that this member has just opened the class (clears its mark)."""
    if not viewer.is_member:
        return
    visit = db.get(ClassVisit, (viewer.id, course.id)) or ClassVisit(user_id=viewer.id, course_id=course.id)
    visit.seen_at = utc_now()
    db.add(visit)
    db.commit()


def timeline_context(db: Session, viewer: Viewer, zoom: int, center: float | None) -> dict:
    """Everything the timeline needs: lane layout from timeline.py, plus each
    class's labels and badges for this viewer."""
    courses = visible_courses(db, viewer)
    today = today_jst()
    layout = build_timeline([(c.id, c.start_month, c.end_month) for c in courses], today, zoom, center)
    by_id = {course.id: course for course in courses}
    this_month = month_number(today)
    unseen = classes_with_new_submissions(db, viewer, list(by_id))
    boxes = [
        {
            "box": box,
            "course": by_id[box.key],
            "lines": first_lines(by_id[box.key].description),
            "enrolled": viewer.is_student and is_enrolled(db, viewer, by_id[box.key]),
            "ended": month_number(by_id[box.key].end_month) < this_month,
            "unseen": box.key in unseen,
        }
        for box in layout.boxes
    ]
    # e.g. "Thu 8 Oct 2026 08:31 JST" (the page keeps the time ticking).
    now = utc_now().astimezone(JST)
    now_label = f"{now:%a} {now.day} {now:%b %Y %H:%M} JST"
    return {"timeline": layout, "boxes": boxes, "zoom_levels": ZOOM_LEVELS, "today": today, "now_label": now_label,
            "axis_height": AXIS_HEIGHT, "box_height": BOX_HEIGHT}


@router.get("/classes")
def classes_page(request: Request, zoom: int = DEFAULT_ZOOM, viewer: Viewer = Depends(current_viewer),
                 db: Session = Depends(get_session)):
    """The first page after signing in: every class the viewer can see, on a timeline."""
    return render(request, db, viewer, "classes.html", **timeline_context(db, viewer, zoom, None))


@router.get("/classes/timeline")
def timeline_fragment(request: Request, zoom: int = DEFAULT_ZOOM, center: float | None = None,
                      viewer: Viewer = Depends(current_viewer), db: Session = Depends(get_session)):
    """The timeline again at another zoom level, keeping `center` (a month number) in the middle."""
    return render(request, db, viewer, "partials/timeline.html", oob=True,
                  **timeline_context(db, viewer, zoom, center))


# ---------------------------------------------------------------------------
# The class form (admins): create and edit
# ---------------------------------------------------------------------------


def add_months(month: date, count: int) -> date:
    number = month_number(month) + count
    return date(number // 12, number % 12 + 1, 1)


def class_form_context(db: Session, course: Course | None, values: dict | None = None,
                       error: str | None = None) -> dict:
    """Data for the class form: every gen and student to choose from, and what's chosen."""
    if values is None:
        if course is None:  # a new class: this month to five months later, nothing chosen
            start = today_jst().replace(day=1)
            values = {"title": "", "description": "", "start": start, "end": add_months(start, 5),
                      "enrolled_gens": set(), "enrolled_users": set(), "hidden_gens": set(),
                      "hidden_users": set(), "hidden_from_guests": False, "has_exams": True, "has_agents": True}
        else:
            values = {"title": course.title or "", "description": course.description,
                      "start": course.start_month, "end": course.end_month,
                      "enrolled_gens": enrolled_gen_ids(db, course), "enrolled_users": enrolled_user_ids(db, course),
                      "hidden_gens": hidden_gen_ids(db, course), "hidden_users": hidden_user_ids(db, course),
                      "hidden_from_guests": course.hidden_from_guests,
                      "has_exams": course.has_exams, "has_agents": course.has_agents}
    gens = list(db.exec(select(Gen).order_by(Gen.number.desc())).all())
    gen_numbers = {gen.id: gen.number for gen in gens}
    students = sorted(
        db.exec(select(User).where(User.role == "student")).all(),
        key=lambda s: (-gen_numbers.get(s.gen_id, 0), s.full_name.casefold()),
    )
    this_year = today_jst().year
    years = sorted({*range(this_year - 5, this_year + 6), values["start"].year, values["end"].year})
    return {"course": course, "values": values, "error": error, "gens": gens, "gen_numbers": gen_numbers,
            "students": students, "month_names": MONTH_NAMES, "years": years}


def read_month(month: int, year: int) -> date | None:
    if 1 <= month <= 12 and 2000 <= year <= 2100:
        return date(year, month, 1)
    return None


def save_links(db: Session, course: Course, values: dict) -> None:
    """Replace who is enrolled in, and hidden from, this class."""
    for model, column, key in (
        (CourseEnrolledGen, "gen_id", "enrolled_gens"),
        (CourseEnrolledUser, "user_id", "enrolled_users"),
        (CourseHiddenGen, "gen_id", "hidden_gens"),
        (CourseHiddenUser, "user_id", "hidden_users"),
    ):
        db.exec(delete(model).where(model.course_id == course.id))
        for value in values[key]:
            db.add(model(course_id=course.id, **{column: value}))


def read_class_form(db: Session, title, description, start_month, start_year, end_month, end_year,
                    enrolled_gen, enrolled_user, hidden_gen, hidden_user, hidden_from_guests,
                    has_exams, has_agents) -> tuple[dict, str | None]:
    """Tidy and check the class form. Returns (values, error or None)."""
    gen_ids = set(db.exec(select(Gen.id)).all())
    student_ids = set(db.exec(select(User.id).where(User.role == "student")).all())
    description, error = clean_text(description, MAX_COURSE_DESCRIPTION)
    values = {
        "title": clean_line(title), "description": description or "",
        "start": read_month(start_month, start_year), "end": read_month(end_month, end_year),
        "enrolled_gens": {g for g in enrolled_gen if g in gen_ids},
        "enrolled_users": {u for u in enrolled_user if u in student_ids},
        "hidden_gens": {g for g in hidden_gen if g in gen_ids},
        "hidden_users": {u for u in hidden_user if u in student_ids},
        "hidden_from_guests": hidden_from_guests,
        "has_exams": has_exams, "has_agents": has_agents,
    }
    if error is None and len(values["title"]) > MAX_TITLE:
        error = f"The title can be at most {MAX_TITLE} characters."
    if error is None and not description:
        error = "The description is required."
    if error is None and (values["start"] is None or values["end"] is None):
        error = "Choose the months the class runs."
    if error is None and values["end"] < values["start"]:
        error = "The class can't end before it starts."
    if values["start"] is None:
        values["start"] = today_jst().replace(day=1)
    if values["end"] is None:
        values["end"] = values["start"]
    return values, error


@router.get("/classes/new")
def new_class_form(request: Request, viewer: Viewer = Depends(require_admin), db: Session = Depends(get_session)):
    """The class form, opened in a pop-up from the timeline's "Create class" button."""
    return render(request, db, viewer, "partials/class_form.html", **class_form_context(db, None))


@router.post("/classes")
def create_class(
    request: Request,
    title: str = Form(""),
    description: str = Form(""),
    start_month: int = Form(0), start_year: int = Form(0),
    end_month: int = Form(0), end_year: int = Form(0),
    enrolled_gen: list[int] = Form([]), enrolled_user: list[int] = Form([]),
    hidden_gen: list[int] = Form([]), hidden_user: list[int] = Form([]),
    hidden_from_guests: bool = Form(False),
    has_exams: bool = Form(False), has_agents: bool = Form(False),
    viewer: Viewer = Depends(checked_admin),
    db: Session = Depends(get_session),
):
    values, error = read_class_form(db, title, description, start_month, start_year, end_month, end_year,
                                    enrolled_gen, enrolled_user, hidden_gen, hidden_user, hidden_from_guests,
                                    has_exams, has_agents)
    if error:
        return render(request, db, viewer, "partials/class_form.html", **class_form_context(db, None, values, error))
    course = Course(title=values["title"] or None, description=values["description"],
                    start_month=values["start"], end_month=values["end"],
                    hidden_from_guests=values["hidden_from_guests"], has_exams=values["has_exams"],
                    has_agents=values["has_agents"], created_by=viewer.id)
    db.add(course)
    db.flush()
    save_links(db, course, values)
    db.commit()
    return hx_redirect(f"/classes/{course.id}")


@router.get("/classes/{course_id}/edit")
def edit_class_form(request: Request, course_id: int, viewer: Viewer = Depends(require_admin),
                    db: Session = Depends(get_session)):
    course = get_course(db, viewer, course_id)
    return render(request, db, viewer, "partials/class_form.html", **class_form_context(db, course))


@router.post("/classes/{course_id}/edit")
def edit_class(
    request: Request,
    course_id: int,
    title: str = Form(""),
    description: str = Form(""),
    start_month: int = Form(0), start_year: int = Form(0),
    end_month: int = Form(0), end_year: int = Form(0),
    enrolled_gen: list[int] = Form([]), enrolled_user: list[int] = Form([]),
    hidden_gen: list[int] = Form([]), hidden_user: list[int] = Form([]),
    hidden_from_guests: bool = Form(False),
    has_exams: bool = Form(False), has_agents: bool = Form(False),
    viewer: Viewer = Depends(checked_admin),
    db: Session = Depends(get_session),
):
    course = get_course(db, viewer, course_id)
    values, error = read_class_form(db, title, description, start_month, start_year, end_month, end_year,
                                    enrolled_gen, enrolled_user, hidden_gen, hidden_user, hidden_from_guests,
                                    has_exams, has_agents)
    if error:
        return render(request, db, viewer, "partials/class_form.html", **class_form_context(db, course, values, error))
    course.title = values["title"] or None
    course.description = values["description"]
    course.start_month, course.end_month = values["start"], values["end"]
    course.hidden_from_guests = values["hidden_from_guests"]
    # Switching a tab off only hides it; its exams, submissions and agents are
    # kept and come back if it's switched on again.
    course.has_exams, course.has_agents = values["has_exams"], values["has_agents"]
    db.add(course)
    save_links(db, course, values)
    db.commit()
    return hx_redirect(f"/classes/{course.id}")


@router.post("/classes/{course_id}/delete")
def remove_class(course_id: int, password: str = Form(""), viewer: Viewer = Depends(checked_admin),
                 db: Session = Depends(get_session)):
    """Delete a class and everything in it. Needs the admin's own password."""
    course = get_course(db, viewer, course_id)
    if not password_matches(password, viewer.user.password_hash):
        return form_error("Your password is incorrect. Nothing was deleted.")
    delete_course(db, course)
    db.commit()
    return hx_redirect("/classes")


# ---------------------------------------------------------------------------
# The class page and its tabs
# ---------------------------------------------------------------------------


def enrolment_summary(db: Session, course: Course) -> str:
    """e.g. "Gen 9, Gen 10 + 2 students" (or "Nobody enrolled yet")."""
    gens = gens_by_id(db)
    gen_ids = enrolled_gen_ids(db, course)
    numbers = sorted(gens[g].number for g in gen_ids if g in gens)
    # Students enrolled by name who aren't already covered by an enrolled gen.
    extra = [u for u in enrolled_user_ids(db, course) if db.get(User, u) and db.get(User, u).gen_id not in gen_ids]
    parts = ", ".join(f"Gen {n}" for n in numbers)
    if extra:
        students = f"{len(extra)} student{'s' if len(extra) != 1 else ''}"
        parts = f"{parts} + {students}" if parts else students
    return parts or "Nobody enrolled yet"


def coursework_window(db: Session, viewer: Viewer, course: Course, kind: str) -> dict:
    """The bar (latest assignment/exam) and the student window (latest submission per student)."""
    tasks = visible_tasks(db, viewer, course, kind)
    latest: dict[int, Submission] = {}
    for submission in visible_submissions(db, viewer, course, kind):  # newest first
        latest.setdefault(submission.student_id, submission)
    groups = student_groups(db, viewer, course)
    for group in groups:
        for cell in group.cells:
            cell["latest"] = latest.get(cell["student"].id)
    return {"latest_task": tasks[0] if tasks else None, "groups": groups}


def agents_window(db: Session, viewer: Viewer, course: Course) -> dict:
    """The Agents tab: when each student's agents in this class last connected."""
    agents = db.exec(select(Agent).where(Agent.course_id == course.id)).all()
    groups = student_groups(db, viewer, course)
    for group in groups:
        for cell in group.cells:
            mine = [agent for agent in agents if agent.owner_id == cell["student"].id]
            seen = [agent.last_seen_at for agent in mine if agent.last_seen_at]
            cell["last_connected"] = max(seen) if seen else None
            cell["online"] = any(is_online(agent) for agent in mine)
    return {"groups": groups}


def tab_context(db: Session, viewer: Viewer, course: Course, tab: str) -> dict:
    """One tab's content (404 for a tab this class doesn't have)."""
    if tab not in course_tabs(course):
        raise not_found()
    context = {"course": course, "tab": tab, "tabs": course_tabs(course), "can_manage": can_manage_classes(viewer),
               "can_submit": can_submit(db, viewer, course), "poll_seconds": POLL_SECONDS}
    if tab == "agents":
        return context | agents_window(db, viewer, course)
    kind = kind_of(tab, course)
    return context | {"kind": kind} | coursework_window(db, viewer, course, kind)


@router.get("/classes/{course_id}")
def class_page(request: Request, course_id: int, tab: str = "assignments",
               viewer: Viewer = Depends(current_viewer), db: Session = Depends(get_session)):
    course = get_course(db, viewer, course_id)
    mark_class_seen(db, viewer, course)
    tab = tab if tab in course_tabs(course) else "assignments"
    return render(request, db, viewer, "class.html", summary=enrolment_summary(db, course),
                  ended=month_number(course.end_month) < month_number(today_jst()),
                  **tab_context(db, viewer, course, tab))


@router.get("/classes/{course_id}/tab/{tab}")
def tab_fragment(request: Request, course_id: int, tab: str, viewer: Viewer = Depends(current_viewer),
                 db: Session = Depends(get_session)):
    """Just one tab's content (the tab buttons swap it in with HTMX)."""
    course = get_course(db, viewer, course_id)
    if tab not in course_tabs(course):
        tab = "assignments"
    return render(request, db, viewer, "partials/tab.html", **tab_context(db, viewer, course, tab))


@router.get("/classes/{course_id}/bar/{kinds}")
def bar_fragment(course_id: int, kinds: str, seen: str | None = None,
                 viewer: Viewer = Depends(current_viewer), db: Session = Depends(get_session)):
    """The bar again, every 15 seconds (204 when nothing changed)."""
    course = get_course(db, viewer, course_id)
    kind_of(kinds, course)  # only Assignments and (if switched on) Exams have a bar
    content = render_text(db, viewer, "partials/bar.html", **tab_context(db, viewer, course, kinds))
    return poll_reply(content, seen, f"bar-seen-{kinds}")


@router.get("/classes/{course_id}/window/{tab}")
def window_fragment(course_id: int, tab: str, seen: str | None = None,
                    viewer: Viewer = Depends(current_viewer), db: Session = Depends(get_session)):
    """The student window again, every 15 seconds (204 when nothing changed)."""
    course = get_course(db, viewer, course_id)
    content = render_text(db, viewer, "partials/student_window.html", **tab_context(db, viewer, course, tab))
    return poll_reply(content, seen, f"window-seen-{tab}")

