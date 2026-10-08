"""The Members page (admins), and students' agent folders."""

import os
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from conftest import PASSWORD
from db import Agent, AgentFolder, Folder, Gen, StoredFile, Submission, User
from main import app
from test_coursework import submit

ADD_CARD = "$dispatch('open-add-agent')"  # the "Add Agent" card on the grid


def user(engine, user_id) -> User | None:
    with Session(engine) as db:
        return db.get(User, user_id)


def test_members_page_groups_accounts_and_shows_storage(admin, make_person):
    make_person("student", "nine", "Nine Student", gen=9)
    make_person("student", "eight", "Eight Student", gen=8)
    page = admin.get("/members").text
    assert page.index("Admins") < page.index("Gen 12") < page.index("Nine Student") < page.index("Eight Student")
    assert "File storage" in page and "900.0 MB" in page


def test_admin_changes_name_and_gen(engine, admin, make_person):
    student = make_person("student", "taro", "Taro", gen=9)
    admin.post(f"/members/{student.id}/name", data={"full_name": "Taro Yamada"})
    with Session(engine) as db:
        gen8 = db.exec(select(Gen).where(Gen.number == 8)).one()
    admin.post(f"/members/{student.id}/gen", data={"gen_id": gen8.id})
    changed = user(engine, student.id)
    assert changed.full_name == "Taro Yamada" and changed.gen_id == gen8.id
    assert changed.guest_number == 1  # a fresh number in the new gen


def test_reset_password_shows_it_once_and_signs_the_member_out(engine, admin, make_person):
    student = make_person("student", "taro")
    page = admin.post(f"/members/{student.id}/reset-password").text
    temporary = page.split('<code class="select-all text-base">')[1].split("</code>")[0]
    assert student.get("/classes", follow_redirects=False).status_code in (303, 401)
    fresh = TestClient(app)
    assert fresh.post("/login", data={"login_id": "taro", "password": temporary}, follow_redirects=False).status_code == 303
    assert temporary not in admin.get("/members").text  # shown only once


def test_delete_account_needs_the_admin_password_and_removes_their_data(engine, admin, make_person, create_class,
                                                                       create_agent):
    course = create_class(enrolled_gens=[9])
    student = make_person("student", "taro", gen=9)
    submit(student, course)
    create_agent(student, course, "helper")
    path = Path(os.environ["LOCAL_FILES_DIR"]) / Session(engine).exec(select(StoredFile)).one().storage_path
    wrong = admin.post(f"/members/{student.id}/delete", data={"password": "wrong-password"})
    assert "incorrect" in wrong.text and user(engine, student.id) is not None
    admin.post(f"/members/{student.id}/delete", data={"password": PASSWORD})
    assert user(engine, student.id) is None
    with Session(engine) as db:
        assert db.exec(select(Submission)).all() == [] and db.exec(select(Agent)).all() == []
        assert db.exec(select(StoredFile)).all() == []
    assert not path.exists()


def test_admins_cannot_delete_themselves(engine, admin):
    page = admin.post(f"/members/{admin.id}/delete", data={"password": PASSWORD}).text
    assert "can&#39;t delete your own account" in page and user(engine, admin.id) is not None


def test_add_the_next_gen(engine, admin, make_person):
    admin.post("/gens")
    with Session(engine) as db:
        assert max(db.exec(select(Gen.number)).all()) == 13
    assert "Gen 13" in TestClient(app).get("/register").text
    assert make_person("student").post("/gens").status_code == 403


# --- Folders ------------------------------------------------------------------------------------


def test_folders_belong_to_their_student_and_are_shared_across_classes(engine, make_person, create_class,
                                                                        create_agent):
    first = create_class(title="First", enrolled_gens=[9])
    second = create_class(title="Second", enrolled_gens=[9])
    taro = make_person("student", "taro", gen=9)
    hanako = make_person("student", "hanako", gen=9)
    a = create_agent(taro, first, "a-bot")
    b = create_agent(taro, second, "b-bot")
    taro.post("/folders", data={"name": "Research", "course_id": first})
    with Session(engine) as db:
        folder = db.exec(select(Folder)).one()
    assert folder.owner_id == taro.id
    taro.post(f"/agents/{a}/tags", data={"folder_id": folder.id})
    taro.post(f"/agents/{b}/tags", data={"folder_id": folder.id})
    # The same folder in both classes, each counting only its own agents.
    page = taro.get(f"/classes/{first}/agents/{taro.id}").text
    assert "Research" in page and "a-bot" in page
    in_folder = taro.get(f"/classes/{second}/agents/{taro.id}?folder={folder.id}").text
    assert "b-bot" in in_folder and "a-bot" not in in_folder
    # Nobody else can change it, or tag their agents with it.
    assert hanako.post(f"/folders/{folder.id}/rename", data={"name": "Mine"}).status_code == 403
    assert hanako.post(f"/folders/{folder.id}/delete").status_code == 403
    h = create_agent(hanako, first, "h-bot")
    assert hanako.post(f"/agents/{h}/tags", data={"folder_id": folder.id}).status_code == 403
    # Deleting a folder keeps its agents.
    taro.post(f"/folders/{folder.id}/delete", data={"course_id": first})
    with Session(engine) as db:
        assert db.exec(select(Folder)).all() == [] and db.exec(select(AgentFolder)).all() == []
        assert len(db.exec(select(Agent)).all()) == 3


