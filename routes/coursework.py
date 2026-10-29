"""Assignments and exams (posted by admins), submissions (by enrolled
students), the history pages, and file downloads.

The Assignments and Exams tabs work the same way; `kind` says which one
("assignment" or "exam"), and the address uses its plural ("assignments").
"""

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlmodel import Session, select

from db import StoredFile, Submission, Task, User, get_session, utc_now
from permissions import (
    can_delete_submission,
    can_download_file,
    can_edit_submission,
    can_manage_classes,
    can_see_submission,
    can_submit,
    student_is_enrolled,
)
from routes.common import (
    delete_submission,
    delete_task,
    get_course,
    kind_of,
    not_found,
    plural,
    student_label,
    visible_submissions,
    visible_tasks,
)
from security import Viewer, checked_admin, checked_member, current_viewer, forbidden, require_admin, require_member
from uploads import (
    COURSEWORK_FILE_LIMIT,
    UploadRefused,
    delete_stored_file,
    file_response,
    has_file,
    preview_response,
    save_upload,
)
from web import clean_line, clean_text, hx_redirect, jst_input_value, parse_jst_input, render

router = APIRouter()

MAX_TITLE = 200
MAX_DESCRIPTION = 5000
MAX_CONTENT_TEXT = 50_000
RECENT_TASKS_IN_DROPDOWN = 5


# ---------------------------------------------------------------------------
# History page: every assignment/exam with the submissions linked to it
# ---------------------------------------------------------------------------


@router.get("/classes/{course_id}/{kinds}")
def history_page(request: Request, course_id: int, kinds: str, viewer: Viewer = Depends(current_viewer),
                 db: Session = Depends(get_session)):
    course = get_course(db, viewer, course_id)
    kind = kind_of(kinds, course)
    tasks = visible_tasks(db, viewer, course, kind)
    submissions = visible_submissions(db, viewer, course, kind)
    students = {u.id: u for u in db.exec(select(User)).all()}
    entries = []
    for task in tasks:
        linked = [s for s in submissions if s.task_id == task.id]
        entries.append({"task": task, "submissions": linked})
    return render(request, db, viewer, "coursework_history.html", course=course, kind=kind, kinds=kinds,
                  entries=entries, students=students, labels={u.id: student_label(db, u) for u in students.values()},
                  files=_files_for(db, tasks, submissions), can_manage=can_manage_classes(viewer))


def _files_for(db: Session, tasks: list[Task], submissions: list[Submission]) -> dict[int, StoredFile]:
    ids = [item.file_id for item in [*tasks, *submissions] if item.file_id]
    if not ids:
        return {}
    return {f.id: f for f in db.exec(select(StoredFile).where(StoredFile.id.in_(ids))).all()}


# ---------------------------------------------------------------------------
# Assignments and exams (admins)
# ---------------------------------------------------------------------------


def task_form(request, db, viewer, course, kind, task=None, values=None, error=None, status_code=200):
    if values is None:
        values = {
            "title": task.title if task else "",
            "description": (task.description or "") if task else "",
            "due_at": jst_input_value(task.due_at) if task else "",
            "mode": "file" if (task and task.file_id) else "text",
            "content_text": (task.content_text or "") if task else "",
            "guest_visible": task.guest_visible if task else False,
        }
    current_file = db.get(StoredFile, task.file_id) if task and task.file_id else None
    return render(request, db, viewer, "partials/task_form.html", status_code=status_code, course=course,
                  kind=kind, kinds=plural(kind), task=task, values=values, error=error, current_file=current_file)


def read_task_form(title, description, due_at, mode, content_text, guest_visible):
    """Tidy and check the task form. Returns (values, due time in UTC, error or None)."""
    description, error = clean_text(description, MAX_DESCRIPTION)
    text, text_error = clean_text(content_text, MAX_CONTENT_TEXT)
    values = {"title": clean_line(title), "description": description or "", "due_at": due_at,
              "mode": "file" if mode == "file" else "text", "content_text": text or "",
              "guest_visible": guest_visible}
    due = parse_jst_input(due_at)
    error = error or text_error
    if error is None and not 1 <= len(values["title"]) <= MAX_TITLE:
        error = f"The title is required (at most {MAX_TITLE} characters)."
    if error is None and due is None:
        error = "Choose the due date and time (JST)."
    return values, due, error


@router.get("/classes/{course_id}/{kinds}/new")
def new_task_form(request: Request, course_id: int, kinds: str, viewer: Viewer = Depends(require_admin),
                  db: Session = Depends(get_session)):
    course = get_course(db, viewer, course_id)
    return task_form(request, db, viewer, course, kind_of(kinds, course))


