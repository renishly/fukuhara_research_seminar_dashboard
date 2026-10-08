"""Students' agent dashboards (in the browser): the 3 x 3 grid, folders,
agent pages, outputs, monitor, activity, controls and the Inbox.

Each student has their own dashboard in each class they're enrolled in.
"The owner" is the student who created an agent: only they can create,
change, control or delete their agents, folders, pins, keys and Inbox files.
Other members see everything read-only; guests see only the grid.
"""

import math
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session, func, select

from db import (
    Agent,
    AgentFolder,
    Command,
    Course,
    Event,
    Folder,
    InboxFile,
    Monitor,
    MonitorNotifySetting,
    NotifySetting,
    Output,
    StoredFile,
    User,
    get_session,
)
from permissions import can_submit, can_view_agent_page, owns_agent, student_is_enrolled
from routes.agent_tools import (
    MAX_AGENT_NAME,
    MAX_CATEGORY,
    MAX_DESCRIPTION,
    MAX_FOLDER_NAME,
    agent_name_taken,
    check_in_text,
    check_new_api_key,
    clean_link,
    clean_name,
    count_outputs,
    file_details,
    folders_of,
    get_monitor,
    is_online,
    latest_event,
    latest_output,
    log_activity,
    monitor_names,
    output_categories,
    set_monitor,
    store_api_key,
)
from routes.common import delete_agent, get_course, not_found, student_label
from security import (
    KeyEncryptionNotConfigured,
    Viewer,
    checked_member,
    current_viewer,
    decrypt_api_key,
    forbidden,
    generate_api_key,
    password_matches,
    require_member,
)
from uploads import (
    INBOX_FILE_LIMIT,
    INBOX_TOTAL_LIMIT,
    INLINE_TYPES,
    UploadRefused,
    delete_stored_file,
    file_response,
    has_file,
    inbox_waiting_total,
    save_upload,
)
from web import (
    BASE_DIR,
    alert_html,
    clean_text,
    form_error,
    hx_redirect,
    human_size,
    poll_reply,
    render,
    render_text,
    toast,
)

router = APIRouter()

CELLS_PER_PAGE = 9  # a 3 x 3 grid (agent cards plus, for the owner, the "Add Agent" card)
OUTPUTS_SHOWN = 50
MAX_INBOX_NOTE = 1000


# ---------------------------------------------------------------------------
# Finding agents and checking who may do what
# ---------------------------------------------------------------------------


def viewable_agent(db: Session, viewer: Viewer, agent_id: int) -> Agent:
    """An agent whose page this viewer may see (members who can view its class)."""
    agent = db.get(Agent, agent_id)
    if agent is None or not can_view_agent_page(db, viewer, agent):
        raise not_found("Agent not found")
    return agent


def own_agent(db: Session, viewer: Viewer, agent_id: int) -> Agent:
    """An agent this viewer owns. Anyone else who can see it gets 403."""
    agent = viewable_agent(db, viewer, agent_id)
    if not owns_agent(viewer, agent):
        raise forbidden("Only the student who created this agent can change or control it.")
    return agent


def own_folder(db: Session, viewer: Viewer, folder_id: int) -> Folder:
    folder = db.get(Folder, folder_id)
    if folder is None:
        raise not_found("Folder not found")
    if folder.owner_id != viewer.id:
        raise forbidden("Only the folder's owner can change it.")
    return folder


def dashboard_owner(db: Session, viewer: Viewer, course: Course, student_id: int) -> User:
    """The student whose agent dashboard this is (they must be enrolled)."""
    student = db.get(User, student_id)
    if student is None or not student_is_enrolled(db, student, course):
        raise not_found("Student not found")
    return student


def grid_url(agent_or_course_id, owner_id: int | None = None, folder_id: int | None = None, page: int = 1) -> str:
    course_id = agent_or_course_id.course_id if isinstance(agent_or_course_id, Agent) else agent_or_course_id
    owner_id = agent_or_course_id.owner_id if isinstance(agent_or_course_id, Agent) else owner_id
    url = f"/classes/{course_id}/agents/{owner_id}?page={page}"
    return url + (f"&folder={folder_id}" if folder_id else "")


# ---------------------------------------------------------------------------
# The grid and the folder sidebar
# ---------------------------------------------------------------------------


def agents_query(course_id: int, owner_id: int, folder_id: int | None):
    statement = select(Agent).where(Agent.course_id == course_id, Agent.owner_id == owner_id)
    if folder_id is not None:
        statement = statement.join(AgentFolder, AgentFolder.agent_id == Agent.id).where(
            AgentFolder.folder_id == folder_id)
    return statement


def count_agents(db: Session, course_id: int, owner_id: int, folder_id: int | None) -> int:
    return db.exec(select(func.count()).select_from(agents_query(course_id, owner_id, folder_id).subquery())).one()


