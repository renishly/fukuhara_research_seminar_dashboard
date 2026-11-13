"""Data points: an agent declares its setup in a "Data points" monitor; its
helper agents' data points are added in, each named one counted once."""

import pytest
from sqlmodel import Session

from conftest import api
from datetime import timedelta

from datapoints import breakdowns, parse
from db import Agent, utc_now


@pytest.fixture
def course(create_class):
    return create_class(enrolled_gens=[9])


@pytest.fixture
def taro(make_person):
    return make_person("student", "taro", "Yamada Taro", gen=9)


def declare(client, name, **content):
    reply = client.put("/api/monitors", json={"name": "Data points", "content": content}, headers=api(name))
    assert reply.status_code < 300, reply.text


def counts(engine, agent_id):
    with Session(engine) as db:
        agent = db.get(Agent, agent_id)
        return breakdowns(db, [agent]).get(agent_id)


def test_parse_maps_types_and_ignores_junk():
    d = parse('{"data_points": {"Market prices": ["A", "a ", "B"], "internal_values": 2, "Weather": ["x"],'
              ' "Company data": "three"}, "debates": 2, "uses": ["Helper", true]}')
    assert d.names == {"market": {"a", "b"}} and d.untyped == {"x"} and d.counts == {"internal": 2}  # "Weather": sorted
    assert d.debates == 2 and d.uses == ["Helper"]
    assert parse("[1, 2]") is None and parse("{}") is None and parse("not json") is None


def test_helpers_are_added_in_once_and_shared_names_count_once(engine, client, taro, make_person, course, create_agent):
    final = create_agent(taro, course, "final")
    news = create_agent(taro, course, "news")
    eps = create_agent(taro, course, "eps")
    hanako = make_person("student", "hanako", gen=9)
    create_agent(hanako, course, "theirs")
    declare(client, "final", data_points={"Internal values": ["DCF"], "Market prices": ["Rohto price"]}, debates=1,
            uses=["News", "eps", "theirs", "ghost"])
    declare(client, "news", data_points={"News & text": 4, "Market prices": ["rohto price", "Nikkei"]},
            uses=["eps", "final"])  # a loop back to final: counted once
    declare(client, "eps", data_points={"Analyst & consensus": ["Consensus EPS"]}, debates=2)
    dp = counts(engine, final)
    assert dp.counts["internal"] == 1 and dp.counts["market"] == 2  # "Rohto price" once, plus Nikkei
    assert dp.counts["news"] == 4 and dp.counts["analyst"] == 1 and dp.total == 8 and dp.external == 7
    assert dp.debates == 3
    assert [a.name for a in dp.helpers] == ["news", "eps"]
    assert dp.missing == ["theirs", "ghost"]  # another student's agent can't be used
    # Dropping a helper changes the count; a newer declaration replaces the old one.
    declare(client, "final", data_points={"Internal values": ["DCF"], "Market prices": ["Rohto price"]}, debates=1)
    dp = counts(engine, final)
    assert dp.total == 2 and dp.debates == 1 and not dp.helpers


def test_forecasts_table_and_agent_tab_show_counts_only(engine, client, taro, course, create_agent):
    agent = create_agent(taro, course, "mitsui")
    other = create_agent(taro, course, "quiet")
    client.post("/api/outputs", json={"category": "Forecast", "content": {"target": "Mitsui (8801)", "value": 1205}},
                headers=api("mitsui"))
    declare(client, "mitsui", data_points={"Market prices": ["Secret feed name"], "Company data": 3}, debates=2)
    tab = taro.get(f"/classes/{course}", params={"tab": "forecasts"}).text
    assert "<th>Data points</th>" in tab and "Market prices <strong" in tab and "Debates <strong" in tab
    assert "Secret feed name" not in tab
    page = taro.get(f"/agents/{agent}").text
    assert "Data points</button>" in page and "Share of each type" in page and "Secret feed name" not in page
    assert page.index("Activity</button>") < page.index("Data points</button>")
    quiet = taro.get(f"/agents/{other}").text
    assert "hasn&#39;t declared its data points" in quiet or "hasn't declared its data points" in quiet


