"""Assignments, exams, submissions, the history pages, and file storage."""

import html
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlmodel import Session, select

import uploads
from conftest import PASSWORD
from db import StoredFile, Submission, Task
from web import JST, due_status, jst_input_value


def due_in(hours: float) -> str:
    """A due time `hours` from now, as typed on the form (JST)."""
    return jst_input_value(datetime.now(timezone.utc) + timedelta(hours=hours))


def post_task(person, course, kinds="assignments", title="Essay 1", hours=24 * 7, text="Write 1000 words.",
              file=None, guest_visible=False):
    data = {"title": title, "description": "About the reading", "due_at": due_in(hours),
            "mode": "file" if file else "text", "content_text": text if not file else ""}
    if guest_visible:
        data["guest_visible"] = "true"
    return person.post(f"/classes/{course}/{kinds}", data=data, files={"file": file} if file else None)


def task_id(engine, title):
    with Session(engine) as db:
        return db.exec(select(Task).where(Task.title == title)).one().id


def submit(person, course, kinds="assignments", title="My essay", task=None, text=None,
           file=("essay.txt", b"hello", "text/plain"), guest_visible=False):
    data = {"title": title, "description": "First draft", "task_id": str(task or ""),
            "mode": "text" if text is not None else "file", "content_text": text or ""}
    if guest_visible:
        data["guest_visible"] = "true"
    return person.post(f"/classes/{course}/{kinds}/submissions", data=data,
                       files={"file": file} if text is None else None)


def only(engine, model):
    with Session(engine) as db:
        return db.exec(select(model)).all()


@pytest.fixture
def course(create_class):
    return create_class(enrolled_gens=[9])


@pytest.fixture
def taro(make_person):
    return make_person("student", "taro", "Yamada Taro", gen=9)


# --- Assignments and exams ------------------------------------------------------------


def test_only_admins_post_edit_and_delete_tasks(engine, admin, taro, guest, course):
    assert post_task(taro, course).status_code == 403
    assert post_task(guest, course).status_code == 403
    assert taro.get(f"/classes/{course}/assignments/new").status_code == 403
    response = post_task(admin, course)
    assert response.headers["HX-Redirect"] == f"/classes/{course}?tab=assignments"
    task = task_id(engine, "Essay 1")
    edit = {"title": "Essay 1 (revised)", "due_at": due_in(48), "mode": "text", "content_text": "New"}
    assert taro.post(f"/tasks/{task}/edit", data=edit).status_code == 403
    assert taro.post(f"/tasks/{task}/delete").status_code == 403
    assert admin.post(f"/tasks/{task}/edit", data=edit).status_code == 200
    assert only(engine, Task)[0].title == "Essay 1 (revised)"
    assert admin.post(f"/tasks/{task}/delete").status_code == 200
    assert only(engine, Task) == []


def test_assignments_and_exams_are_kept_apart(engine, admin, course):
    post_task(admin, course, "exams", title="Midterm")
    assert "Midterm" in admin.get(f"/classes/{course}/exams").text
    assert "Midterm" not in admin.get(f"/classes/{course}/assignments").text
    assert admin.get(f"/classes/{course}/quizzes").status_code == 404


def test_task_needs_title_due_time_and_content(admin, course):
    assert "title is required" in post_task(admin, course, title=" ").text
    no_due = admin.post(f"/classes/{course}/assignments",
                        data={"title": "T", "due_at": "", "mode": "text", "content_text": "x"})
    assert "due date" in no_due.text
    assert "Write the content" in post_task(admin, course, text="").text
    no_file = admin.post(f"/classes/{course}/assignments",
                         data={"title": "T", "due_at": due_in(5), "mode": "file"})
    assert "Choose a file" in no_file.text


def test_task_with_a_file_keeps_exactly_one_kind_of_content(engine, admin, course):
    post_task(admin, course, file=("brief.pdf", b"%PDF-1.4 brief", "application/pdf"))
    task = only(engine, Task)[0]
    assert task.file_id and task.content_text is None
    # Switching to text deletes the old file.
    admin.post(f"/tasks/{task.id}/edit", data={"title": "Essay 1", "due_at": due_in(5), "mode": "text",
                                               "content_text": "Now text"})
    task = only(engine, Task)[0]
    assert task.file_id is None and task.content_text == "Now text"
    assert only(engine, StoredFile) == []


def test_due_indicator():
    now = datetime.now(timezone.utc)
    assert due_status(now + timedelta(hours=71), now) == "soon"
    assert due_status(now + timedelta(hours=73), now) is None
    assert due_status(now - timedelta(minutes=1), now) == "past"