@router.post("/classes/{course_id}/{kinds}")
def create_task(
    request: Request,
    course_id: int,
    kinds: str,
    title: str = Form(""),
    description: str = Form(""),
    due_at: str = Form(""),
    mode: str = Form("text"),
    content_text: str = Form(""),
    file: UploadFile | None = File(None),
    guest_visible: bool = Form(False),
    viewer: Viewer = Depends(checked_admin),
    db: Session = Depends(get_session),
):
    course = get_course(db, viewer, course_id)
    kind = kind_of(kinds, course)
    values, due, error = read_task_form(title, description, due_at, mode, content_text, guest_visible)
    if error is None and values["mode"] == "text" and not values["content_text"]:
        error = "Write the content, or switch to a file."
    if error is None and values["mode"] == "file" and not has_file(file):
        error = "Choose a file, or switch to text."
    task = Task(course_id=course.id, kind=kind, title=values["title"], description=values["description"] or None,
                due_at=due, guest_visible=guest_visible, created_by=viewer.id)
    if error is None:
        try:
            if values["mode"] == "file":
                task.file_id = save_upload(db, file, COURSEWORK_FILE_LIMIT, viewer.id).id
            else:
                task.content_text = values["content_text"]
        except UploadRefused as refusal:
            error = str(refusal)
    if error:
        db.rollback()
        return task_form(request, db, viewer, course, kind, values=values, error=error, status_code=400)
    db.add(task)
    db.commit()
    return hx_redirect(f"/classes/{course.id}?tab={kinds}")