def page_of(db: Session, agent: Agent) -> int:
    """Which grid page an agent appears on (agents are sorted A-Z, ignoring case)."""
    before = agents_query(agent.course_id, agent.owner_id, None).where(
        func.lower(Agent.name) < agent.name.lower()).subquery()
    return db.exec(select(func.count()).select_from(before)).one() // CELLS_PER_PAGE + 1


def owner_folders(db: Session, owner_id: int) -> list[Folder]:
    return list(db.exec(select(Folder).where(Folder.owner_id == owner_id).order_by(Folder.name)).all())


def grid_context(db: Session, viewer: Viewer, course: Course, owner: User, page: int,
                 folder: Folder | None) -> dict:
    """One page of a student's grid: up to 9 cells (the owner's last one is "Add Agent")."""
    is_owner = viewer.id == owner.id
    folder_id = folder.id if folder else None
    total = count_agents(db, course.id, owner.id, folder_id)
    cells_needed = total + (1 if is_owner else 0)
    total_pages = max(1, math.ceil(cells_needed / CELLS_PER_PAGE))
    page = min(max(page, 1), total_pages)
    first = (page - 1) * CELLS_PER_PAGE
    agents = db.exec(
        agents_query(course.id, owner.id, folder_id)
        .order_by(func.lower(Agent.name), Agent.name).offset(first).limit(CELLS_PER_PAGE)
    ).all()
    cells = []
    for agent in agents:
        cell = {"agent": agent, "online": is_online(agent)}
        if viewer.is_member:  # guests only see names, status and descriptions
            cell["latest"] = latest_event(db, agent.id)
            cell["folders"] = folders_of(db, agent.id)
            # The pinned item: the newest output of the pinned category, or the pinned monitor.
            if agent.pinned_category:
                cell["pinned"] = latest_output(db, agent.id, agent.pinned_category)
                cell["pinned_label"] = agent.pinned_category
            elif agent.pinned_monitor_name:
                cell["pinned"] = get_monitor(db, agent.id, agent.pinned_monitor_name)
                cell["pinned_label"] = agent.pinned_monitor_name
        cells.append(cell)
    pinned_files = file_details(db, [c["pinned"].file_id for c in cells if c.get("pinned") and c["pinned"].file_id])
    return {
        "course": course, "owner": owner, "is_owner": is_owner, "cells": cells,
        "show_add_card": is_owner and first <= total < first + CELLS_PER_PAGE,
        "page": page, "total_pages": total_pages, "total_agents": total, "folder": folder,
        "pinned_files": pinned_files, "inline_types": INLINE_TYPES,
    }


def sidebar_context(db: Session, course: Course, owner: User, folder: Folder | None) -> dict:
    folders = [{"folder": f, "count": count_agents(db, course.id, owner.id, f.id)} for f in owner_folders(db, owner.id)]
    return {"folders": folders, "all_count": count_agents(db, course.id, owner.id, None), "current_folder": folder}


def site_url(request: Request) -> str:
    """This website's address as the browser sees it (Render handles https in
    front of the app and says so in X-Forwarded-Proto)."""
    forwarded = request.headers.get("x-forwarded-proto", "")
    scheme = forwarded.split(",")[0].strip() or request.url.scheme
    return f"{scheme}://{request.url.netloc}"


CONNECTOR_FILES = {
    "code": (BASE_DIR / "connector" / "dashboard_connector.py", "dashboard_connector.py", "text/x-python"),
    "prompt": (BASE_DIR / "connector" / "connector_prompt.md", "dashboard_connector_prompt.md", "text/markdown"),
}


def connector_text(request: Request, file_format: str) -> str:
    """The connector code or prompt, with only the "downloaded from" hint filled in.
    It never contains an API key."""
    source = CONNECTOR_FILES[file_format][0]
    return source.read_text(encoding="utf-8").replace("{{DOWNLOADED_FROM}}", site_url(request))


def folder_for_grid(db: Session, owner: User, folder: int | None) -> Folder | None:
    if folder is None:
        return None
    found = db.get(Folder, folder)
    if found is None or found.owner_id != owner.id:
        raise not_found("Folder not found")
    return found


@router.get("/classes/{course_id}/agents/{student_id}")
def agent_grid_page(request: Request, course_id: int, student_id: int, page: int = 1, folder: int | None = None,
                    viewer: Viewer = Depends(current_viewer), db: Session = Depends(get_session)):
    """A student's agent dashboard for one class."""
    course = get_course(db, viewer, course_id)
    owner = dashboard_owner(db, viewer, course, student_id)
    current = folder_for_grid(db, owner, folder) if viewer.is_member else None
    context = grid_context(db, viewer, course, owner, page, current) | sidebar_context(db, course, owner, current)
    if context["is_owner"]:
        context |= {"site_url": site_url(request), "connector_code": connector_text(request, "code"),
                    "connector_prompt": connector_text(request, "prompt")}
    return render(request, db, viewer, "agent_grid.html", owner_gen=student_label(db, owner), **context)