def test_connector_sends_decisions_and_data_points(monkeypatch):
    import sys
    sys.path.insert(0, "connector")
    import dashboard_connector as connector
    sent = []
    monkeypatch.setattr(connector, "output", lambda content, category: sent.append((category, content)) or True)
    monkeypatch.setattr(connector, "set_monitor", lambda content, name: sent.append((name, content)) or True)
    assert connector.send_forecast("Will SpaceX top $3T by 31 Dec?", "NO")
    assert connector.send_forecast("Mitsui", 1205, unit="JPY")
    assert not connector.send_forecast("Mitsui", None)
    assert connector.set_data_points({"Market prices": ("A", "B"), "Company data": 3, "Bad": "x"}, debates=1,
                                     uses=["News Scout"])
    assert connector.set_data_points(["Rohto price", "BOJ Tankan"])
    assert sent == [("Forecast", {"target": "Will SpaceX top $3T by 31 Dec?", "value": "NO"}),
                    ("Forecast", {"target": "Mitsui", "value": 1205, "unit": "JPY"}),
                    ("Data points", {"data_points": {"Market prices": ["A", "B"], "Company data": 3}, "debates": 1,
                                     "uses": ["News Scout"]}),
                    ("Data points", {"data_points": ["Rohto price", "BOJ Tankan"], "debates": 0})]


def test_names_without_a_type_are_sorted_by_rules_then_by_the_ai_within_its_budget(engine, client, taro, course,
                                                                                   create_agent, monkeypatch):
    import datapoint_types as sorter
    import market
    from db import DataPointRun
    agent = create_agent(taro, course, "rohto")
    declare(client, "rohto", data_points=["Rohto share price", "BOJ Tankan", "Our DCF fair value", "Shop visits"],
            debates=1)
    # Keyword rules at once: share price, Tankan, DCF; "Shop visits" fits none yet.
    dp = counts(engine, agent)
    assert (dp.counts["market"], dp.counts["economic"], dp.counts["internal"], dp.counts["other"]) == (1, 1, 1, 1)
    assert dp.sorted_by == {"rules": 4}

    sent = []
    monkeypatch.setattr(market, "ai_key", lambda: "test-key")
    def model(prompt):
        sent.append(prompt)
        return ('```json\n{"types": {"shop visits": "alternative", "rohto share price": "market", '
                '"boj tankan": "economic", "our dcf fair value": "internal", "not sent": "news"}}\n```')
    monkeypatch.setattr(market, "ask_model", model)
    with Session(engine) as db:
        assert sorter.due(db)
        record = sorter.run_if_due(db)
        assert record.ok and record.names == 4 and len(sent) == 1  # every name in one request
        assert not sorter.due(db)  # nothing left to sort (and too soon anyway)
    dp = counts(engine, agent)
    assert dp.counts["alternative"] == 1 and dp.counts["other"] == 0 and dp.sorted_by == {"ai": 4}

    # A new name later: still within the 2 a day, but only after 6 hours.
    declare(client, "rohto", data_points=["Rohto share price", "Consensus EPS"])
    with Session(engine) as db:
        assert not sorter.due(db)
        db.exec(DataPointRun.__table__.update().values(started_at=utc_now() - timedelta(hours=7)))
        db.commit()
        assert sorter.due(db) and sorter.run_if_due(db).names == 1
        db.exec(DataPointRun.__table__.update().values(started_at=utc_now() - timedelta(hours=7)))
        db.commit()
    declare(client, "rohto", data_points=["Something new"])
    with Session(engine) as db:
        assert sorter.ai_runs_left(db) == 0 and not sorter.due(db)  # today's share is used up
        assert sorter.run(db) is None


def test_the_owner_sees_where_the_dashboard_would_type_a_name_differently(engine, client, taro, make_person, course,
                                                                          create_agent):
    agent = create_agent(taro, course, "rohto")
    declare(client, "rohto", data_points={"Company data": ["Rohto share price"], "Market prices": ["Nikkei 225"]})
    dp = counts(engine, agent)
    assert dp.counts["company"] == 1  # the agent's own type counts
    assert [(d["name"], d["given"], d["suggested"]) for d in dp.disagreements] == \
        [("rohto share price", "Company data", "Market prices")]
    own = taro.get(f"/agents/{agent}").text
    assert "Suggested types" in own and "rohto share price" in own
    other = make_person("student", "hanako", gen=9).get(f"/agents/{agent}").text
    assert "Suggested types" not in other and "rohto share price" not in other  # names stay with the owner
