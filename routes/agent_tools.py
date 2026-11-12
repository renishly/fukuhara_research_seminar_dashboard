"""Agent helpers shared by the agent pages (agents.py), the agent API
(agent_api.py) and the class page's Agents tab (classes.py)."""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlmodel import Session, delete, func, select

import run_status
from run_status import RunStatus
from checkins import CHECK_IN_SLACK, allowed_gap, check_in_interval  # noqa: F401 (used here and re-exported)
from db import Agent, AgentFolder, Event, Folder, Monitor, MonitorNotifySetting, Output, StoredFile, as_utc, utc_now
from security import MIN_API_KEY_LENGTH, encrypt_api_key, hash_api_key
from uploads import delete_stored_file

# "Online" follows each agent's own rhythm: how often it checks in (1 second
# to 35 days), set by the agent (check_in_every) or by its owner on the agent
# page (the owner's setting wins). It counts as online until that time has
# passed plus some slack: half the interval, and at least 30 seconds, so one
# slow check-in doesn't flip it to offline. With no interval at all, lateness
# can't be judged, so the agent "needs attention" until one is set.
MIN_CHECK_IN = 1
MAX_CHECK_IN = 35 * 24 * 60 * 60

# The rhythm tag each interval gets: up to (but not including) each limit.
RHYTHMS = [
    (15 * 60, "Real-time"),
    (3 * 60 * 60, "Hourly"),
    (3 * 24 * 60 * 60, "Daily"),
    (14 * 24 * 60 * 60, "Weekly"),
    (MAX_CHECK_IN + 1, "Monthly"),
]

MAX_AGENT_NAME = 50
MAX_DESCRIPTION = 1000
MAX_FOLDER_NAME = 40
MAX_CATEGORY = 50  # output categories and monitor names
MAX_MONITORS_PER_AGENT = 100
MAX_OUTPUT_TEXT = 20_000
MAX_URL = 2000
LINK_PATTERN = re.compile(r"^https?://\S+$")

# Each agent keeps at most this many outputs and Activity lines; the oldest go
# first. This stops one chatty agent from filling the free 500 MB database.
MAX_OUTPUTS_PER_AGENT = 1000
MAX_EVENTS_PER_AGENT = 2000

# Something that arrives more than a minute after it was made was sent late,
# typically from the connector's offline buffer after an outage or a pause.
LATE_AFTER = timedelta(minutes=1)
LATE_NOTE = " (delivered late)"


def interval_mismatch(agent: Agent) -> bool:
    """True if the owner's setting overrides a different value the agent reports."""
    return bool(agent.owner_check_in_seconds and agent.check_in_seconds
                and agent.owner_check_in_seconds != agent.check_in_seconds)


def is_online(agent: Agent, now: datetime | None = None) -> bool:
    """True if the agent has checked in as often as expected.

    E.g. an agent that checks in every 5 seconds is online if seen in the last
    35 seconds; an hourly one, if seen in the last 90 minutes. Without an
    interval it can't be judged late, so it counts as online once connected.
    """
    if agent.last_seen_at is None:
        return False
    interval = check_in_interval(agent)
    if interval is None:
        return True
    return (now or utc_now()) - as_utc(agent.last_seen_at) <= allowed_gap(interval)


def rhythm_tag(seconds: int | None) -> str | None:
    """The general tag for an interval: "Real-time", "Hourly", "Daily",
    "Weekly" or "Monthly"; None if there's no interval."""
    if not seconds:
        return None
    for limit, tag in RHYTHMS:
        if seconds < limit:
            return tag
    return RHYTHMS[-1][1]


def rhythm_key(tag: str | None) -> str:
    """The CSS name of a rhythm tag's colour: "realtime", "daily"..., "unspecified"."""
    return (tag or "unspecified").lower().replace("-", "")


# States that mean an agent isn't running correctly. Paused isn't one: the
# owner chose it.
PROBLEM_STATES = ("offline", "attention")


