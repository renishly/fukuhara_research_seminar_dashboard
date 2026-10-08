"""The agent API: what agents (through the connector) talk to.

Every request must send the agent's own key in the X-API-Key header; the key
says which agent is calling (see require_agent in security.py). Replies are
JSON. Explore it at /docs while the site runs.

Any agent can READ the data of every agent in the same class (to build on
each other's work), but can only DELETE its own. Reads never include API
keys, Login IDs, Inbox files, or the Start/Pause/messages sent to an agent.
"""

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlmodel import Session, func, select

from db import (
    LEGACY_MONITOR_NAME,
    Agent,
    Command,
    Event,
    Gen,
    InboxFile,
    Monitor,
    Output,
    StoredFile,
    User,
    as_utc,
    get_session,
)
from routes.agent_tools import (
    LATE_NOTE,
    MAX_CHECK_IN,
    MIN_CHECK_IN,
    check_in_interval,
    clean_category,
    clean_link,
    describe_content,
    file_details,
    folders_of,
    is_online,
    log_activity,
    parse_time,
    preview,
    remove_monitor,
    set_monitor,
    trim_history,
    when_made,
)
from security import require_agent
from uploads import AGENT_FILE_LIMIT, UploadRefused, delete_stored_file, file_response, save_upload
from web import human_size

router = APIRouter(prefix="/api", tags=["agent API"])

DEFAULT_READ_LIMIT = 50
MAX_READ_LIMIT = 500


def iso(moment: datetime | None) -> str | None:
    """A time as ISO 8601 text in UTC, e.g. 2026-10-03T08:00:00+00:00."""
    return as_utc(moment).isoformat() if moment else None


def refuse_upload(refusal: UploadRefused) -> HTTPException:
    """An upload the dashboard can't take (too big, or storage full): 413 with the reason."""
    return HTTPException(413, str(refusal))


# ---------------------------------------------------------------------------
# Reporting: events, outputs, monitors
# ---------------------------------------------------------------------------


class EventIn(BaseModel):
    message: str
    type: str = "log"
    data: dict[str, Any] | None = None
    created_at: datetime | None = None  # when it happened, if sent late (offline buffer)


