"""Who has handed in what: Ngoc's submission grid and to-do list.

A submission counts for an assignment (or exam) when the student linked it to
that task on the submit form. For each student and task:

  on time   a linked submission made (or last edited) by the due time
  late      only linked submissions after the due time
  missing   nothing linked, and the due time has passed
  not due   nothing linked yet, and it isn't due yet
"""

from dataclasses import dataclass, field
from datetime import datetime

from sqlmodel import Session

from db import Course, Submission, Task, as_utc, utc_now
from routes.common import student_groups, visible_submissions, visible_tasks
from security import Viewer
from web import DUE_SOON, is_late

STATUS_WORDS = {"on_time": "On time", "late": "Late", "missing": "Missing", "pending": "Not due yet"}


def status_of(submissions: list[Submission], task: Task, now: datetime) -> str:
    """One student's status for one task, from their submissions linked to it."""
    if submissions:
        return "late" if all(is_late(s, task) for s in submissions) else "on_time"
    return "missing" if now > as_utc(task.due_at) else "pending"


@dataclass
class Grid:
    tasks: list[Task]
    rows: list[dict] = field(default_factory=list)  # {"student", "cells": [status...], "flagged": bool}
    totals: dict = field(default_factory=dict)  # status -> count


def submission_grid(db: Session, viewer: Viewer, course: Course, kind: str, now: datetime | None = None) -> Grid:
    """Every enrolled student x every task of this kind (oldest due first)."""
    now = now or utc_now()
    tasks = sorted(visible_tasks(db, viewer, course, kind), key=lambda t: as_utc(t.due_at))
    linked: dict[tuple[int, int], list[Submission]] = {}
    for submission in visible_submissions(db, viewer, course, kind):
        if submission.task_id:
            linked.setdefault((submission.student_id, submission.task_id), []).append(submission)
    grid = Grid(tasks=tasks, totals={key: 0 for key in STATUS_WORDS})
    for group in student_groups(db, viewer, course):
        for cell in group.cells:
            student = cell["student"]
            cells = [status_of(linked.get((student.id, task.id), []), task, now) for task in tasks]
            for status in cells:
                grid.totals[status] += 1
            grid.rows.append({"student": student, "cells": cells,
                              "flagged": any(status in ("late", "missing") for status in cells)})
    return grid


def todo_list(db: Session, viewer: Viewer, course: Course, kind: str, now: datetime | None = None) -> dict:
    """The viewer's own tasks of this kind, by deadline: overdue (not handed in),
    due soon (within 72 hours), upcoming, and done."""
    now = now or utc_now()
    mine: dict[int, list[Submission]] = {}
    for submission in visible_submissions(db, viewer, course, kind, student_id=viewer.id):
        if submission.task_id:
            mine.setdefault(submission.task_id, []).append(submission)
    sections = {"overdue": [], "soon": [], "upcoming": [], "done": []}
    for task in sorted(visible_tasks(db, viewer, course, kind), key=lambda t: as_utc(t.due_at)):
        status = status_of(mine.get(task.id, []), task, now)
        item = {"task": task, "status": status}
        if status in ("on_time", "late"):
            sections["done"].append(item)
        elif status == "missing":
            sections["overdue"].append(item)
        elif as_utc(task.due_at) - now <= DUE_SOON:
            sections["soon"].append(item)
        else:
            sections["upcoming"].append(item)
    sections["done"].reverse()  # most recent first
    return sections