def rhythm_mix(agents: list[Agent], states: "dict[int, AgentState]") -> list[dict]:
    """How many agents have each rhythm, in rhythm order (Real-time first,
    Unspecified last), leaving out rhythms nobody has. Each item:
    {"tag", "key", "count", "percent", "ok", "problems", "offline", "attention"},
    where problems = offline + needs attention, and ok = the rest. `states`
    comes from states_for()."""
    groups: dict[str | None, dict] = {}
    for agent in agents:
        state = states[agent.id]
        group = groups.setdefault(state.rhythm, {"count": 0, "offline": 0, "attention": 0})
        group["count"] += 1
        if state.key in PROBLEM_STATES:
            group[state.key] += 1
    total = len(agents)
    order = [tag for _, tag in RHYTHMS] + [None]
    mix = []
    for tag in order:
        if tag not in groups:
            continue
        group = groups[tag]
        problems = group["offline"] + group["attention"]
        mix.append({"tag": tag or "Unspecified", "key": rhythm_key(tag), "count": group["count"],
                    "percent": round(100 * group["count"] / total), "problems": problems,
                    "ok": group["count"] - problems, "offline": group["offline"], "attention": group["attention"],
                    "ok_percent": 100 * (group["count"] - problems) / total, "problem_percent": 100 * problems / total})
    return mix


def human_interval(seconds: int) -> str:
    """A check-in interval in words: "3 s", "5 min", "1 h 30 min", "1 day"."""
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [f"{days} day{'s' if days != 1 else ''}" if days else "",
             f"{hours} h" if hours else "", f"{minutes} min" if minutes else "", f"{secs} s" if secs else ""]
    return " ".join(part for part in parts if part)


def check_in_text(agent: Agent) -> str:
    interval = check_in_interval(agent)
    return "every " + human_interval(interval) if interval else "interval unspecified"


def rough_duration(seconds: float) -> str:
    """A length of time, roughly, in words: "40 s", "12 min", "3 h", "2 days"."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 2 * 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"


def ago(moment: datetime, now: datetime) -> str:
    """How long ago, roughly: "just now", "12 min ago", "3 h ago", "2 days ago"."""
    seconds = (now - as_utc(moment)).total_seconds()
    return "just now" if seconds < 60 else rough_duration(seconds) + " ago"


@dataclass
class AgentState:
    """What an agent's card says about it: one state, plus a line saying when
    it last checked in and (for anything but Live) a line saying why.

    key: "paused", "offline", "attention" or "live" (checked in that order;
    the first that applies wins). `when` and `why` are for members only;
    guests see the state's label and the rhythm tag.
    """

    key: str
    label: str
    when: str
    why: str | None
    rhythm: str | None  # "Daily" etc., None if the interval is unspecified
    interval: int | None

    @property
    def every(self) -> str | None:
        """The interval in words ("1 day"), or None."""
        return human_interval(self.interval) if self.interval else None


STATE_LABELS = {"paused": "Paused", "offline": "Offline", "attention": "Needs attention", "live": "Live"}


def agent_state(agent: Agent, now: datetime | None = None, run: RunStatus | None = None) -> AgentState:
    """Work out an agent's state from its check-ins and settings, and from its
    "Run status" monitor if it sends one (run_status.py). Use state_of() or
    states_for() to have the Run status loaded for you."""
    now = now or utc_now()
    interval = check_in_interval(agent)
    seen = as_utc(agent.last_seen_at) if agent.last_seen_at else None
    when = f"checked in {ago(seen, now)}" if seen else "never connected"

    def state(key: str, why: str | None) -> AgentState:
        return AgentState(key, STATE_LABELS[key], when, why, rhythm_tag(interval), interval)

    if agent.paused:
        return state("paused", "paused by owner")
    if seen is None:
        return state("offline", "hasn't connected yet")
    if interval is None:
        return state("attention", "interval unspecified: set it on the agent page or with check_in(every=...)")
    if now - seen > allowed_gap(interval):
        return state("offline", f"no check-in for {rough_duration((now - seen).total_seconds())}, "
                                f"expected every {human_interval(interval)}")
    problem = run.problem() if run else None
    if problem:
        return state("attention", problem)
    return state("live", None)


def states_for(db: Session, agents: list[Agent], now: datetime | None = None) -> dict[int, AgentState]:
    """Each agent's state, with all their Run status monitors read in one query."""
    now = now or utc_now()
    runs = run_status.load(db, [agent.id for agent in agents])
    return {agent.id: agent_state(agent, now, runs.get(agent.id)) for agent in agents}


