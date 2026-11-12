"""The "Run status" monitor: the seminar's Automatic Updates & Logs standard.

An agent may keep a monitor named exactly "Run status" (set_monitor(content,
"Run status")) holding, as JSON:

    {
      "last_run": "2026-11-10T04:22:00+09:00",
      "last_status": "new_data",   # failed | not_due | no_new_data | new_data
      "steps": [
        {"name": "competitor prices", "state": "failing",   # ok | failing | silent
         "last_run": "...", "last_new_data": "...", "note": "HTTP 503 from source, 2 runs in a row"}
      ]
    }

The dashboard only reads it: a failed run, or a failing or silent step, makes
the agent "Needs attention" (see agent_tools.agent_state()), and the agent's
page shows the steps as reported. Anything missing or malformed is ignored
(an agent without this monitor simply shows Live or Offline).
"""

import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlmodel import Session, select

from db import Monitor, as_utc

RUN_STATUS = "Run status"
RUN_RESULTS = ("failed", "not_due", "no_new_data", "new_data")
STEP_STATES = ("ok", "failing", "silent")
MAX_STEPS = 50


@dataclass
class RunStep:
    name: str
    state: str  # "ok", "failing" or "silent" ("unknown" if it said something else)
    last_run: datetime | None = None
    last_new_data: datetime | None = None
    note: str | None = None


@dataclass
class RunStatus:
    last_run: datetime | None
    last_status: str | None  # one of RUN_RESULTS, or None
    steps: list[RunStep] = field(default_factory=list)
    updated_at: datetime | None = None  # when the agent sent it

    def problem(self) -> str | None:
        """Why the agent needs attention, or None if the last run went fine."""
        failing = [step for step in self.steps if step.state == "failing"]
        silent = [step for step in self.steps if step.state == "silent"]
        if self.last_status == "failed":
            return "last run failed" + (f": {', '.join(s.name for s in failing)} failing" if failing else "")
        for kind, steps in (("failing", failing), ("silent", silent)):
            if steps:
                first = steps[0]
                why = f"{first.name} {kind}" + (f": {first.note}" if first.note else "")
                return why + (f" (+{len(steps) - 1} more)" if len(steps) > 1 else "")
        return None


def _time(raw) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return as_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def _text(raw, limit: int) -> str | None:
    if raw is None:
        return None
    text = " ".join(str(raw).split())
    return text[:limit] or None


def parse(monitor: Monitor | None) -> RunStatus | None:
    """A Run status monitor's content, or None if it isn't one or can't be read."""
    if monitor is None or monitor.name != RUN_STATUS or monitor.kind != "json":
        return None
    try:
        data = json.loads(monitor.value)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    last_status = data.get("last_status")
    steps = []
    for raw in data.get("steps") or []:
        if not isinstance(raw, dict) or not _text(raw.get("name"), 80):
            continue
        state = raw.get("state")
        steps.append(RunStep(name=_text(raw.get("name"), 80), state=state if state in STEP_STATES else "unknown",
                             last_run=_time(raw.get("last_run")), last_new_data=_time(raw.get("last_new_data")),
                             note=_text(raw.get("note"), 300)))
        if len(steps) >= MAX_STEPS:
            break
    return RunStatus(last_run=_time(data.get("last_run")),
                     last_status=last_status if last_status in RUN_RESULTS else None,
                     steps=steps, updated_at=as_utc(monitor.updated_at))


def load(db: Session, agent_ids: list[int]) -> dict[int, RunStatus]:
    """The readable Run status of each of these agents that has one."""
    if not agent_ids:
        return {}
    monitors = db.exec(select(Monitor).where(Monitor.agent_id.in_(agent_ids), Monitor.name == RUN_STATUS)).all()
    found = {}
    for monitor in monitors:
        status = parse(monitor)
        if status is not None:
            found[monitor.agent_id] = status
    return found
