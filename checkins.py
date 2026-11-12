"""Each agent's check-in history, for the "Check-in activity" graph on its page.

The dashboard used to keep only the time an agent was last seen. To draw its
history, every API call is also recorded here, but compactly: calls that come
close together (less than half an interval apart) are merged into one *span*
of activity (started_at to ended_at). So a gap between two spans is exactly
the time the agent was silent, and an agent that checks in once a day makes
one small row a day, however many calls each run makes.

The graph shows the latest 12 intervals. Each gap between spans is
  on time  up to 1.5x the interval (the same slack as the Live/Offline state),
  late     from there up to 2x the interval,
  missed   at 2x or more, with one mark per expected check-in that never came
           (a gap of 3.4 intervals = 2 missed check-ins).
The gap from the last span to now is judged the same way ("so far").

Only what the graph can show is kept: spans older than 13 intervals go.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlmodel import Session, delete, select

from db import Agent, CheckInSpan, as_utc, utc_now

JST = ZoneInfo("Asia/Tokyo")
CHECK_IN_SLACK = 30  # seconds: the least slack an agent gets, however short its interval
WINDOW_INTERVALS = 12  # the graph shows this many intervals
KEEP_INTERVALS = 13  # spans older than this many intervals are deleted
UNSPECIFIED_MERGE = 60  # seconds: how close calls merge when the agent has no interval
MAX_SPANS_PER_AGENT = 500


def check_in_interval(agent: Agent) -> int | None:
    """How often the agent is expected to check in (seconds), or None if
    nobody has said. The owner's setting wins over the agent's own."""
    return agent.owner_check_in_seconds or agent.check_in_seconds or None


def allowed_gap(interval: int) -> timedelta:
    """How long after its last check-in an agent still counts as on time."""
    return timedelta(seconds=interval + max(CHECK_IN_SLACK, interval / 2))


def missed_after(interval: int) -> timedelta:
    """A gap at least this long counts as missed check-ins (2x the interval,
    but never less than the on-time allowance)."""
    return max(timedelta(seconds=2 * interval), allowed_gap(interval))


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def record_activity(db: Session, agent: Agent, now: datetime | None = None) -> None:
    """Note that the agent was just seen: extend its latest span, or start a
    new one after a pause. Called on every API call; the caller commits."""
    now = now or utc_now()
    interval = check_in_interval(agent)
    merge = timedelta(seconds=interval / 2 if interval else UNSPECIFIED_MERGE)
    latest = db.exec(select(CheckInSpan).where(CheckInSpan.agent_id == agent.id)
                     .order_by(CheckInSpan.ended_at.desc()).limit(1)).first()
    if latest is not None and now - as_utc(latest.ended_at) < merge:
        latest.ended_at = now
        db.add(latest)
        return
    db.add(CheckInSpan(agent_id=agent.id, started_at=now, ended_at=now))
    if agent.check_ins_since is None:
        agent.check_ins_since = now
        db.add(agent)
    forget_old_spans(db, agent, now)


def forget_old_spans(db: Session, agent: Agent, now: datetime) -> None:
    """Delete spans the graph can no longer show (and any beyond the cap)."""
    keep = timedelta(seconds=KEEP_INTERVALS * (check_in_interval(agent) or 35 * 86400))
    db.exec(delete(CheckInSpan).where(CheckInSpan.agent_id == agent.id, CheckInSpan.ended_at < now - keep))
    extra = db.exec(select(CheckInSpan.id).where(CheckInSpan.agent_id == agent.id)
                    .order_by(CheckInSpan.ended_at.desc()).offset(MAX_SPANS_PER_AGENT)).all()
    if extra:
        db.exec(delete(CheckInSpan).where(CheckInSpan.id.in_(extra)))


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------

# How each rhythm labels its time axis (in Japan time).
AXIS_FORMATS = {"Real-time": "%H:%M", "Hourly": "%H:%M", "Daily": "%m/%d", "Weekly": "%m/%d", "Monthly": "%m/%d"}


Y_CAP = 3.0  # bars are drawn up to 3 intervals of silence; longer ones are cut off (and say so)


@dataclass
class Bar:
    """One check-in. Its height is how long the agent had been silent before
    it, in intervals (1.0 = exactly on schedule)."""

    x: float  # 0 to 1 across the window
    height: float  # 0 to 1 of the graph's height
    kind: str  # "ok" (green), "late" (orange) or "first" (the first one recorded: nothing to compare)
    text: str  # for the hover text
    cut: bool = False  # taller than the graph: drawn cut off


@dataclass
class Activity:
    """Everything the bar graph draws, with x positions from 0 (12 intervals ago) to 1 (now)."""

    interval: int
    window_text: str  # "12 days"
    y_max: float  # the top of the graph, in intervals (at least 2)
    bars: list[Bar] = field(default_factory=list)
    misses: list[tuple[float, str]] = field(default_factory=list)  # (x, hover text): expected but never came
    waiting: Bar | None = None  # silent right now for longer than allowed: a hollow bar at "now"
    ticks: list[tuple[float, str]] = field(default_factory=list)  # (x, label or ""): one per interval
    guides: list[tuple[float, str]] = field(default_factory=list)  # (height 0 to 1, label): 1x and 1.5x lines
    unrecorded_until: float = 0.0  # before this x, nothing was recorded yet
    on_time_count: int = 0
    late_count: int = 0
    missed_count: int = 0


def describe(seconds: float) -> str:
    """A length of time in words for hover texts: "45 s", "12 min", "5 h 10 min", "2 days 3 h"."""
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} day{'s' if days != 1 else ''}" + (f" {hours} h" if hours else "")
    if hours:
        return f"{hours} h" + (f" {minutes} min" if minutes else "")
    if minutes:
        return f"{minutes} min" + (f" {secs} s" if secs and minutes < 10 else "")
    return f"{secs} s"


def stamp(moment: datetime, interval: int) -> str:
    """A time for hover texts, as precise as the interval needs (Japan time)."""
    local = as_utc(moment).astimezone(JST)
    if interval < 15 * 60:
        return local.strftime("%m/%d %H:%M:%S")
    return local.strftime("%m/%d %H:%M")


def activity(db: Session, agent: Agent, rhythm: str | None, now: datetime | None = None) -> Activity | None:
    """The bar graph's contents for the latest 12 intervals, or None if the
    agent has no interval (lateness can't be judged without one).

    One bar per check-in (the start of each span), as tall as the silence
    before it: green when on time, orange when late (over 1.5x the interval,
    including a check-in that finally comes after misses). Each expected
    check-in that never came is a red mark on the axis, with no bar.
    """
    interval = check_in_interval(agent)
    if not interval:
        return None
    now = now or utc_now()
    step = timedelta(seconds=interval)
    start = now - WINDOW_INTERVALS * step
    width = (now - start).total_seconds()
    every = describe(interval)

    def x(moment: datetime) -> float:
        return min(1.0, max(0.0, (as_utc(moment) - start).total_seconds() / width))

    spans = list(db.exec(select(CheckInSpan).where(CheckInSpan.agent_id == agent.id, CheckInSpan.started_at >= start)
                         .order_by(CheckInSpan.started_at)).all())
    # The last span before the window too, so the first bar's silence is known.
    before = db.exec(select(CheckInSpan).where(CheckInSpan.agent_id == agent.id, CheckInSpan.started_at < start)
                     .order_by(CheckInSpan.started_at.desc()).limit(1)).first()
    since = as_utc(agent.check_ins_since) if agent.check_ins_since else now

    raw: list[tuple[float, float, str, str]] = []  # (x, silence in intervals, kind, text)
    misses: list[tuple[float, str]] = []

    def note_misses(gap_start: datetime, gap_end: datetime) -> None:
        if gap_end - gap_start < missed_after(interval):
            return
        expected = gap_start + step
        while expected < gap_end - step / 2:
            if expected >= start:
                misses.append((x(expected), f"Missed: no check-in by {stamp(expected, interval)} (expected every {every})"))
            expected += step

    previous = before
    for span in spans:
        when = stamp(span.started_at, interval)
        if previous is None:
            raw.append((x(span.started_at), 1.0, "first", f"Checked in {when} (the first one recorded)"))
        else:
            gap_start, gap_end = as_utc(previous.ended_at), as_utc(span.started_at)
            silence = (gap_end - gap_start).total_seconds()
            late = gap_end - gap_start > allowed_gap(interval)
            raw.append((x(span.started_at), silence / interval, "late" if late else "ok",
                        f"{'Late' if late else 'On time'}: checked in {when}, {describe(silence)} after the one before "
                        f"(expected every {every})"))
            note_misses(gap_start, gap_end)
        previous = span

    waiting = None
    if previous is not None:
        silent = now - as_utc(previous.ended_at)
        if silent > allowed_gap(interval):
            waiting = (silent.total_seconds() / interval,
                       f"No check-in for {describe(silent.total_seconds())} so far (expected every {every})")
        note_misses(as_utc(previous.ended_at), now)

    tallest = max([r[1] for r in raw] + [waiting[0] if waiting else 0, 2.0])
    y_max = min(Y_CAP, math.ceil(tallest * 2) / 2)  # up to the next half interval, at most 3

    def bar(x_pos: float, silence: float, kind: str, text: str) -> Bar:
        return Bar(x_pos, min(silence, y_max) / y_max, kind, text, cut=silence > y_max)

    result = Activity(interval=interval, window_text=describe(width), y_max=y_max,
                      bars=[bar(*r) for r in raw], misses=misses, unrecorded_until=x(since),
                      guides=[(1 / y_max, "expected"), (1.5 / y_max, "late")],
                      on_time_count=sum(r[2] in ("ok", "first") for r in raw),
                      late_count=sum(r[2] == "late" for r in raw), missed_count=len(misses))
    if waiting:
        result.waiting = bar(1.0, waiting[0], "waiting", waiting[1])

    # One gridline per interval, counting back from now; about 4 labelled.
    label_every = 3
    fmt = AXIS_FORMATS.get(rhythm or "", "%m/%d %H:%M")
    for n in range(WINDOW_INTERVALS + 1):
        moment = start + n * step
        label = moment.astimezone(JST).strftime(fmt).lstrip("0") if n % label_every == 0 else ""
        result.ticks.append((n / WINDOW_INTERVALS, "now" if n == WINDOW_INTERVALS else label))
    return result
