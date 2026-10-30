"""Lookups and clean-ups used by several route files.

- Finding a class (and checking the viewer may see it), a kind of task
  from the address ("assignments" -> "assignment").
- The student window: enrolled students grouped by gen.
- Deleting things together with everything that belongs to them (their
  files in storage too), used when deleting a class, an agent or an account.
"""

import math
from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlmodel import Session, delete, select

from db import (
    Agent,
    AgentFolder,
    Attachment,
    ClassVisit,
    Command,
    Course,
    CourseEnrolledGen,
    CourseEnrolledUser,
    CourseHiddenGen,
    CourseHiddenUser,
    Event,
    FeedFilter,
    Gen,
    InboxFile,
    Monitor,
    MonitorNotifySetting,
    NotifySetting,
    Output,
    Skill,
    SkillTree,
    StoredFile,
    Submission,
    Task,
    User,
)
from permissions import (
    KINDS,
    can_see_submission,
    can_see_task,
    can_view_course,
    enrolled_students,
    kind_switched_on,
)
from security import Viewer
from uploads import delete_stored_file


def not_found(what: str = "Not found") -> HTTPException:
    return HTTPException(404, what)


def get_course(db: Session, viewer: Viewer, course_id: int) -> Course:
    """The class, if it exists and this viewer may see it (404 otherwise, so
    hidden classes look exactly like missing ones)."""
    course = db.get(Course, course_id)
    if course is None or not can_view_course(db, viewer, course):
        raise not_found("Class not found")
    return course


def kind_of(kinds: str, course: Course | None = None) -> str:
    """ "assignments" -> "assignment", "exams" -> "exam" (404 for anything else).

    With a class: also 404 for exams when that class has no Exams tab.
    """
    if kinds not in KINDS or (course is not None and not kind_switched_on(course, KINDS[kinds])):
        raise not_found()
    return KINDS[kinds]


def plural(kind: str) -> str:
    return kind + "s"


def gens_by_id(db: Session) -> dict[int, Gen]:
    return {gen.id: gen for gen in db.exec(select(Gen)).all()}


# ---------------------------------------------------------------------------
# Tasks and submissions as a viewer sees them
# ---------------------------------------------------------------------------


def visible_tasks(db: Session, viewer: Viewer, course: Course, kind: str) -> list[Task]:
    """A tab's assignments or exams the viewer may see, newest first."""
    tasks = db.exec(
        select(Task).where(Task.course_id == course.id, Task.kind == kind)
        .order_by(Task.created_at.desc(), Task.id.desc())
    ).all()
    return [task for task in tasks if can_see_task(viewer, task)]


def visible_submissions(db: Session, viewer: Viewer, course: Course, kind: str,
                        student_id: int | None = None) -> list[Submission]:
    """A tab's submissions the viewer may see (optionally one student's), newest first."""
    statement = select(Submission).where(Submission.course_id == course.id, Submission.kind == kind)
    if student_id is not None:
        statement = statement.where(Submission.student_id == student_id)
    submissions = db.exec(statement.order_by(Submission.created_at.desc(), Submission.id.desc())).all()
    return [s for s in submissions if can_see_submission(viewer, s)]


# ---------------------------------------------------------------------------
# The student window: one cell per enrolled student, grouped by gen
# ---------------------------------------------------------------------------


@dataclass
class GenGroup:
    gen: Gen | None
    cells: list[dict] = field(default_factory=list)  # each cell: {"student": User, ...}

    @property
    def columns(self) -> int:
        """Lay the cells out in a roughly square block."""
        return max(1, math.ceil(math.sqrt(len(self.cells))))


def student_groups(db: Session, viewer: Viewer, course: Course) -> list[GenGroup]:
    """Enrolled students, grouped by gen (newest gen first), each group sorted
    by full name. (For guests, by placeholder number, so the order of real
    names isn't revealed.)"""
    gens = gens_by_id(db)
    groups: dict[int | None, GenGroup] = {}
    for student in enrolled_students(db, course):
        groups.setdefault(student.gen_id, GenGroup(gen=gens.get(student.gen_id))).cells.append({"student": student})
    for group in groups.values():
        if viewer.is_guest:
            group.cells.sort(key=lambda cell: cell["student"].guest_number or 0)
        else:
            group.cells.sort(key=lambda cell: (cell["student"].full_name.casefold(), cell["student"].id))
    return sorted(groups.values(), key=lambda group: -(group.gen.number if group.gen else 0))


# ---------------------------------------------------------------------------
# Deleting things with everything that belongs to them. Callers commit.
# ---------------------------------------------------------------------------


