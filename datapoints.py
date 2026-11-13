"""Data points: how many data points of each type an agent's forecast rests on.

An agent declares its *current setup* in a monitor named "Data points" (any
case). A monitor is replaced each time it's sent, so the newest declaration
always wins. A failed fetch on one run doesn't change the count. Only
changing the setup does: dropping a data point, or no longer using a helper
agent.

    set_monitor({
      "data_points": {                      # type -> its data points
        "Market prices": ["Rohto share price", "Nikkei 225"],
        "Internal values": ["DCF fair value"],
        "Company data": 3                   # or just a count
      },
      "debates": 1,                         # debates run to reach the forecast
      "uses": ["News Scout"]                # this student's agents that feed this one
    }, "Data points")

Each type's data points can be listed by name or given as a count. Names are
only used to count a data point once when several agents declare it, and are
never shown. Counts can't be matched that way, so they are always added.

An agent's totals are its own data points plus those of every agent it
`uses`, followed through chains (A uses B, B uses C), each agent once. Only
the same student's agents in the same class can be used. Debates are added
up across those agents.
"""

import json
from dataclasses import dataclass, field

from sqlmodel import Session, func, select

from db import Agent, Monitor

MONITOR = "data points"
# The fixed list, in display order. Internal values are what the agent works
# out itself (fair values, its own probabilities...); every other type is data
# from outside. A type the dashboard doesn't know counts as Other.
TYPES = [
    ("internal", "Internal values"),
    ("market", "Market prices"),
    ("company", "Company data"),
    ("economic", "Economic indicators"),
    ("analyst", "Analyst & consensus"),
    ("news", "News & text"),
    ("alternative", "Alternative data"),
    ("other", "Other"),
]
LABELS = dict(TYPES)
MAX_NAMES = 500  # per type per agent
MAX_COUNT = 10_000  # a sent count is capped here
MAX_USES = 50


def _key(name) -> str:
    """ "Market prices", "market_prices", "MARKET" -> "market"; unknown -> "other"."""
    text = " ".join(str(name).replace("_", " ").replace("-", " ").split()).lower()
    for key, label in TYPES:
        if text in (key, label.lower(), label.lower().replace(" & ", " and ")):
            return key
    return "other"


def _count(raw) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return max(0, min(int(raw), MAX_COUNT))


@dataclass
class Declaration:
    """One agent's own "Data points" monitor, as read."""

    names: dict[str, set[str]] = field(default_factory=dict)  # type -> data point names (lower case)
    counts: dict[str, int] = field(default_factory=dict)  # type -> counts sent as numbers
    debates: int = 0
    uses: list = field(default_factory=list)  # agent names (or ids) as sent


def parse(value: str) -> Declaration | None:
    """A "Data points" monitor's content, or None if it can't be read."""
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    points = data.get("data_points")
    declaration = Declaration(debates=_count(data.get("debates")) or 0)
    if isinstance(points, dict):
        for name, entry in points.items():
            key = _key(name)
            if isinstance(entry, list):
                names = {" ".join(str(item).split()).lower() for item in entry[:MAX_NAMES]
                         if isinstance(item, (str, int, float)) and not isinstance(item, bool) and str(item).strip()}
                declaration.names.setdefault(key, set()).update(names)
            elif (count := _count(entry)) is not None:
                declaration.counts[key] = declaration.counts.get(key, 0) + count
    uses = data.get("uses")
    if isinstance(uses, list):
        declaration.uses = [item for item in uses[:MAX_USES] if isinstance(item, (str, int)) and not isinstance(item, bool)]
    if not declaration.names and not declaration.counts and not declaration.debates and not declaration.uses:
        return None
    return declaration


@dataclass
class Breakdown:
    """An agent's data points, its helpers' included."""

    counts: dict[str, int]  # type key -> data points, every type in TYPES order (0 if none)
    debates: int
    helpers: list[Agent]  # agents it uses, directly or through others, in the order reached
    missing: list[str]  # names in `uses` that aren't one of this student's agents in this class

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def parts(self) -> list[dict]:
        """The types with any data points, for the share bar and the list:
        {"key", "label", "count", "percent"}."""
        total = self.total
        return [{"key": key, "label": LABELS[key], "count": n, "percent": round(100 * n / total) if total else 0}
                for key, n in self.counts.items() if n]

    @property
    def external(self) -> int:
        return self.total - self.counts["internal"]


def _declarations(db: Session, agent_ids: set[int]) -> dict[int, Declaration]:
    monitors = db.exec(select(Monitor).where(Monitor.agent_id.in_(agent_ids), func.lower(Monitor.name) == MONITOR,
                                             Monitor.kind == "json")).all() if agent_ids else []
    found = {}
    for monitor in monitors:
        declaration = parse(monitor.value)
        if declaration is not None:
            found[monitor.agent_id] = declaration
    return found


def breakdowns(db: Session, agents: list[Agent]) -> dict[int, Breakdown]:
    """Each agent's Breakdown (agents that declare nothing are left out)."""
    if not agents:
        return {}
    # Every agent of these students in these classes: the ones that can be used.
    pairs = {(agent.owner_id, agent.course_id) for agent in agents}
    siblings = db.exec(select(Agent).where(Agent.owner_id.in_({o for o, _ in pairs}),
                                           Agent.course_id.in_({c for _, c in pairs}))).all()
    by_id = {agent.id: agent for agent in siblings} | {agent.id: agent for agent in agents}
    declared = _declarations(db, set(by_id))
    result = {}
    for agent in agents:
        if agent.id not in declared:
            continue
        family = {(a.name.lower(), a.id): a for a in by_id.values()
                  if a.owner_id == agent.owner_id and a.course_id == agent.course_id}
        by_name = {name: a for (name, _), a in family.items()}
        by_number = {a.id: a for a in family.values()}
        names: dict[str, set[str]] = {}
        counts = {key: 0 for key, _ in TYPES}
        debates = 0
        helpers: list[Agent] = []
        missing: list[str] = []
        seen = {agent.id}
        queue = [agent.id]
        while queue:  # breadth first; `seen` stops loops and counts each agent once
            current = queue.pop(0)
            declaration = declared.get(current)
            if declaration is None:
                continue
            for key, points in declaration.names.items():
                names.setdefault(key, set()).update(points)
            for key, n in declaration.counts.items():
                counts[key] += n
            debates += declaration.debates
            for ref in declaration.uses:
                used = by_number.get(ref) if isinstance(ref, int) else by_name.get(" ".join(ref.split()).lower())
                if used is None:
                    if current == agent.id:
                        missing.append(str(ref))
                    continue
                if used.id not in seen:
                    seen.add(used.id)
                    helpers.append(used)
                    queue.append(used.id)
        for key, points in names.items():
            counts[key] += len(points)
        result[agent.id] = Breakdown(counts=counts, debates=debates, helpers=helpers, missing=missing)
    return result


def breakdown(db: Session, agent: Agent) -> Breakdown | None:
    return breakdowns(db, [agent]).get(agent.id)
