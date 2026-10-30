"""The Agent Skills market: sharing, visibility, the agent API, and the organizer.

No test ever contacts OpenRouter: the AI organizer is tested with a stand-in reply.
"""

import json
from datetime import datetime

import pytest
from sqlmodel import Session, select

import market
from conftest import api, class_form
from db import OrganizerRun, Skill, SkillCategory, SkillTree
from web import JST

RETRIEVAL = "# Paper finder\n\nSearch arXiv and fetch papers through the API, then collect the results."
DECISION = "# Choose a method\n\nDecide which method to use: weigh criteria, score each option, judge the risk."


@pytest.fixture
def course(create_class):
    return create_class(title="Methods", enrolled_gens=[9])


@pytest.fixture
def taro(make_person):
    return make_person("student", "taro", "Yamada Taro", gen=9)


def share(person, course, content=RETRIEVAL, name="", summary="", share="class"):
    return person.post(f"/classes/{course}/skills", data={"name": name, "summary": summary, "share": share,
                                                          "mode": "text", "content_text": content})


def skills(engine):
    """Members' and agents' skills (not the built-in ones)."""
    with Session(engine) as db:
        return db.exec(select(Skill).where(Skill.builtin.is_(None)).order_by(Skill.id)).all()


def market_html(person, course, **params):
    return person.get(f"/classes/{course}/skills", params=params).text


# --- Sharing and finding skills ---------------------------------------------------------------


def test_a_member_shares_a_skill_and_it_appears_with_its_identifier(engine, taro, course):
    response = share(taro, course, summary="Finds papers")
    skill = skills(engine)[0]
    assert response.headers["HX-Redirect"] == f"/classes/{course}?tab=agents&skill={skill.id}"
    assert skill.name == "Paper finder"  # from the file's first heading
    listing = market_html(taro, course)
    assert "Paper finder" in listing and f"skill-{skill.id}" in listing and "Yamada Taro" in listing
    detail = taro.get(f"/skills/{skill.id}").text
    assert f'read_skill("skill-{skill.id}")' in detail and f"/api/skills/{skill.id}/content" in detail
    assert "Search arXiv" in detail
    assert "Agent skills" in taro.get(f"/classes/{course}?tab=agents").text


def test_sharing_again_with_the_same_name_updates_it(engine, taro, course):
    share(taro, course, name="Finder")
    share(taro, course, name="Finder", content=RETRIEVAL + "\nNow also PubMed.")
    [skill] = skills(engine)
    assert skill.version == 2 and "PubMed" in skill.content


def test_skill_files_are_checked(taro, course):
    assert "empty" in share(taro, course, content="   ").text
    big = share(taro, course, content="x" * (market.MAX_SKILL_BYTES + 1))
    assert big.status_code == 400 and "100 KB" in big.text


def test_several_skill_files_become_several_skills(engine, taro, course):
    files = [("files", ("a.md", b"# Paper finder\nFind papers.", "text/markdown")),
             ("files", ("b.md", b"# Chart maker\nMake charts.", "text/markdown"))]
    reply = taro.post(f"/classes/{course}/skills", data={"mode": "file", "summary": "Mine", "share": "seminar",
                                                         "name": "Ignored"}, files=files)
    assert reply.status_code == 200
    made = skills(engine)
    assert [s.name for s in made] == ["Paper finder", "Chart maker"]
    assert all(s.share == "seminar" and s.summary == "Mine" for s in made)


def test_one_bad_skill_file_shares_none(engine, taro, course):
    files = [("files", ("a.md", b"# Good\nFine.", "text/markdown")), ("files", ("empty.md", b"   ", "text/markdown"))]
    reply = taro.post(f"/classes/{course}/skills", data={"mode": "file"}, files=files)
    assert reply.status_code == 400 and "empty.md" in reply.text and skills(engine) == []


