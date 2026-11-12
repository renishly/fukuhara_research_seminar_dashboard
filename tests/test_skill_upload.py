"""Raiyaan's skill sorter: an agent's skills zip, checked, sorted, confirmed, published."""

import io
import json
import zipfile

import pytest
from sqlmodel import Session, select

from db import Skill, SkillCategory, SkillUpload


@pytest.fixture
def course(create_class):
    return create_class(enrolled_gens=[9])


@pytest.fixture
def taro(make_person):
    return make_person("student", "taro", "Yamada Taro", gen=9)


def zip_of(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buffer.getvalue()


def skill_md(name, description, extra=""):
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n# {name}\n\n{description}\n"


GOOD = zip_of({
    "my-agent/skills/ir-extract/SKILL.md": skill_md("ir-extract", "Extract EPS and net debt from the IR page and yuho."),
    "my-agent/skills/dcf-value/SKILL.md": skill_md("dcf-value", "Value per share from a DCF with WACC from CAPM.",
                                                   "reusability: generic\n"),
    "my-agent/skills/greeter/SKILL.md": skill_md("greeter", "Tell a joke to cheer everyone up."),
    "my-agent/skills/leaky/SKILL.md": skill_md("leaky", "Fetch prices.", "") + "\napi_key = 'sk-abcdefghijklmnopqrstuvwxyz123456'\n",
    "my-agent/skills/ir-extract/scripts/run.py": "print('never run')\n",
})


def upload(person, agent, data, name="skills.zip"):
    return person.post(f"/agents/{agent}/skills/zip", files={"file": (name, data, "application/zip")})


def test_a_good_zip_is_sorted_and_published_after_the_owner_checks_it(engine, taro, course, create_agent):
    agent = create_agent(taro, course, "helper")
    screen = upload(taro, agent, GOOD).text
    assert "Check your skills" in screen and "4 skills" in screen and "1</strong> needs your choice" in screen
    assert "blocked" in screen and "possible API key" in screen  # the leaky one can't be published
    with Session(engine) as db:
        pending = db.exec(select(SkillUpload)).one()
        assert db.exec(select(Skill).where(Skill.agent_id == agent)).all() == []  # nothing published yet
        order = [s["name"] for s in pending.skills]
    form = {f"include_{i}": "true" for i in range(4)}
    form |= {f"category_{order.index('greeter')}": "9", f"scope_{order.index('dcf-value')}": "common"}
    form |= {f"category_{order.index('ir-extract')}": "1", f"category_{order.index('dcf-value')}": "6"}
    reply = taro.post(f"/agents/{agent}/skills/zip/{pending.id}/publish", data=form)
    assert reply.headers["HX-Redirect"] == f"/classes/{course}?tab=skills"
    with Session(engine) as db:
        published = {s.name: (db.get(SkillCategory, s.category_id).name, s.scope, s.category_source)
                     for s in db.exec(select(Skill).where(Skill.agent_id == agent)).all()}
        assert db.exec(select(SkillUpload)).all() == []
    assert published == {"ir-extract": ("Public data collection", "personal", "owner"),
                         "dcf-value": ("Valuation", "common", "owner"),
                         "greeter": ("Operations & publishing", "personal", "owner")}  # not "leaky"


def test_a_bad_zip_is_rejected_with_every_problem(taro, course, create_agent):
    agent = create_agent(taro, course, "helper")
    bad = zip_of({"notes.txt": "hi", "skills/My Skill/SKILL.md": "no frontmatter", "skills/x/SKILL.md": "---\nname: y\n---\n"})
    screen = upload(taro, agent, bad).text
    assert "can&#39;t be used yet" in screen or "can't be used yet" in screen
    assert "S1" in screen and "S3" in screen
    assert "That isn" in upload(taro, agent, b"not a zip at all").text


def test_only_the_owner_uploads(taro, make_person, course, create_agent):
    hanako = make_person("student", "hanako", gen=9)
    agent = create_agent(taro, course, "helper")
    assert upload(hanako, agent, GOOD).status_code == 403


def test_the_class_skill_map_shows_counts_gaps_and_compares(engine, client, taro, make_person, course, create_agent):
    from conftest import api
    hanako = make_person("student", "hanako", "Sato Hanako", gen=9)
    agent = create_agent(taro, course, "helper")
    client.post("/api/state", json={"check_in_every": 86400}, headers=api("helper"))  # live
    upload(taro, agent, GOOD)
    with Session(engine) as db:
        pending = db.exec(select(SkillUpload)).one()
        order = [s["name"] for s in pending.skills]
    taro.post(f"/agents/{agent}/skills/zip/{pending.id}/publish",
              data={f"include_{order.index('ir-extract')}": "true", f"include_{order.index('dcf-value')}": "true",
                    f"category_{order.index('ir-extract')}": "1", f"category_{order.index('dcf-value')}": "6"})
    page = hanako.get(f"/classes/{course}/skill-map").text
    assert "2</span><span class=\"text-xs\">skills built or live" in page and "100%" in page  # its agent is live
    # The graph: 10 category nodes on one path (9 links), 8 of them empty; 2 live skill nodes, each on an inner path.
    assert page.count('<g class="sg-cat ') == 10 and page.count('class="sg-main"') == 9
    assert page.count("sg-cat-empty") == 8 and page.count("data-sg-skill=") == 2 and page.count('class="sg-inner"') == 2
    assert page.count('class="sg-live"') == 2 and page.count('class="sg-new"') == 2  # new to Hanako
    assert "sg-new" not in taro.get(f"/classes/{course}/skill-map").text  # not to their owner
    # The hover label's data: the skill's name and its creator ("you" for your own).
    assert 'data-name="dcf-value"' in page and 'data-owner-name="Yamada Taro"' in page and "data-sg-tip" in page
    assert 'data-owner-name="you"' in taro.get(f"/classes/{course}/skill-map").text
    node = page[page.index('data-sg-skill="'):]
    assert "<title>" not in node[:node.index("</g>")]  # no slow browser tooltip on skill nodes
    category = hanako.get(f"/classes/{course}/skill-map", params={"category": 6}).text
    assert "6 &middot; Valuation" in category and "dcf-value" in category and "nothing in this category" in category
    compared = hanako.get(f"/classes/{course}/skill-map", params={"a": taro.id, "b": hanako.id}).text
    assert "Sato Hanako has nothing here: Yamada Taro could share" in compared and "Gap for both" in compared
    assert 'hx-get="/classes/' in taro.get(f"/classes/{course}", params={"tab": "skills"}).text


def test_votes_size_nodes_and_checking_clears_the_new_mark(engine, taro, make_person, course, create_agent):
    hanako = make_person("student", "hanako", "Sato Hanako", gen=9)
    taro.post(f"/classes/{course}/skills", data={"name": "dcf-value", "share": "class", "mode": "text",
                                                 "content_text": "# dcf-value\n\nValue per share from a DCF."})
    with Session(engine) as db:
        skill = db.exec(select(Skill).where(Skill.builtin.is_(None))).one().id
    tab = hanako.get(f"/classes/{course}/tab/skills").text
    assert 'class="tab-new"' in tab and "Mark all as checked (1)" in tab
    assert 'class="skill-new"' in hanako.get(f"/classes/{course}/skills").text
    popup = hanako.get(f"/skills/{skill}").text  # opening it = checking it
    assert "Rate this skill" in popup
    assert 'class="tab-new"' not in hanako.get(f"/classes/{course}/tab/skills").text
    assert ">±0<" in hanako.get(f"/classes/{course}/skills").text  # the market always shows the total
    hanako.post(f"/skills/{skill}/vote", data={"value": 1})
    voted = taro.post(f"/skills/{skill}/vote", data={"value": 1})
    assert "+2" in voted.text
    assert json.loads(voted.headers["HX-Trigger"]) == {"skill-voted": {"id": skill, "score": 2, "r": 14.4}}
    graph = hanako.get(f"/classes/{course}/skill-map").text
    assert 'r="14.4"' in graph  # +2 makes the node bigger than 11
    hanako.post(f"/skills/{skill}/vote", data={"value": 1})  # the same vote again takes it back
    taro.post(f"/skills/{skill}/vote", data={"value": -1})  # changing it
    assert "▼-1" in hanako.get(f"/classes/{course}/skills").text
    # Updating it makes it "updated" for Hanako again.
    taro.post(f"/skills/{skill}/edit", data={"summary": "", "share": "class", "mode": "text",
                                            "content_text": "# dcf-value\n\nValue per share from a DCF, v2."})
    assert 'aria-label="Updated: not checked yet"' in hanako.get(f"/classes/{course}/skills").text


def test_a_drawn_path_downloads_as_a_zip_by_category(engine, taro, course):
    for name, text in (("ir-extract", "Extract EPS and net debt from the IR page and yuho."),
                       ("dcf-value", "Value per share from a DCF with WACC from CAPM.")):
        taro.post(f"/classes/{course}/skills", data={"name": name, "share": "class", "mode": "text",
                                                     "content_text": f"# {name}\n\n{text}"})
    with Session(engine) as db:
        ids = {s.name: s.id for s in db.exec(select(Skill)).all()}
    reply = taro.post(f"/classes/{course}/skills/bundle", data={"ids": [ids["ir-extract"], ids["dcf-value"]]})
    assert reply.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(reply.content)) as zf:
        names = sorted(zf.namelist())
        readme = zf.read("README.md").decode()
    assert names == ["01-public-data-collection/ir-extract/SKILL.md", "06-valuation/dcf-value/SKILL.md", "README.md"]
    assert readme.index("1. ir-extract") < readme.index("2. dcf-value")  # the order it was drawn


