"""Linked dashboards (links.py): a student adds, checks, archives and deletes
the links to where their work lives outside the dashboard. Only the student
whose dashboard it is can change its links. Members see them; guests don't."""

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse
from sqlmodel import Session, func, select

import links
from db import Agent, ExternalLink, get_session, utc_now
from permissions import student_is_enrolled
from routes.agent_tools import clean_link
from routes.common import get_course, not_found
from security import Viewer, checked_member, forbidden

router = APIRouter()


def back(course_id: int, agent_id: int | None, owner_id: int, error: str | None = None) -> RedirectResponse:
    """To the agent's page if the link belongs to an agent, else to the student's dashboard."""
    url = f"/agents/{agent_id}?" if agent_id else f"/classes/{course_id}/agents/{owner_id}?"
    if error:
        url += f"link_error={quote(error)}"
    return RedirectResponse(url + "#links", status_code=303)


def own_link(db: Session, viewer: Viewer, link_id: int) -> ExternalLink:
    link = db.get(ExternalLink, link_id)
    if link is None:
        raise not_found("Link not found")
    if link.owner_id != viewer.id:
        raise forbidden("Only the student who added this link can change it.")
    return link


@router.post("/classes/{course_id}/links")
def add_link(course_id: int, url: str = Form(""), kind: str = Form("site"), label: str = Form(""),
             access: str = Form("public"), agent: str = Form("", alias="agent_id"),
             viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    """Add a link to your own dashboard in this class (or to one of your agents
    in it), and check it straight away."""
    course = get_course(db, viewer, course_id)
    if not viewer.is_student or not course.has_agents or not student_is_enrolled(db, viewer.user, course):
        raise forbidden("Only students in this class can add links to their dashboard.")
    agent_id = int(agent) if agent.strip().isdigit() else None
    if agent_id is not None:
        owned = db.get(Agent, agent_id)
        if owned is None or owned.owner_id != viewer.id or owned.course_id != course.id:
            raise forbidden("You can only add links to your own agents in this class.")
    error = None
    clean = clean_link(url)
    label = " ".join(label.split())
    if clean is None:
        error = "Enter a web address starting with http:// or https://."
    elif kind not in dict(links.KINDS) or access not in dict(links.ACCESS):
        error = "Choose what it is and who can open it."
    elif len(label) > links.MAX_LABEL:
        error = f"The label can be at most {links.MAX_LABEL} characters."
    elif db.exec(select(func.count()).select_from(ExternalLink).where(
            ExternalLink.owner_id == viewer.id, ExternalLink.course_id == course.id)).one() >= links.MAX_LINKS:
        error = f"You can have at most {links.MAX_LINKS} links in a class. Delete one you no longer need."
    if error:
        return back(course.id, agent_id, viewer.id, error)
    link = ExternalLink(owner_id=viewer.id, course_id=course.id, agent_id=agent_id, url=clean, kind=kind,
                        label=label or clean, access=access)
    db.add(link)
    db.commit()
    db.refresh(link)
    links.check(db, link)  # check on save
    return back(course.id, agent_id, viewer.id)


@router.post("/links/{link_id}/check")
def check_link(link_id: int, viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    """Check now (at most once a minute per link)."""
    link = own_link(db, viewer, link_id)
    if link.archived_at is None and links.recheck_allowed(link):
        links.check(db, link)
    return back(link.course_id, link.agent_id, link.owner_id)


@router.post("/links/{link_id}/archive")
def archive_link(link_id: int, viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    """Retire a link: it stays listed in grey and is no longer checked."""
    link = own_link(db, viewer, link_id)
    link.archived_at = link.archived_at or utc_now()
    db.add(link)
    db.commit()
    return back(link.course_id, link.agent_id, link.owner_id)


@router.post("/links/{link_id}/restore")
def restore_link(link_id: int, viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    link = own_link(db, viewer, link_id)
    link.archived_at = None
    link.checked_at = None  # checked again soon
    db.add(link)
    db.commit()
    return back(link.course_id, link.agent_id, link.owner_id)


@router.post("/links/{link_id}/delete")
def delete_link(link_id: int, viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    link = own_link(db, viewer, link_id)
    course_id, agent_id, owner_id = link.course_id, link.agent_id, link.owner_id
    db.delete(link)
    db.commit()
    return back(course_id, agent_id, owner_id)
