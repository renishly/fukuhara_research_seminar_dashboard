"""The check-in history (checkins.py) and the Check-in activity graph."""

from datetime import timedelta

import pytest
from sqlmodel import Session, select

from checkins import activity, record_activity
from conftest import PASSWORD, api
from db import Agent, CheckInSpan, utc_now

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


def check_in_at(db: Session, agent: Agent, *moments) -> None:
    for moment in moments:
        agent.last_seen_at = moment
        record_activity(db, agent, moment)
        db.commit()


def make_daily(engine, create_agent, taro, course) -> int:
    agent_id = create_agent(taro, course, "daily")
    with Session(engine) as db:
        agent = db.get(Agent, agent_id)
        agent.check_in_seconds = DAY
        db.add(agent)
        db.commit()
    return agent_id


def test_calls_close_together_merge_into_one_span(engine, client, taro, course, create_agent):
    agent_id = make_daily(engine, create_agent, taro, course)
    now = utc_now()
    with Session(engine) as db:
        agent = db.get(Agent, agent_id)
        # One daily run of three calls in five minutes, then the next day's run.
        check_in_at(db, agent, now - timedelta(days=1, minutes=5), now - timedelta(days=1, minutes=2),
                    now - timedelta(days=1), now)
        spans = db.exec(select(CheckInSpan).order_by(CheckInSpan.started_at)).all()
        assert len(spans) == 2
        assert spans[1].started_at - spans[0].started_at == timedelta(days=1, minutes=5)
        assert agent.check_ins_since is not None
    # Real API calls are recorded too.
    client.post("/api/state", json={}, headers=api("daily"))
    with Session(engine) as db:
        assert len(db.exec(select(CheckInSpan)).all()) == 2  # within half a day of the last: merged


def test_gaps_are_on_time_late_or_missed(engine, taro, course, create_agent):
    agent_id = make_daily(engine, create_agent, taro, course)
    now = utc_now()
    days_ago = [11.5, 10.3, 8.6, 5.2, 4.0, 3.0]  # gaps: 1.2 (ok), 1.7 (late), 3.4 (2 missed), 1.2, 1.0
    with Session(engine) as db:
        agent = db.get(Agent, agent_id)
        check_in_at(db, agent, *[now - timedelta(days=d) for d in days_ago])
        graph = activity(db, agent, "Daily", now)
    assert graph.window_text == "12 days" and len(graph.ticks) == 13 and graph.ticks[-1][1] == "now"
    # One bar per check-in, as tall as the silence before it (in intervals).
    assert [b.kind for b in graph.bars] == ["first", "ok", "late", "late", "ok", "ok"]
    assert (graph.on_time_count, graph.late_count) == (4, 2)
    assert graph.missed_count == 2 + 2  # 3.4 days: 2 misses; 3 days so far: 2 misses (red marks, no bars)
    assert graph.y_max == 3.0 and graph.bars[3].cut and not graph.bars[2].cut  # 3.4 intervals: cut off at 3
    assert graph.bars[2].height == pytest.approx(1.7 / 3)
    assert "Late: checked in" in graph.bars[2].text and "1 day 16 h after the one before" in graph.bars[2].text
    assert graph.waiting is not None and "so far" in graph.waiting.text  # silent for 3 days right now
    assert [label for _, label in graph.guides] == ["expected", "late"]


def test_no_interval_no_graph_and_history_before_recording_is_marked(engine, taro, course, create_agent):
    agent_id = create_agent(taro, course, "plain")
    now = utc_now()
    with Session(engine) as db:
        agent = db.get(Agent, agent_id)
        check_in_at(db, agent, now - timedelta(hours=3))
        assert activity(db, agent, None, now) is None
        agent.owner_check_in_seconds = 3600
        graph = activity(db, agent, "Hourly", now)
    assert graph.window_text == "12 h"
    assert graph.unrecorded_until == pytest.approx(9 / 12)  # history began 3 h ago
    assert graph.missed_count == 2 and graph.late_count == 0  # silent 3 h: 2 expected check-ins missed
    assert [b.kind for b in graph.bars] == ["first"] and graph.waiting is not None


def test_old_spans_are_forgotten(engine, taro, course, create_agent):
    agent_id = make_daily(engine, create_agent, taro, course)
    now = utc_now()
    with Session(engine) as db:
        agent = db.get(Agent, agent_id)
        check_in_at(db, agent, now - timedelta(days=20), now - timedelta(days=12), now)
        ends = [span.ended_at for span in db.exec(select(CheckInSpan)).all()]
    assert len(ends) == 2  # the 20-day-old one is past 13 intervals


def test_agent_page_shows_the_graph_and_deleting_the_agent_removes_its_history(engine, client, taro, hanako, course,
                                                                             create_agent):
    agent_id = make_daily(engine, create_agent, taro, course)
    client.post("/api/state", json={}, headers=api("daily"))
    page = hanako.get(f"/agents/{agent_id}").text
    assert "Check-in activity" in page and "Last 12 intervals (12 days)" in page and "ci-bar" in page
    assert taro.post(f"/agents/{agent_id}/delete", data={"password": PASSWORD}).status_code in (200, 303)
    with Session(engine) as db:
        assert db.exec(select(CheckInSpan)).all() == []
