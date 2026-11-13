"""The live feed and the summary line (Koichi's Agent Live Status, parts 4-5).

The feed records *changes* only, never every check-in: an agent went offline,
came back, needs attention, recovered, was paused or resumed, or brought new
data (its Run status says new_data). It's shown as five categories (Live,
Needs attention, Offline, Paused, New data), each with a speech bubble of the
latest agents with that kind of activity, repeats of one agent joined.

States are worked out when pages load (agent_tools.agent_state()), so the feed
is brought up to date then too (sync()), and also when the owner presses
Start/Pause and when an agent sends its Run status. Each row carries the time
the change really happened, not when it was noticed:
  went offline   its last check-in + the allowed gap (1.5x the interval)
  came back      its first check-in after that (from the check-in history)
  anything else  when it happened (the button, the Run status update)
An agent that went offline and came back between two page loads still gets
both rows, found from the gaps in its check-in history.

The first time an agent is seen, its state is only noted (no row), so the
feed doesn't fill up when this is first deployed.
"""

import math
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, delete, func, select

from checkins import allowed_gap, check_in_interval, describe
from db import Agent, CheckInSpan, Monitor, Output, StateChange, User, as_utc, get_session, utc_now
from routes.agent_tools import AgentState, rhythm_mix, states_for
from routes.common import get_course, not_found, student_groups
from run_status import RunStatus
from security import Viewer, require_member
from web import JST, poll_reply, render, render_text

router = APIRouter()

KEEP_PER_AGENT = 200
STATE_ORDER = [("live", "live"), ("attention", "need attention"), ("offline", "offline"), ("paused", "paused")]


# ---------------------------------------------------------------------------
# Writing the feed
# ---------------------------------------------------------------------------


def add_row(db: Session, agent: Agent, at: datetime, kind: str, state: str, message: str) -> None:
    db.add(StateChange(agent_id=agent.id, course_id=agent.course_id, at=at, kind=kind, state=state, message=message))


def unseen_outages(db: Session, agent: Agent, since: datetime, interval: int, now: datetime) -> None:
    """Rows for any time the agent went offline and came back since `since`
    (found from gaps in its check-in history), which no page load caught.
    Each stretch of history is looked at once: feed_scanned_at marks how far."""
    allowed = allowed_gap(interval)
    if agent.feed_scanned_at is not None:
        since = max(since, as_utc(agent.feed_scanned_at))
    agent.feed_scanned_at = now
    db.add(agent)
    previous = db.exec(select(CheckInSpan).where(CheckInSpan.agent_id == agent.id, CheckInSpan.started_at <= since)
                       .order_by(CheckInSpan.started_at.desc()).limit(1)).first()
    spans = db.exec(select(CheckInSpan).where(CheckInSpan.agent_id == agent.id, CheckInSpan.started_at > since)
                    .order_by(CheckInSpan.started_at)).all()
    previous_end = as_utc(previous.ended_at) if previous else None
    for span in spans:
        start = as_utc(span.started_at)
        if previous_end is not None and start - previous_end > allowed:
            went = max(previous_end + allowed, since)
            add_row(db, agent, went, "state", "offline", f"went offline (no check-in for {describe(allowed.total_seconds())})")
            add_row(db, agent, start, "state", "live", f"back online after {describe((start - went).total_seconds())}")
        previous_end = as_utc(span.ended_at)


def first_check_in_after(db: Session, agent: Agent, moment: datetime) -> datetime | None:
    span = db.exec(select(CheckInSpan).where(CheckInSpan.agent_id == agent.id, CheckInSpan.started_at > moment)
                   .order_by(CheckInSpan.started_at).limit(1)).first()
    return as_utc(span.started_at) if span else None