def test_bar_shows_the_latest_task_with_its_indicator(admin, taro, course):
    assert "No assignments yet" in taro.get(f"/classes/{course}").text
    post_task(admin, course, title="Old one", hours=-5)
    bar = taro.get(f"/classes/{course}/bar/assignments").text
    assert "Old one" in bar and "Past due" in bar
    post_task(admin, course, title="New one", hours=10)
    bar = taro.get(f"/classes/{course}/bar/assignments").text
    assert "New one" in bar and "Due soon" in bar and "JST" in bar
    assert "Add assignment" in admin.get(f"/classes/{course}/bar/assignments").text
    assert "Add assignment" not in bar


def test_polling_answers_204_when_nothing_changed(taro, course):
    first = taro.get(f"/classes/{course}/window/assignments")
    seen = first.text.split('name="seen" value="')[1].split('"')[0]
    again = taro.get(f"/classes/{course}/window/assignments", params={"seen": seen})
    assert again.status_code == 204


# --- Submissions ----------------------------------------------------------------------------


def test_enrolled_student_submits_and_it_appears_everywhere(engine, admin, taro, course):
    post_task(admin, course)
    task = task_id(engine, "Essay 1")
    response = submit(taro, course, task=task)
    assert response.headers["HX-Redirect"] == f"/classes/{course}/assignments/students/{taro.id}"
    history = admin.get(f"/classes/{course}/assignments").text
    assert "My essay" in history and "essay.txt" in history and "Yamada Taro" in history
    cell = admin.get(f"/classes/{course}/window/assignments").text
    assert "My essay" in cell and "JST" in cell
    own = taro.get(f"/classes/{course}/assignments/students/{taro.id}").text
    assert "Create new submission" in own and "Edit" in own and "You" in taro.get(f"/classes/{course}").text


def test_only_enrolled_students_can_submit(admin, make_person, course):
    outsider = make_person("student", "outsider", gen=3)
    assert submit(outsider, course).status_code == 403
    assert submit(admin, course).status_code == 403
    assert submit(make_person("guest"), course).status_code == 403


def test_only_the_owner_edits_or_deletes_a_submission(engine, admin, taro, make_person, course):
    hanako = make_person("student", "hanako", "Sato Hanako", gen=9)
    submit(taro, course)
    sub = only(engine, Submission)[0]
    edit = {"title": "Changed", "mode": "text", "content_text": "text now"}
    assert hanako.post(f"/submissions/{sub.id}/edit", data=edit).status_code == 403
    assert admin.post(f"/submissions/{sub.id}/edit", data=edit).status_code == 403
    assert hanako.post(f"/submissions/{sub.id}/delete").status_code == 403
    assert taro.post(f"/submissions/{sub.id}/edit", data=edit).status_code == 200
    sub = only(engine, Submission)[0]
    assert sub.title == "Changed" and sub.content_text == "text now" and sub.file_id is None and sub.edited_at
    assert only(engine, StoredFile) == []  # the replaced file is gone
    assert taro.post(f"/submissions/{sub.id}/delete").status_code == 200
    assert only(engine, Submission) == []


def test_admins_can_delete_any_submission(engine, admin, taro, course):
    submit(taro, course)
    sub = only(engine, Submission)[0]
    assert admin.post(f"/submissions/{sub.id}/delete").status_code == 200
    assert only(engine, Submission) == []


def test_link_only_to_a_task_of_the_same_class_and_kind(engine, admin, taro, course, create_class):
    other_course = create_class(title="Other", enrolled_gens=[9])
    post_task(admin, course, "exams", title="Exam A")
    post_task(admin, other_course, title="Elsewhere")
    assert "Choose an assignment" in submit(taro, course, task=task_id(engine, "Exam A")).text
    assert "Choose an assignment" in submit(taro, course, task=task_id(engine, "Elsewhere")).text
    assert only(engine, Submission) == []
    assert submit(taro, course, "exams", task=task_id(engine, "Exam A")).status_code == 200


def test_submission_without_a_task_is_fine(engine, taro, course):
    assert submit(taro, course, task=None, text="Work set outside the dashboard").status_code == 200
    assert only(engine, Submission)[0].task_id is None


def test_submission_content_is_text_or_file(engine, taro, course):
    assert "Write the content" in submit(taro, course, text="").text
    no_file = taro.post(f"/classes/{course}/assignments/submissions", data={"mode": "file"})
    assert "Choose a file" in no_file.text
    submit(taro, course, text="Plain text")
    sub = only(engine, Submission)[0]
    assert sub.content_text == "Plain text" and sub.file_id is None


