"""Guests: limited, read-only viewing, and never a student's real name (Addendum 1)."""

import pytest
from sqlmodel import Session, select

from conftest import PASSWORD
from db import StoredFile, Submission, Task, User
from names import NameMask
from security import Viewer
from test_coursework import due_in, post_task, submit


@pytest.fixture
def course(create_class):
    return create_class(title="Seminar with Yamada Taro", description="Thanks to taro and ＴＡＲＯ-kun.\nAlso Ken.",
                        enrolled_gens=[9])


@pytest.fixture
def taro(make_person):
    return make_person("student", "taro.y", "Yamada Taro", gen=9)


def all_pages(person, course, student, agent=None) -> str:
    """Every page a guest can open in a class, joined together."""
    urls = ["/classes", f"/classes/{course}", f"/classes/{course}?tab=exams", f"/classes/{course}?tab=agents",
            f"/classes/{course}/assignments", f"/classes/{course}/exams",
            f"/classes/{course}/window/assignments", f"/classes/{course}/window/agents",
            f"/classes/{course}/bar/assignments", f"/classes/{course}/assignments/students/{student}",
            f"/classes/{course}/agents/{student}", f"/classes/{course}/agents/{student}/grid"]
    pages = []
    for url in urls:
        response = person.get(url)
        assert response.status_code == 200, url
        pages.append(response.text)
    return "\n".join(pages)


# --- Placeholders ------------------------------------------------------------------------


def test_placeholders_are_fixed_and_never_reused(engine, admin, make_person):
    first = make_person("student", "stu1", "First Student", gen=9)
    second = make_person("student", "stu2", "Second Student", gen=9)
    other_gen = make_person("student", "stu3", "Other Gen", gen=8)
    admin.post(f"/members/{second.id}/delete", data={"password": PASSWORD})
    third = make_person("student", "stu4", "Third Student", gen=9)
    with Session(engine) as db:
        numbers = {u.login_id: u.guest_number for u in db.exec(select(User)).all()}
    assert numbers["stu1"] == 1 and numbers["stu4"] == 3  # 2 is never given again
    assert numbers["stu3"] == 1  # each gen counts on its own
    with Session(engine) as db:
        mask = NameMask(db, Viewer(session=None, user=None))
        assert mask.name(db.get(User, first.id)) == "Gen 9 Student 1"
        assert mask.name(db.get(User, other_gen.id)) == "Gen 8 Student 1"
        assert mask.name(db.get(User, third.id)) == "Gen 9 Student 3"
        assert mask.name(db.get(User, admin.id)) == "Fukuhara Admin"  # admins keep their names


# --- Name search ------------------------------------------------------------------------------


def test_name_search_catches_parts_case_widths_and_other_names(engine, make_person):
    make_person("student", "taro", "Yamada Taro", gen=9)
    hanako = make_person("student", "hanako", "佐藤 花子", gen=9)
    hanako.post("/account/other-names", data={"other_names": "Hana-chan\nSato Hanako"})
    make_person("student", "ann", "Ann Lee", gen=9)
    with Session(engine) as db:
        mask = NameMask(db, Viewer(session=None, user=None)).text
        assert mask("By Yamada Taro") == "By Gen 9 Student 1"  # the full name: one placeholder
        assert mask("yamada  TARO and Taro") == "Gen 9 Student 1 and Gen 9 Student 1"
        assert mask("ｙａｍａｄａ ＴＡＲＯ") == "Gen 9 Student 1"  # full-width letters
        assert mask("YamadaTaro wrote this") == "Gen 9 Student 1 wrote this"
        assert mask("佐藤花子さんの発表") == "Gen 9 Student 2さんの発表"  # Japanese without the space
        assert mask("花子") == "Gen 9 Student 2"  # a part of the name
        assert mask("Thanks hana-chan!") == "Thanks Gen 9 Student 2!"  # other names
        assert mask("sato hanako") == "Gen 9 Student 2"
        assert mask("Annual report") == "Annual report"  # a Latin part must stand alone
        assert mask("Ann's notes") == "Gen 9 Student 3's notes"
        assert mask("Lee") == "Gen 9 Student 3"


def test_members_always_see_the_original_text(engine, taro, admin, course):
    page = admin.get(f"/classes/{course}").text
    assert "Seminar with Yamada Taro" in page and "Thanks to taro and ＴＡＲＯ-kun" in page


def test_guests_never_see_a_real_name_or_login_id_anywhere(engine, client, admin, taro, make_person, course,
                                                            create_agent):
    taro.post("/account/other-names", data={"other_names": "Tarochan"})
    post_task(admin, course, title="Interview Yamada", text="Ask TARO about it", guest_visible=True)
    with Session(engine) as db:
        task = db.exec(select(Task)).one().id
    submit(taro, course, title="Yamada's essay", text="I, Yamada Taro (aka tarochan), wrote this.", task=task,
           guest_visible=True)
    create_agent(taro, course, "Taro's helper", description="Helps Yamada.")
    guest = make_person("guest")
    pages = all_pages(guest, course, taro.id)
    for secret in ("Yamada", "yamada", "Taro", "TARO", "taro", "ＴＡＲＯ", "Tarochan", "tarochan", "taro.y"):
        assert secret not in pages, secret
    assert "Gen 9 Student 1" in pages
    # Renaming a student takes effect straight away.
    admin.post(f"/members/{taro.id}/name", data={"full_name": "Suzuki Jiro"})
    assert "Suzuki" not in all_pages(guest, course, taro.id)


