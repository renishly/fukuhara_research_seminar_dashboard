"""One place answering "can this viewer see / do X in this class?".

Every route asks these functions on the server before showing or changing
anything. Hiding a button in a template is only a convenience: the real
protection is here.

The rules, in short:
  - Admins see every class and manage classes, assignments and exams.
  - Students see every class except those hidden from them (by gen or by
    name). A class they're enrolled in is always visible (enrolment wins).
  - Guests see every class not marked "Hidden from guests", and inside it
    only assignments, exams and submissions ticked "Visible to guests".
  - Only a submission's or agent's own student can change it.
"""

from datetime import date

from sqlmodel import Session, select

from db import (
    Agent,
    Attachment,
    Course,
    CourseEnrolledGen,
    CourseEnrolledUser,
    CourseHiddenGen,
    CourseHiddenUser,
    Gen,
    InboxFile,
    Monitor,
    Output,
    StoredFile,
    Submission,
    Task,
    User,
)
from security import Viewer

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
KINDS = {"assignments": "assignment", "exams": "exam"}  # address part -> Task/Submission kind


# ---------------------------------------------------------------------------
# Names and labels
# ---------------------------------------------------------------------------


def month_label(month: date) -> str:
    """e.g. "Apr 2026"."""
    return f"{MONTH_NAMES[month.month - 1]} {month.year}"


def timespan_label(course: Course) -> str:
    """e.g. "Apr 2026 – Sep 2026", or just "Apr 2026" for a one-month class."""
    if course.start_month == course.end_month:
        return month_label(course.start_month)
    return f"{month_label(course.start_month)} – {month_label(course.end_month)}"


def course_name(course: Course) -> str:
    """A class's display name: its title, or its timespan if it has none."""
    return (course.title or "").strip() or timespan_label(course)


def course_tabs(course: Course) -> list[str]:
    """The class page's tabs: Assignments always; Exams and Agents if switched on."""
    tabs = ["assignments"]
    if course.has_exams:
        tabs.append("exams")
    if course.has_agents:
        tabs.append("agents")
    return tabs


def kind_switched_on(course: Course, kind: str) -> bool:
    """Assignments always exist; exams only in classes with an Exams tab."""
    return kind == "assignment" or (kind == "exam" and course.has_exams)


def gen_label(gen: Gen | None) -> str:
    return f"Gen {gen.number}" if gen else ""


# ---------------------------------------------------------------------------
# Enrolment and visibility of classes
# ---------------------------------------------------------------------------


def _ids(db: Session, column, course_id: int) -> set[int]:
    """The gen or user ids in one of the four Course... link tables for a class."""
    table = column.class_
    return set(db.exec(select(column).where(table.course_id == course_id)).all())


def enrolled_gen_ids(db: Session, course: Course) -> set[int]:
    return _ids(db, CourseEnrolledGen.gen_id, course.id)


def enrolled_user_ids(db: Session, course: Course) -> set[int]:
    return _ids(db, CourseEnrolledUser.user_id, course.id)


def hidden_gen_ids(db: Session, course: Course) -> set[int]:
    return _ids(db, CourseHiddenGen.gen_id, course.id)


def hidden_user_ids(db: Session, course: Course) -> set[int]:
    return _ids(db, CourseHiddenUser.user_id, course.id)


def student_is_enrolled(db: Session, student: User, course: Course) -> bool:
    """Enrolled by gen or by name. Gen enrolment is "live": checked now, so a
    student who registers later into an enrolled gen is enrolled too."""
    if student.role != "student":
        return False
    return student.gen_id in enrolled_gen_ids(db, course) or student.id in enrolled_user_ids(db, course)


def is_enrolled(db: Session, viewer: Viewer, course: Course) -> bool:
    """Admins count as enrolled everywhere (but they get no student cell)."""
    if viewer.is_admin:
        return True
    return viewer.is_student and student_is_enrolled(db, viewer.user, course)


def can_view_course(db: Session, viewer: Viewer, course: Course) -> bool:
    if viewer.is_admin:
        return True
    if viewer.is_guest:
        return not course.hidden_from_guests
    if student_is_enrolled(db, viewer.user, course):
        return True  # enrolment wins over hiding
    hidden = viewer.user.gen_id in hidden_gen_ids(db, course) or viewer.user.id in hidden_user_ids(db, course)
    return not hidden