def state_of(db: Session, agent: Agent, now: datetime | None = None) -> AgentState:
    return states_for(db, [agent], now)[agent.id]


def clean_name(raw: str, max_length: int) -> tuple[str, str | None]:
    """Trim a name and check its length. Returns (name, error or None)."""
    name = " ".join((raw or "").split())
    if not name:
        return name, "The name can't be empty."
    if len(name) > max_length:
        return name, f"The name can be at most {max_length} characters."
    return name, None


def agent_name_taken(db: Session, owner_id: int, course_id: int, name: str, except_id: int | None = None) -> bool:
    """Names are unique among one owner's agents in one class."""
    existing = db.exec(select(Agent).where(
        Agent.owner_id == owner_id, Agent.course_id == course_id, Agent.name == name
    )).first()
    return existing is not None and existing.id != except_id


def check_new_api_key(db: Session, api_key: str) -> str | None:
    """An error message if this key can't be used, or None if it's fine."""
    if len(api_key) < MIN_API_KEY_LENGTH or any(c.isspace() for c in api_key):
        return f"The API key must be at least {MIN_API_KEY_LENGTH} characters with no spaces."
    if db.exec(select(Agent).where(Agent.api_key_hash == hash_api_key(api_key))).first():
        return "That API key is already used by another agent."
    return None


def store_api_key(agent: Agent, api_key: str) -> None:
    """Save a key's fingerprint (to recognise it) and an encrypted copy (for "View key").

    Raises KeyEncryptionNotConfigured if KEY_ENCRYPTION_SECRET isn't set.
    """
    agent.api_key_encrypted = encrypt_api_key(api_key)
    agent.api_key_hash = hash_api_key(api_key)


def latest_event(db: Session, agent_id: int) -> Event | None:
    return db.exec(
        select(Event).where(Event.agent_id == agent_id).order_by(Event.created_at.desc(), Event.id.desc())
    ).first()


def latest_output(db: Session, agent_id: int, category: str) -> Output | None:
    return db.exec(
        select(Output).where(Output.agent_id == agent_id, Output.category == category)
        .order_by(Output.created_at.desc(), Output.id.desc())
    ).first()


def newest_per(db: Session, model, group_columns, where) -> list:
    """The newest row of `model` for each group (e.g. each agent), in ONE
    database query instead of one per group. It numbers each group's rows from
    the newest (a "window function", supported by Postgres and SQLite) and
    keeps row 1."""
    number = func.row_number().over(partition_by=group_columns, order_by=(model.created_at.desc(), model.id.desc()))
    ranked = select(model.id, number.label("n")).where(*where).subquery()
    ids = db.exec(select(ranked.c.id).where(ranked.c.n == 1)).all()
    return list(db.exec(select(model).where(model.id.in_(ids))).all()) if ids else []


def grid_details(db: Session, agents: list[Agent]) -> dict[int, dict]:
    """For a page of agent cards: each agent's latest activity line, folder tags
    and pinned item, in a handful of queries for the whole page (not three per card)."""
    ids = [agent.id for agent in agents]
    details = {agent_id: {"latest": None, "folders": [], "pinned": None} for agent_id in ids}
    if not ids:
        return details
    for event in newest_per(db, Event, [Event.agent_id], [Event.agent_id.in_(ids)]):
        details[event.agent_id]["latest"] = event
    for agent_id, folder in db.exec(
        select(AgentFolder.agent_id, Folder).join(Folder, Folder.id == AgentFolder.folder_id)
        .where(AgentFolder.agent_id.in_(ids)).order_by(Folder.name)
    ).all():
        details[agent_id]["folders"].append(folder)
    categories = {agent.pinned_category for agent in agents if agent.pinned_category}
    if categories:
        pinned = {(a.id, a.pinned_category) for a in agents if a.pinned_category}
        for output in newest_per(db, Output, [Output.agent_id, Output.category],
                                 [Output.agent_id.in_(ids), Output.category.in_(categories)]):
            if (output.agent_id, output.category) in pinned:
                details[output.agent_id]["pinned"] = output
    monitor_names_pinned = {a.pinned_monitor_name for a in agents if a.pinned_monitor_name and not a.pinned_category}
    if monitor_names_pinned:
        wanted = {(a.id, a.pinned_monitor_name) for a in agents if a.pinned_monitor_name and not a.pinned_category}
        for monitor in db.exec(select(Monitor).where(Monitor.agent_id.in_(ids),
                                                     Monitor.name.in_(monitor_names_pinned))).all():
            if (monitor.agent_id, monitor.name) in wanted:
                details[monitor.agent_id]["pinned"] = monitor
    return details


