"""Koichi's Agent Live Status, parts 3-5: the Run status monitor and its steps
view, the live feed, and the summary line."""

from datetime import timedelta

import pytest
from sqlmodel import Session, select

from conftest import api
from db import Agent, CheckInSpan, StateChange, as_utc, utc_now
from routes.agent_tools import state_of
from routes.live_feed import sync

DAY = 86400


@pytest.fixture
def course(create_class):
    return create_class(enrolled_gens=[9])


@pytest.fixture
def taro(make_person):
    return make_person("student", "taro", "Yamada Taro", gen=9)


@pytest.fixture
def hanako(make_person):
    return make_person("student", "hanako", "Sato Hanako", gen=9)


def run_status(client, name, last_status="no_new_data", steps=()):
    body = {"last_run": utc_now().isoformat(), "last_status": last_status, "steps": list(steps)}
    return client.put("/api/monitors", json={"name": "Run status", "content": body}, headers=api(name))


def feed(engine) -> list[str]:
    with Session(engine) as db:
        rows = db.exec(select(StateChange).where(StateChange.kind != "start")
                       .order_by(StateChange.at, StateChange.id)).all()
        return [f"{row.state}: {row.message}" for row in rows]


def daily_agent(client, create_agent, taro, course, name="park"):
    agent = create_agent(taro, course, name)
    client.post("/api/state", json={"check_in_every": DAY}, headers=api(name))
    return agent


def test_run_status_failures_need_attention_and_the_steps_show(engine, client, taro, hanako, course, create_agent):
    agent = daily_agent(client, create_agent, taro, course)
    failing = {"name": "competitor prices", "state": "failing", "note": "HTTP 503 from source, 2 runs in a row"}
    run_status(client, "park", "new_data", [{"name": "parking panel", "state": "ok"}, failing])
    with Session(engine) as db:
        state = state_of(db, db.get(Agent, agent))
    assert state.key == "attention" and state.why == "competitor prices failing: HTTP 503 from source, 2 runs in a row"
    page = hanako.get(f"/agents/{agent}").text
    assert "Run status" in page and "parking panel" in page and "step-failing" in page and "HTTP 503" in page
    # A failed run, a silent step, and all fine.
    run_status(client, "park", "failed")
    with Session(engine) as db:
        assert state_of(db, db.get(Agent, agent)).why == "last run failed"
    run_status(client, "park", "no_new_data", [{"name": "Mitsui network", "state": "silent"}])
    with Session(engine) as db:
        assert state_of(db, db.get(Agent, agent)).why == "Mitsui network silent"
    run_status(client, "park", "no_new_data", [{"name": "parking panel", "state": "ok"}])
    with Session(engine) as db:
        assert state_of(db, db.get(Agent, agent)).key == "live"


def test_a_malformed_run_status_is_ignored(engine, client, taro, course, create_agent):
    agent = daily_agent(client, create_agent, taro, course)
    client.put("/api/monitors", json={"name": "Run status", "content": "everything failed"}, headers=api("park"))
    client.put("/api/monitors", json={"name": "Run status", "content": [1, 2]}, headers=api("park"))
    with Session(engine) as db:
        assert state_of(db, db.get(Agent, agent)).key == "live"


def test_feed_lists_changes_only(engine, client, taro, course, create_agent):
    agent = daily_agent(client, create_agent, taro, course)
    taro.get(f"/classes/{course}/agents/{taro.id}")  # first seen: noted, no row
    assert feed(engine) == []
    for _ in range(3):  # check-ins alone change nothing
        client.post("/api/state", json={}, headers=api("park"))
        taro.get(f"/classes/{course}/agents/{taro.id}")
    assert feed(engine) == []
    taro.post(f"/agents/{agent}/command", data={"command": "pause"})
    taro.post(f"/agents/{agent}/command", data={"command": "start"})
    run_status(client, "park", "failed")
    run_status(client, "park", "new_data", [{"name": "parking panel", "state": "ok",
                                             "last_new_data": utc_now().isoformat()}])
    assert feed(engine) == ["paused: paused by owner", "live: resumed", "attention: needs attention: last run failed",
                            "data: new data: parking panel", "live: recovered"]


