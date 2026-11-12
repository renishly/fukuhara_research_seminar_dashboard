"""Pages that refresh themselves every few seconds stay cheap: no database
writes when nothing changed, and no work for parts they don't show."""

import re

import pytest
from sqlalchemy import event

from conftest import api


@pytest.fixture
def course(create_class):
    return create_class(enrolled_gens=[9])


@pytest.fixture
def taro(make_person):
    return make_person("student", "taro", "Yamada Taro", gen=9)


@pytest.fixture
def statements(engine):
    seen = []
    listener = lambda conn, cursor, statement, *rest: seen.append(statement.lstrip().split(" ")[0].upper())  # noqa: E731
    event.listen(engine, "before_cursor_execute", listener)
    yield seen
    event.remove(engine, "before_cursor_execute", listener)


def writes(seen):
    return [s for s in seen if s in ("INSERT", "UPDATE", "DELETE")]


def test_refreshing_when_nothing_changed_writes_nothing(client, taro, course, create_agent, statements):
    create_agent(taro, course, "park")
    client.post("/api/state", json={"check_in_every": 86400}, headers=api("park"))
    for url in (f"/classes/{course}/window/agents", f"/classes/{course}/feed", f"/classes/{course}/skill-map"):
        taro.get(url)  # the first look may note things
    statements.clear()
    for url in (f"/classes/{course}/window/agents", f"/classes/{course}/feed", f"/classes/{course}/skill-map",
                f"/classes/{course}/agents/{taro.id}/grid", f"/classes/{course}/bar/assignments"):
        assert taro.get(url).status_code in (200, 204)
    assert writes(statements) == []


def test_the_window_refresh_skips_the_feed_and_the_page_skips_the_connector_text(taro, course, statements):
    statements.clear()
    window = taro.get(f"/classes/{course}/window/agents").text
    assert "Live feed" not in window and "lf-chip" not in window
    page = taro.get(f"/classes/{course}/agents/{taro.id}").text
    assert "def check_in(" not in page and 'hx-get="/connector/code/preview"' in page  # loaded only when opened
    code = taro.get("/connector/code/preview").text
    assert "def check_in(" in code and taro.get("/connector/nope/preview").status_code == 404
    assert re.search(r"&(lt|gt|quot|#x27|amp);", code)  # escaped, shown as text