def sync(db: Session, agent: Agent, state: AgentState, now: datetime | None = None) -> bool:
    """Bring the agent's feed up to date with its current state. Returns True
    if it changed anything (the caller commits)."""
    now = now or utc_now()
    if agent.feed_state is None:  # first seen: note it (a hidden "start" row, for the daily bars), no feed line
        agent.feed_state, agent.feed_state_at = state.key, now
        add_row(db, agent, now, "start", state.key, "first seen")
        db.add(agent)
        return True
    before, since = agent.feed_state, as_utc(agent.feed_state_at or now)
    interval = check_in_interval(agent)
    # Outages between page loads can only be found from check-ins made since the
    # last look: with none, there's nothing to scan (no queries, no writes).
    scanned = as_utc(agent.feed_scanned_at) if agent.feed_scanned_at else None
    seen = as_utc(agent.last_seen_at) if agent.last_seen_at else None
    changed = False
    if before in ("live", "attention") and interval and seen and (scanned is None or seen > scanned):
        unseen_outages(db, agent, since, interval, now)
        changed = True
    if state.key == before:
        return changed

    at, message = now, ""
    seen = as_utc(agent.last_seen_at) if agent.last_seen_at else None
    if state.key == "offline":
        if seen and interval:
            at = max(seen + allowed_gap(interval), since)
            message = f"went offline (no check-in for {describe(allowed_gap(interval).total_seconds())})"
        else:
            message = "is offline: " + (state.why or "no check-in")
    elif before == "offline":
        at = first_check_in_after(db, agent, since) or seen or now
        at = min(max(at, since), now)
        message = f"back online after {describe((at - since).total_seconds())}"
        if state.key == "attention":
            message += f", needs attention: {state.why}"
        elif state.key == "paused":
            message += ", paused"
    elif state.key == "paused":
        message = "paused by owner"
    elif before == "paused":
        message = "resumed" + (f", needs attention: {state.why}" if state.key == "attention" else "")
    elif state.key == "attention":
        message = f"needs attention: {state.why}"
    else:  # attention -> live
        message = "recovered"
    add_row(db, agent, at, "state", state.key, message)
    agent.feed_state, agent.feed_state_at = state.key, at
    db.add(agent)
    forget_old_rows(db, agent)
    return True


def sync_all(db: Session, agents: list[Agent], states: dict[int, AgentState], now: datetime | None = None) -> None:
    """sync() for a page's agents, then commit."""
    now = now or utc_now()
    changed = False
    for agent in agents:
        changed = sync(db, agent, states[agent.id], now) or changed
    # Only save when something changed: saving makes every loaded agent stale,
    # and each would then be read from the database again.
    if changed:
        db.commit()


def note_new_data(db: Session, agent: Agent, old: RunStatus | None, new: RunStatus | None) -> None:
    """A row when a Run status update says the latest run brought new data.
    The caller commits."""
    if new is None or new.last_status != "new_data" or (old is not None and old.last_run == new.last_run):
        return
    fresh = [step for step in new.steps if step.last_new_data]
    newest = max((step.last_new_data for step in fresh), default=None)
    names = [step.name for step in fresh if step.last_new_data == newest]
    add_row(db, agent, min(new.last_run or utc_now(), utc_now()), "data", "data",
            "new data" + (f": {', '.join(names)}" if names else ""))
    forget_old_rows(db, agent)


def forget_old_rows(db: Session, agent: Agent) -> None:
    db.flush()
    old = db.exec(select(StateChange.id).where(StateChange.agent_id == agent.id)
                  .order_by(StateChange.at.desc(), StateChange.id.desc()).offset(KEEP_PER_AGENT)).all()
    if old:
        db.exec(delete(StateChange).where(StateChange.id.in_(old)))


# ---------------------------------------------------------------------------
# Reading it: the daily bars, five categories with chat bubbles, the summary line
# ---------------------------------------------------------------------------

# The feed's five categories: (key, label).
CATEGORIES = [("live", "Live"), ("attention", "Needs attention"), ("offline", "Offline"), ("paused", "Paused"),
              ("data", "New data")]
BUBBLE_AGENTS = 12  # at most this many agents per bubble (it scrolls)
BAR_DAYS = 14
DAY = timedelta(days=1)
# Worst first: an agent's state for a day is the worst it was in that day.
WORST = ["offline", "attention", "live"]
BAR_STATES = [("live", "Live"), ("attention", "Needs attention"), ("offline", "Offline"), ("unknown", "No record")]


def iso(moment: datetime | None) -> str | None:
    return as_utc(moment).isoformat() if moment else None


