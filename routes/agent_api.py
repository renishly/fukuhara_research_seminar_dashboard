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

from db import Agent, Command, Event, Gen, InboxFile, Output, StoredFile, User, as_utc, get_session
from routes.agent_tools import (
    LATE_NOTE,
    MAX_AGENT_NAME,
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
# Reporting: events, outputs, the monitor
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


def replace_monitor(db: Session, agent: Agent, *, url: str | None = None, stored: StoredFile | None = None,
                    filename: str | None = None, label: str | None = None, made_at=None) -> bool:
    """Make a link, a PDF, or nothing the agent's monitor, and log it. Commits.

    A late update older than the current monitor is skipped (returns False),
    so a late arrival never replaces something newer. A replaced PDF is deleted.
    """
    when, late = when_made(made_at)
    if agent.monitor_updated_at and when < as_utc(agent.monitor_updated_at):
        if stored is not None:
            db.flush()
            delete_stored_file(db, stored.id)
        log_activity(db, agent.id, "monitor", "Older monitor update skipped" + LATE_NOTE, created_at=when)
        db.commit()
        return False
    old_file = agent.monitor_file_id
    agent.monitor_url = url
    agent.monitor_file_id = stored.id if stored else None
    agent.monitor_filename = filename if stored else None
    agent.monitor_label = " ".join((label or "").split())[:MAX_AGENT_NAME] or None
    agent.monitor_updated_at = when
    db.add(agent)
    if stored is not None:
        message = f"Monitor updated: PDF {filename}"
    elif url is not None:
        message = f"Monitor link set: {url}"
    else:
        message = "Monitor removed"
    log_activity(db, agent.id, "monitor", message + (LATE_NOTE if late else ""), created_at=when)
    trim_history(db, agent.id)
    db.flush()
    if old_file and old_file != agent.monitor_file_id:
        delete_stored_file(db, old_file)
    db.commit()
    db.refresh(agent)
    return True


class MonitorIn(BaseModel):
    url: str | None  # null removes the monitor
    label: str | None = None
    created_at: datetime | None = None


@router.put("/monitor")
def set_monitor(monitor_in: MonitorIn, agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """set_monitor(link): a link to where the agent's data lives (or null to remove it)."""
    if monitor_in.url is None or not monitor_in.url.strip():
        applied = replace_monitor(db, agent, made_at=monitor_in.created_at)
    else:
        url = clean_link(monitor_in.url)
        if url is None:
            raise HTTPException(422, "url must start with http:// or https://")
        applied = replace_monitor(db, agent, url=url, label=monitor_in.label, made_at=monitor_in.created_at)
    return {"url": agent.monitor_url, "label": agent.monitor_label, "applied": applied}


@router.post("/monitor/file")
def set_monitor_pdf(file: UploadFile = File(...), label: str | None = Form(None), created_at: str | None = Form(None),
                    agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """set_monitor(pdf): a PDF (up to 5 MB) shown inside the agent's page."""
    # Every real PDF starts with "%PDF-", so nothing else can ever be shown
    # inside the page pretending to be one.
    if not file.file.read(5).startswith(b"%PDF-"):
        raise HTTPException(422, "monitor files must be PDFs")
    file.file.seek(0)
    try:
        stored = save_upload(db, file, AGENT_FILE_LIMIT, agent.owner_id)
    except UploadRefused as refusal:
        raise refuse_upload(refusal)
    stored.content_type = "application/pdf"
    applied = replace_monitor(db, agent, stored=stored, filename=stored.filename, label=label, made_at=created_at)
    return {"filename": stored.filename, "label": agent.monitor_label, "applied": applied}


@router.delete("/monitor")
def delete_monitor(agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """Remove the caller's own Monitor."""
    had_monitor = bool(agent.monitor_url or agent.monitor_file_id)
    if had_monitor:
        replace_monitor(db, agent)
    return {"deleted": 1 if had_monitor else 0}


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


def monitor_info(agent: Agent) -> dict | None:
    common = {"label": agent.monitor_label, "updated_at": iso(agent.monitor_updated_at)}
    if agent.monitor_file_id:
        return {"kind": "pdf", "filename": agent.monitor_filename,
                "file_url": f"/api/agents/{agent.id}/monitor.pdf", **common}
    if agent.monitor_url:
        return {"kind": "link", "url": agent.monitor_url, **common}
    return None


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
        "last_seen": iso(agent.last_seen_at), "monitor": monitor_info(agent),
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


@router.get("/agents/{ref}/monitor")
def read_monitor(ref: str, caller: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """What an agent's Monitor shows: {"monitor": {...}} or {"monitor": null}."""
    return {"monitor": monitor_info(find_agent(db, caller, ref))}


@router.get("/agents/{ref}/monitor.pdf")
def read_monitor_pdf(ref: str, caller: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    agent = find_agent(db, caller, ref)
    stored = db.get(StoredFile, agent.monitor_file_id) if agent.monitor_file_id else None
    if stored is None:
        raise HTTPException(404, "This agent's monitor isn't a PDF")
    return file_response(stored, inline=False, filename=agent.monitor_filename or "monitor.pdf")


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