def get_task(db: Session, viewer: Viewer, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise not_found("Not found")
    course = get_course(db, viewer, task.course_id)  # 404 if the class can't be seen
    kind_of(plural(task.kind), course)  # ...or if its Exams tab is switched off
    return task


@router.get("/tasks/{task_id}/edit")
def edit_task_form(request: Request, task_id: int, viewer: Viewer = Depends(require_admin),
                   db: Session = Depends(get_session)):
    task = get_task(db, viewer, task_id)
    return task_form(request, db, viewer, get_course(db, viewer, task.course_id), task.kind, task=task)


@router.post("/tasks/{task_id}/edit")
def edit_task(
    request: Request,
    task_id: int,
    title: str = Form(""),
    description: str = Form(""),
    due_at: str = Form(""),
    mode: str = Form("text"),
    content_text: str = Form(""),
    file: UploadFile | None = File(None),
    guest_visible: bool = Form(False),
    viewer: Viewer = Depends(checked_admin),
    db: Session = Depends(get_session),
):
    task = get_task(db, viewer, task_id)
    course = get_course(db, viewer, task.course_id)
    values, due, error = read_task_form(title, description, due_at, mode, content_text, guest_visible)
    new_file = has_file(file)
    if error is None and values["mode"] == "text" and not values["content_text"]:
        error = "Write the content, or switch to a file."
    if error is None and values["mode"] == "file" and not new_file and not task.file_id:
        error = "Choose a file, or switch to text."
    old_file_id = None
    if error is None:
        try:
            if values["mode"] == "file" and new_file:
                old_file_id, task.file_id = task.file_id, save_upload(db, file, COURSEWORK_FILE_LIMIT, viewer.id).id
                task.content_text = None
            elif values["mode"] == "text":
                old_file_id, task.file_id = task.file_id, None
                task.content_text = values["content_text"]
        except UploadRefused as refusal:
            error = str(refusal)
    if error:
        db.rollback()
        return task_form(request, db, viewer, course, task.kind, task=task, values=values, error=error,
                         status_code=400)
    task.title, task.description, task.due_at = values["title"], values["description"] or None, due
    task.guest_visible, task.edited_at = guest_visible, utc_now()
    db.add(task)
    db.flush()
    delete_stored_file(db, old_file_id)
    db.commit()
    return hx_redirect(f"/classes/{course.id}/{plural(task.kind)}")


@router.post("/tasks/{task_id}/delete")
def remove_task(task_id: int, viewer: Viewer = Depends(checked_admin),
                db: Session = Depends(get_session)):
    """Delete an assignment or exam. Submissions linked to it stay (unlinked)."""
    task = get_task(db, viewer, task_id)
    course_id, kinds = task.course_id, plural(task.kind)
    delete_task(db, task)
    db.commit()
    return hx_redirect(f"/classes/{course_id}/{kinds}")


# ---------------------------------------------------------------------------
# A student's submission history, and their own submissions
# ---------------------------------------------------------------------------


@router.get("/classes/{course_id}/{kinds}/students/{student_id}")
def student_history(request: Request, course_id: int, kinds: str, student_id: int,
                    viewer: Viewer = Depends(current_viewer), db: Session = Depends(get_session)):
    course = get_course(db, viewer, course_id)
    kind = kind_of(kinds, course)
    student = db.get(User, student_id)
    if student is None or student.role != "student":
        raise not_found("Student not found")
    submissions = visible_submissions(db, viewer, course, kind, student_id=student.id)
    if not submissions and not student_is_enrolled(db, student, course):
        raise not_found("Student not found")
    tasks = {t.id: t for t in db.exec(select(Task).where(Task.course_id == course.id)).all()}
    own = viewer.id == student.id and can_submit(db, viewer, course)
    return render(request, db, viewer, "student_history.html", course=course, kind=kind, kinds=kinds,
                  student=student, student_gen=student_label(db, student), submissions=submissions,
                  tasks=tasks, files=_files_for(db, [], submissions), own=own,
                  can_moderate=viewer.is_admin)


def task_choices(db: Session, viewer: Viewer, course, kind: str) -> tuple[list[Task], list[Task]]:
    """The dropdown's tasks: the 5 most recent, and the older ones ("Show older")."""
    tasks = visible_tasks(db, viewer, course, kind)
    return tasks[:RECENT_TASKS_IN_DROPDOWN], tasks[RECENT_TASKS_IN_DROPDOWN:]


def submission_form(request, db, viewer, course, kind, submission=None, values=None, error=None, status_code=200):
    if values is None:
        values = {
            "title": (submission.title or "") if submission else "",
            "description": (submission.description or "") if submission else "",
            "task_id": submission.task_id if submission else None,
            "mode": "text" if (submission and submission.content_text is not None) else "file",
            "content_text": (submission.content_text or "") if submission else "",
            "guest_visible": submission.guest_visible if submission else False,
        }
    recent, older = task_choices(db, viewer, course, kind)
    current_file = db.get(StoredFile, submission.file_id) if submission and submission.file_id else None
    return render(request, db, viewer, "partials/submission_form.html", status_code=status_code,
                  course=course, kind=kind, kinds=plural(kind), submission=submission, values=values, error=error,
                  recent=recent, older=older, show_older=any(t.id == values["task_id"] for t in older),
                  current_file=current_file)


def read_submission_form(db, course, kind, title, description, task_id, mode, content_text, guest_visible):
    """Tidy and check the submission form. Returns (values, error or None)."""
    description, error = clean_text(description, MAX_DESCRIPTION)
    text, text_error = clean_text(content_text, MAX_CONTENT_TEXT)
    linked = int(task_id) if task_id and task_id.isdigit() else None
    values = {"title": clean_line(title), "description": description or "", "task_id": linked,
              "mode": "text" if mode == "text" else "file", "content_text": text or "",
              "guest_visible": guest_visible}
    error = error or text_error
    if error is None and len(values["title"]) > MAX_TITLE:
        error = f"The title can be at most {MAX_TITLE} characters."
    if error is None and linked is not None:
        task = db.get(Task, linked)
        # Only an assignment/exam of this same class and tab can be linked.
        if task is None or task.course_id != course.id or task.kind != kind:
            error = f"Choose an {kind} of this class."
    return values, error


def own_course(db: Session, viewer: Viewer, course_id: int):
    """The class, if this viewer is a student enrolled in it (else 403)."""
    course = get_course(db, viewer, course_id)
    if not can_submit(db, viewer, course):
        raise forbidden("Only students enrolled in this class can submit here.")
    return course


@router.get("/classes/{course_id}/{kinds}/submissions/new")
def new_submission_form(request: Request, course_id: int, kinds: str, viewer: Viewer = Depends(require_member),
                        db: Session = Depends(get_session)):
    course = own_course(db, viewer, course_id)
    return submission_form(request, db, viewer, course, kind_of(kinds, course))


@router.post("/classes/{course_id}/{kinds}/submissions")
def create_submission(
    request: Request,
    course_id: int,
    kinds: str,
    title: str = Form(""),
    description: str = Form(""),
    task_id: str = Form(""),
    mode: str = Form("file"),
    content_text: str = Form(""),
    file: UploadFile | None = File(None),
    guest_visible: bool = Form(False),
    viewer: Viewer = Depends(checked_member),
    db: Session = Depends(get_session),
):
    course = own_course(db, viewer, course_id)
    kind = kind_of(kinds, course)
    values, error = read_submission_form(db, course, kind, title, description, task_id, mode, content_text,
                                         guest_visible)
    if error is None and values["mode"] == "text" and not values["content_text"]:
        error = "Write the content, or switch to a file."
    if error is None and values["mode"] == "file" and not has_file(file):
        error = "Choose a file, or switch to text."
    submission = Submission(course_id=course.id, kind=kind, student_id=viewer.id, title=values["title"] or None,
                            description=values["description"] or None, task_id=values["task_id"],
                            guest_visible=guest_visible)
    if error is None:
        try:
            if values["mode"] == "file":
                submission.file_id = save_upload(db, file, COURSEWORK_FILE_LIMIT, viewer.id).id
            else:
                submission.content_text = values["content_text"]
        except UploadRefused as refusal:
            error = str(refusal)
    if error:
        db.rollback()
        return submission_form(request, db, viewer, course, kind, values=values, error=error, status_code=400)
    db.add(submission)
    db.commit()
    return hx_redirect(f"/classes/{course.id}/{kinds}/students/{viewer.id}")


def get_submission(db: Session, viewer: Viewer, submission_id: int) -> Submission:
    submission = db.get(Submission, submission_id)
    if submission is None or not can_see_submission(viewer, submission):
        raise not_found("Submission not found")
    course = get_course(db, viewer, submission.course_id)
    kind_of(plural(submission.kind), course)  # 404 if its Exams tab is switched off
    return submission


@router.get("/submissions/{submission_id}/edit")
def edit_submission_form(request: Request, submission_id: int, viewer: Viewer = Depends(require_member),
                         db: Session = Depends(get_session)):
    submission = get_submission(db, viewer, submission_id)
    if not can_edit_submission(viewer, submission):
        raise forbidden("Only the student who made a submission can edit it.")
    course = get_course(db, viewer, submission.course_id)
    return submission_form(request, db, viewer, course, submission.kind, submission=submission)


@router.post("/submissions/{submission_id}/edit")
def edit_submission(
    request: Request,
    submission_id: int,
    title: str = Form(""),
    description: str = Form(""),
    task_id: str = Form(""),
    mode: str = Form("file"),
    content_text: str = Form(""),
    file: UploadFile | None = File(None),
    guest_visible: bool = Form(False),
    viewer: Viewer = Depends(checked_member),
    db: Session = Depends(get_session),
):
    submission = get_submission(db, viewer, submission_id)
    if not can_edit_submission(viewer, submission):
        raise forbidden("Only the student who made a submission can edit it.")
    course = get_course(db, viewer, submission.course_id)
    values, error = read_submission_form(db, course, submission.kind, title, description, task_id, mode,
                                         content_text, guest_visible)
    new_file = has_file(file)
    if error is None and values["mode"] == "text" and not values["content_text"]:
        error = "Write the content, or switch to a file."
    if error is None and values["mode"] == "file" and not new_file and not submission.file_id:
        error = "Choose a file, or switch to text."
    old_file_id = None
    if error is None:
        try:
            if values["mode"] == "file" and new_file:
                old_file_id = submission.file_id
                submission.file_id = save_upload(db, file, COURSEWORK_FILE_LIMIT, viewer.id).id
                submission.content_text = None
            elif values["mode"] == "text":
                old_file_id, submission.file_id = submission.file_id, None
                submission.content_text = values["content_text"]
        except UploadRefused as refusal:
            error = str(refusal)
    if error:
        db.rollback()
        return submission_form(request, db, viewer, course, submission.kind, submission=submission, values=values,
                               error=error, status_code=400)
    submission.title = values["title"] or None
    submission.description = values["description"] or None
    submission.task_id = values["task_id"]
    submission.guest_visible = guest_visible
    submission.edited_at = utc_now()
    db.add(submission)
    db.flush()
    delete_stored_file(db, old_file_id)
    db.commit()
    return hx_redirect(f"/classes/{course.id}/{plural(submission.kind)}/students/{submission.student_id}")


@router.post("/submissions/{submission_id}/delete")
def remove_submission(submission_id: int, viewer: Viewer = Depends(checked_member),
                      db: Session = Depends(get_session)):
    """Delete a submission and its file: its own student, or an admin (moderation)."""
    submission = get_submission(db, viewer, submission_id)
    if not can_delete_submission(viewer, submission):
        raise forbidden("Only the student who made a submission (or an admin) can delete it.")
    back = f"/classes/{submission.course_id}/{plural(submission.kind)}/students/{submission.student_id}"
    delete_submission(db, submission)
    db.commit()
    return hx_redirect(back)


# ---------------------------------------------------------------------------
# File downloads: every file goes through here, after a permission check
# ---------------------------------------------------------------------------


@router.get("/files/{file_id}/preview")
def preview_file(file_id: int, viewer: Viewer = Depends(current_viewer), db: Session = Depends(get_session)):
    """A PDF, HTML or text file shown inside the page (same permissions as downloading it)."""
    stored = db.get(StoredFile, file_id)
    if stored is None or not can_download_file(db, viewer, stored):
        raise not_found("File not found")
    return preview_response(stored)


@router.get("/files/{file_id}")
def download_file(file_id: int, viewer: Viewer = Depends(current_viewer),
                  db: Session = Depends(get_session)):
    stored = db.get(StoredFile, file_id)
    # "Not found" whether it doesn't exist or isn't for this viewer, so
    # nobody can tell hidden files from missing ones.
    if stored is None or not can_download_file(db, viewer, stored):
        raise not_found("File not found")
    return file_response(stored)