def folders_of(db: Session, agent_id: int) -> list[Folder]:
    """The folders an agent is tagged with, A to Z."""
    return list(db.exec(
        select(Folder).join(AgentFolder, AgentFolder.folder_id == Folder.id)
        .where(AgentFolder.agent_id == agent_id).order_by(Folder.name)
    ).all())


def file_details(db: Session, file_ids: list[int]) -> dict[int, StoredFile]:
    if not file_ids:
        return {}
    return {f.id: f for f in db.exec(select(StoredFile).where(StoredFile.id.in_(file_ids))).all()}


def output_categories(db: Session, agent_id: int) -> list[str]:
    names = db.exec(select(Output.category).where(Output.agent_id == agent_id).distinct()).all()
    return sorted(names, key=str.lower)


def count_outputs(db: Session, agent_id: int) -> int:
    return db.exec(select(func.count()).select_from(Output).where(Output.agent_id == agent_id)).one()


# ---------------------------------------------------------------------------
# Outputs and times sent by agents
# ---------------------------------------------------------------------------


def clean_category(raw: str, what: str = "category") -> str:
    """Trim an output category or monitor name and check it, or raise 422."""
    category = " ".join(str(raw).split())
    if not category or len(category) > MAX_CATEGORY:
        raise HTTPException(422, f"{what} must be 1 to {MAX_CATEGORY} characters")
    return category


def clean_link(raw: str | None) -> str | None:
    """A web address, if it's a safe http(s) one; None otherwise.

    Only http:// and https:// are allowed, so a link can never be a
    "javascript:" trick that runs code when clicked.
    """
    url = (raw or "").strip()
    if url and len(url) <= MAX_URL and LINK_PATTERN.match(url):
        return url
    return None


def describe_content(content: Any) -> tuple[str, str]:
    """How to store and show an output: (kind, value as text).

    numbers -> "number", web addresses -> "link", lists/dicts -> "json",
    everything else -> "text". (Files arrive through their own endpoint.)
    """
    if isinstance(content, bool):  # True/False are technically numbers in Python
        kind, value = "text", "true" if content else "false"
    elif isinstance(content, (int, float)):
        kind, value = "number", repr(content)
    elif isinstance(content, (list, dict)):
        kind, value = "json", json.dumps(content, indent=2, ensure_ascii=False)
    elif isinstance(content, str) and clean_link(content):
        kind, value = "link", content.strip()
    else:
        kind, value = "text", str(content)
    if len(value) > MAX_OUTPUT_TEXT:
        raise HTTPException(422, f"content is too long (max {MAX_OUTPUT_TEXT:,} characters)")
    return kind, value