def test_going_offline_is_dated_when_it_happened(engine, client, taro, course, create_agent):
    agent_id = daily_agent(client, create_agent, taro, course)
    taro.get(f"/classes/{course}/agents/{taro.id}")  # noted as live
    now = utc_now()
    with Session(engine) as db:
        agent = db.get(Agent, agent_id)
        agent.last_seen_at = now - timedelta(days=3)
        agent.feed_state_at = now - timedelta(days=3)  # it was live then
        db.add(agent)
        db.commit()
    taro.get(f"/classes/{course}/agents/{taro.id}")  # noticed now...
    with Session(engine) as db:
        row = db.exec(select(StateChange).where(StateChange.kind == "state")).one()
    assert row.state == "offline" and row.message == "went offline (no check-in for 1 day 12 h)"
    # ...but dated 1.5 days after its last check-in.
    assert abs(as_utc(row.at) - (now - timedelta(days=3) + timedelta(days=1.5))) < timedelta(seconds=5)


def test_an_outage_nobody_saw_still_gets_both_rows(engine, client, taro, course, create_agent):
    agent_id = daily_agent(client, create_agent, taro, course)
    now = utc_now()
    with Session(engine) as db:
        agent = db.get(Agent, agent_id)
        agent.feed_state, agent.feed_state_at = "live", now - timedelta(days=6)
        db.add(CheckInSpan(agent_id=agent_id, started_at=now - timedelta(days=6), ended_at=now - timedelta(days=6)))
        # Silent from 6 days ago until 1 day ago (then it checked in today too, when it was added).
        db.add(CheckInSpan(agent_id=agent_id, started_at=now - timedelta(days=1), ended_at=now - timedelta(days=1)))
        db.add(agent)
        db.commit()
        sync(db, agent, state_of(db, agent), now)
        db.commit()
    assert feed(engine) == ["offline: went offline (no check-in for 1 day 12 h)", "live: back online after 3 days 12 h"]
    # Looking again (every page load, every 15 seconds) finds nothing new.
    for _ in range(3):
        with Session(engine) as db:
            agent = db.get(Agent, agent_id)
            sync(db, agent, state_of(db, agent))
            db.commit()
    assert len(feed(engine)) == 2


def test_the_feed_is_on_the_class_page_for_members_only(engine, client, taro, hanako, guest, course, create_agent):
    daily_agent(client, create_agent, taro, course, "park")
    create_agent(taro, course, "never")  # never connected: offline
    tab, visitor = hanako.get(f"/classes/{course}/tab/agents").text, guest.get(f"/classes/{course}/tab/agents").text
    assert "Live feed" in tab and "lf-chip-live" in tab and "Agents due each day" in tab
    assert tab.index("Live feed") < tab.index("sw-frame")  # above the student window
    assert "Live feed" not in visitor and "1 live" in visitor and "1 offline" in visitor
    # A student's own page: no feed and no summary line (its rhythm bar shows how its agents are doing).
    grid = hanako.get(f"/classes/{course}/agents/{taro.id}").text
    assert "Live feed" not in grid and "Agent states" not in grid and "Check-in rhythms" in grid
    assert guest.get(f"/classes/{course}/feed").status_code == 403


def test_offline_cards_are_shaded_with_a_warning(client, taro, hanako, course, create_agent):
    daily_agent(client, create_agent, taro, course, "park")
    create_agent(taro, course, "never")
    grid = hanako.get(f"/classes/{course}/agents/{taro.id}").text
    assert grid.count("card-down") == 1 and grid.count('aria-label="Not working"') == 1


