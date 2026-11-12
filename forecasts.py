"""Jimin's standard forecasts: every member's agent presents the same things
each week, so forecasts can be compared across the seminar.

An agent sends its weekly forecast as an output in the category "Forecast"
(any case), as JSON:

    output({
      "target": "Mitsui Fudosan (8801)",   # what it forecasts
      "week": "2026-W46",                  # optional: ISO week (default: the week it was sent, Japan time)
      "value": 1205,                       # the forecast, e.g. a fair value
      "unit": "JPY",                       # optional
      "market": 1470,                      # optional: today's market price, for comparison
      "view": "Underweight",               # optional: e.g. Overweight / Neutral / Underweight
      "why_changed": "...",                # optional: why it differs from last week
      "evidence": [{"title": "...", "url": "https://..."}]   # optional: sources
    }, "Forecast")

Outputs keep their history, so earlier weeks come for free. Per week, the
newest Forecast counts. Anything that can't be read is ignored. The "AI agent
debates" are the agent page's Debate tab (outputs whose category contains
"debate").
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlmodel import Session, func, select

from db import Agent, Output, as_utc
from routes.agent_tools import clean_link
from web import JST, templates

CATEGORY = "forecast"
WEEK = re.compile(r"^(\d{4})-W(\d{2})$")
MAX_WEEKS = 12  # weeks shown on an agent's Forecast tab
MAX_EVIDENCE = 20


@dataclass
class Forecast:
    week: str  # "2026-W46"
    target: str
    value: float
    unit: str = ""
    market: float | None = None
    view: str = ""
    why_changed: str = ""
    evidence: list[tuple[str, str]] = field(default_factory=list)  # (title, safe http(s) url)
    sent_at: datetime | None = None

    @property
    def week_label(self) -> str:
        """ "Week of Nov 9, 2026" (the Monday), or the raw week if it isn't valid."""
        match = WEEK.match(self.week)
        if not match:
            return self.week
        monday = datetime.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
        return f"Week of {monday:%b} {monday.day}, {monday.year}"

    @property
    def vs_market(self) -> float | None:
        """How far the forecast is from the market price, in % (None without one)."""
        return (self.value - self.market) / self.market * 100 if self.market else None


def week_of(moment: datetime) -> str:
    year, week, _ = as_utc(moment).astimezone(JST).isocalendar()
    return f"{year}-W{week:02d}"


def _number(raw) -> float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _text(raw, limit: int) -> str:
    return " ".join(str(raw).split())[:limit] if raw is not None else ""


def parse(output: Output) -> Forecast | None:
    """A Forecast output's content, or None if it can't be read."""
    if output.category.strip().lower() != CATEGORY or output.kind != "json":
        return None
    try:
        data = json.loads(output.value)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = _number(data.get("value"))
    if value is None:
        return None
    week = str(data.get("week") or "")
    if not WEEK.match(week):
        week = week_of(output.created_at)
    evidence = []
    for item in data.get("evidence") or []:
        if isinstance(item, dict):
            url = clean_link(str(item.get("url") or ""))
            title = _text(item.get("title"), 120) or url
            if url or title:
                evidence.append((title, url))
        elif isinstance(item, str) and item.strip():
            evidence.append((_text(item, 120), clean_link(item)))
        if len(evidence) >= MAX_EVIDENCE:
            break
    why = str(data.get("why_changed") or "").strip()[:3000]
    return Forecast(week=week, target=_text(data.get("target"), 120) or "(no target given)", value=value,
                    unit=_text(data.get("unit"), 12), market=_number(data.get("market")),
                    view=_text(data.get("view"), 40), why_changed=why, evidence=evidence,
                    sent_at=as_utc(output.created_at))


def weekly(db: Session, agent_ids: list[int], weeks: int = MAX_WEEKS) -> dict[int, list[Forecast]]:
    """Each agent's forecasts, one per week (its newest that week), newest week first."""
    result: dict[int, list[Forecast]] = {i: [] for i in agent_ids}
    if not agent_ids:
        return result
    outputs = db.exec(select(Output).where(Output.agent_id.in_(agent_ids), func.lower(Output.category) == CATEGORY)
                      .order_by(Output.created_at.desc(), Output.id.desc())).all()
    for output in outputs:
        forecast = parse(output)
        if forecast is None:
            continue
        mine = result[output.agent_id]
        if len(mine) < weeks and all(f.week != forecast.week for f in mine):
            mine.append(forecast)
    for mine in result.values():
        mine.sort(key=lambda f: f.week, reverse=True)
    return result


def change(this: Forecast, last: Forecast | None) -> dict | None:
    """This week's forecast against last week's: the difference and %."""
    if last is None:
        return None
    difference = this.value - last.value
    percent = difference / last.value * 100 if last.value else None
    return {"difference": difference, "percent": percent,
            "direction": "up" if difference > 0 else ("down" if difference < 0 else "same")}


def overview(db: Session, agents: list[Agent]) -> list[dict]:
    """Level 1: one row per agent that sends forecasts: this week's and the
    week before, and the change. Agents without forecasts are left out."""
    by_agent = weekly(db, [agent.id for agent in agents], weeks=2)
    rows = []
    for agent in agents:
        forecasts = by_agent[agent.id]
        if not forecasts:
            continue
        this, last = forecasts[0], forecasts[1] if len(forecasts) > 1 else None
        rows.append({"agent": agent, "this": this, "last": last, "change": change(this, last)})
    rows.sort(key=lambda row: (row["this"].target.lower(), row["agent"].name.lower()))
    return rows


def by_person(db: Session, students: list, agents: list[Agent]) -> list[dict]:
    """Level 1, per person: every student, with one line per agent of theirs
    that sends forecasts ([] = no forecast yet)."""
    rows = overview(db, agents)
    return [{"student": student, "rows": [row for row in rows if row["agent"].owner_id == student.id]}
            for student in students]


def number(value: float | None) -> str:
    """1205.0 -> "1,205"; 12.345 -> "12.35"."""
    if value is None:
        return "—"
    return f"{value:,.0f}" if abs(value) >= 100 or value == int(value) else f"{value:,.2f}"


templates.env.filters["num"] = number