def next_run(agent: Agent) -> datetime | None:
    """When the agent is next expected to check in: its last check-in + its interval."""
    interval = check_in_interval(agent)
    return as_utc(agent.last_seen_at) + timedelta(seconds=interval) if agent.last_seen_at and interval else None


def categories(db: Session, agents: list[Agent], states: dict[int, AgentState], owners: dict[int, User]) -> list[dict]:
    """The five categories, each with its count and its bubble.

    Live: the live agents, latest check-in first, each counting down to its
    next run. The others: the latest agents with that kind of change, one
    line per agent (repeats joined: "x3"). And "typing": agents whose next run
    is due now but hasn't come in yet, shown at the top of every bubble but
    Paused until it lands in one (on time: Live; new data; needs attention;
    or, if it never comes, Offline)."""
    now = utc_now()
    by_id = {agent.id: agent for agent in agents}
    ids = list(by_id)
    typing = sorted((a for a in agents if states[a.id].key in ("live", "attention") and next_run(a)
                     and next_run(a) <= now), key=next_run)
    result = []
    # Every category's latest agents in two queries (not one per agent per category):
    # how many changes each agent has per state, and the newest rows to read the messages from.
    counts: dict[tuple[str, int], int] = {}
    newest: dict[tuple[str, int], tuple[datetime, StateChange]] = {}
    if ids:
        for state, agent_id, count in db.exec(
                select(StateChange.state, StateChange.agent_id, func.count(StateChange.id))
                .where(StateChange.agent_id.in_(ids), StateChange.kind != "start")
                .group_by(StateChange.state, StateChange.agent_id)).all():
            counts[(state, agent_id)] = count
        number = func.row_number().over(partition_by=(StateChange.state, StateChange.agent_id),
                                        order_by=(StateChange.at.desc(), StateChange.id.desc()))
        ranked = (select(StateChange.id, number.label("n"))
                  .where(StateChange.agent_id.in_(ids), StateChange.kind != "start").subquery())
        latest_ids = [row_id for row_id, n in db.exec(select(ranked.c.id, ranked.c.n)).all() if n == 1]
        for row in db.exec(select(StateChange).where(StateChange.id.in_(latest_ids))).all() if latest_ids else []:
            newest[(row.state, row.agent_id)] = (as_utc(row.at), row)
    for key, label in CATEGORIES:
        entries = []
        if key == "live":
            for agent in sorted((a for a in agents if states[a.id].key == "live"),
                                key=lambda a: as_utc(a.last_seen_at or a.created_at), reverse=True)[:BUBBLE_AGENTS]:
                entries.append({"agent": agent, "owner": owners.get(agent.owner_id), "state": states[agent.id],
                                "message": "checked in", "count": 1, "countdown": iso(next_run(agent))})
        else:
            mine = sorted(((at, row) for (state, _), (at, row) in newest.items() if state == key),
                          key=lambda pair: pair[0], reverse=True)[:BUBBLE_AGENTS]
            for _, row in mine:
                agent = by_id[row.agent_id]
                entries.append({"agent": agent, "owner": owners.get(agent.owner_id), "state": states.get(agent.id),
                                "message": row.message, "count": counts.get((key, agent.id), 1), "countdown": None})
        chip = {"key": key, "label": label, "entries": entries, "countdown": None,
                "typing": [] if key == "paused" else [{"agent": a, "owner": owners.get(a.owner_id),
                                                       "state": states[a.id]} for a in typing]}
        if key == "data":
            chip["count"] = db.exec(select(func.count(StateChange.id)).where(
                StateChange.agent_id.in_(ids), StateChange.state == "data",
                StateChange.at > now - DAY)).one() if ids else 0
            chip["count_text"] = f"{chip['count']} in 24 h"
        else:
            chip["count"] = sum(1 for agent_id in ids if states[agent_id].key == key)
            chip["count_text"] = str(chip["count"])
            if key == "live":
                upcoming = [next_run(by_id[i]) for i in ids if states[i].key == "live" and next_run(by_id[i])
                            and next_run(by_id[i]) > now]
                chip["countdown"] = iso(min(upcoming)) if upcoming else None
        result.append(chip)
    return result


