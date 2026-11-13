"""Linked dashboards: students list where their work lives outside the
dashboard; the dashboard checks each link (only on the public internet)."""

import socket
import threading
from datetime import timedelta

import pytest
from sqlmodel import Session, select

import links
from conftest import PASSWORD
from db import ExternalLink, utc_now


@pytest.fixture
def course(create_class):
    return create_class(enrolled_gens=[9])


@pytest.fixture
def taro(make_person):
    return make_person("student", "taro", "Yamada Taro", gen=9)


@pytest.fixture
def answers(monkeypatch):
    """What the (fake) internet answers for each address; "ok" by default.
    No test reaches the real network."""
    found: dict[str, links.Result] = {}
    monkeypatch.setattr(links, "fetch", lambda url: found.get(url, links.Result("ok", "reachable (200)")))
    return found


def add(person, course, **form):
    data = {"url": "https://example.com/site", "label": "Valuation site", "kind": "site", "access": "public"} | form
    return person.post(f"/classes/{course}/links", data=data, follow_redirects=False)


def all_links(engine):
    with Session(engine) as db:
        return db.exec(select(ExternalLink).order_by(ExternalLink.id)).all()


def test_owner_adds_links_members_see_them_guests_dont(engine, taro, make_person, guest, course, create_agent, answers):
    agent = create_agent(taro, course, "nowcaster")
    reply = add(taro, course)
    assert reply.status_code == 303 and reply.headers["location"].startswith(f"/classes/{course}/agents/{taro.id}")
    reply = add(taro, course, url="https://claude.ai/artifact/x", label="Report", kind="artifact", access="keio",
                agent_id=str(agent))
    assert reply.headers["location"].startswith(f"/agents/{agent}")
    saved = all_links(engine)
    assert [(l.label, l.agent_id, l.result) for l in saved] == [("Valuation site", None, "ok"), ("Report", agent, "ok")]

    hanako = make_person("student", "hanako", gen=9)
    grid = hanako.get(f"/classes/{course}/agents/{taro.id}").text
    assert "Linked dashboards" in grid and "Valuation site" in grid and "Report" in grid and "Keio login" in grid
    assert "+ Add link" not in grid and "Check now" not in grid  # read-only for others
    page = hanako.get(f"/agents/{agent}").text
    assert "Report" in page and "Valuation site" not in page  # an agent's page shows its own links
    assert "Valuation site" not in guest.get(f"/classes/{course}/agents/{taro.id}").text
    assert "+ Add link" in taro.get(f"/classes/{course}/agents/{taro.id}").text


def test_only_the_owner_changes_links_and_only_for_their_own_agents(engine, taro, make_person, course, create_agent,
                                                                   answers):
    hanako = make_person("student", "hanako", gen=9)
    theirs = create_agent(hanako, course, "theirs")
    assert add(taro, course, agent_id=str(theirs)).status_code == 403
    add(taro, course)
    link = all_links(engine)[0]
    for action in ("check", "archive", "restore", "delete"):
        assert hanako.post(f"/links/{link.id}/{action}").status_code == 403
    assert "javascript" not in add(taro, course, url="javascript:alert(1)").headers["location"]
    assert "link_error" in add(taro, course, url="javascript:alert(1)").headers["location"]
    assert len(all_links(engine)) == 1


def test_archive_restore_delete(engine, taro, course, answers):
    add(taro, course)
    link = all_links(engine)[0]
    taro.post(f"/links/{link.id}/archive")
    assert all_links(engine)[0].archived_at is not None
    assert "Archived" in taro.get(f"/classes/{course}/agents/{taro.id}").text
    taro.post(f"/links/{link.id}/restore")
    assert all_links(engine)[0].archived_at is None
    taro.post(f"/links/{link.id}/delete")
    assert all_links(engine) == []


def test_two_failures_make_a_link_broken_and_public_login_pages_are_flagged(engine, taro, course, answers):
    answers["https://example.com/gone"] = links.Result("failed", "HTTP 404")
    answers["https://example.com/private"] = links.Result("login", "asks for a login (401)")
    add(taro, course, url="https://example.com/gone", label="Old prototype")
    add(taro, course, url="https://example.com/private", label="Private", access="public")
    gone, private = all_links(engine)
    assert links.status(gone)["key"] == "failing"
    with Session(engine) as db:
        db.exec(select(ExternalLink)).all()
        links.check(db, db.get(ExternalLink, gone.id))
    grid = taro.get(f"/classes/{course}/agents/{taro.id}").text
    assert "Broken: failed 2 checks in a row" in grid
    assert "Marked Public, but it asks for a login" in grid
    assert all_links(engine)[1].access == "public"  # flagged, never changed by the check


def test_daily_check_takes_links_not_checked_for_a_day(engine, taro, course, answers):
    for name in ("a", "b", "c"):
        add(taro, course, url=f"https://example.com/{name}")
    with Session(engine) as db:
        a, b, c = db.exec(select(ExternalLink).order_by(ExternalLink.id)).all()
        a.checked_at = utc_now() - timedelta(days=2)
        c.checked_at = utc_now() - timedelta(days=2)
        c.archived_at = utc_now()
        db.add(a)
        db.add(c)
        db.commit()
        assert links.check_due(db) == 1  # a only: b was checked just now, c is archived


def test_deleting_an_agent_keeps_its_links_on_the_dashboard(engine, taro, course, create_agent, answers):
    agent = create_agent(taro, course, "nowcaster")
    add(taro, course, agent_id=str(agent))
    taro.post(f"/agents/{agent}/delete", data={"password": PASSWORD})
    assert [(l.agent_id, l.label) for l in all_links(engine)] == [(None, "Valuation site")]


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://localhost/admin", "http://10.0.0.5/", "http://[::1]/",
                                 "http://169.254.169.254/latest/meta-data/", "http://192.168.1.1/",
                                 "https://example.com:22/", "ftp://example.com/", "https://user:pw@example.com/",
                                 "file:///etc/passwd"])
def test_the_check_never_fetches_private_or_odd_addresses(url):
    assert links.allowed(url) is not None
    assert links.fetch(url).result == "failed"


def test_the_address_actually_connected_to_is_checked_too(monkeypatch):
    """Even if a name passed the first look (say it resolved to a public
    address, then to this computer), the connection itself is refused."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    got = []
    threading.Thread(target=lambda: got.append(server.accept()), daemon=True).start()
    monkeypatch.setattr(links, "allowed", lambda url: None)
    try:
        found = links.fetch(f"http://127.0.0.1:{port}/")
    finally:
        server.close()
    assert (found.result, found.note) == ("failed", "not a public address")