def test_the_connector_skill_is_built_in_everywhere(engine, admin, taro, client, make_person, course, create_class,
                                                   create_agent):
    other = create_class(title="Other", enrolled_gens=[9])
    for course_id in (course, other):
        assert "skill-connector" in market_html(taro, course_id)  # every class's market, class view too
    with Session(engine) as db:
        builtin = db.exec(select(Skill).where(Skill.builtin == "connector")).one()
    assert builtin.share == "seminar" and builtin.course_id is None and builtin.owner_id is None
    detail = taro.get(f"/skills/{builtin.id}").text
    assert "built in" in detail and "Edit" not in detail and "Remove it from the market" not in detail
    assert "http://testserver/api/skills/connector/content" in detail and "{{DOWNLOADED_FROM}}" not in detail
    # Nobody edits or removes it, not even an admin.
    assert taro.post(f"/skills/{builtin.id}/edit", data={"summary": "x"}).status_code == 403
    assert admin.post(f"/skills/{builtin.id}/delete").status_code == 403
    # Agents read it by its fixed identifier, with this site's address filled in.
    create_agent(taro, course, "helper")
    text = client.get("/api/skills/skill-connector/content", headers=api("helper"))
    assert text.status_code == 200 and text.headers["x-skill-version"] == "1"
    assert text.text.startswith("# Dashboard connector (built-in skill)") and "/api/outputs" in text.text
    assert "http://testserver" in text.text and "{{DOWNLOADED_FROM}}" not in text.text
    assert client.delete("/api/skills/skill-connector", headers=api("helper")).status_code == 403
    # Guests never see the market.
    assert make_person("guest").get(f"/skills/{builtin.id}").status_code in (403, 404)


def test_the_connector_skill_follows_the_dashboard(engine, monkeypatch):
    with Session(engine) as db:
        market.sync_builtin_skills(db)  # unchanged: same version
        assert db.exec(select(Skill).where(Skill.builtin == "connector")).one().version == 1
        changed = market.builtin_skills()
        changed["connector"]["content"] += "\nA new endpoint."
        monkeypatch.setattr(market, "builtin_skills", lambda: changed)
        market.sync_builtin_skills(db)
        skill = db.exec(select(Skill).where(Skill.builtin == "connector")).one()
        assert skill.version == 2 and skill.content.endswith("A new endpoint.")


def test_older_databases_let_skills_have_no_class_or_owner(engine):
    from sqlalchemy import inspect

    from db import allow_empty_columns, create_db_and_tables

    # Simulate the older table, where class and owner were required.
    with engine.begin() as connection:
        connection.exec_driver_sql('DROP TABLE "skill"')
        connection.exec_driver_sql(
            'CREATE TABLE "skill" (id INTEGER PRIMARY KEY, course_id INTEGER NOT NULL, share VARCHAR NOT NULL, '
            'owner_id INTEGER NOT NULL, agent_id INTEGER, agent_name VARCHAR, name VARCHAR NOT NULL, '
            'summary VARCHAR, content TEXT NOT NULL, version INTEGER NOT NULL, downloads INTEGER NOT NULL, '
            'category_id INTEGER, specialty VARCHAR, organized_version INTEGER, created_at DATETIME NOT NULL, '
            'updated_at DATETIME NOT NULL)')
        connection.exec_driver_sql('CREATE INDEX "ix_skill_course_id" ON "skill" (course_id)')
        connection.exec_driver_sql(
            "INSERT INTO skill VALUES (7, 1, 'class', 1, NULL, NULL, 'Old', NULL, '# Old', 3, 0, NULL, NULL, NULL, "
            "'2026-01-01', '2026-01-01')")
    assert allow_empty_columns(engine) == ["skill.course_id", "skill.owner_id"]
    create_db_and_tables(engine)  # adds the built-in skill
    columns = {c["name"]: c for c in inspect(engine).get_columns("skill")}
    assert columns["course_id"]["nullable"] and columns["owner_id"]["nullable"] and "builtin" in columns
    with Session(engine) as db:
        old = db.get(Skill, 7)
        assert old.name == "Old" and old.version == 3
        assert db.exec(select(Skill).where(Skill.builtin == "connector")).one().id != 7
    assert allow_empty_columns(engine) == []  # only once