def test_unticked_items_do_not_exist_for_guests(engine, admin, taro, make_person, course):
    post_task(admin, course, title="Members-only task", file=("brief.txt", b"brief", "text/plain"))
    post_task(admin, course, title="Public task", guest_visible=True)
    submit(taro, course, title="Private work", file=("mine.txt", b"mine", "text/plain"))
    guest = make_person("guest")
    pages = all_pages(guest, course, taro.id)
    assert "Members-only task" not in pages and "Private work" not in pages
    assert "Public task" in pages and "No submissions yet" in pages
    with Session(engine) as db:
        for stored in db.exec(select(StoredFile)).all():
            assert guest.get(f"/files/{stored.id}").status_code == 404
            assert taro.get(f"/files/{stored.id}").status_code == 200


def test_ticked_submissions_are_shown_in_full(engine, admin, taro, make_person, course):
    submit(taro, course, title="Shared work", file=("shared.txt", b"shared", "text/plain"), guest_visible=True)
    guest = make_person("guest")
    history = guest.get(f"/classes/{course}/assignments/students/{taro.id}").text
    assert "Shared work" in history and "shared.txt" in history
    with Session(engine) as db:
        stored = db.exec(select(StoredFile)).one()
    assert guest.get(f"/files/{stored.id}").content == b"shared"


def test_ticked_submission_linked_to_a_hidden_task_shows_no_link(engine, admin, taro, make_person, course):
    post_task(admin, course, title="Hidden task")
    with Session(engine) as db:
        task = db.exec(select(Task)).one().id
    submit(taro, course, title="Visible answer", text="answer", task=task, guest_visible=True)
    page = make_person("guest").get(f"/classes/{course}/assignments/students/{taro.id}").text
    assert "Visible answer" in page and "Hidden task" not in page and "For:" not in page


def test_class_hidden_from_guests_hides_everything_in_it(engine, admin, taro, make_person, create_class):
    secret = create_class(title="Closed", enrolled_gens=[9], hidden_from_guests=True)
    submit(taro, secret, title="Ticked anyway", file=("t.txt", b"t", "text/plain"), guest_visible=True)
    guest = make_person("guest")
    assert guest.get(f"/classes/{secret}/assignments/students/{taro.id}").status_code == 404
    with Session(engine) as db:
        stored = db.exec(select(StoredFile)).one()
    assert guest.get(f"/files/{stored.id}").status_code == 404


def test_guest_cell_shows_the_latest_guest_visible_submission(admin, taro, make_person, course):
    submit(taro, course, title="Older public", text="a", guest_visible=True)
    submit(taro, course, title="Newer private", text="b")
    window = make_person("guest").get(f"/classes/{course}/window/assignments").text
    assert "Older public" in window and "Newer private" not in window


def test_visible_to_guests_checkbox_warns_about_files(admin, taro, course):
    form = taro.get(f"/classes/{course}/assignments/submissions/new").text
    assert "Visible to guests" in form and "names inside a file are not" in form
    assert "names inside a file are not" in admin.get(f"/classes/{course}/assignments/new").text


# --- Guests are read-only -------------------------------------------------------------------


def test_guests_are_refused_on_every_upload_edit_and_control_route(engine, admin, taro, make_person, course,
                                                                   create_agent):
    post_task(admin, course)
    submit(taro, course)
    agent = create_agent(taro, course, "helper")
    with Session(engine) as db:
        task = db.exec(select(Task)).one().id
        sub = db.exec(select(Submission)).one().id
    guest = make_person("guest")
    file = {"files": ("x.txt", b"x", "text/plain")}
    attempts = [
        ("/classes", {}), (f"/classes/{course}/edit", {}), (f"/classes/{course}/delete", {"password": "x"}),
        (f"/classes/{course}/assignments", {"title": "x", "due_at": due_in(5)}),
        (f"/tasks/{task}/edit", {}), (f"/tasks/{task}/delete", {}),
        (f"/classes/{course}/assignments/submissions", {"mode": "text", "content_text": "x"}),
        (f"/submissions/{sub}/edit", {}), (f"/submissions/{sub}/delete", {}),
        (f"/classes/{course}/agents", {"name": "x"}), (f"/agents/{agent}/command", {"command": "start"}),
        (f"/agents/{agent}/rename", {"name": "x"}), (f"/agents/{agent}/inbox", {}),
        (f"/agents/{agent}/delete", {"password": "x"}), ("/folders", {"name": "x"}),
        (f"/members/{taro.id}/name", {"full_name": "x"}), ("/gens", {}),
        ("/account/password", {}), ("/account/other-names", {"other_names": "x"}),
    ]
    for url, data in attempts:
        response = guest.post(url, data=data, files=file if "inbox" in url else None)
        assert response.status_code == 403, url
    page = guest.get(f"/classes/{course}").text
    assert "Add assignment" not in page and "Edit" not in page
    history = guest.get(f"/classes/{course}/assignments/students/{taro.id}").text
    assert "Create new submission" not in history


def test_guests_never_see_the_members_page_or_login_ids(admin, taro, make_person, course):
    guest = make_person("guest")
    assert guest.get("/members").status_code == 403
    assert taro.get("/members").status_code == 403
    page = admin.get("/members").text
    assert "taro.y" in page and "Gen 9 Student 1" in page
