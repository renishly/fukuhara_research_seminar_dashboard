"""Jimin's standard forecasts: agents send them weekly; a class overview and an agent tab show them."""

from datetime import timedelta

import pytest
from sqlmodel import Session, select

from conftest import api
from db import Output, utc_now
from forecasts import parse, weekly


@pytest.fixture
def course(create_class):
    return create_class(enrolled_gens=[9])


@pytest.fixture
def taro(make_person):
    return make_person("student", "taro", "Yamada Taro", gen=9)


def send(client, name, **content):
    body = {"target": "Mitsui Fudosan (8801)", "value": 1205, "unit": "JPY"} | content
    return client.post("/api/outputs", json={"category": "Forecast", "content": body}, headers=api(name))


def test_one_forecast_per_week_newest_counts_and_bad_ones_are_ignored(engine, client, taro, course, create_agent):
    agent = create_agent(taro, course, "mitsui")
    send(client, "mitsui", week="2026-W45", value=1286)
    send(client, "mitsui", week="2026-W46", value=1300)
    send(client, "mitsui", week="2026-W46", value=1205, view="Underweight", market=1470,
         why_changed="Fama-French cost of equity rose", evidence=[{"title": "Peers", "url": "https://example.com/p"},
                                                                 {"title": "Bad link", "url": "javascript:alert(1)"}])
    send(client, "mitsui", value="not a number")  # ignored
    client.post("/api/outputs", json={"category": "Forecast", "content": "just text"}, headers=api("mitsui"))  # ignored
    with Session(engine) as db:
        weeks = weekly(db, [agent])[agent]
    assert [(f.week, f.value) for f in weeks] == [("2026-W46", 1205.0), ("2026-W45", 1286.0)]
    this = weeks[0]
    assert this.view == "Underweight" and round(this.vs_market, 1) == -18.0
    assert this.evidence == [("Peers", "https://example.com/p"), ("Bad link", None)]  # unsafe links dropped
    assert this.week_label == "Week of Nov 9, 2026"


def test_class_overview_and_agent_tab(engine, client, taro, make_person, guest, course, create_agent):
    hanako = make_person("student", "hanako", gen=9)
    agent = create_agent(taro, course, "mitsui")
    create_agent(taro, course, "quiet")  # sends no forecasts: not listed, no tab
    send(client, "mitsui", week="2026-W45", value=1286)
    send(client, "mitsui", week="2026-W46", value=1205, view="Underweight", why_changed="Cost of equity rose")
    tab = hanako.get(f"/classes/{course}", params={"tab": "forecasts"}).text
    assert "Mitsui Fudosan (8801)" in tab and "1,205" in tab and "1,286" in tab
    assert "▼" in tab and "-81" in tab and "(-6.3%)" in tab and "quiet" not in tab
    # Per person: Hanako has no forecasting agents.
    assert ">No forecast<" in tab and tab.count("<tbody>") == 2
    assert f'href="/agents/{agent}#forecast"' in tab
    page = hanako.get(f"/agents/{agent}").text
    assert "Forecast</button>" in page and "Why it changed" in page and "Cost of equity rose" in page
    quiet_page = hanako.get(f"/agents/{agent + 1}").text
    assert "Forecast</button>" not in quiet_page
    assert "forecasts" not in guest.get(f"/classes/{course}").text.split('role="tablist"')[1].split("</div>")[0]


def test_a_person_with_several_forecasting_agents_gets_a_line_each(client, taro, course, create_agent):
    create_agent(taro, course, "mitsui")
    create_agent(taro, course, "mufg")
    send(client, "mitsui", week="2026-W46", value=1205)
    send(client, "mufg", target="MUFG (8306)", week="2026-W46", value=3517)
    tab = taro.get(f"/classes/{course}", params={"tab": "forecasts"}).text
    assert 'rowspan="2"' in tab and "2 forecasts" in tab and "Mitsui Fudosan (8801)" in tab and "MUFG (8306)" in tab
    assert ">You" in tab