def test_late_badge(engine, admin, taro, course):
    post_task(admin, course, title="Gone", hours=-1)
    post_task(admin, course, title="Ahead", hours=48)
    submit(taro, course, title="Too late", task=task_id(engine, "Gone"))
    submit(taro, course, title="In time", task=task_id(engine, "Ahead"))
    history = admin.get(f"/classes/{course}/assignments").text
    gone = history.split("Too late")[1][:400]
    ahead = history.split("In time")[1][:400]
    assert "Late" in gone and "Late" not in ahead


def test_dropdown_lists_five_recent_tasks_then_older(engine, admin, taro, course):
    for number in range(7):
        post_task(admin, course, title=f"Task {number}")
    form = taro.get(f"/classes/{course}/assignments/submissions/new").text
    recent, older = form.split("Show older")
    assert all(f"Task {n}" in recent for n in (6, 5, 4, 3, 2))
    assert "Task 1" in older and "Task 0" in older and "Task 1" not in recent


# --- Files -----------------------------------------------------------------------------


def test_file_download_headers(engine, taro, course):
    submit(taro, course, file=("page.html", b"<script>alert(1)</script>", "text/html"))
    stored = only(engine, StoredFile)[0]
    response = taro.get(f"/files/{stored.id}")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.content == b"<script>alert(1)</script>"


def test_images_may_show_inline(engine, taro, course):
    submit(taro, course, file=("pic.png", b"\x89PNG fake", "image/png"))
    stored = only(engine, StoredFile)[0]
    response = taro.get(f"/files/{stored.id}")
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["content-type"] == "image/png"


def test_files_are_kept_in_file_storage_not_the_database(engine, taro, course):
    submit(taro, course, file=("data.bin", b"12345", "application/octet-stream"))
    stored = only(engine, StoredFile)[0]
    assert (Path(os.environ["LOCAL_FILES_DIR"]) / stored.storage_path).read_bytes() == b"12345"


def test_deleting_a_submission_deletes_its_file(engine, taro, course):
    submit(taro, course)
    stored = only(engine, StoredFile)[0]
    path = Path(os.environ["LOCAL_FILES_DIR"]) / stored.storage_path
    assert path.exists()
    taro.post(f"/submissions/{only(engine, Submission)[0].id}/delete")
    assert not path.exists() and only(engine, StoredFile) == []


def test_file_size_limit(engine, taro, course, monkeypatch):
    monkeypatch.setattr(uploads, "COURSEWORK_FILE_LIMIT", 10)
    import routes.coursework
    monkeypatch.setattr(routes.coursework, "COURSEWORK_FILE_LIMIT", 10)
    response = submit(taro, course, file=("big.txt", b"x" * 11, "text/plain"))
    assert "too big" in response.text and only(engine, StoredFile) == []


def test_uploads_stop_when_storage_is_nearly_full(engine, taro, course, monkeypatch):
    monkeypatch.setattr(uploads, "STORAGE_TOTAL_LIMIT", 3)
    response = submit(taro, course, file=("four.txt", b"1234", "text/plain"))
    assert "ask an admin" in html.unescape(response.text) and only(engine, StoredFile) == []


def test_file_permissions(engine, admin, taro, make_person, course, create_class):
    hidden_from = make_person("student", "hidden", gen=8)
    secret = create_class(title="Secret", enrolled_gens=[9], hidden_gens=[8])
    submit(taro, secret)
    stored = only(engine, StoredFile)[0]
    assert admin.get(f"/files/{stored.id}").status_code == 200
    assert hidden_from.get(f"/files/{stored.id}").status_code == 404
    assert make_person("guest").get(f"/files/{stored.id}").status_code == 404  # not ticked for guests
    assert admin.get("/files/9999").status_code == 404


def test_text_content_is_shown_as_plain_text(admin, taro, course):
    submit(taro, course, title="<b>bold</b>", text="<script>alert('x')</script>")
    page = admin.get(f"/classes/{course}/assignments/students/{taro.id}").text
    assert "<script>alert" not in page and "&lt;script&gt;" in page and "<b>bold</b>" not in page


def test_deleting_a_class_deletes_its_files(engine, admin, taro, course):
    submit(taro, course)
    path = Path(os.environ["LOCAL_FILES_DIR"]) / only(engine, StoredFile)[0].storage_path
    admin.post(f"/classes/{course}/delete", data={"password": PASSWORD})
    assert not path.exists() and only(engine, Submission) == [] and only(engine, StoredFile) == []


