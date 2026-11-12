"""The OB/OG directory (Kenichi): members only, consent required, a visit log."""

from sqlmodel import Session, select

from db import Alumnus, AlumnusContact


def add(person, **fields):
    data = {"name": "Kato Ken", "year": "2021", "company": "Mizuho", "industry": "Finance",
            "email": "ken@example.com", "phone": "090-1234-5678", "prefers": "email", "consent": "true"} | fields
    return person.post("/alumni", data=data)


def test_members_add_and_see_guests_never(engine, admin, guest, make_person):
    taro = make_person("student", "taro", gen=9)
    assert add(taro).status_code == 200  # (followed the redirect back to the list)
    page = taro.get("/alumni").text
    assert "Kato Ken" in page and 'href="mailto:ken@example.com"' in page and 'href="tel:090-1234-5678"' in page
    assert ">OB/OG</a>" in page  # in the top bar
    assert guest.get("/alumni").status_code == 403
    assert ">OB/OG</a>" not in guest.get("/classes").text


def test_consent_and_a_way_to_reach_them_are_required(engine, make_person):
    taro = make_person("student", "taro", gen=9)
    assert "tick the consent box" in add(taro, consent="").text
    assert "at least an email address or a phone number" in add(taro, email="", phone="").text
    assert "look right" in add(taro, email="not-an-email").text
    with Session(engine) as db:
        assert db.exec(select(Alumnus)).all() == []


def test_only_the_adder_or_an_admin_changes_an_entry(engine, admin, make_person):
    taro, hanako = make_person("student", "taro", gen=9), make_person("student", "hanako", gen=9)
    add(taro)
    with Session(engine) as db:
        entry = db.exec(select(Alumnus)).one().id
    edit = {"name": "Kato Ken", "company": "Nomura", "email": "ken@example.com", "consent": "true"}
    assert hanako.post(f"/alumni/{entry}/edit", data=edit).status_code == 403
    taro.post(f"/alumni/{entry}/edit", data=edit)
    with Session(engine) as db:
        assert db.get(Alumnus, entry).company == "Nomura"
    assert hanako.post(f"/alumni/{entry}/delete").status_code == 403
    admin.post(f"/alumni/{entry}/delete")
    with Session(engine) as db:
        assert db.exec(select(Alumnus)).all() == []


def test_visit_requests_are_logged_and_flagged_to_the_next_member(engine, make_person):
    taro, hanako = make_person("student", "taro", "Yamada Taro", gen=9), make_person("student", "hanako", gen=9)
    add(taro)
    with Session(engine) as db:
        entry = db.exec(select(Alumnus)).one().id
    taro.post(f"/alumni/{entry}/contact")
    page = hanako.get("/alumni").text
    assert "Yamada Taro" in page and "Check with them first" in page and "1</span><span class=\"text-xs\">visit requests" in page
    with Session(engine) as db:
        assert len(db.exec(select(AlumnusContact)).all()) == 1


def test_search_and_industry_filter(make_person):
    taro = make_person("student", "taro", gen=9)
    add(taro)
    add(taro, name="Sato Ai", company="Google", industry="Tech", email="ai@example.com")
    assert "Sato Ai" not in taro.get("/alumni", params={"industry": "Finance"}).text
    assert "Kato Ken" not in taro.get("/alumni", params={"q": "google"}).text
    assert "Sato Ai" in taro.get("/alumni", params={"q": "google"}).text