def test_downloading_counts(engine, taro, course):
    share(taro, course)
    skill = skills(engine)[0]
    reply = taro.get(f"/skills/{skill.id}/download")
    assert reply.headers["content-disposition"].startswith("attachment") and reply.text == RETRIEVAL
    assert skills(engine)[0].downloads == 1


def test_class_only_and_seminar_wide_skills(engine, admin, taro, make_person, course, create_class):
    other_class = create_class(title="Other", enrolled_gens=[9])
    share(taro, course, name="Local", share="class")
    share(taro, course, name="Shared", content=DECISION, share="seminar")
    here = market_html(taro, course)
    assert "Local" in here and "Shared" in here  # the origin class shows both
    there = market_html(taro, other_class)
    assert "Local" not in there and "Shared" not in there  # another class's default view: its own only
    there_all = market_html(taro, other_class, scope="seminar")
    assert "Shared" in there_all and "Local" not in there_all  # "Whole seminar" adds seminar-wide skills
    # A student who can't see the origin class still sees the seminar-wide skill.
    outsider = make_person("student", "outsider", gen=8)
    admin.post(f"/classes/{course}/edit", data=class_form(engine, title="Methods", enrolled_gens=[9], hidden_gens=[8]))
    local, shared = skills(engine)
    assert outsider.get(f"/skills/{local.id}").status_code == 404
    assert outsider.get(f"/skills/{shared.id}").status_code == 200


def test_the_market_is_for_members_only(engine, taro, make_person, course):
    share(taro, course)
    guest = make_person("guest")
    skill = skills(engine)[0]
    for url in (f"/classes/{course}/skills", f"/skills/{skill.id}", f"/skills/{skill.id}/download"):
        assert guest.get(url).status_code == 403, url
    assert share(guest, course).status_code == 403
    assert "Agent skills" not in guest.get(f"/classes/{course}?tab=agents").text


def test_only_its_member_edits_and_admins_can_remove(engine, admin, taro, make_person, course):
    share(taro, course)
    skill = skills(engine)[0]
    hanako = make_person("student", "hanako", gen=9)
    edit = {"summary": "Changed", "share": "seminar", "mode": "text", "content_text": ""}
    assert hanako.post(f"/skills/{skill.id}/edit", data=edit).status_code == 403
    assert hanako.post(f"/skills/{skill.id}/delete").status_code == 403
    assert taro.post(f"/skills/{skill.id}/edit", data=edit).status_code == 200
    changed = skills(engine)[0]
    assert changed.summary == "Changed" and changed.share == "seminar" and changed.version == 1  # text kept
    assert admin.post(f"/skills/{skill.id}/delete").status_code == 200  # moderation
    assert skills(engine) == []


def test_filters_search_source_and_mine(engine, client, taro, make_person, course, create_agent):
    hanako = make_person("student", "hanako", "Sato Hanako", gen=9)
    share(taro, course, name="Taro skill")
    share(hanako, course, name="Hanako skill", content=DECISION)
    create_agent(taro, course, "helper")
    client.post("/api/skills", json={"content": "# Agent skill\nFetch data."}, headers=api("helper"))
    assert "Hanako skill" in market_html(taro, course, q="sato")
    assert "Taro skill" not in market_html(taro, course, q="sato")
    by_agents = market_html(taro, course, source="agent")
    assert "Agent skill" in by_agents and "Taro skill" not in by_agents
    mine = market_html(taro, course, mine="true")
    assert "Taro skill" in mine and "Hanako skill" not in mine and "Agent skill" in mine  # credited to Taro
    by_name = market_html(taro, course, sort="name")
    assert by_name.index("Agent skill") < by_name.index("Hanako skill") < by_name.index("Taro skill")


# --- The agent API ------------------------------------------------------------------------------