def test_deleting_a_task_keeps_its_submissions(engine, admin, taro, course):
    post_task(admin, course)
    submit(taro, course, task=task_id(engine, "Essay 1"))
    admin.post(f"/tasks/{task_id(engine, 'Essay 1')}/delete")
    assert only(engine, Submission)[0].task_id is None


def test_due_time_is_entered_and_shown_in_jst(engine, admin, course):
    admin.post(f"/classes/{course}/assignments", data={"title": "Timed", "due_at": "2026-12-01T09:30",
                                                        "mode": "text", "content_text": "x"})
    task = only(engine, Task)[0]
    assert task.due_at.replace(tzinfo=timezone.utc).astimezone(JST).strftime("%H:%M") == "09:30"
    assert "2026-12-01 09:30 JST" in admin.get(f"/classes/{course}/assignments").text


# --- The notification mark on the timeline ----------------------------------------------

NEW_MARK = 'class="tl-new"'  # the notification mark on a timeline box


def test_new_submission_marks_the_class_until_its_page_is_opened(admin, taro, make_person, course, create_class):
    hanako = make_person("student", "hanako", "Sato Hanako", gen=9)
    other = create_class(title="Quiet class", enrolled_gens=[9])
    for person in (admin, taro, hanako):
        person.get(f"/classes/{course}")  # everyone has seen the class so far
    assert NEW_MARK not in admin.get("/classes").text
    submit(taro, course)
    assert admin.get("/classes").text.count(NEW_MARK) == 1  # only this class, not the quiet one
    assert NEW_MARK in hanako.get("/classes").text  # other students too
    assert NEW_MARK not in taro.get("/classes").text  # never for your own submission
    admin.get(f"/classes/{course}?tab=exams")  # opening the class (any tab) clears it
    assert NEW_MARK not in admin.get("/classes").text
    assert NEW_MARK in hanako.get("/classes").text  # ...for that member only
    assert other


def test_new_members_only_see_marks_for_submissions_after_they_registered(admin, taro, make_person, course):
    submit(taro, course)
    late = make_person("student", "latecomer", gen=9)
    assert NEW_MARK not in late.get("/classes").text
    submit(taro, course, title="Second")
    assert NEW_MARK in late.get("/classes").text


def test_guests_get_no_notification_marks(taro, make_person, course):
    guest = make_person("guest")
    submit(taro, course, guest_visible=True)
    assert NEW_MARK not in guest.get("/classes").text


def test_supabase_keys_are_sent_the_way_each_kind_needs():
    import storage

    assert storage._headers("sb_secret_abc") == {"apikey": "sb_secret_abc"}  # new keys: apikey only
    legacy = storage._headers("eyJhbGciOiJIUzI1NiJ9.x.y")  # old service_role keys are JWTs
    assert legacy["Authorization"] == "Bearer eyJhbGciOiJIUzI1NiJ9.x.y" and legacy["apikey"].startswith("eyJ")


# --- Supabase Storage, against a stand-in server --------------------------------------------