def day_bounds(now: datetime) -> list[tuple[datetime, datetime]]:
    """The last 14 days in Japan time, oldest first, as (start, end) in UTC."""
    today = now.astimezone(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    return [((today - n * DAY).astimezone(as_utc(now).tzinfo), (today - (n - 1) * DAY).astimezone(as_utc(now).tzinfo))
            for n in range(BAR_DAYS - 1, -1, -1)]


def daily_bars(db: Session, agents: list[Agent], states: dict[int, AgentState], now: datetime | None = None) -> list[dict]:
    """One bar per day for the last 14 days: how many agents were due to run
    that day (by their intervals), split by the worst state each was in that
    day (Live, Needs attention, Offline; "No record" before the feed tracked
    it). Today's bar is each agent's state *now* instead, so an agent that was
    down overnight and has been fixed shows Live straight away, not red until
    tomorrow (its outage is still in the feed and in earlier days' bars).
    Paused agents aren't counted: they weren't meant to run.

    Due: an agent checking in daily or faster is due every day once it has
    connected; a slower one on the days its schedule falls (its last check-in
    before the day + whole intervals), or any day it did check in."""
    now = now or utc_now()
    days = day_bounds(now)
    ids = [agent.id for agent in agents]
    spans: dict[int, list[datetime]] = {i: [] for i in ids}
    rows: dict[int, list[tuple[datetime, str]]] = {i: [] for i in ids}
    if ids:
        for agent_id, started in db.exec(select(CheckInSpan.agent_id, CheckInSpan.started_at)
                                         .where(CheckInSpan.agent_id.in_(ids)).order_by(CheckInSpan.started_at)).all():
            spans[agent_id].append(as_utc(started))
        for agent_id, at, state in db.exec(select(StateChange.agent_id, StateChange.at, StateChange.state)
                                           .where(StateChange.agent_id.in_(ids), StateChange.kind != "data")
                                           .order_by(StateChange.at, StateChange.id)).all():
            rows[agent_id].append((as_utc(at), state))

    def due(agent: Agent, start: datetime, end: datetime) -> bool:
        if as_utc(agent.created_at) >= end:
            return False
        starts = spans[agent.id]
        if any(start <= moment < end for moment in starts):
            return True
        interval = check_in_interval(agent)
        before = [moment for moment in starts if moment < start]
        if not interval or not before:
            return False
        if interval <= DAY.total_seconds():
            return True
        step = timedelta(seconds=interval)
        expected = before[-1] + step * math.ceil((start - before[-1]) / step)
        return expected < end

    def worst(agent: Agent, start: datetime, end: datetime, is_today: bool) -> str | None:
        """The worst state that day (today: the state now); None if paused (not counted)."""
        if is_today:
            key = states[agent.id].key
            return None if key == "paused" else key
        history = rows[agent.id]
        seen = {state for at, state in history if start <= at < end}
        earlier = [state for at, state in history if at < start]
        if earlier:
            seen.add(earlier[-1])
        if not earlier and not any(start <= at < end for at, _ in history):
            return "unknown"
        for key in WORST:
            if key in seen:
                return key
        return None if seen else "unknown"

    bars = []
    for number, (start, end) in enumerate(days):
        is_today = number == len(days) - 1
        groups: dict[str, list[int]] = {key: [] for key, _ in BAR_STATES}
        for agent in agents:
            if due(agent, start, end):
                state = worst(agent, start, end, is_today)
                if state is not None:
                    groups[state].append(agent.id)
        local = start.astimezone(JST)
        bars.append({"day": local.strftime("%Y-%m-%d"), "label": "today" if is_today else local.strftime("%m/%d").lstrip("0"),
                     "weekday": local.strftime("%a"), "total": sum(len(v) for v in groups.values()),
                     "parts": [(key, words, groups[key]) for key, words in BAR_STATES if groups[key]]})
    return bars


def summary(states: dict[int, AgentState], agent_ids: list[int] | None = None) -> list[tuple[str, str, int]]:
    """[(state key, words, count)] in Koichi's order: live, attention, offline, paused."""
    keys = [states[i].key for i in (agent_ids if agent_ids is not None else states)]
    return [(key, words, keys.count(key)) for key, words in STATE_ORDER]


def feed_context(db: Session, agents: list[Agent], states: dict[int, AgentState], course_id: int) -> dict:
    owners = {user.id: user for user in db.exec(select(User).where(User.id.in_({a.owner_id for a in agents}))).all()} \
        if agents else {}
    bars = daily_bars(db, agents, states)
    # The y axis: whole numbers of agents, at most 4 steps, its top a whole step at or above the tallest bar.
    tallest = max([bar["total"] for bar in bars] + [1])
    step = max(1, math.ceil(tallest / 4))
    top = step * math.ceil(tallest / step)
    return {"feed_categories": categories(db, agents, states, owners), "feed_bars": bars,
            "feed_bar_top": top, "feed_bar_ticks": list(range(0, top + 1, step)),
            "feed_url": f"/classes/{course_id}/feed", "feed_course_id": course_id}


def class_agents(db: Session, viewer: Viewer, course_id: int):
    course = get_course(db, viewer, course_id)
    agents = list(db.exec(select(Agent).where(Agent.course_id == course.id)).all())
    states = states_for(db, agents)
    sync_all(db, agents, states)
    return course, agents, states


@router.get("/classes/{course_id}/feed")
def feed_fragment(course_id: int, seen: str | None = None, viewer: Viewer = Depends(require_member),
                  db: Session = Depends(get_session)):
    """The feed's body again, every 15 seconds (204 if nothing changed, so the
    page isn't redrawn and sliding names don't restart). Members only: guests
    see the summary line, never the feed."""
    course, agents, states = class_agents(db, viewer, course_id)
    content = render_text(db, viewer, "partials/live_feed_body.html", **feed_context(db, agents, states, course.id))
    return poll_reply(content, seen, "feed-seen")


@router.get("/classes/{course_id}/agents-by-state")
def agents_by_state(request: Request, course_id: int, day: str, state: str, viewer: Viewer = Depends(require_member),
                    db: Session = Depends(get_session)):
    """The agents in one slice of the daily bars (e.g. Offline on 11/10), whoever owns them, as cards."""
    course, agents, states = class_agents(db, viewer, course_id)
    if state not in dict(BAR_STATES):
        raise not_found("Unknown state")
    bar = next((bar for bar in daily_bars(db, agents, states) if bar["day"] == day), None)
    if bar is None:
        raise not_found("That day isn't on the chart")
    chosen = next((ids for key, _, ids in bar["parts"] if key == state), [])
    by_id = {agent.id: agent for agent in agents}
    picked = sorted((by_id[i] for i in chosen), key=lambda a: a.name.lower())
    owners = {user.id: user for user in db.exec(select(User).where(User.id.in_({a.owner_id for a in picked}))).all()} \
        if picked else {}
    return render(request, db, viewer, "agents_by_state.html", course=course, bar=bar, state_key=state,
                  state_words=dict(BAR_STATES)[state], cards=[{"agent": a, "owner": owners.get(a.owner_id),
                                                               "state": states[a.id]} for a in picked])


# ---------------------------------------------------------------------------
# One category's own dashboard (click a chip in the feed)
# ---------------------------------------------------------------------------

HISTORY_ROWS = 5  # the latest changes on each card
CATEGORY_NOTES = {
    "live": "Checking in on time, with nothing wrong in their latest run. Soonest next run first.",
    "attention": "Running, but something needs fixing: no interval set, a failed run, or a failing or silent step. "
                 "Longest first.",
    "offline": "Missed their check-in (by more than half an interval), or never connected. Longest first.",
    "paused": "Paused by their owners, so not expected to run. Longest first.",
    "data": "Agents whose runs brought new data in the last 7 days. Newest first.",
}


def shown_agents(db: Session, viewer: Viewer, course) -> list[Agent]:
    """The class's agents the feed counts: those of the students in the Agents
    tab (see classes.agents_window()), so the page matches the chip's number."""
    owners = {cell["student"].id for group in student_groups(db, viewer, course) for cell in group.cells}
    return list(db.exec(select(Agent).where(Agent.course_id == course.id, Agent.owner_id.in_(owners))).all()) \
        if owners else []


def category_dashboard(db: Session, agents: list[Agent], states: dict[int, AgentState], key: str,
                       now: datetime | None = None) -> dict:
    """The agents in one feed category, each with what matters for it, plus a
    few figures for the whole category."""
    now = now or utc_now()
    week_ago, day_ago = now - WEEK, now - DAY
    ids = [agent.id for agent in agents]
    history: dict[int, list[StateChange]] = {i: [] for i in ids}
    data_rows: dict[int, list[StateChange]] = {i: [] for i in ids}
    if ids:
        number = func.row_number().over(partition_by=StateChange.agent_id,
                                        order_by=(StateChange.at.desc(), StateChange.id.desc()))
        ranked = (select(StateChange.id, number.label("n"))
                  .where(StateChange.agent_id.in_(ids), StateChange.kind != "start").subquery())
        latest = [row_id for row_id, n in db.exec(select(ranked.c.id, ranked.c.n)).all() if n <= HISTORY_ROWS]
        for row in db.exec(select(StateChange).where(StateChange.id.in_(latest))
                           .order_by(StateChange.at.desc(), StateChange.id.desc())).all() if latest else []:
            history[row.agent_id].append(row)
        for row in db.exec(select(StateChange).where(StateChange.agent_id.in_(ids), StateChange.kind == "data",
                                                     StateChange.at >= week_ago)
                           .order_by(StateChange.at.desc(), StateChange.id.desc())).all():
            data_rows[row.agent_id].append(row)

    if key == "data":
        picked = [agent for agent in agents if data_rows[agent.id]]
        picked.sort(key=lambda a: as_utc(data_rows[a.id][0].at), reverse=True)
    else:
        picked = [agent for agent in agents if states[agent.id].key == key]
        if key == "live":
            far = now + timedelta(days=3650)
            picked.sort(key=lambda a: next_run(a) or far)
        else:  # longest in this state first
            picked.sort(key=lambda a: as_utc(a.feed_state_at or a.created_at))
    owners = {user.id: user for user in db.exec(select(User).where(User.id.in_({a.owner_id for a in picked}))).all()} \
        if picked else {}
    weeks = weekly_summaries(db, picked, now)

    cards = []
    for agent in picked:
        state = states[agent.id]
        card = {"agent": agent, "owner": owners.get(agent.owner_id), "state": state, "week": weeks[agent.id],
                "history": history[agent.id], "countdown": None, "headline": None, "since": None}
        since = as_utc(agent.feed_state_at) if agent.feed_state_at and agent.feed_state == state.key else None
        if key == "data":
            rows = data_rows[agent.id]
            card["headline"] = rows[0].message
            card["since"] = as_utc(rows[0].at)
            card["data_day"] = sum(1 for row in rows if as_utc(row.at) > day_ago)
            card["data_week"] = len(rows)
        elif key == "live":
            run = next_run(agent)
            card["countdown"] = iso(run) if run and run > now else None
            card["headline"] = "due now, waiting for its run" if run and run <= now else "next run"
        else:
            card["headline"] = state.why
            card["since"] = since
            if since:
                card["for"] = describe((now - since).total_seconds())
        cards.append(card)

    figures: list[tuple[str, str]] = [("Agents", str(len(picked)))]
    reasons: list[tuple[str, int]] = []
    if key == "live":
        upcoming = [next_run(a) for a in picked if next_run(a) and next_run(a) > now]
        figures.append(("Due now", str(sum(1 for a in picked if next_run(a) and next_run(a) <= now))))
    elif key == "data":
        figures += [("New data in 24 h", str(sum(c["data_day"] for c in cards))),
                    ("New data in 7 days", str(sum(c["data_week"] for c in cards)))]
    else:
        longest = [c for c in cards if c.get("for")]
        if longest:
            figures.append(("Longest", longest[0]["for"]))
        if key in ("attention", "offline"):
            counted: dict[str, int] = {}
            for card in cards:
                reason = (card["headline"] or "unknown").split(":")[0] if key == "attention" else \
                    ("never connected" if not card["agent"].last_seen_at else "missed check-ins")
                counted[reason] = counted.get(reason, 0) + 1
            reasons = sorted(counted.items(), key=lambda pair: (-pair[1], pair[0]))
    if picked and key != "paused":
        figures.append(("Owners", str(len({a.owner_id for a in picked}))))
    return {"cards": cards, "figures": figures, "reasons": reasons,
            "next_countdown": iso(min(upcoming)) if key == "live" and upcoming else None,
            "mix": rhythm_mix(picked, states) if picked else []}


@router.get("/classes/{course_id}/feed/{key}")
def category_page(request: Request, course_id: int, key: str, viewer: Viewer = Depends(require_member),
                  db: Session = Depends(get_session)):
    """One feed category's dashboard (Live, Needs attention, Offline, Paused,
    New data): its agents as cards, whoever owns them."""
    labels = dict(CATEGORIES)
    if key not in labels:
        raise not_found("Unknown category")
    course = get_course(db, viewer, course_id)
    agents = shown_agents(db, viewer, course)
    now = utc_now()
    states = states_for(db, agents, now)
    sync_all(db, agents, states, now)
    counts = {k: n for k, _, n in summary(states)}
    ids = [agent.id for agent in agents]
    counts["data"] = db.exec(select(func.count(func.distinct(StateChange.agent_id))).where(
        StateChange.agent_id.in_(ids), StateChange.kind == "data", StateChange.at >= now - WEEK)).one() if ids else 0
    board = category_dashboard(db, agents, states, key, now)
    tabs = [{"key": k, "label": label, "count": counts[k]} for k, label in CATEGORIES]
    return render(request, db, viewer, "feed_category.html", course=course, key=key, label=labels[key],
                  note=CATEGORY_NOTES[key], tabs=tabs, **board)


# ---------------------------------------------------------------------------
# "This week" on each agent card (Yui's summary)
# ---------------------------------------------------------------------------

WEEK = timedelta(days=7)
WEEK_ITEMS = 4


def weekly_summaries(db: Session, agents: list[Agent], now: datetime | None = None) -> dict[int, list[str]]:
    """What changed for each agent in the last 7 days, in a few short items:
    times offline (and for how long in all), times it needed attention, new
    data, and new output categories and monitors. [] if nothing changed."""
    now = now or utc_now()
    since = now - WEEK
    ids = [agent.id for agent in agents]
    result: dict[int, list[str]] = {i: [] for i in ids}
    if not ids:
        return result
    rows: dict[int, list[StateChange]] = {i: [] for i in ids}
    for row in db.exec(select(StateChange).where(StateChange.agent_id.in_(ids), StateChange.kind != "start",
                                                 StateChange.at >= since)
                       .order_by(StateChange.at, StateChange.id)).all():
        rows[row.agent_id].append(row)
    new_categories: dict[int, list[str]] = {i: [] for i in ids}
    for agent_id, category, first in db.exec(
            select(Output.agent_id, Output.category, func.min(Output.created_at))
            .where(Output.agent_id.in_(ids)).group_by(Output.agent_id, Output.category)).all():
        if as_utc(first) >= since:
            new_categories[agent_id].append(category)
    new_monitors: dict[int, list[str]] = {i: [] for i in ids}
    for monitor in db.exec(select(Monitor).where(Monitor.agent_id.in_(ids), Monitor.created_at >= since)).all():
        new_monitors[monitor.agent_id].append(monitor.name)

    for agent_id in ids:
        items, history = [], rows[agent_id]
        offline = [row for row in history if row.state == "offline"]
        if offline:
            down = timedelta()
            for row in offline:  # until the next change (or now, if still offline)
                after = next((r for r in history if r.at > row.at and r.kind == "state"), None)
                down += (as_utc(after.at) if after else now) - as_utc(row.at)
            items.append(f"offline {len(offline)}× ({describe(down.total_seconds())})")
        attention = sum(1 for row in history if row.state == "attention")
        if attention:
            items.append(f"needed attention {attention}×")
        data = sum(1 for row in history if row.kind == "data")
        if data:
            items.append(f"{data}× new data")
        items += [f"new output \u201c{name}\u201d" for name in sorted(new_categories[agent_id], key=str.lower)]
        items += [f"new monitor \u201c{name}\u201d" for name in sorted(new_monitors[agent_id], key=str.lower)]
        result[agent_id] = items[:WEEK_ITEMS] + ([f"+{len(items) - WEEK_ITEMS} more"] if len(items) > WEEK_ITEMS else [])
    return result