def test_agents_post_read_update_and_download_skills(engine, client, taro, course, create_agent):
    create_agent(taro, course, "helper")
    posted = client.post("/api/skills", json={"content": RETRIEVAL, "summary": "Papers", "share": "seminar"},
                         headers=api("helper"))
    assert posted.status_code == 201
    info = posted.json()
    assert info["credit"] == "from Yamada Taro's agent, helper" and info["identifier"] == f"skill-{info['id']}"
    assert info["address"].endswith(f"/api/skills/{info['id']}/content") and info["created"] is True
    again = client.post("/api/skills", json={"content": RETRIEVAL + "\nv2", "name": "Paper finder"},
                        headers=api("helper")).json()
    assert again["id"] == info["id"] and again["version"] == 2 and again["created"] is False
    text = client.get(f"/api/skills/{info['identifier']}/content", headers=api("helper"))
    assert text.text.endswith("v2") and text.headers["x-skill-version"] == "2"  # always the latest
    assert text.headers["content-type"].startswith("text/markdown")
    full = client.get(f"/api/skills/{info['id']}", headers=api("helper")).json()
    assert full["content"].endswith("v2") and full["downloads"] == 0  # reading isn't downloading
    client.get(f"/api/skills/{info['id']}/download", headers=api("helper"))
    assert skills(engine)[0].downloads == 1
    listed = client.get("/api/skills", headers=api("helper")).json()
    assert [s["identifier"] for s in listed] == ["skill-connector", info["identifier"]]  # built-in first
    assert "content" not in listed[1]
    assert "Yamada Taro's agent" in client.get("/api/skills", params={"scope": "seminar"},
                                                headers=api("helper")).json()[1]["credit"]


def test_agents_only_delete_their_own_skills(engine, client, taro, make_person, course, create_agent):
    hanako = make_person("student", "hanako", gen=9)
    create_agent(taro, course, "mine")
    create_agent(hanako, course, "theirs")
    skill_id = client.post("/api/skills", json={"content": RETRIEVAL}, headers=api("mine")).json()["id"]
    assert client.delete(f"/api/skills/{skill_id}", headers=api("theirs")).status_code == 403
    assert client.delete(f"/api/skills/skill-{skill_id}", headers=api("mine")).json() == {"deleted": 1}
    assert client.get(f"/api/skills/{skill_id}", headers=api("mine")).status_code == 404


def test_agents_see_skills_as_their_owner_does(engine, client, taro, course, create_class, create_agent):
    other_class = create_class(title="Other", enrolled_gens=[9])
    share(taro, course, name="Here only", share="class")
    create_agent(taro, other_class, "elsewhere")
    listed = client.get("/api/skills", headers=api("elsewhere")).json()
    assert [s["identifier"] for s in listed] == ["skill-connector"]  # its class: only the built-in skill
    skill = skills(engine)[0]
    # Its owner can see the origin class, so asking for the skill directly works.
    assert client.get(f"/api/skills/{skill.id}", headers=api("elsewhere")).status_code == 200
    assert client.get("/api/skills/999", headers=api("elsewhere")).status_code == 404
    assert client.get("/api/skills", headers={"X-API-Key": "wrong-key-0123456789-abc"}).status_code == 401


def test_skill_trees(engine, client, taro, make_person, course, create_agent):
    agent_id = create_agent(taro, course, "helper")
    tree = "# Helper's skills\n- Paper finder: first step\n- Choose a method: after reading"
    assert client.put("/api/skill-tree", json={"content": tree}, headers=api("helper")).json()["version"] == 1
    skill_id = client.post("/api/skills", json={"content": RETRIEVAL}, headers=api("helper")).json()["id"]
    read = client.get(f"/api/skill-trees/{agent_id}", headers=api("helper")).json()
    assert read["content"] == tree and read["agent"] == "helper"
    assert "first step" in taro.get(f"/skills/{skill_id}").text  # shown with the skill
    # Members can set one of their own agents' trees on the page too...
    taro.post(f"/classes/{course}/skill-trees", data={"agent_id": agent_id, "mode": "text", "content_text": "# New tree"})
    with Session(engine) as db:
        assert db.exec(select(SkillTree)).one().version == 2
    # ...but not someone else's.
    hanako = make_person("student", "hanako", gen=9)
    refused = hanako.post(f"/classes/{course}/skill-trees", data={"agent_id": agent_id, "mode": "text",
                                                                  "content_text": "# Mine now"})
    assert refused.status_code == 403