def test_graph_nodes_stay_inside_their_category_and_misc_ones_outside(engine, taro, course, create_agent):
    import math
    import skillmap
    from security import Viewer
    agent = create_agent(taro, course, "helper")
    upload(taro, agent, GOOD)  # dcf-value, ir-extract, greeter (no category), leaky (blocked)
    with Session(engine) as db:
        pending = db.exec(select(SkillUpload)).one()
    taro.post(f"/agents/{agent}/skills/zip/{pending.id}/publish", data={f"include_{i}": "true" for i in range(4)})
    with Session(engine) as db:
        from db import Course, User
        viewer = Viewer(session=None, user=db.exec(select(User).where(User.login_id == "taro")).one())
        g = skillmap.graph(db, viewer, db.get(Course, course), "abc1")
        g2 = skillmap.graph(db, viewer, db.get(Course, course), "abc1")
        g3 = skillmap.graph(db, viewer, db.get(Course, course), "ffff")
        layouts = [skillmap.graph(db, viewer, db.get(Course, course), f"{seed:x}") for seed in range(30)]
    assert [c["cy"] for c in g["categories"]] == [c["cy"] for c in g2["categories"]]  # same layout: same picture
    assert [c["cy"] for c in g["categories"]] != [c["cy"] for c in g3["categories"]]  # a new load: a new one
    assert len({round(c["cy"]) for c in g["categories"]}) > 1  # not a straight line
    for layout in layouts:  # in every random layout:
        for c in layout["categories"]:
            for n in c["skills"]:  # skill nodes inside their category
                assert math.hypot(n["x"] - c["cx"], n["y"] - c["cy"]) + n["r"] <= c["r"] + 0.5
        assert [n["name"] for n in layout["misc"]] == ["greeter"]
        for n in layout["misc"]:  # miscellaneous ones outside every category, and on the canvas
            assert 0 < n["y"] - n["r"] and n["y"] + n["r"] < layout["height"]
            for c in layout["categories"]:
                assert math.hypot(n["x"] - c["cx"], n["y"] - c["cy"]) >= c["r"] + n["r"]


