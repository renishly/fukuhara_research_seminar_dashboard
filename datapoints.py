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

or, leaving the types to the dashboard, just the names:

    set_monitor({"data_points": ["Rohto share price", "BOJ Tankan", "DCF fair value"]}, "Data points")

Types: the dashboard suggests one for every name (datapoint_types.py: the AI
model, keyword rules until it has looked). A name sent without a type, or
under a type the dashboard doesn't know, is counted under the suggested type.
A name the agent gave a type keeps it; if the suggestion differs, only the
agent's owner is told, so they can fix the agent. Counts can't be sorted, so a
count under an unknown type is Other.

Names make a data point used by several agents count once (under the first
type it was given), and are only ever shown to the agent's owner. An agent's
totals are its own data points plus those of every agent it `uses`, followed
through chains (A uses B, B uses C), each agent once. Only the same student's
agents in the same class can be used. Debates are added up across them.
"""

import json
from dataclasses import dataclass, field

from sqlmodel import Session, func, select

from datapoint_types import LABELS, TYPES, clean_name, suggestions, type_key
from db import Agent, Monitor

MONITOR = "data points"
MAX_NAMES = 500  # per type per agent
MAX_COUNT = 10_000  # a sent count is capped here
MAX_USES = 50


def _count(raw) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return max(0, min(int(raw), MAX_COUNT))


def _names(entry: list) -> set[str]:
    return {clean_name(item) for item in entry[:MAX_NAMES]
            if isinstance(item, (str, int, float)) and not isinstance(item, bool) and str(item).strip()}


@dataclass
class Declaration:
    """One agent's own "Data points" monitor, as read."""

    names: dict[str, set[str]] = field(default_factory=dict)  # type the agent gave -> names (lower case)
    untyped: set[str] = field(default_factory=set)  # names sent without a (known) type
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
    if isinstance(points, list):  # names only: the dashboard sorts them
        declaration.untyped |= _names(points)
    elif isinstance(points, dict):
        for name, entry in points.items():
            key = type_key(name)
            if isinstance(entry, list):
                if key:
                    declaration.names.setdefault(key, set()).update(_names(entry))
                else:
                    declaration.untyped |= _names(entry)
            elif (count := _count(entry)) is not None:
                declaration.counts[key or "other"] = declaration.counts.get(key or "other", 0) + count
    uses = data.get("uses")
    if isinstance(uses, list):
        declaration.uses = [item for item in uses[:MAX_USES] if isinstance(item, (str, int)) and not isinstance(item, bool)]
    if not (declaration.names or declaration.untyped or declaration.counts or declaration.debates or declaration.uses):
        return None
    return declaration


@dataclass
class Breakdown:
    """An agent's data points, its helpers' included."""

    counts: dict[str, int]  # type key -> data points, every type in TYPES order (0 if none)
    debates: int
    helpers: list[Agent]  # agents it uses, directly or through others, in the order reached
    missing: list[str]  # names in `uses` that aren't one of this student's agents in this class
    sorted_by: dict[str, int] = field(default_factory=dict)  # names the dashboard typed: {"ai": n, "rules": n}
    # Names the agent typed where the dashboard suggests another type (owner only):
    # [{"name", "given", "suggested", "how", "agent"}]
    disagreements: list[dict] = field(default_factory=list)

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

    @property
    def auto_sorted(self) -> int:
        return sum(self.sorted_by.values())


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
    every_name = set()
    for declaration in declared.values():
        every_name |= declaration.untyped
        for points in declaration.names.values():
            every_name |= points
    suggested = suggestions(db, every_name)
    result = {}
    for agent in agents:
        if agent.id not in declared:
            continue
        family = [a for a in by_id.values() if a.owner_id == agent.owner_id and a.course_id == agent.course_id]
        by_name = {a.name.lower(): a for a in family}
        by_number = {a.id: a for a in family}
        typed: dict[str, str] = {}  # name -> the type it counts under (first one given wins)
        untyped: list[str] = []
        counts = {key: 0 for key, _ in TYPES}
        debates = 0
        helpers: list[Agent] = []
        missing: list[str] = []
        disagreements: list[dict] = []
        seen = {agent.id}
        queue = [agent.id]
        while queue:  # breadth first; `seen` stops loops and counts each agent once
            current = queue.pop(0)
            declaration = declared.get(current)
            if declaration is None:
                continue
            for key, points in declaration.names.items():
                for name in sorted(points):
                    if name not in typed:
                        typed[name] = key
                        hint, how = suggested[name]
                        # Keyword rules' "other" only means they didn't recognise it: not a suggestion.
                        if hint != key and not (how == "rules" and hint == "other"):
                            disagreements.append({"name": name, "given": LABELS[key], "suggested": LABELS[hint],
                                                  "how": how, "agent": by_id[current]})
            untyped += sorted(declaration.untyped)
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
        sorted_by: dict[str, int] = {}
        for name in untyped:
            if name not in typed:  # a name some agent typed keeps that type
                typed[name], how = suggested[name]
                sorted_by[how] = sorted_by.get(how, 0) + 1
        for key in typed.values():
            counts[key] += 1
        result[agent.id] = Breakdown(counts=counts, debates=debates, helpers=helpers, missing=missing,
                                     sorted_by=sorted_by, disagreements=disagreements)
    return result


def breakdown(db: Session, agent: Agent) -> Breakdown | None:
    return breakdowns(db, [agent]).get(agent.id)