def test_grid_pages_hold_nine_cells_including_add_agent(make_person, create_class, create_agent):
    course = create_class(enrolled_gens=[9])
    taro = make_person("student", "taro", gen=9)
    for number in range(9):
        create_agent(taro, course, f"agent-{number}")
    first = taro.get(f"/classes/{course}/agents/{taro.id}").text
    # 9 agents fill the first page; the Add Agent card is the 10th cell, on page 2.
    assert "page 1 of 2" in first and "agent-8" in first and ADD_CARD not in first
    second = taro.get(f"/classes/{course}/agents/{taro.id}?page=2").text
    assert "agent-0" not in second and ADD_CARD in second
    # Other members see 9 agents on a single page (no Add Agent card for them).
    hanako = make_person("student", "hanako", gen=9)
    assert "page 1 of" not in hanako.get(f"/classes/{course}/agents/{taro.id}").text


def test_members_page_shows_the_database_size(admin):
    page = admin.get("/members").text
    assert "Database" in page and "of 500.0 MB used" in page


# --- Storage breakdowns ------------------------------------------------------------------------


def test_file_storage_breakdown_by_type_class_person_agent_and_largest(engine, client, admin, make_person,
                                                                       create_class, create_agent):
    from usage import file_breakdown
    from test_agents import api

    course = create_class(title="Methods", enrolled_gens=[9])
    taro = make_person("student", "taro", "Yamada Taro", gen=9)
    submit(taro, course, file=("essay.txt", b"x" * 3000, "text/plain"))
    create_agent(taro, course, "helper")
    client.post("/api/outputs/file", data={"category": "files"}, files={"file": ("chart.png", b"y" * 1000, "image/png")},
                headers=api("helper"))
    with Session(engine) as db:
        views = {view.key: view for view in file_breakdown(db)}
    by_type = {row.label: row.size for row in views["files-type"].rows}
    assert by_type == {"Submission files": 3000, "Agent output files": 1000}
    assert views["files-type"].total == 4000 and views["files-type"].rows[0].share == 75
    assert [(r.label, r.size) for r in views["files-class"].rows] == [("Methods", 4000)]
    assert [(r.label, r.size) for r in views["files-person"].rows] == [("Yamada Taro", 4000)]
    assert [(r.label, r.size) for r in views["files-agent"].rows] == [("helper (Yamada Taro)", 1000)]
    assert [r.label for r in views["files-largest"].rows] == ["essay.txt", "chart.png"]
    assert "Submission files · Methods · Yamada Taro" == views["files-largest"].rows[0].detail


def test_pie_gives_six_colours_then_other(engine):
    from usage import OTHER_COLOUR, SLICE_COLOURS, Row, View, finish

    view = finish(View("x", "Test", [Row(f"item {n}", 100 - n) for n in range(9)]))
    assert [s.colour for s in view.slices] == SLICE_COLOURS + [OTHER_COLOUR]
    assert view.slices[-1].label == "Other (3)" and view.slices[-1].size == 94 + 93 + 92
    assert abs(sum(s.share for s in view.slices) - 100) < 1e-9
    assert all(s.path.startswith("M ") for s in view.slices)
    whole = finish(View("y", "One", [Row("only", 5)]))  # a single slice is a whole ring
    assert whole.slices[0].path.count("M ") == 2


def test_database_breakdown_estimates_by_class_person_and_agent(engine, client, admin, make_person,
                                                                create_class, create_agent):
    from usage import database_breakdown
    from test_agents import api

    course = create_class(title="Methods", enrolled_gens=[9])
    taro = make_person("student", "taro", "Yamada Taro", gen=9)
    create_agent(taro, course, "helper")
    client.post("/api/outputs", json={"category": "notes", "content": "z" * 500}, headers=api("helper"))
    submit(taro, course, text="w" * 200)
    with Session(engine) as db:
        views = {view.key: view for view in database_breakdown(db)}
    agent = dict((r.label, r.size) for r in views["db-agent"].rows)["helper (Yamada Taro)"]
    assert agent >= 505  # the output's text and category, plus its activity line
    person = dict((r.label, r.size) for r in views["db-person"].rows)["Yamada Taro"]
    assert person >= agent + 200  # plus the text submission
    assert "Estimated" in views["db-class"].note


def test_breakdowns_are_for_admins_only(admin, make_person):
    for part in ("files", "database"):
        page = admin.get(f"/members/usage/{part}")
        assert page.status_code == 200 and "<html" not in page.text
    assert admin.get("/members/usage/other").status_code == 404
    assert make_person("student").get("/members/usage/files").status_code == 403
    assert make_person("guest").get("/members/usage/database").status_code == 403
    assert 'hx-get="/members/usage/files"' in admin.get("/members").text