def parse_time(raw: str | datetime | None, name: str = "created_at") -> datetime | None:
    """An ISO 8601 time sent by an agent, as UTC (422 if it can't be read)."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return as_utc(raw)
    try:
        return as_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
    except ValueError:
        raise HTTPException(422, f"{name} must be an ISO date and time, e.g. 2026-10-03T08:00:00Z")


def when_made(made_at: str | datetime | None) -> tuple[datetime, bool]:
    """When the agent says something happened, and whether it arrived late.

    A missing time means "now", and a time in the future (a computer clock
    that's off) also becomes "now".
    """
    now = utc_now()
    made = parse_time(made_at)
    if made is None or made > now:
        return now, False
    return made, now - made > LATE_AFTER


def log_activity(db: Session, agent_id: int, kind: str, message: str, data: dict | None = None,
                 created_at: datetime | None = None) -> None:
    """Add a timestamped line to an agent's Activity log. The caller commits."""
    db.add(Event(agent_id=agent_id, type=kind, message=message, data=data, created_at=created_at or utc_now()))


def preview(text: str, limit: int = 80) -> str:
    """The first line of some text, shortened for the Activity log."""
    lines = text.strip().splitlines()
    first = lines[0] if lines else ""
    return first if len(first) <= limit else first[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Monitors: named datapoints that are replaced (outputs are one-off items)
# ---------------------------------------------------------------------------


def monitor_names(db: Session, agent_id: int) -> list[str]:
    names = db.exec(select(Monitor.name).where(Monitor.agent_id == agent_id)).all()
    return sorted(names, key=str.lower)


def get_monitor(db: Session, agent_id: int, name: str) -> Monitor | None:
    return db.exec(select(Monitor).where(Monitor.agent_id == agent_id, Monitor.name == name)).first()


def remove_monitor(db: Session, agent: Agent, monitor: Monitor, note: str = "") -> None:
    """Delete one monitor (and its file), unpin it, and log it. The caller commits."""
    file_id, name = monitor.file_id, monitor.name
    db.delete(monitor)
    db.exec(delete(MonitorNotifySetting).where(MonitorNotifySetting.agent_id == agent.id,
                                               MonitorNotifySetting.name == name))
    if agent.pinned_monitor_name == name:
        agent.pinned_monitor_name = None
        db.add(agent)
    log_activity(db, agent.id, "monitor", f'Monitor "{name}" removed{note}')
    db.flush()
    delete_stored_file(db, file_id)


def set_monitor(db: Session, agent: Agent, name: str, kind: str | None, value: str | None,
                stored: StoredFile | None = None, made_at=None, note: str = "") -> bool:
    """Replace (or create; or, with kind=None, remove) the agent's monitor `name`. Commits.

    A late update older than that monitor's current value is skipped and
    returns False, so a late arrival never replaces something newer. Only
    creating and removing a monitor is noted in Activity: a monitor may be
    updated every few seconds, and each update simply replaces the last.
    """
    when, late = when_made(made_at)
    existing = get_monitor(db, agent.id, name)
    if existing is not None and when < as_utc(existing.updated_at):
        if stored is not None:
            db.flush()
            delete_stored_file(db, stored.id)
        log_activity(db, agent.id, "monitor", f'Older update of monitor "{name}" skipped{LATE_NOTE}', created_at=when)
        db.commit()
        return False
    if kind is None:  # remove it
        if existing is not None:
            remove_monitor(db, agent, existing, note)
        db.commit()
        return True
    if existing is None:
        count = db.exec(select(func.count(Monitor.id)).where(Monitor.agent_id == agent.id)).one()
        if count >= MAX_MONITORS_PER_AGENT:
            if stored is not None:
                db.flush()
                delete_stored_file(db, stored.id)
                db.commit()
            raise HTTPException(422, f"an agent can have at most {MAX_MONITORS_PER_AGENT} monitors; remove some first")
        existing = Monitor(agent_id=agent.id, name=name, kind=kind, value=value)
        log_activity(db, agent.id, "monitor", f'New monitor "{name}"{note}' + (LATE_NOTE if late else ""),
                     created_at=when)
    old_file = existing.file_id
    existing.kind, existing.value = kind, value
    existing.file_id = stored.id if stored else None
    existing.updated_at = when
    db.add(existing)
    db.flush()
    if old_file and old_file != existing.file_id:
        delete_stored_file(db, old_file)
    db.commit()
    return True


def trim_history(db: Session, agent_id: int) -> None:
    """Delete an agent's oldest outputs (with their files) and Activity lines
    beyond the caps above. The caller commits."""
    def oldest_beyond(model, cap):
        return db.exec(
            select(model).where(model.agent_id == agent_id)
            .order_by(model.created_at.desc(), model.id.desc()).offset(cap)
        ).all()

    for output in oldest_beyond(Output, MAX_OUTPUTS_PER_AGENT):
        file_id = output.file_id
        db.delete(output)
        db.flush()
        delete_stored_file(db, file_id)
    for event in oldest_beyond(Event, MAX_EVENTS_PER_AGENT):
        db.delete(event)