@router.post("/events", status_code=201)
def create_event(event_in: EventIn, agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """report(): a line in the agent's Activity log."""
    when, late = when_made(event_in.created_at)
    event = Event(agent_id=agent.id, message=event_in.message[:2000] + (LATE_NOTE if late else ""),
                  type=(event_in.type or "log")[:20], data=event_in.data, created_at=when)
    db.add(event)
    trim_history(db, agent.id)
    db.commit()
    db.refresh(event)
    return event_info(event)


class OutputIn(BaseModel):
    category: str
    content: Any  # text, a number, a link, or a list/dict
    created_at: datetime | None = None


def log_new_output(db: Session, output: Output, late: bool) -> None:
    """Note a new output in Activity, at the output's own time."""
    what = {"file": f"file {output.value}", "json": "a list/dict"}.get(output.kind) or preview(output.value)
    log_activity(db, output.agent_id, "output",
                 f'New output in "{output.category}": {what}' + (LATE_NOTE if late else ""),
                 {"category": output.category, "output_id": output.id}, created_at=output.created_at)


@router.post("/outputs", status_code=201)
def create_output(output_in: OutputIn, agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """output(content, category): text, a number, a link, or a list/dict."""
    if output_in.content is None:
        raise HTTPException(422, "content can't be empty")
    kind, value = describe_content(output_in.content)
    when, late = when_made(output_in.created_at)
    output = Output(agent_id=agent.id, category=clean_category(output_in.category), kind=kind, value=value,
                    created_at=when)
    db.add(output)
    db.flush()  # gives the output its id, for the Activity line
    log_new_output(db, output, late)
    trim_history(db, agent.id)
    db.commit()
    db.refresh(output)
    return outputs_info(db, [output])[0]


@router.post("/outputs/file", status_code=201)
def create_file_output(category: str = Form(...), file: UploadFile = File(...), created_at: str | None = Form(None),
                       agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """output(file, category): a file of up to 5 MB, kept in file storage."""
    category = clean_category(category)
    when, late = when_made(created_at)
    try:
        stored = save_upload(db, file, AGENT_FILE_LIMIT, agent.owner_id)
    except UploadRefused as refusal:
        raise refuse_upload(refusal)
    output = Output(agent_id=agent.id, category=category, kind="file", value=stored.filename,
                    file_id=stored.id, created_at=when)
    db.add(output)
    db.flush()
    log_new_output(db, output, late)
    trim_history(db, agent.id)
    db.commit()
    db.refresh(output)
    return outputs_info(db, [output])[0]


class StateIn(BaseModel):
    """A check-in. Leave a field out to keep it as it is."""

    accepts_messages: bool | None = None
    accepts_inbox: bool | None = None
    check_in_every: int | None = Field(default=None, ge=MIN_CHECK_IN, le=MAX_CHECK_IN)


@router.post("/state")
def agent_state(state_in: StateIn, agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """The check-in: says how often the agent checks in and whether it takes
    messages and Inbox files; learns whether it's paused and how many Inbox
    files are waiting."""
    if state_in.accepts_messages is not None:
        agent.accepts_messages = state_in.accepts_messages
    if state_in.accepts_inbox is not None:
        agent.accepts_inbox = state_in.accepts_inbox
    if state_in.check_in_every is not None:
        agent.check_in_seconds = state_in.check_in_every
    db.add(agent)
    db.commit()
    db.refresh(agent)
    waiting = db.exec(select(func.count(InboxFile.id)).where(InboxFile.agent_id == agent.id)).one()
    return {"paused": agent.paused, "accepts_messages": agent.accepts_messages,
            "accepts_inbox": agent.accepts_inbox, "inbox_waiting": waiting,
            "check_in_every": check_in_interval(agent)}


# --- Monitors: named datapoints, each replaced by the next value ----------------
#
# An output is a one-off item that adds to the agent's Outputs list. A monitor
# is a datapoint that is *replaced*: set_monitor(content, name) overwrites the
# monitor called `name` (a temperature, "today's summary" PDF, a status...).


def is_pdf(upload: UploadFile) -> bool:
    """Every real PDF starts with "%PDF-", so nothing else can be shown inside
    the page pretending to be one."""
    is_real_pdf = upload.file.read(5).startswith(b"%PDF-")
    upload.file.seek(0)
    return is_real_pdf


class MonitorIn(BaseModel):
    name: str
    content: Any = None  # text, a number, a link or a list/dict; null removes the monitor
    created_at: datetime | None = None  # when the agent made this value, if sent late


@router.put("/monitors")
def put_monitor(monitor_in: MonitorIn, agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """set_monitor(content, name): replace the monitor called `name` (or create it).
    Content null removes it."""
    name = clean_category(monitor_in.name, "name")
    if monitor_in.content is None:
        applied = set_monitor(db, agent, name, None, None, made_at=monitor_in.created_at)
    else:
        kind, value = describe_content(monitor_in.content)
        applied = set_monitor(db, agent, name, kind, value, made_at=monitor_in.created_at)
    trim_history(db, agent.id)
    db.commit()
    return {"name": name, "applied": applied}


@router.post("/monitors/file")
def put_monitor_file(name: str = Form(...), file: UploadFile = File(...), created_at: str | None = Form(None),
                     agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """set_monitor(file, name): a file (up to 5 MB) as the monitor's value. A PDF
    is shown inside the agent's page; images too; other files are downloads."""
    name = clean_category(name, "name")
    pdf = is_pdf(file)
    try:
        stored = save_upload(db, file, AGENT_FILE_LIMIT, agent.owner_id)
    except UploadRefused as refusal:
        raise refuse_upload(refusal)
    if pdf:
        stored.content_type = "application/pdf"
    applied = set_monitor(db, agent, name, "pdf" if pdf else "file", stored.filename, stored=stored, made_at=created_at)
    return {"name": name, "filename": stored.filename, "applied": applied}


@router.delete("/monitors")
def delete_monitors(name: str | None = None, all: bool = False, agent: Agent = Depends(require_agent),
                    db: Session = Depends(get_session)):
    """Remove one of the caller's own monitors (?name=), or all of them (?all=true)."""
    if name is None and not all:
        raise HTTPException(422, "say what to delete: name, or all=true")
    statement = select(Monitor).where(Monitor.agent_id == agent.id)
    if name is not None:
        statement = statement.where(Monitor.name == " ".join(name.split()))
    monitors = list(db.exec(statement).all())
    for monitor in monitors:
        remove_monitor(db, agent, monitor)
    if monitors:
        count = len(monitors)
        log_activity(db, agent.id, "delete", f"Deleted {count} monitor{'s' if count != 1 else ''}")
    db.commit()
    return {"deleted": len(monitors)}


# The single, unnamed monitor of the earlier version. Kept so connectors
# downloaded before monitors had names keep working: they set the monitor
# called "Monitor".


class OldMonitorIn(BaseModel):
    url: str | None
    label: str | None = None
    created_at: datetime | None = None


@router.put("/monitor", include_in_schema=False)
def put_old_monitor(monitor_in: OldMonitorIn, agent: Agent = Depends(require_agent),
                    db: Session = Depends(get_session)):
    if monitor_in.url is None or not monitor_in.url.strip():
        applied = set_monitor(db, agent, LEGACY_MONITOR_NAME, None, None, made_at=monitor_in.created_at)
    else:
        url = clean_link(monitor_in.url)
        if url is None:
            raise HTTPException(422, "url must start with http:// or https://")
        applied = set_monitor(db, agent, LEGACY_MONITOR_NAME, "link", url, made_at=monitor_in.created_at)
    return {"url": monitor_in.url, "label": monitor_in.label, "applied": applied}


@router.post("/monitor/file", include_in_schema=False)
def put_old_monitor_pdf(file: UploadFile = File(...), label: str | None = Form(None),
                        created_at: str | None = Form(None), agent: Agent = Depends(require_agent),
                        db: Session = Depends(get_session)):
    if not is_pdf(file):
        raise HTTPException(422, "monitor files must be PDFs")
    reply = put_monitor_file(LEGACY_MONITOR_NAME, file, created_at, agent, db)
    return {"filename": reply["filename"], "label": label, "applied": reply["applied"]}


@router.delete("/monitor", include_in_schema=False)
def delete_old_monitor(agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    return delete_monitors(LEGACY_MONITOR_NAME, False, agent, db)


# ---------------------------------------------------------------------------
# Commands (Start, Pause, messages) and the Inbox: this agent's own only
# ---------------------------------------------------------------------------


@router.get("/commands")
def pending_commands(agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """The calling agent's commands it hasn't handled yet, oldest first."""
    commands = db.exec(select(Command).where(Command.agent_id == agent.id, Command.status == "pending")
                       .order_by(Command.created_at, Command.id)).all()
    return [{"id": c.id, "command": c.command, "payload": c.payload, "status": c.status,
             "created_at": iso(c.created_at)} for c in commands]


class CommandUpdate(BaseModel):
    status: str = "done"


@router.patch("/commands/{command_id}")
def update_command(command_id: int, update: CommandUpdate, agent: Agent = Depends(require_agent),
                   db: Session = Depends(get_session)):
    command = db.get(Command, command_id)
    if command is None or command.agent_id != agent.id:  # another agent's looks "not found"
        raise HTTPException(404, "Command not found")
    command.status = update.status[:20]
    db.add(command)
    db.commit()
    return {"id": command.id, "command": command.command, "status": command.status}


def inbox_item_info(item: InboxFile, stored: StoredFile) -> dict:
    return {"id": item.id, "filename": stored.filename, "content_type": stored.content_type, "size": stored.size,
            "note": item.note, "uploaded_at": iso(item.created_at), "file_url": f"/api/inbox/{item.id}/file"}


def own_inbox_item(db: Session, agent: Agent, item_id: int) -> InboxFile:
    item = db.get(InboxFile, item_id)
    if item is None or item.agent_id != agent.id:
        raise HTTPException(404, "Inbox file not found")
    return item


@router.get("/inbox")
def api_inbox(agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """The files waiting in the calling agent's Inbox, oldest first."""
    items = db.exec(select(InboxFile).where(InboxFile.agent_id == agent.id)
                    .order_by(InboxFile.created_at, InboxFile.id)).all()
    files = file_details(db, [item.file_id for item in items])
    return [inbox_item_info(item, files[item.file_id]) for item in items if item.file_id in files]


@router.get("/inbox/{item_id}/file")
def api_inbox_file(item_id: int, agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """Download one of the calling agent's Inbox files (streamed)."""
    item = own_inbox_item(db, agent, item_id)
    return file_response(db.get(StoredFile, item.file_id), inline=False)


def confirm_received(db: Session, agent: Agent, item_id: int) -> dict:
    item = own_inbox_item(db, agent, item_id)
    stored = db.get(StoredFile, item.file_id)
    if stored is not None:
        log_activity(db, agent.id, "inbox", f"Inbox: agent received {stored.filename} ({human_size(stored.size)})")
    db.delete(item)
    db.flush()
    delete_stored_file(db, item.file_id)
    db.commit()
    return {"deleted": 1}


@router.post("/inbox/{item_id}/received")
def api_inbox_received(item_id: int, agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """The agent has stored an Inbox file its own way: the dashboard deletes its copy."""
    return confirm_received(db, agent, item_id)


@router.delete("/inbox/{item_id}", include_in_schema=False)
def api_inbox_received_old(item_id: int, agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """The earlier project's way of confirming (kept so older connectors still work)."""
    return confirm_received(db, agent, item_id)


# ---------------------------------------------------------------------------
# Reading the agents of the same class
# ---------------------------------------------------------------------------


def find_agent(db: Session, caller: Agent, ref: str) -> Agent:
    """The agent a read is about: "me", its id, or its exact name, in the caller's class.

    Agents of other classes are "not found". Two agents in the class sharing
    the name gives 409 with their ids, so the caller can use an id instead.
    """
    if ref == "me":
        return caller
    if ref.isdigit():
        agent = db.get(Agent, int(ref))
        if agent is not None and agent.course_id == caller.course_id:
            return agent
    matches = db.exec(select(Agent).where(Agent.course_id == caller.course_id, Agent.name == ref)).all()
    if len(matches) > 1:
        raise HTTPException(409, {"message": "Several agents in this class have that name; use an id.",
                                  "ids": sorted(agent.id for agent in matches)})
    if not matches:
        raise HTTPException(404, "Agent not found")
    return matches[0]


def monitors_info(db: Session, monitors: list[Monitor]) -> list[dict]:
    """Monitors as JSON, like outputs: content as sent; files described, with an address."""
    files = file_details(db, [m.file_id for m in monitors if m.file_id])
    result = []
    for monitor in monitors:
        item = {"id": monitor.id, "agent_id": monitor.agent_id, "name": monitor.name, "kind": monitor.kind,
                "content": output_content(monitor), "updated_at": iso(monitor.updated_at)}
        if monitor.file_id in files:
            stored = files[monitor.file_id]
            item["file"] = {"filename": stored.filename, "content_type": stored.content_type, "size": stored.size,
                            "url": f"/api/agents/{monitor.agent_id}/monitors/{monitor.id}/file"}
        result.append(item)
    return result


def agent_info(db: Session, agent: Agent, caller: Agent) -> dict:
    """Everything one agent may know about another (never its key or its owner's Login ID)."""
    owner = db.get(User, agent.owner_id)
    gen = db.get(Gen, owner.gen_id) if owner and owner.gen_id else None
    return {
        "id": agent.id, "name": agent.name, "description": agent.description, "you": agent.id == caller.id,
        "owner": {"full_name": owner.full_name if owner else None, "gen": gen.number if gen else None},
        "folders": [folder.name for folder in folders_of(db, agent.id)],
        "online": is_online(agent), "paused": agent.paused, "accepts_messages": agent.accepts_messages,
        "check_in_every": check_in_interval(agent), "created_at": iso(agent.created_at),
        "last_seen": iso(agent.last_seen_at),
        "monitors": [m.name for m in db.exec(select(Monitor).where(Monitor.agent_id == agent.id)
                                             .order_by(func.lower(Monitor.name))).all()],
    }


def output_content(output: Output) -> Any:
    if output.kind in ("number", "json"):
        return json.loads(output.value)
    return output.value


def outputs_info(db: Session, outputs: list[Output]) -> list[dict]:
    """Outputs as JSON. Files are described (name, type, size, address), not included."""
    files = file_details(db, [o.file_id for o in outputs if o.file_id])
    result = []
    for output in outputs:
        item = {"id": output.id, "agent_id": output.agent_id, "category": output.category, "kind": output.kind,
                "content": output_content(output), "created_at": iso(output.created_at)}
        if output.file_id in files:
            stored = files[output.file_id]
            item["file"] = {"filename": stored.filename, "content_type": stored.content_type, "size": stored.size,
                            "url": f"/api/agents/{output.agent_id}/outputs/{output.id}/file"}
        result.append(item)
    return result


def event_info(event: Event) -> dict:
    return {"id": event.id, "agent_id": event.agent_id, "type": event.type, "message": event.message,
            "data": event.data, "created_at": iso(event.created_at)}


@router.get("/agents")
def list_agents(caller: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """Every agent in the caller's class, A to Z. "you": true marks the caller."""
    agents = db.exec(select(Agent).where(Agent.course_id == caller.course_id)
                     .order_by(func.lower(Agent.name), Agent.id)).all()
    return [agent_info(db, agent, caller) for agent in agents]


@router.get("/agents/{ref}")
def get_agent(ref: str, caller: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """One agent ("me", an id, or a name), with its output categories."""
    agent = find_agent(db, caller, ref)
    rows = db.exec(select(Output.category, func.count(Output.id), func.max(Output.created_at))
                   .where(Output.agent_id == agent.id).group_by(Output.category)
                   .order_by(func.lower(Output.category))).all()
    info = agent_info(db, agent, caller)
    info["categories"] = [{"name": name, "count": count, "latest_at": iso(latest)} for name, count, latest in rows]
    return info


@router.get("/agents/{ref}/outputs")
def read_outputs(
    ref: str,
    category: list[str] | None = Query(None),
    after: str | None = None,
    before: str | None = None,
    since: str | None = None,  # the earlier connector's name for "after"
    limit: int = Query(DEFAULT_READ_LIMIT, ge=1, le=MAX_READ_LIMIT),
    offset: int = Query(0, ge=0),
    caller: Agent = Depends(require_agent),
    db: Session = Depends(get_session),
):
    """An agent's outputs, newest first, filtered by category and time, with paging."""
    agent = find_agent(db, caller, ref)
    statement = select(Output).where(Output.agent_id == agent.id)
    if category:
        statement = statement.where(Output.category.in_(category))
    if (after_time := parse_time(after or since, "after")) is not None:
        statement = statement.where(Output.created_at >= after_time)
    if (before_time := parse_time(before, "before")) is not None:
        statement = statement.where(Output.created_at < before_time)
    statement = statement.order_by(Output.created_at.desc(), Output.id.desc()).offset(offset).limit(limit)
    return outputs_info(db, list(db.exec(statement).all()))


@router.get("/agents/{ref}/outputs/{output_id}/file")
def read_output_file(ref: str, output_id: int, caller: Agent = Depends(require_agent),
                     db: Session = Depends(get_session)):
    agent = find_agent(db, caller, ref)
    output = db.get(Output, output_id)
    stored = db.get(StoredFile, output.file_id) if output and output.file_id else None
    if output is None or output.agent_id != agent.id or stored is None:
        raise HTTPException(404, "File not found")
    return file_response(stored, inline=False)


@router.get("/agents/{ref}/events")
def read_events(
    ref: str,
    type: str | None = None,
    after: str | None = None,
    before: str | None = None,
    since: str | None = None,
    limit: int = Query(DEFAULT_READ_LIMIT, ge=1, le=MAX_READ_LIMIT),
    offset: int = Query(0, ge=0),
    caller: Agent = Depends(require_agent),
    db: Session = Depends(get_session),
):
    """An agent's Activity log, newest first, filtered by type and time, with paging."""
    agent = find_agent(db, caller, ref)
    statement = select(Event).where(Event.agent_id == agent.id)
    if type:
        statement = statement.where(Event.type == type)
    if (after_time := parse_time(after or since, "after")) is not None:
        statement = statement.where(Event.created_at >= after_time)
    if (before_time := parse_time(before, "before")) is not None:
        statement = statement.where(Event.created_at < before_time)
    statement = statement.order_by(Event.created_at.desc(), Event.id.desc()).offset(offset).limit(limit)
    return [event_info(event) for event in db.exec(statement).all()]


@router.get("/agents/{ref}/monitors")
def read_monitors(ref: str, name: list[str] | None = Query(None), caller: Agent = Depends(require_agent),
                  db: Session = Depends(get_session)):
    """An agent's monitors (each with its current value), A to Z. Optional: only some names (repeat ?name=)."""
    agent = find_agent(db, caller, ref)
    statement = select(Monitor).where(Monitor.agent_id == agent.id)
    if name:
        statement = statement.where(Monitor.name.in_(name))
    return monitors_info(db, list(db.exec(statement.order_by(func.lower(Monitor.name))).all()))


@router.get("/agents/{ref}/monitors/{monitor_id}/file")
def read_monitor_file(ref: str, monitor_id: int, caller: Agent = Depends(require_agent),
                      db: Session = Depends(get_session)):
    agent = find_agent(db, caller, ref)
    monitor = db.get(Monitor, monitor_id)
    stored = db.get(StoredFile, monitor.file_id) if monitor and monitor.file_id else None
    if monitor is None or monitor.agent_id != agent.id or stored is None:
        raise HTTPException(404, "File not found")
    return file_response(stored, inline=False)


@router.get("/agents/{ref}/monitor", include_in_schema=False)
def read_old_monitor(ref: str, caller: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """The earlier version's single monitor: the one called "Monitor"."""
    monitors = read_monitors(ref, [LEGACY_MONITOR_NAME], caller, db)
    return {"monitor": monitors[0] if monitors else None}


# ---------------------------------------------------------------------------
# Deleting: the caller's OWN data only (others get 403). Each delete leaves a
# "delete" line in the caller's Activity log, so its owner sees what went.
# ---------------------------------------------------------------------------


def delete_outputs(db: Session, agent: Agent, outputs: list[Output], what: str) -> int:
    for output in outputs:
        file_id = output.file_id
        db.delete(output)
        db.flush()
        delete_stored_file(db, file_id)
    if outputs:
        count = len(outputs)
        log_activity(db, agent.id, "delete", f"Deleted {count} output{'s' if count != 1 else ''}{what}")
    db.commit()
    return len(outputs)


@router.delete("/outputs/{output_id}")
def delete_one_output(output_id: int, agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    output = db.get(Output, output_id)
    if output is None:
        raise HTTPException(404, "Output not found")
    if output.agent_id != agent.id:
        raise HTTPException(403, "Agents can only delete their own outputs")
    return {"deleted": delete_outputs(db, agent, [output], f' in "{output.category}"')}


@router.delete("/outputs")
def delete_many_outputs(category: str | None = None, before: str | None = None, all: bool = False,
                        agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """Delete the caller's outputs in a ?category=, and/or made ?before= a time.
    Deleting everything needs ?all=true, so a missing filter can't wipe it all."""
    before_time = parse_time(before, "before")
    if category is None and before_time is None and not all:
        raise HTTPException(422, "say what to delete: category, before, or all=true")
    statement = select(Output).where(Output.agent_id == agent.id)
    what = ""
    if category is not None:
        category = " ".join(category.split())
        statement = statement.where(Output.category == category)
        what += f' in "{category}"'
    if before_time is not None:
        statement = statement.where(Output.created_at < before_time)
        what += f" made before {before_time:%Y-%m-%d %H:%M:%S} UTC"
    return {"deleted": delete_outputs(db, agent, list(db.exec(statement).all()), what)}


def delete_events(db: Session, agent: Agent, events: list[Event]) -> int:
    for event in events:
        db.delete(event)
    if events:
        count = len(events)
        log_activity(db, agent.id, "delete", f"Deleted {count} activity line{'s' if count != 1 else ''}")
    db.commit()
    return len(events)


@router.delete("/events/{event_id}")
def delete_one_event(event_id: int, agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(404, "Activity line not found")
    if event.agent_id != agent.id:
        raise HTTPException(403, "Agents can only delete their own activity")
    return {"deleted": delete_events(db, agent, [event])}


@router.delete("/events")
def delete_many_events(type: str | None = None, before: str | None = None, all: bool = False,
                       agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """Delete the caller's Activity lines of a ?type= and/or made ?before= a time (or ?all=true)."""
    before_time = parse_time(before, "before")
    if type is None and before_time is None and not all:
        raise HTTPException(422, "say what to delete: type, before, or all=true")
    statement = select(Event).where(Event.agent_id == agent.id)
    if type is not None:
        statement = statement.where(Event.type == type)
    if before_time is not None:
        statement = statement.where(Event.created_at < before_time)
    return {"deleted": delete_events(db, agent, list(db.exec(statement).all()))}