@router.get("/classes/{course_id}/agents/{student_id}/grid")
def agent_grid_fragment(course_id: int, student_id: int, page: int = 1, folder: int | None = None,
                        seen: str | None = None, viewer: Viewer = Depends(current_viewer),
                        db: Session = Depends(get_session)):
    """The grid again, every 5 seconds (204 if nothing changed)."""
    course = get_course(db, viewer, course_id)
    owner = dashboard_owner(db, viewer, course, student_id)
    current = folder_for_grid(db, owner, folder) if viewer.is_member else None
    content = render_text(db, viewer, "partials/agent_cards.html",
                          **grid_context(db, viewer, course, owner, page, current))
    return poll_reply(content, seen, "grid-seen")


@router.get("/connector/{file_format}")
def download_connector(request: Request, file_format: str, viewer: Viewer = Depends(require_member)):
    """Download the connector as Python code ("code") or as an AI prompt ("prompt")."""
    if file_format not in CONNECTOR_FILES:
        raise not_found("Unknown connector format")
    _, filename, media_type = CONNECTOR_FILES[file_format]
    return Response(connector_text(request, file_format), media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------------------
# Adding agents, and folders
# ---------------------------------------------------------------------------


@router.post("/classes/{course_id}/agents")
def create_agent(
    request: Request,
    course_id: int,
    name: str = Form(""),
    api_key: str = Form(""),
    description: str = Form(""),
    folder_id: int | None = Form(None),
    viewer: Viewer = Depends(checked_member),
    db: Session = Depends(get_session),
):
    """Add an agent from the "Add Agent" card. A blank key means one is generated."""
    course = get_course(db, viewer, course_id)
    if not can_submit(db, viewer, course):
        raise forbidden("Only students enrolled in this class can add agents here.")
    name, error = clean_name(name, MAX_AGENT_NAME)
    api_key = api_key.strip()
    generated = not api_key
    api_key = api_key or generate_api_key()
    if error is None and agent_name_taken(db, viewer.id, course.id, name):
        error = "You already have an agent with that name in this class."
    description, description_error = clean_text(description, MAX_DESCRIPTION)
    error = error or description_error or check_new_api_key(db, api_key)
    agent = Agent(owner_id=viewer.id, course_id=course.id, name=name, description=description, api_key_hash="")
    if error is None:
        try:
            store_api_key(agent, api_key)
        except KeyEncryptionNotConfigured:
            error = "KEY_ENCRYPTION_SECRET isn't set on the server, so keys can't be stored."
    if error:
        return render(request, db, viewer, "partials/add_agent_form.html", status_code=400, course=course,
                      error=error, name=name, description=description, folder_id=folder_id)
    db.add(agent)
    db.commit()
    db.refresh(agent)
    folder = db.get(Folder, folder_id) if folder_id else None
    if folder is not None and folder.owner_id == viewer.id:
        db.add(AgentFolder(agent_id=agent.id, folder_id=folder.id))
        db.commit()
    return render(request, db, viewer, "partials/agent_created.html", agent=agent,
                  api_key=api_key if generated else None, site_url=site_url(request),
                  done_url=grid_url(agent, page=page_of(db, agent)))


def folder_redirect(course_id: int | None, viewer: Viewer, folder_id: int | None = None, error: str | None = None):
    """Back to the owner's grid in the class they were looking at."""
    url = grid_url(course_id, viewer.id, folder_id) if course_id else "/classes"
    if error:
        url += f"&error={quote(error)}"
    return RedirectResponse(url, status_code=303)


@router.post("/folders")
def create_folder(name: str = Form(""), course_id: int | None = Form(None),
                  viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    """Folders belong to their student and are shared across their classes."""
    if not viewer.is_student:
        raise forbidden("Only students have agent folders.")
    name, error = clean_name(name, MAX_FOLDER_NAME)
    if error is None and db.exec(select(Folder).where(Folder.owner_id == viewer.id, Folder.name == name)).first():
        error = "You already have a folder with that name."
    if error:
        return folder_redirect(course_id, viewer, error=error)
    folder = Folder(owner_id=viewer.id, name=name)
    db.add(folder)
    db.commit()
    return folder_redirect(course_id, viewer, folder.id)


@router.post("/folders/{folder_id}/rename")
def rename_folder(folder_id: int, name: str = Form(""), course_id: int | None = Form(None),
                  viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    folder = own_folder(db, viewer, folder_id)
    name, error = clean_name(name, MAX_FOLDER_NAME)
    clash = db.exec(select(Folder).where(Folder.owner_id == viewer.id, Folder.name == name)).first()
    if error is None and clash is not None and clash.id != folder.id:
        error = "You already have a folder with that name."
    if error:
        return folder_redirect(course_id, viewer, folder.id, error)
    folder.name = name
    db.add(folder)
    db.commit()
    return folder_redirect(course_id, viewer, folder.id)


@router.post("/folders/{folder_id}/delete")
def delete_folder(folder_id: int, course_id: int | None = Form(None), viewer: Viewer = Depends(checked_member),
                  db: Session = Depends(get_session)):
    """Delete a folder. The agents in it are kept; they just lose this tag."""
    folder = own_folder(db, viewer, folder_id)
    for link in db.exec(select(AgentFolder).where(AgentFolder.folder_id == folder.id)).all():
        db.delete(link)
    db.delete(folder)
    db.commit()
    return folder_redirect(course_id, viewer)


# ---------------------------------------------------------------------------
# One agent's page
# ---------------------------------------------------------------------------


def header_context(db: Session, viewer: Viewer, agent: Agent, settings_open: bool = False, **errors) -> dict:
    tagged = folders_of(db, agent.id)
    tagged_ids = {folder.id for folder in tagged}
    untagged = [f for f in owner_folders(db, agent.owner_id) if f.id not in tagged_ids] if owns_agent(viewer, agent) else []
    return {"agent": agent, "online": is_online(agent), "tagged": tagged, "untagged": untagged,
            "is_owner": owns_agent(viewer, agent), "settings_open": settings_open,
            "check_in": check_in_text(agent), "owner": db.get(User, agent.owner_id),
            "error": errors.get("error"), "monitor_error": errors.get("monitor_error"),
            "description_error": errors.get("description_error")}


# Outputs and Monitors work the same way on an agent's page (the "feeds"):
# a Filter panel to choose which categories/monitors to show, sorting, a bell
# per name (notify me) and one pin per agent (shown on its grid card).
#   - outputs:  one-off items that add to a list, grouped by category;
#   - monitors: named datapoints, each replaced by the agent's next value.
FEEDS = {
    "outputs": {"model": Output, "name": Output.category, "time": Output.created_at, "noun": "category",
                "nouns": "categories", "title": "Outputs", "sorts": {"date": "Date", "name": "Category name",
                                                                       "content": "Content"}},
    "monitors": {"model": Monitor, "name": Monitor.name, "time": Monitor.updated_at, "noun": "monitor",
                 "nouns": "monitors", "title": "Monitors", "sorts": {"date": "Last updated", "name": "Name",
                                                                     "content": "Content"}},
}


def feed_names(db: Session, agent: Agent, section: str) -> list[str]:
    return output_categories(db, agent.id) if section == "outputs" else monitor_names(db, agent.id)


def pinned_name(agent: Agent, section: str) -> str | None:
    return agent.pinned_category if section == "outputs" else agent.pinned_monitor_name


def notifying_names(db: Session, viewer: Viewer, agent: Agent, section: str) -> set[str]:
    if section == "outputs":
        column, model = NotifySetting.category, NotifySetting
    else:
        column, model = MonitorNotifySetting.name, MonitorNotifySetting
    return set(db.exec(select(column).where(model.agent_id == agent.id, model.user_id == viewer.id)).all())


def feed_context(db: Session, viewer: Viewer, agent: Agent, section: str, selected: list[str] | None = None,
                 show_all: bool = False, sort: str = "date", order: str = "desc") -> dict:
    """A feed (Outputs or Monitors) and its Filter panel, for this viewer's choices.

    selected=None means a first visit: nothing is ticked, except the pinned
    name, which starts ticked. Each visit starts fresh.
    """
    feed = FEEDS[section]
    model = feed["model"]
    names = feed_names(db, agent, section)
    notifying = notifying_names(db, viewer, agent, section)
    pinned = pinned_name(agent, section)
    rows = [{"name": n, "notify": n in notifying, "pinned": n == pinned} for n in names]
    if selected is None:
        selected = [pinned] if pinned else []
    selected = [name for name in dict.fromkeys(selected) if name in names]
    sort = sort if sort in feed["sorts"] else "date"
    order = "asc" if order == "asc" else "desc"
    items = []
    if show_all or selected:
        statement = select(model).where(model.agent_id == agent.id)
        if not show_all:
            statement = statement.where(feed["name"].in_(selected))
        columns = {"date": [feed["time"], model.id],
                   "name": [func.lower(feed["name"]), feed["name"]],
                   "content": [func.lower(model.value), model.value]}[sort]
        ordering = [c.asc() if order == "asc" else c.desc() for c in columns]
        if sort != "date":
            ordering += [feed["time"].desc(), model.id.desc()]
        items = db.exec(statement.order_by(*ordering).limit(OUTPUTS_SHOWN)).all()
    if show_all:
        summary = "All"
    elif selected:
        summary = selected[0] if len(selected) == 1 else f"{len(selected)} {feed['nouns']}"
    else:
        summary = "Choose..."
    total = (count_outputs(db, agent.id) if section == "outputs" else len(names))
    return {"agent": agent, "section": section, "feed": feed, "names": rows, "selected": selected,
            "show_all": show_all, "sort": sort, "order": order, "summary": summary, "entries": items,
            "files": file_details(db, [i.file_id for i in items if i.file_id]), "inline_types": INLINE_TYPES,
            "total": total, "is_owner": owns_agent(viewer, agent)}


def both_feeds(db: Session, viewer: Viewer, agent: Agent) -> dict:
    return {"outputs_feed": feed_context(db, viewer, agent, "outputs"),
            "monitors_feed": feed_context(db, viewer, agent, "monitors")}


def inbox_context(db: Session, agent: Agent, error: str | None = None, notice: str | None = None) -> dict:
    items = db.exec(select(InboxFile).where(InboxFile.agent_id == agent.id)
                    .order_by(InboxFile.created_at, InboxFile.id)).all()
    files = file_details(db, [item.file_id for item in items])
    return {"agent": agent, "inbox": [{"item": i, "file": files.get(i.file_id)} for i in items],
            "inbox_error": error, "inbox_notice": notice, "inbox_max": human_size(INBOX_FILE_LIMIT)}


@router.get("/agents/{agent_id}")
def agent_page(request: Request, agent_id: int, viewer: Viewer = Depends(current_viewer),
               db: Session = Depends(get_session)):
    """One agent's page: Monitors, Outputs, Activity, and (for its owner) Controls."""
    if viewer.is_guest:
        raise forbidden("Guests can see the agent grid, but not agent pages.")
    agent = viewable_agent(db, viewer, agent_id)
    context = header_context(db, viewer, agent) | both_feeds(db, viewer, agent)
    if owns_agent(viewer, agent):
        context |= inbox_context(db, agent) | {"site_url": site_url(request)}
    course = db.get(Course, agent.course_id)
    return render(request, db, viewer, "agent_page.html", course=course,
                  back_url=grid_url(agent, page=page_of(db, agent)), **context)


def feed_list(db: Session, viewer: Viewer, agent_id: int, section: str, c: list[str], show_all: bool,
              sort: str, order: str, known: list[str], seen: str | None):
    """A feed's list for the Filter's current choices (also re-asked every few seconds).

    Only the list is replaced, so an open Filter panel isn't disturbed. The
    panel's rows are only re-sent (out of band) when names were added or
    removed: `known` lists the ones the panel shows now.
    """
    agent = viewable_agent(db, viewer, agent_id)
    context = feed_context(db, viewer, agent, section, c, show_all, sort, order)
    parts = [render_text(db, viewer, "partials/feed_list.html", f=context),
             render_text(db, viewer, "partials/feed_summary.html", f=context, oob=True)]
    if sorted(known) != sorted(row["name"] for row in context["names"]):
        parts.append(render_text(db, viewer, "partials/feed_options.html", f=context, oob=True))
    return poll_reply("".join(parts), seen, f"{section}-seen")


@router.get("/agents/{agent_id}/outputs")
def agent_outputs(agent_id: int, c: list[str] = Query([]), show_all: bool = Query(False, alias="all"),
                  sort: str = "date", order: str = "desc", known: list[str] = Query([]), seen: str | None = None,
                  viewer: Viewer = Depends(require_member), db: Session = Depends(get_session)):
    return feed_list(db, viewer, agent_id, "outputs", c, show_all, sort, order, known, seen)


@router.get("/agents/{agent_id}/monitors")
def agent_monitors(agent_id: int, c: list[str] = Query([]), show_all: bool = Query(False, alias="all"),
                   sort: str = "date", order: str = "desc", known: list[str] = Query([]), seen: str | None = None,
                   viewer: Viewer = Depends(require_member), db: Session = Depends(get_session)):
    return feed_list(db, viewer, agent_id, "monitors", c, show_all, sort, order, known, seen)


def toggle_pin(db: Session, viewer: Viewer, agent_id: int, section: str, name: str, c: list[str], show_all: bool):
    """Pin an output category or a monitor to the agent's grid card, or unpin it.

    An agent has one pin, shared by both feeds: pinning here unpins whatever
    was pinned in the other feed, whose pin button is sent back too (out of band).
    """
    agent = own_agent(db, viewer, agent_id)
    other = "monitors" if section == "outputs" else "outputs"
    other_pinned = pinned_name(agent, other)
    now_pinned = None if pinned_name(agent, section) == name else name
    agent.pinned_category = now_pinned if section == "outputs" else None
    agent.pinned_monitor_name = now_pinned if section == "monitors" else None
    db.add(agent)
    db.commit()
    reply = render_text(db, viewer, "partials/feed_options.html", f=feed_context(db, viewer, agent, section, c, show_all))
    if other_pinned and now_pinned:
        reply += render_text(db, viewer, "partials/feed_pin.html", agent=agent, section=other, is_owner=True,
                             row={"name": other_pinned, "pinned": False}, oob=True)
    return HTMLResponse(reply)


@router.post("/agents/{agent_id}/outputs/pin")
def pin_output_category(agent_id: int, name: str = Form(...), c: list[str] = Form([]),
                        show_all: bool = Form(False, alias="all"), viewer: Viewer = Depends(checked_member),
                        db: Session = Depends(get_session)):
    return toggle_pin(db, viewer, agent_id, "outputs", name, c, show_all)


@router.post("/agents/{agent_id}/monitors/pin")
def pin_monitor(agent_id: int, name: str = Form(...), c: list[str] = Form([]),
                show_all: bool = Form(False, alias="all"), viewer: Viewer = Depends(checked_member),
                db: Session = Depends(get_session)):
    return toggle_pin(db, viewer, agent_id, "monitors", name, c, show_all)


def toggle_notify(db: Session, viewer: Viewer, agent_id: int, section: str, name: str, c: list[str],
                  show_all: bool):
    """The bell: "notify me" about a category or monitor. Stored per viewer; a
    future Discord bot will send the notifications (nothing is sent yet)."""
    agent = viewable_agent(db, viewer, agent_id)
    if section == "outputs":
        existing = db.get(NotifySetting, (viewer.id, agent.id, name))
        new = NotifySetting(user_id=viewer.id, agent_id=agent.id, category=name)
    else:
        existing = db.get(MonitorNotifySetting, (viewer.id, agent.id, name))
        new = MonitorNotifySetting(user_id=viewer.id, agent_id=agent.id, name=name)
    db.delete(existing) if existing else db.add(new)
    db.commit()
    context = feed_context(db, viewer, agent, section, c, show_all)
    return HTMLResponse(render_text(db, viewer, "partials/feed_options.html", f=context))


@router.post("/agents/{agent_id}/outputs/notify")
def notify_output_category(agent_id: int, name: str = Form(...), c: list[str] = Form([]),
                           show_all: bool = Form(False, alias="all"), viewer: Viewer = Depends(checked_member),
                           db: Session = Depends(get_session)):
    return toggle_notify(db, viewer, agent_id, "outputs", name, c, show_all)


@router.post("/agents/{agent_id}/monitors/notify")
def notify_monitor(agent_id: int, name: str = Form(...), c: list[str] = Form([]),
                   show_all: bool = Form(False, alias="all"), viewer: Viewer = Depends(checked_member),
                   db: Session = Depends(get_session)):
    return toggle_notify(db, viewer, agent_id, "monitors", name, c, show_all)


@router.get("/agents/{agent_id}/monitors/{monitor_id}/view")
def view_monitor_pdf(agent_id: int, monitor_id: int, viewer: Viewer = Depends(require_member),
                     db: Session = Depends(get_session)):
    """A monitor's PDF, shown inside the page by the browser's own PDF viewer.
    (The one file type from agents shown inline besides images: the API only
    marks real PDFs as "pdf".)"""
    agent = viewable_agent(db, viewer, agent_id)
    monitor = db.get(Monitor, monitor_id)
    stored = db.get(StoredFile, monitor.file_id) if monitor and monitor.file_id else None
    if monitor is None or monitor.agent_id != agent.id or monitor.kind != "pdf" or stored is None:
        raise not_found("No such monitor PDF")
    return file_response(stored, inline=True, filename=stored.filename, media_type="application/pdf")


@router.post("/agents/{agent_id}/monitors/link")
def set_monitor_link(agent_id: int, name: str = Form(""), url: str = Form(""),
                     viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    """Agent settings: set a link monitor by name (or remove a monitor: leave the link empty)."""
    agent = own_agent(db, viewer, agent_id)
    name = " ".join(name.split())
    error = None
    if not name or len(name) > MAX_CATEGORY:
        error = f"Give the monitor a name of 1 to {MAX_CATEGORY} characters."
    elif not url.strip():
        set_monitor(db, agent, name, None, None, note=" (from the dashboard)")
    elif clean_link(url) is None:
        error = "The monitor link must start with http:// or https://."
    else:
        set_monitor(db, agent, name, "link", clean_link(url), note=" (from the dashboard)")
    return header_reply(db, viewer, agent, monitor_error=error)


# --- Activity, controls, messages ------------------------------------------------


@router.get("/agents/{agent_id}/events")
def agent_events(agent_id: int, seen: str | None = None, viewer: Viewer = Depends(require_member),
                 db: Session = Depends(get_session)):
    """The 50 newest Activity lines (every 3 seconds; 204 if nothing changed)."""
    agent = viewable_agent(db, viewer, agent_id)
    events = db.exec(select(Event).where(Event.agent_id == agent.id)
                     .order_by(Event.created_at.desc(), Event.id.desc()).limit(50)).all()
    return poll_reply(render_text(db, viewer, "partials/events.html", events=events), seen, "events-seen")


@router.post("/agents/{agent_id}/command")
def agent_command(agent_id: int, command: str = Form(...), text: str | None = Form(None),
                  viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    """Start, Pause, or a message, for the agent to pick up at its next check-in.

    Start and Pause also switch the stored paused state, which the agent
    learns at its check-ins. Messages are refused until the agent says it
    accepts them.
    """
    agent = own_agent(db, viewer, agent_id)
    command = command.strip()
    payload = None
    if command not in ("start", "pause", "message"):
        return toast("warning", "Unknown command.", 422)
    if command == "message":
        if not agent.accepts_messages:
            return toast("warning", f"{agent.name} doesn't accept messages.", 409)
        if not text or not text.strip():
            return toast("warning", "Type a message first.", 422)
        payload = {"text": text.strip()[:5000]}
    db.add(Command(agent_id=agent.id, command=command, payload=payload))
    if command in ("start", "pause"):
        agent.paused = command == "pause"
        db.add(agent)
    db.commit()
    db.refresh(agent)
    reply = alert_html("success", f'Sent "{command}" to {agent.name}.')
    # Also update the running/paused badge at the top of the page.
    return HTMLResponse(reply + render_text(db, viewer, "partials/run_state.html", agent=agent, oob=True))


@router.get("/agents/{agent_id}/messages-ready")
def messages_ready(agent_id: int, ready: bool, viewer: Viewer = Depends(require_member),
                   db: Session = Depends(get_session)):
    """Has the agent started or stopped accepting messages (or its Inbox) since
    the page loaded? Asked every 5 seconds: 204 if not, otherwise the page reloads."""
    agent = viewable_agent(db, viewer, agent_id)
    if agent.accepts_messages == ready:
        return Response(status_code=204)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


# --- The Inbox ---------------------------------------------------------------------


@router.get("/agents/{agent_id}/inbox")
def agent_inbox(agent_id: int, seen: str | None = None, viewer: Viewer = Depends(require_member),
                db: Session = Depends(get_session)):
    agent = own_agent(db, viewer, agent_id)
    return poll_reply(render_text(db, viewer, "partials/inbox_list.html", **inbox_context(db, agent)),
                      seen, "inbox-seen")


@router.post("/agents/{agent_id}/inbox")
def upload_to_inbox(agent_id: int, files: list[UploadFile] = File(...), note: str = Form(""),
                    viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    """Put files in an agent's Inbox (up to 50 MB each, 200 MB waiting in all).

    Refused until the agent has switched its Inbox on (accept_inbox in the connector).
    """
    agent = own_agent(db, viewer, agent_id)

    def reply(error=None, notice=None, status_code=200):
        return HTMLResponse(render_text(db, viewer, "partials/inbox_list.html",
                                        **inbox_context(db, agent, error, notice)), status_code=status_code)

    if not agent.accepts_inbox:
        return reply(error=f"{agent.name} hasn't switched its Inbox on yet, so it can't take files.",
                     status_code=409)
    note = note.strip()[:MAX_INBOX_NOTE] or None
    stored_names = []
    for upload in files:
        if not has_file(upload):
            continue
        try:
            stored = save_upload(db, upload, INBOX_FILE_LIMIT, viewer.id,
                                 extra_total=(inbox_waiting_total(db), INBOX_TOTAL_LIMIT))
        except UploadRefused as refusal:
            db.rollback()
            done = f"Uploaded {', '.join(stored_names)}. " if stored_names else ""
            return reply(error=done + f"Not uploaded: {refusal}", status_code=400)
        db.add(InboxFile(agent_id=agent.id, file_id=stored.id, note=note))
        log_activity(db, agent.id, "inbox", f"Inbox: the owner uploaded {stored.filename} ({human_size(stored.size)})")
        db.commit()
        stored_names.append(stored.filename)
    if not stored_names:
        return reply(error="Choose a file to upload.", status_code=400)
    return reply(notice=f"Uploaded {', '.join(stored_names)}.")


def own_inbox_item(db: Session, viewer: Viewer, item_id: int) -> tuple[InboxFile, Agent]:
    item = db.get(InboxFile, item_id)
    if item is None:
        raise not_found("Inbox file not found (the agent may have taken it already)")
    return item, own_agent(db, viewer, item.agent_id)


@router.get("/inbox/{item_id}/file")
def inbox_file(item_id: int, viewer: Viewer = Depends(require_member), db: Session = Depends(get_session)):
    """Download a waiting Inbox file again (owner only)."""
    item, _ = own_inbox_item(db, viewer, item_id)
    return file_response(db.get(StoredFile, item.file_id), inline=False)


@router.post("/inbox/{item_id}/delete")
def remove_from_inbox(item_id: int, viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    """Take a waiting file back out of the Inbox (owner only)."""
    item, agent = own_inbox_item(db, viewer, item_id)
    stored = db.get(StoredFile, item.file_id)
    filename = stored.filename if stored else "the file"
    log_activity(db, agent.id, "inbox", f"Inbox: the owner removed {filename} before the agent took it")
    db.delete(item)
    db.flush()
    delete_stored_file(db, item.file_id)
    db.commit()
    return HTMLResponse(render_text(db, viewer, "partials/inbox_list.html",
                                    **inbox_context(db, agent, notice=f"Removed {filename}.")))


# --- Agent settings (owner only) ------------------------------------------------------


def header_reply(db: Session, viewer: Viewer, agent: Agent, **errors) -> HTMLResponse:
    """The page's header again, with the settings pop-up kept open."""
    return HTMLResponse(render_text(db, viewer, "partials/agent_header.html",
                                    **header_context(db, viewer, agent, settings_open=True, **errors)))


@router.post("/agents/{agent_id}/rename")
def rename_agent(agent_id: int, name: str = Form(""), viewer: Viewer = Depends(checked_member),
                 db: Session = Depends(get_session)):
    """Rename an agent. Its key doesn't change, so it stays connected."""
    agent = own_agent(db, viewer, agent_id)
    name, error = clean_name(name, MAX_AGENT_NAME)
    if error is None and agent_name_taken(db, agent.owner_id, agent.course_id, name, except_id=agent.id):
        error = "You already have an agent with that name in this class."
    if error is None:
        agent.name = name
        db.add(agent)
        db.commit()
        db.refresh(agent)
    return header_reply(db, viewer, agent, error=error)


@router.post("/agents/{agent_id}/description")
def set_description(agent_id: int, description: str = Form(""), viewer: Viewer = Depends(checked_member),
                    db: Session = Depends(get_session)):
    agent = own_agent(db, viewer, agent_id)
    cleaned, error = clean_text(description, MAX_DESCRIPTION)
    if error is None:
        agent.description = cleaned
        db.add(agent)
        db.commit()
        db.refresh(agent)
    return header_reply(db, viewer, agent, description_error=error)


@router.post("/agents/{agent_id}/tags")
def add_tag(agent_id: int, folder_id: int = Form(...), viewer: Viewer = Depends(checked_member),
            db: Session = Depends(get_session)):
    agent = own_agent(db, viewer, agent_id)
    own_folder(db, viewer, folder_id)
    if db.get(AgentFolder, (agent.id, folder_id)) is None:
        db.add(AgentFolder(agent_id=agent.id, folder_id=folder_id))
        db.commit()
    return header_reply(db, viewer, agent)


@router.post("/agents/{agent_id}/tags/{folder_id}/remove")
def remove_tag(agent_id: int, folder_id: int, viewer: Viewer = Depends(checked_member),
               db: Session = Depends(get_session)):
    agent = own_agent(db, viewer, agent_id)
    link = db.get(AgentFolder, (agent.id, folder_id))
    if link is not None:
        db.delete(link)
        db.commit()
    return header_reply(db, viewer, agent)


@router.post("/agents/{agent_id}/delete")
def remove_agent(agent_id: int, password: str = Form(""), viewer: Viewer = Depends(checked_member),
                 db: Session = Depends(get_session)):
    """Delete an agent and everything it sent or was sent. Needs the owner's password."""
    agent = own_agent(db, viewer, agent_id)
    if not password_matches(password, viewer.user.password_hash):
        return form_error("Your password is incorrect. Nothing was deleted.")
    back = grid_url(agent)
    delete_agent(db, agent)
    db.commit()
    return hx_redirect(back)


def key_reply(request: Request, db: Session, viewer: Viewer, agent: Agent, api_key: str, replaced: bool):
    response = render(request, db, viewer, "partials/api_key.html", agent=agent, api_key=api_key,
                      replaced=replaced, site_url=site_url(request))
    response.headers["Cache-Control"] = "no-store"  # never keep a key in the browser's cache
    return response


@router.post("/agents/{agent_id}/key/view")
def view_api_key(request: Request, agent_id: int, password: str = Form(""), viewer: Viewer = Depends(checked_member),
                 db: Session = Depends(get_session)):
    """Show an agent's API key again. Needs the owner's account password."""
    agent = own_agent(db, viewer, agent_id)
    if not password_matches(password, viewer.user.password_hash):
        return form_error("Your password is incorrect.")
    try:
        api_key = decrypt_api_key(agent.api_key_encrypted) if agent.api_key_encrypted else None
    except KeyEncryptionNotConfigured:
        return form_error("KEY_ENCRYPTION_SECRET isn't set on the server.")
    if api_key is None:
        return form_error("This key can't be decrypted (KEY_ENCRYPTION_SECRET has probably changed). "
                          "Use Replace key to make a new one.")
    return key_reply(request, db, viewer, agent, api_key, replaced=False)


@router.post("/agents/{agent_id}/key/replace")
def replace_api_key(request: Request, agent_id: int, password: str = Form(""), new_key: str = Form(""),
                    viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    """Give an agent a new key (blank: generate one). The old key stops working at once."""
    agent = own_agent(db, viewer, agent_id)
    if not password_matches(password, viewer.user.password_hash):
        return form_error("Your password is incorrect. The key was not changed.")
    new_key = new_key.strip() or generate_api_key()
    error = check_new_api_key(db, new_key)
    if error:
        return form_error(error)
    try:
        store_api_key(agent, new_key)
    except KeyEncryptionNotConfigured:
        return form_error("KEY_ENCRYPTION_SECRET isn't set on the server, so keys can't be stored.")
    db.add(agent)
    db.commit()
    return key_reply(request, db, viewer, agent, new_key, replaced=True)