def visible_courses(db: Session, viewer: Viewer) -> list[Course]:
    courses = db.exec(select(Course).order_by(Course.start_month, Course.id)).all()
    return [course for course in courses if can_view_course(db, viewer, course)]


def enrolled_students(db: Session, course: Course) -> list[User]:
    """Every student enrolled in a class (by gen or by name)."""
    gen_ids, user_ids = enrolled_gen_ids(db, course), enrolled_user_ids(db, course)
    students = db.exec(select(User).where(User.role == "student")).all()
    return [s for s in students if s.gen_id in gen_ids or s.id in user_ids]


def can_manage_classes(viewer: Viewer) -> bool:
    """Create, edit and delete classes, assignments and exams."""
    return viewer.is_admin


def can_submit(db: Session, viewer: Viewer, course: Course) -> bool:
    """Enrolled students make submissions and agents in a class."""
    return viewer.is_student and student_is_enrolled(db, viewer.user, course)


# ---------------------------------------------------------------------------
# Assignments, exams and submissions
# ---------------------------------------------------------------------------


def can_see_task(viewer: Viewer, task: Task) -> bool:
    """Members see every task of a class they can view; guests only ticked ones."""
    return viewer.is_member or task.guest_visible


def can_see_submission(viewer: Viewer, submission: Submission) -> bool:
    """Members see every submission (with its content); guests only ticked ones."""
    return viewer.is_member or submission.guest_visible


def can_edit_submission(viewer: Viewer, submission: Submission) -> bool:
    """Only the student who made it, not even admins."""
    return viewer.is_student and viewer.id == submission.student_id


def can_delete_submission(viewer: Viewer, submission: Submission) -> bool:
    """Its student, or an admin (moderation)."""
    return can_edit_submission(viewer, submission) or viewer.is_admin


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


def owns_agent(viewer: Viewer, agent: Agent) -> bool:
    """Only the student who created an agent can change or control it."""
    return viewer.is_student and viewer.id == agent.owner_id


def can_view_agent_page(db: Session, viewer: Viewer, agent: Agent) -> bool:
    """Members who can view the class see agent pages (read-only unless theirs).
    Guests only see the agent grid, never agent pages."""
    course = db.get(Course, agent.course_id)
    return (viewer.is_member and course is not None and course.has_agents
            and can_view_course(db, viewer, course))


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def can_download_file(db: Session, viewer: Viewer, stored: StoredFile) -> bool:
    """May this viewer download this stored file? Decided by what the file belongs to."""

    def course_ok(course_id: int, kind: str) -> bool:
        # A switched-off Exams tab hides its files too (they come back if it's switched on again).
        course = db.get(Course, course_id)
        return course is not None and kind_switched_on(course, kind) and can_view_course(db, viewer, course)

    attachment = db.exec(select(Attachment).where(Attachment.file_id == stored.id)).first()
    if attachment is not None:
        task = db.get(Task, attachment.task_id) if attachment.task_id else None
        if task is not None:
            return course_ok(task.course_id, task.kind) and can_see_task(viewer, task)
        submission = db.get(Submission, attachment.submission_id) if attachment.submission_id else None
        if submission is not None:
            return course_ok(submission.course_id, submission.kind) and can_see_submission(viewer, submission)
        return False

    task = db.exec(select(Task).where(Task.file_id == stored.id)).first()
    if task is not None:
        return course_ok(task.course_id, task.kind) and can_see_task(viewer, task)

    submission = db.exec(select(Submission).where(Submission.file_id == stored.id)).first()
    if submission is not None:
        return course_ok(submission.course_id, submission.kind) and can_see_submission(viewer, submission)

    output = db.exec(select(Output).where(Output.file_id == stored.id)).first()
    if output is not None:
        agent = db.get(Agent, output.agent_id)
        return agent is not None and can_view_agent_page(db, viewer, agent)

    monitor = db.exec(select(Monitor).where(Monitor.file_id == stored.id)).first()
    if monitor is not None:
        agent = db.get(Agent, monitor.agent_id)
        return agent is not None and can_view_agent_page(db, viewer, agent)

    inbox = db.exec(select(InboxFile).where(InboxFile.file_id == stored.id)).first()
    if inbox is not None:
        agent = db.get(Agent, inbox.agent_id)
        return agent is not None and owns_agent(viewer, agent)

    return False  # not attached to anything: nobody