def tree_files(engine):
    from db import StoredFile

    with Session(engine) as db:
        return db.exec(select(StoredFile)).all()


def test_a_skill_tree_needs_its_ai_version_first(engine, taro, course, create_agent):
    agent_id = create_agent(taro, course, "helper")
    pdf = {"view": ("tree.pdf", b"%PDF-1.4 tree", "application/pdf")}
    refused = taro.post(f"/classes/{course}/skill-trees", data={"agent_id": agent_id, "mode": "file"}, files=pdf)
    assert refused.status_code == 400 and "AI version (.md) comes first" in refused.text
    # Both at once works.
    taro.post(f"/classes/{course}/skill-trees", data={"agent_id": agent_id, "mode": "text", "content_text": "# Tree"},
              files=pdf)
    with Session(engine) as db:
        tree = db.exec(select(SkillTree)).one()
        assert tree.content == "# Tree" and tree.view_file_id is not None


def test_people_see_the_user_friendly_version_first(engine, taro, course, create_agent):
    agent_id = create_agent(taro, course, "helper")
    taro.post(f"/classes/{course}/skill-trees", data={"agent_id": agent_id, "mode": "text", "content_text": "# AI tree"})
    page = taro.get(f"/skill-trees/{agent_id}").text
    assert "# AI tree" in page and "<iframe" not in page and "add one" in page  # only the AI version so far
    # Later, just the user-friendly version (the AI version is kept).
    taro.post(f"/classes/{course}/skill-trees", data={"agent_id": agent_id, "mode": "file"},
              files={"view": ("tree.html", b"<h1>My tree</h1>", "text/html")})
    [stored] = tree_files(engine)
    page = taro.get(f"/skill-trees/{agent_id}").text
    assert f'src="/files/{stored.id}/preview"' in page and "Show the AI version (.md)" in page and "# AI tree" in page
    assert stored.content_type == "text/html"
    preview = taro.get(f"/files/{stored.id}/preview")
    assert preview.status_code == 200 and "sandbox" in preview.headers["content-security-policy"]
    # Replacing it deletes the old file; an image is shown directly.
    taro.post(f"/classes/{course}/skill-trees", data={"agent_id": agent_id, "mode": "file"},
              files={"view": ("tree.png", b"png", "image/png")})
    [stored] = tree_files(engine)
    assert stored.filename == "tree.png" and f'<img src="/files/{stored.id}"' in taro.get(f"/skill-trees/{agent_id}").text
    with Session(engine) as db:
        assert db.exec(select(SkillTree)).one().content == "# AI tree"


def test_user_friendly_versions_are_checked_and_protected(engine, taro, make_person, course, create_agent):
    agent_id = create_agent(taro, course, "helper")
    taro.post(f"/classes/{course}/skill-trees", data={"agent_id": agent_id, "mode": "text", "content_text": "# T"})
    for name, data in (("tree.docx", b"x"), ("fake.pdf", b"<html>")):
        reply = taro.post(f"/classes/{course}/skill-trees", data={"agent_id": agent_id, "mode": "file"},
                          files={"view": (name, data, "application/octet-stream")})
        assert reply.status_code == 400
    taro.post(f"/classes/{course}/skill-trees", data={"agent_id": agent_id, "mode": "file"},
              files={"view": ("tree.pdf", b"%PDF-1.4 t", "application/pdf")})
    [stored] = tree_files(engine)
    assert make_person("guest").get(f"/files/{stored.id}").status_code == 404  # members only
    hanako = make_person("student", "hanako", gen=9)
    assert hanako.get(f"/files/{stored.id}").status_code == 200
    assert hanako.post(f"/skill-trees/{agent_id}/view/delete").status_code == 403
    assert taro.post(f"/skill-trees/{agent_id}/view/delete").status_code == 200
    assert tree_files(engine) == []
    with Session(engine) as db:
        assert db.exec(select(SkillTree)).one().content == "# T"