def attachments_of(db: Session, item: Task | Submission) -> list[Attachment]:
    """An assignment's, exam's or submission's files (Attachment rows), in order."""
    column = Attachment.submission_id if isinstance(item, Submission) else Attachment.task_id
    return list(db.exec(select(Attachment).where(column == item.id).order_by(Attachment.position, Attachment.id)).all())


def attached_files(db: Session, tasks: list[Task], submissions: list[Submission]) -> dict[str, dict[int, list[StoredFile]]]:
    """The files of these tasks and submissions, in one lookup:
    {"tasks": {task id: [files]}, "submissions": {submission id: [files]}}."""
    found = {"tasks": {}, "submissions": {}}
    task_ids, submission_ids = [t.id for t in tasks], [s.id for s in submissions]
    if not task_ids and not submission_ids:
        return found
    rows = db.exec(
        select(Attachment, StoredFile).join(StoredFile, StoredFile.id == Attachment.file_id)
        .where(Attachment.task_id.in_(task_ids or [-1]) | Attachment.submission_id.in_(submission_ids or [-1]))
        .order_by(Attachment.position, Attachment.id)
    ).all()
    for attachment, stored in rows:
        if attachment.task_id is not None:
            found["tasks"].setdefault(attachment.task_id, []).append(stored)
        else:
            found["submissions"].setdefault(attachment.submission_id, []).append(stored)
    return found


def detach_files(db: Session, attachments: list[Attachment]) -> None:
    """Remove these Attachment rows with their files (in storage too)."""
    for attachment in attachments:
        file_id = attachment.file_id
        db.delete(attachment)
        db.flush()
        delete_stored_file(db, file_id)


def delete_submission(db: Session, submission: Submission) -> None:
    detach_files(db, attachments_of(db, submission))
    file_id = submission.file_id  # legacy
    db.delete(submission)
    db.flush()
    delete_stored_file(db, file_id)


def delete_task(db: Session, task: Task) -> None:
    """Delete an assignment or exam. Its submissions stay; they just lose the link."""
    for submission in db.exec(select(Submission).where(Submission.task_id == task.id)).all():
        submission.task_id = None
        db.add(submission)
    detach_files(db, attachments_of(db, task))
    file_id = task.file_id  # legacy
    db.delete(task)
    db.flush()
    delete_stored_file(db, file_id)


def delete_agent(db: Session, agent: Agent) -> None:
    """Delete an agent with its activity, commands, outputs and monitors (and
    their files), folder tags, notify bells and Inbox files."""
    for output in db.exec(select(Output).where(Output.agent_id == agent.id)).all():
        file_id = output.file_id
        db.delete(output)
        db.flush()
        delete_stored_file(db, file_id)
    for monitor in db.exec(select(Monitor).where(Monitor.agent_id == agent.id)).all():
        file_id = monitor.file_id
        db.delete(monitor)
        db.flush()
        delete_stored_file(db, file_id)
    for item in db.exec(select(InboxFile).where(InboxFile.agent_id == agent.id)).all():
        file_id = item.file_id
        db.delete(item)
        db.flush()
        delete_stored_file(db, file_id)
    for model in (Event, Command, AgentFolder, NotifySetting, MonitorNotifySetting, FeedFilter, SkillTree):
        db.exec(delete(model).where(model.agent_id == agent.id))
    # Skills it shared stay on the market, still credited to it by name.
    for skill in db.exec(select(Skill).where(Skill.agent_id == agent.id)).all():
        skill.agent_id = None
        db.add(skill)
    db.delete(agent)


def delete_course(db: Session, course: Course) -> None:
    """Delete a class with all its assignments, exams, submissions, agents and files."""
    for submission in db.exec(select(Submission).where(Submission.course_id == course.id)).all():
        delete_submission(db, submission)
    for task in db.exec(select(Task).where(Task.course_id == course.id)).all():
        delete_task(db, task)
    for agent in db.exec(select(Agent).where(Agent.course_id == course.id)).all():
        delete_agent(db, agent)
    db.exec(delete(Skill).where(Skill.course_id == course.id))
    for model in (CourseEnrolledGen, CourseEnrolledUser, CourseHiddenGen, CourseHiddenUser, ClassVisit):
        db.exec(delete(model).where(model.course_id == course.id))
    db.delete(course)


def student_label(db: Session, user: User) -> str:
    """e.g. "Gen 9" for a student, "Admin" for an admin."""
    if user.role == "admin":
        return "Admin"
    gen = db.get(Gen, user.gen_id) if user.gen_id else None
    return f"Gen {gen.number}" if gen else ""