@pytest.fixture
def fake_supabase(monkeypatch):
    """A tiny stand-in for Supabase Storage's REST API, recording every request."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    state = {"objects": {}, "requests": [], "bucket": "seminar-files", "key": "sb_secret_test"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body=b"", kind="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            state["requests"].append((self.command, self.path, dict(self.headers)))
            if self.headers.get("apikey") != state["key"] or "Authorization" in self.headers:
                self.reply(401, json.dumps({"message": "Invalid API key"}).encode())
                return False
            return True

        def do_GET(self):
            if not self.allowed():
                return
            if self.path == f"/storage/v1/bucket/{state['bucket']}":
                return self.reply(200, json.dumps({"name": state["bucket"], "public": False}).encode())
            if self.path.startswith("/storage/v1/bucket/"):
                return self.reply(404, json.dumps({"message": "Bucket not found"}).encode())
            data = state["objects"].get(self.path.removeprefix(f"/storage/v1/object/{state['bucket']}/"))
            if data is None:
                return self.reply(400, json.dumps({"message": "Object not found"}).encode())
            self.reply(200, data, "application/octet-stream")

        def do_POST(self):
            if not self.allowed():
                return
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state["objects"][self.path.removeprefix(f"/storage/v1/object/{state['bucket']}/")] = body
            self.reply(200, json.dumps({"Key": self.path}).encode())

        def do_DELETE(self):
            if not self.allowed():
                return
            for path in json.loads(self.rfile.read(int(self.headers["Content-Length"])))["prefixes"]:
                state["objects"].pop(path, None)
            self.reply(200, b"[]")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # People often paste the longer address; it should still work.
    monkeypatch.setenv("SUPABASE_URL", f"http://127.0.0.1:{server.server_port}/rest/v1/")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", state["key"])
    yield state
    server.shutdown()


def test_files_go_to_supabase_storage_and_back(engine, admin, taro, course, fake_supabase):
    import storage

    assert storage.check()["ok"] is True
    data = os.urandom(3 * 1024 * 1024)  # a realistic 3 MB file
    submit(taro, course, file=("essay.pdf", data, "application/pdf"))
    stored = only(engine, StoredFile)[0]
    assert fake_supabase["objects"][stored.storage_path] == data  # arrived whole, streamed
    upload = next(r for r in fake_supabase["requests"] if r[0] == "POST")
    assert upload[2]["Content-Type"] == "application/pdf" and upload[2]["x-upsert"] == "false"
    assert admin.get(f"/files/{stored.id}").content == data  # and comes back the same
    taro.post(f"/submissions/{only(engine, Submission)[0].id}/delete")
    assert fake_supabase["objects"] == {}  # deleted from storage too


def test_supabase_refusals_are_explained(engine, admin, taro, course, fake_supabase, monkeypatch):
    import storage

    monkeypatch.setenv("SUPABASE_BUCKET", "wrong-name")
    status = storage.check()
    assert status["ok"] is False and "Bucket not found" in status["message"] and "SUPABASE_BUCKET" in status["message"]
    monkeypatch.setenv("SUPABASE_BUCKET", "seminar-files")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "sb_publishable_wrong")
    response = submit(taro, course, file=("essay.pdf", b"x", "application/pdf"))
    assert "File storage refused the upload (HTTP 401: Invalid API key)" in response.text
    assert only(engine, StoredFile) == []  # nothing half-saved
    assert "Invalid API key" in admin.get("/members").text  # and the Members page says why


def test_storage_check_warns_about_render_without_supabase(admin, monkeypatch):
    import storage

    monkeypatch.setenv("RENDER", "true")
    status = storage.check()
    assert status["ok"] is False and "lost" in status["message"]
    assert "temporary disk" in admin.get("/members").text


def test_a_missing_file_gives_a_clear_message(engine, admin, taro, course):
    submit(taro, course)
    stored = only(engine, StoredFile)[0]
    (Path(os.environ["LOCAL_FILES_DIR"]) / stored.storage_path).unlink()
    response = admin.get(f"/files/{stored.id}", headers={"accept": "text/html"})
    assert response.status_code == 404 and "missing from file storage" in response.text


# --- Previews of PDFs, HTML and text ------------------------------------------------------


def preview_of(person, engine, filename):
    with Session(engine) as db:
        stored = db.exec(select(StoredFile).where(StoredFile.filename == filename)).one()
    return person.get(f"/files/{stored.id}/preview")


def test_pdfs_html_and_text_can_be_previewed(engine, admin, taro, course):
    submit(taro, course, title="P", file=("paper.pdf", b"%PDF-1.4 real", "application/pdf"))
    submit(taro, course, title="H", file=("page.html", b"<h1>Hi</h1><script>alert(1)</script>", "text/html"))
    submit(taro, course, title="T", file=("notes.md", b"# Notes", "application/octet-stream"))
    submit(taro, course, title="Z", file=("data.zip", b"PK", "application/zip"))
    pdf = preview_of(admin, engine, "paper.pdf")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.headers["content-disposition"].startswith("inline")
    html_page = preview_of(admin, engine, "page.html")
    policy = html_page.headers["content-security-policy"]
    assert html_page.headers["content-type"].startswith("text/html")
    assert policy.startswith("sandbox;") and "script-src" not in policy and "default-src 'none'" in policy
    text = preview_of(admin, engine, "notes.md")
    assert text.headers["content-type"].startswith("text/plain") and text.content == b"# Notes"
    assert "sandbox" in text.headers["content-security-policy"]
    assert preview_of(admin, engine, "data.zip").status_code == 404  # not previewable: download only
    page = admin.get(f"/classes/{course}/assignments/students/{taro.id}").text
    assert page.count("Preview</button>") == 3


def test_a_fake_pdf_is_not_previewed(engine, admin, taro, course):
    submit(taro, course, file=("fake.pdf", b"<html><script>alert(1)</script>", "application/pdf"))
    assert preview_of(admin, engine, "fake.pdf").status_code == 404


def test_previews_follow_download_permissions(engine, make_person, taro, course):
    submit(taro, course, file=("private.pdf", b"%PDF-1.4 x", "application/pdf"))  # not ticked for guests
    assert preview_of(make_person("guest"), engine, "private.pdf").status_code == 404