def test_agents_set_the_user_friendly_version_after_the_ai_one(engine, client, taro, course, create_agent):
    agent_id = create_agent(taro, course, "helper")
    pdf = {"file": ("tree.pdf", b"%PDF-1.4 tree", "application/pdf")}
    assert client.put("/api/skill-tree/view", files=pdf, headers=api("helper")).status_code == 409
    client.put("/api/skill-tree", json={"content": "# Tree"}, headers=api("helper"))
    made = client.put("/api/skill-tree/view", files=pdf, headers=api("helper")).json()
    assert made["view"]["filename"] == "tree.pdf"
    read = client.get(f"/api/skill-trees/{agent_id}", headers=api("helper")).json()
    assert read["content"] == "# Tree" and read["view"]["content_type"] == "application/pdf"
    assert client.get(f"/api/skill-trees/{agent_id}/view", headers=api("helper")).content == b"%PDF-1.4 tree"
    # Deleting the whole tree deletes the file too.
    assert client.delete("/api/skill-tree", headers=api("helper")).json() == {"deleted": 1}
    assert tree_files(engine) == []


def test_deleting_an_agent_deletes_its_tree_files(engine, client, taro, course, create_agent):
    from conftest import PASSWORD

    agent_id = create_agent(taro, course, "helper")
    client.put("/api/skill-tree", json={"content": "# Tree"}, headers=api("helper"))
    client.put("/api/skill-tree/view", files={"file": ("t.png", b"png", "image/png")}, headers=api("helper"))
    taro.post(f"/agents/{agent_id}/delete", data={"password": PASSWORD})
    assert tree_files(engine) == []


def test_deleting_an_agent_keeps_its_skills_credited(engine, client, taro, course, create_agent):
    from conftest import PASSWORD

    agent_id = create_agent(taro, course, "helper")
    client.post("/api/skills", json={"content": RETRIEVAL}, headers=api("helper"))
    client.put("/api/skill-tree", json={"content": "# Tree"}, headers=api("helper"))
    taro.post(f"/agents/{agent_id}/delete", data={"password": PASSWORD})
    [skill] = skills(engine)
    assert skill.agent_id is None and skill.agent_name == "helper"
    with Session(engine) as db:
        assert db.exec(select(SkillTree)).all() == []


# --- The organizer ------------------------------------------------------------------------------


def test_keyword_rules_sort_skills_by_function(engine, admin, taro, course):
    share(taro, course, name="Finder", content=RETRIEVAL)
    share(taro, course, name="Chooser", content=DECISION)
    reply = admin.post("/market/organize")
    assert "Organized 2 skills" in reply.text
    with Session(engine) as db:
        names = {s.name: db.get(SkillCategory, s.category_id).name for s in skills(engine)}
        assert db.exec(select(OrganizerRun)).one().method == "rules"
    assert names == {"Finder": "Data retrieval", "Chooser": "Decision frameworks"}
    grouped_view = market_html(taro, course)
    assert grouped_view.index("Data retrieval") < grouped_view.index("Decision frameworks")
    assert "Find and fetch information" in grouped_view  # each category's description on top
    with Session(engine) as db:
        decision = db.exec(select(SkillCategory).where(SkillCategory.name == "Decision frameworks")).one().id
    chosen = market_html(taro, course, category=str(decision))
    assert "Help an agent choose" in chosen and "Chooser" in chosen and "Finder" not in chosen
    assert taro.post("/market/organize").status_code == 403  # admins only


def ai_reply(skill_ids):
    return ("Here you go:\n```json\n" + json.dumps({
        "categories": [{"name": "Finding things", "description": "Skills that look things up."},
                       {"name": "Making choices", "description": "Skills that decide."}],
        "skills": [{"id": skill_ids[0], "category": "Finding things", "specialty": "Only arXiv, but thorough."},
                   {"id": skill_ids[1], "category": "Making choices", "specialty": "Scores options with weights."}],
    }) + "\n```")