def test_paths_must_follow_the_map_left_to_right(engine, taro, course, create_agent):
    import skillmap
    following = {1: [2], 2: [10, 20], 10: [], 20: []}
    assert skillmap.traversable(following, [1, 2, 20]) and not skillmap.traversable(following, [2, 1])
    agent = create_agent(taro, course, "helper")
    upload(taro, agent, GOOD)
    with Session(engine) as db:
        pending = db.exec(select(SkillUpload)).one()
    taro.post(f"/agents/{agent}/skills/zip/{pending.id}/publish", data={f"include_{i}": "true" for i in range(4)})
    with Session(engine) as db:
        ids = {s.name: s.id for s in db.exec(select(Skill).where(Skill.builtin.is_(None))).all()}
    # ir-extract (category 1) then dcf-value (category 6): along the main path, left to right.
    ok = taro.post(f"/classes/{course}/skills/bundle", data={"ids": [ids["ir-extract"], ids["dcf-value"]]})
    assert ok.status_code == 200
    backwards = taro.post(f"/classes/{course}/skills/bundle", data={"ids": [ids["dcf-value"], ids["ir-extract"]]})
    assert backwards.status_code == 400 and "can't be followed" in backwards.text
    page = taro.get(f"/classes/{course}/skill-map").text
    assert "data-sg-next='{" in page


def test_each_page_load_gets_a_new_layout_kept_while_navigating(taro, course):
    import re as regex
    first = taro.get(f"/classes/{course}/skill-map").text
    second = taro.get(f"/classes/{course}/skill-map").text
    seed = regex.search(r"layout=([0-9a-f]+)&", first).group(1)
    assert seed != regex.search(r"layout=([0-9a-f]+)&", second).group(1)
    again = taro.get(f"/classes/{course}/skill-map", params={"layout": seed}).text
    assert regex.findall(r'<circle cx="[^"]+" cy="([^"]+)" r="[^"]+"/>', again)[:10] == \
        regex.findall(r'<circle cx="[^"]+" cy="([^"]+)" r="[^"]+"/>', first)[:10]