def test_bubbles_list_the_latest_agents_per_kind_joining_repeats(engine, client, taro, hanako, course, create_agent):
    agent = daily_agent(client, create_agent, taro, course)
    hanako.get(f"/classes/{course}/tab/agents")  # first seen: noted as live
    for _ in range(3):  # paused three times: one line, "3x"
        taro.post(f"/agents/{agent}/command", data={"command": "pause"})
        taro.post(f"/agents/{agent}/command", data={"command": "start"})
    run_status(client, "park", "new_data", [{"name": "parking panel", "state": "ok",
                                             "last_new_data": utc_now().isoformat()}])
    body = hanako.get(f"/classes/{course}/feed").text
    paused = body[body.index("lf-bubble-paused"):body.index("lf-bubble-data")]
    assert paused.count("<span>park</span>") == 1 and "&times;3" in paused  # one line, joined
    assert 'class="lf-person" title="Yamada Taro"' in paused  # whose agent it is: the person marker
    assert ">by: Yamada Taro<" in paused  # and in words beside the box
    assert f'href="/agents/{agent}" class="lf-link' in paused  # the name in the text links to the agent
    data = body[body.index("lf-bubble-data"):]
    assert "<span>park</span>" in data and "1 in 24 h" in body
    offline = body[body.index("lf-bubble-offline"):body.index("lf-bubble-paused")]
    assert "Nothing yet" in offline
    # Live: counting down to the next run, on the chip and the agent's line.
    live = body[body.index("lf-chip-live"):body.index("lf-bubble-attention")]
    assert live.count("data-countdown=") == 2


def test_daily_bars_count_agents_due_by_their_worst_state_and_slices_open(engine, client, taro, hanako, course,
                                                                          create_agent):
    from routes.agent_tools import states_for
    from routes.live_feed import daily_bars
    daily = daily_agent(client, create_agent, taro, course, "daily")
    weekly = create_agent(taro, course, "weekly")
    client.post("/api/state", json={"check_in_every": 7 * DAY}, headers=api("weekly"))
    create_agent(taro, course, "never")  # never connected: not due
    paused = daily_agent(client, create_agent, taro, course, "resting")
    taro.post(f"/agents/{paused}/command", data={"command": "pause"})
    now = utc_now()
    with Session(engine) as db:
        # The weekly one last ran 3 days ago (so it's due again in 4 days, not today).
        db.exec(CheckInSpan.__table__.delete().where(CheckInSpan.agent_id == weekly))
        db.add(CheckInSpan(agent_id=weekly, started_at=now - timedelta(days=3), ended_at=now - timedelta(days=3)))
        db.get(Agent, weekly).created_at = now - timedelta(days=10)  # it existed then
        db.commit()
        hanako.get(f"/classes/{course}/tab/agents")  # notes everyone's state
        agents = list(db.exec(select(Agent)).all())
        bars = daily_bars(db, agents, states_for(db, agents))
    assert len(bars) == 14 and bars[-1]["label"] == "today"
    today = {key: len(ids) for key, _, ids in bars[-1]["parts"]}
    assert today == {"live": 1} and bars[-1]["total"] == 1  # daily only: not the weekly, the never, the paused
    three_days_ago = {key: len(ids) for key, _, ids in bars[-4]["parts"]}
    assert three_days_ago == {"unknown": 1}  # the weekly ran then, before the feed tracked it
    page = hanako.get(f"/classes/{course}/agents-by-state", params={"day": bars[-1]["day"], "state": "live"}).text
    assert "1 of 1 agent" in page and "<span>daily</span>" in page and "Yamada Taro" in page
    assert hanako.get(f"/classes/{course}/agents-by-state", params={"day": "1999-01-01", "state": "live"}).status_code == 404


def test_a_run_that_is_due_shows_typing_until_it_comes_in(engine, client, taro, hanako, course, create_agent):
    agent_id = daily_agent(client, create_agent, taro, course, "park")
    daily_agent(client, create_agent, taro, course, "other")
    with Session(engine) as db:  # its daily run was due 10 minutes ago (still within the slack: not offline yet)
        agent = db.get(Agent, agent_id)
        agent.last_seen_at = utc_now() - timedelta(days=1, minutes=10)
        db.add(agent)
        db.commit()
    body = hanako.get(f"/classes/{course}/feed").text
    assert body.count("lf-msg lf-typing") == 4  # Live, Needs attention, Offline, New data; not Paused
    paused = body[body.index("lf-bubble-paused"):body.index("lf-bubble-data")]
    assert "lf-typing" not in paused
    client.post("/api/state", json={}, headers=api("park"))  # it comes in
    body = hanako.get(f"/classes/{course}/feed").text
    assert "lf-typing" not in body
    live = body[body.index("lf-bubble-live"):body.index("lf-bubble-attention")]
    assert live.index("<span>park</span>") < live.index("<span>other</span>")  # most recent check-in first