def test_the_ai_organizer_uses_one_request_and_applies_its_answer(engine, taro, course, monkeypatch):
    share(taro, course, name="Finder", content=RETRIEVAL)
    share(taro, course, name="Chooser", content=DECISION)
    ids = [s.id for s in skills(engine)]
    sent = []

    class Reply:
        ok = True

        def json(self):
            return {"choices": [{"message": {"content": ai_reply(ids)}}]}

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-real")
    monkeypatch.setattr(market.requests, "post", lambda url, **kwargs: sent.append((url, kwargs)) or Reply())
    with Session(engine) as db:
        record = market.run(db, "manual")
    assert record.ok and record.method == "ai" and len(sent) == 1
    url, kwargs = sent[0]
    assert url == market.OPENROUTER_URL and kwargs["json"]["model"] == "openrouter/free"
    assert "BY FUNCTION" in kwargs["json"]["messages"][0]["content"] and "Search arXiv" in kwargs["json"]["messages"][0]["content"]
    with Session(engine) as db:
        finder = db.get(Skill, ids[0])
        assert db.get(SkillCategory, finder.category_id).name == "Finding things"
        assert finder.specialty == "Only arXiv, but thorough."
    assert "Skills that look things up." in market_html(taro, course)


def test_a_bad_ai_answer_changes_nothing_and_the_daily_limit_holds(engine, taro, course, monkeypatch):
    share(taro, course)
    calls = []

    class Reply:
        ok = True

        def json(self):
            return {"choices": [{"message": {"content": "Sorry, I can't help with that."}}]}

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-real")
    monkeypatch.setattr(market.requests, "post", lambda url, **kwargs: calls.append(url) or Reply())
    with Session(engine) as db:
        first = market.run(db, "manual")
        assert not first.ok and "no JSON" in first.message
        assert skills(engine)[0].category_id is None  # nothing changed
        market.run(db, "manual")
        third = market.run(db, "manual")
    assert len(calls) == 2  # at most two requests a day
    assert "already ran 2 times today" in third.message


def jst(hour, minute=0):
    return datetime(2026, 10, 30, hour, minute, tzinfo=JST)


def test_the_schedule_always_runs_at_3pm_and_once_more_only_if_needed(engine, taro, course, monkeypatch):
    with Session(engine) as db:
        assert market.due(db, jst(14, 59)) is None  # before 15:00 with nothing to organize
        assert market.due(db, jst(15, 0)) == "3 pm"
        assert market.due(db, jst(22, 0)) == "3 pm"  # a sleeping server catches up when it wakes
        monkeypatch.setattr(market, "jst_now", lambda: jst(15, 1))
        market.run(db, "3 pm")
        assert market.due(db, jst(18, 0)) is None  # done for today
    share(taro, course)  # something new...
    with Session(engine) as db:
        next_morning = datetime(2026, 10, 31, 4, 0, tzinfo=JST)
        assert market.due(db, next_morning) == "morning"  # ...so the optional second update runs
        assert market.due(db, datetime(2026, 10, 31, 2, 0, tzinfo=JST)) is None  # not before 03:00


def test_the_daily_timer_needs_its_token(client, monkeypatch):
    monkeypatch.setenv("ORGANIZER_TOKEN", "test-organizer-token-0123456789")
    assert client.post("/internal/organize-market").status_code == 401
    assert client.post("/internal/organize-market", headers={"X-Organizer-Token": "wrong"}).status_code == 401
    reply = client.post("/internal/organize-market", headers={"X-Organizer-Token": "test-organizer-token-0123456789"})
    assert reply.status_code == 200 and "ran" in reply.json()
    monkeypatch.setenv("ORGANIZER_TOKEN", "")
    assert client.post("/internal/organize-market", headers={"X-Organizer-Token": ""}).status_code == 401
