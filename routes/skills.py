"""The Agent Skills market in the browser: the section under the Agents tab's
student window, skill pages, uploads, skill trees, and running the organizer.

Members only. See market.py for who sees which skills.
"""

import os

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlmodel import Session, select

import market
from db import Agent, Course, Skill, SkillCategory, SkillTree, User, get_session, utc_now
from routes.agents import site_url
from routes.common import get_course, not_found
from security import Viewer, checked_admin, checked_member, forbidden, require_member, secret_matches
from uploads import chosen_files
from web import hx_redirect, render, toast

router = APIRouter()


def market_course(db: Session, viewer: Viewer, course_id: int) -> Course:
    """The class whose market this is (members only; the class needs its Agents tab)."""
    course = get_course(db, viewer, course_id)
    if not course.has_agents:
        raise not_found("This class has no Agents tab")
    return course


def get_skill(db: Session, viewer: Viewer, skill_id: int) -> Skill:
    skill = db.get(Skill, skill_id)
    if skill is None or not market.can_see_skill(db, viewer, skill):
        raise not_found("Skill not found")
    return skill


def my_agents(db: Session, viewer: Viewer, course: Course) -> list[Agent]:
    return list(db.exec(select(Agent).where(Agent.owner_id == viewer.id, Agent.course_id == course.id)
                        .order_by(Agent.name)).all())


MAX_SKILL_FILES = 20  # skill files shared in one go


async def read_text_input(mode: str, content_text: str, file: UploadFile | None) -> str:
    """The skill text from the form: typed, or from an uploaded .md/.txt file."""
    if mode == "file":
        if file is None or not file.filename:
            raise market.SkillRefused("Choose a file, or switch to typing the text.")
        return market.clean_skill_text(await file.read(market.MAX_SKILL_BYTES + 1))
    return market.clean_skill_text(content_text)


# --- The market section -----------------------------------------------------------------------


@router.get("/classes/{course_id}/skills")
def market_list(request: Request, course_id: int, scope: str = "class", q: str = "", category: str = "",
                source: str = "", sort: str = "category", mine: bool = False,
                viewer: Viewer = Depends(require_member), db: Session = Depends(get_session)):
    """The market's results for the filters (loaded when the Agents tab opens,
    and again whenever a filter changes)."""
    course = market_course(db, viewer, course_id)
    query = market.MarketQuery(scope="seminar" if scope == "seminar" else "class", q=q[:200], category=category,
                               source=source, sort=sort if sort in market.SORTS else "category", mine=mine)
    skills = market.listed_skills(db, viewer, course, query)
    categories = market.categories(db)
    chosen = next((c for c in categories if str(c.id) == category), None)
    return render(request, db, viewer, "partials/skills_list.html", course=course, query=query, skills=skills,
                  groups=market.grouped(db, skills) if query.sort == "category" and not chosen else None,
                  chosen=chosen, owners=market.owner_names(db, skills), categories={c.id: c for c in categories},
                  trees=set(db.exec(select(SkillTree.agent_id)).all()), identifier=market.identifier,
                  class_trees=db.exec(select(SkillTree, Agent).join(Agent, Agent.id == SkillTree.agent_id)
                                      .where(Agent.course_id == course.id).order_by(Agent.name)).all())


@router.get("/skills/{skill_id}")
def skill_page(request: Request, skill_id: int, viewer: Viewer = Depends(require_member),
               db: Session = Depends(get_session)):
    """A skill's details in a pop-up: its text, identifier, address and skill tree."""
    skill = get_skill(db, viewer, skill_id)
    owner = db.get(User, skill.owner_id)
    category = db.get(SkillCategory, skill.category_id) if skill.category_id else None
    course = db.get(Course, skill.course_id)
    return render(request, db, viewer, "partials/skill_detail.html", skill=skill, owner=owner, category=category,
                  course=course, credit=market.credit(skill, owner), identifier=market.identifier(skill),
                  address=market.address(skill, site_url(request)), tree=market.visible_tree(db, viewer, skill.agent_id),
                  can_change=market.can_change_skill(viewer, skill), can_delete=market.can_delete_skill(viewer, skill))


@router.get("/skills/{skill_id}/download")
def download_skill(skill_id: int, viewer: Viewer = Depends(require_member), db: Session = Depends(get_session)):
    skill = get_skill(db, viewer, skill_id)
    skill.downloads += 1
    db.add(skill)
    db.commit()
    return Response(skill.content, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="skill-{skill.id}.md"',
                             "X-Content-Type-Options": "nosniff"})


# --- Uploading and changing skills -----------------------------------------------------------------


def skill_form(request, db, viewer, course, skill=None, values=None, error=None, status_code=200):
    values = values or {"name": skill.name if skill else "", "summary": (skill.summary or "") if skill else "",
                        "share": skill.share if skill else "class", "mode": "file", "content_text": ""}
    return render(request, db, viewer, "partials/skill_form.html", status_code=status_code, course=course,
                  skill=skill, values=values, error=error, shares=market.SHARES)


@router.get("/classes/{course_id}/skills/new")
def new_skill_form(request: Request, course_id: int, viewer: Viewer = Depends(require_member),
                   db: Session = Depends(get_session)):
    return skill_form(request, db, viewer, market_course(db, viewer, course_id))


@router.post("/classes/{course_id}/skills")
async def upload_skill(request: Request, course_id: int, name: str = Form(""), summary: str = Form(""),
                       share: str = Form("class"), mode: str = Form("file"), content_text: str = Form(""),
                       files: list[UploadFile] | None = File(None), viewer: Viewer = Depends(checked_member),
                       db: Session = Depends(get_session)):
    """A member shares skills directly (credited to them): typed, or one or
    more files, each becoming its own skill (several files: each is named
    after its first heading). Uploading a skill with the same name again
    updates it (same identifier, next version)."""
    course = market_course(db, viewer, course_id)
    values = {"name": name, "summary": summary, "share": share, "mode": mode, "content_text": content_text}
    chosen = chosen_files(files) if mode == "file" else []
    try:
        if len(chosen) > MAX_SKILL_FILES:
            raise market.SkillRefused(f"Share at most {MAX_SKILL_FILES} skill files at once.")
        # Read and check every file first, so either all are shared or none.
        contents = []
        for file in chosen or [None]:
            try:
                contents.append(await read_text_input(mode, content_text, file))
            except market.SkillRefused as refusal:
                raise market.SkillRefused(f"{file.filename}: {refusal}" if len(chosen) > 1 else str(refusal))
    except market.SkillRefused as refusal:
        return skill_form(request, db, viewer, course, values=values, error=str(refusal), status_code=400)
    skills = [market.save_skill(db, course=course, owner=viewer.user, content=content,
                                name=name if len(contents) == 1 else None, summary=summary, share=share)[0]
              for content in contents]
    if len({skill.id for skill in skills}) == 1:
        return hx_redirect(f"/classes/{course.id}?tab=agents&skill={skills[0].id}")
    return hx_redirect(f"/classes/{course.id}?tab=agents")


@router.get("/skills/{skill_id}/edit")
def edit_skill_form(request: Request, skill_id: int, viewer: Viewer = Depends(require_member),
                    db: Session = Depends(get_session)):
    skill = get_skill(db, viewer, skill_id)
    if not market.can_change_skill(viewer, skill):
        raise forbidden("Only the member a skill is credited to can change it.")
    return skill_form(request, db, viewer, db.get(Course, skill.course_id), skill=skill)


@router.post("/skills/{skill_id}/edit")
async def edit_skill(request: Request, skill_id: int, summary: str = Form(""), share: str = Form("class"),
                     mode: str = Form("file"), content_text: str = Form(""),
                     files: list[UploadFile] | None = File(None),
                     viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    """Change a skill's description and sharing, and (optionally) replace its text."""
    file = next(iter(chosen_files(files)), None)
    skill = get_skill(db, viewer, skill_id)
    if not market.can_change_skill(viewer, skill):
        raise forbidden("Only the member a skill is credited to can change it.")
    replacing = (mode == "file" and file is not None and file.filename) or (mode == "text" and content_text.strip())
    if replacing:
        try:
            content = await read_text_input(mode, content_text, file)
        except market.SkillRefused as refusal:
            values = {"name": skill.name, "summary": summary, "share": share, "mode": mode, "content_text": content_text}
            return skill_form(request, db, viewer, db.get(Course, skill.course_id), skill=skill, values=values,
                              error=str(refusal), status_code=400)
        if content != skill.content:
            skill.content, skill.version = content, skill.version + 1
    skill.summary = " ".join(summary.split())[:market.MAX_SUMMARY] or None
    skill.share = share if share in market.SHARES else "class"
    skill.updated_at = utc_now()
    db.add(skill)
    db.commit()
    return hx_redirect(f"/classes/{skill.course_id}?tab=agents&skill={skill.id}")


@router.post("/skills/{skill_id}/delete")
def remove_skill(skill_id: int, viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    """Remove a skill from the market: its member, or an admin (moderation)."""
    skill = get_skill(db, viewer, skill_id)
    if not market.can_delete_skill(viewer, skill):
        raise forbidden("Only the member a skill is credited to (or an admin) can remove it.")
    course_id = skill.course_id
    db.delete(skill)
    db.commit()
    return hx_redirect(f"/classes/{course_id}?tab=agents")


# --- Skill trees ----------------------------------------------------------------------------------------


@router.get("/classes/{course_id}/skill-trees/new")
def new_tree_form(request: Request, course_id: int, viewer: Viewer = Depends(require_member),
                  db: Session = Depends(get_session)):
    course = market_course(db, viewer, course_id)
    return render(request, db, viewer, "partials/skill_tree_form.html", course=course,
                  agents=my_agents(db, viewer, course), error=None, values={"mode": "file", "content_text": ""})


@router.post("/classes/{course_id}/skill-trees")
async def upload_tree(request: Request, course_id: int, agent_id: int = Form(0), mode: str = Form("file"),
                      content_text: str = Form(""), file: UploadFile | None = File(None),
                      viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    """Set (or replace) the skill tree of one of your agents in this class."""
    course = market_course(db, viewer, course_id)
    agent = db.get(Agent, agent_id)
    if agent is None or agent.owner_id != viewer.id or agent.course_id != course.id:
        raise forbidden("Choose one of your own agents in this class.")
    try:
        content = await read_text_input(mode, content_text, file)
    except market.SkillRefused as refusal:
        return render(request, db, viewer, "partials/skill_tree_form.html", status_code=400, course=course,
                      agents=my_agents(db, viewer, course), error=str(refusal),
                      values={"mode": mode, "content_text": content_text})
    market.save_tree(db, agent, content)
    return hx_redirect(f"/classes/{course.id}?tab=agents")


@router.get("/skill-trees/{agent_id}")
def tree_page(request: Request, agent_id: int, viewer: Viewer = Depends(require_member),
              db: Session = Depends(get_session)):
    tree = market.visible_tree(db, viewer, agent_id)
    if tree is None:
        raise not_found("Skill tree not found")
    agent = db.get(Agent, agent_id)
    return render(request, db, viewer, "partials/skill_tree_detail.html", tree=tree, agent=agent,
                  owner=db.get(User, tree.owner_id), can_delete=viewer.id == tree.owner_id or viewer.is_admin)


@router.post("/skill-trees/{agent_id}/delete")
def remove_tree(agent_id: int, viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    tree = db.exec(select(SkillTree).where(SkillTree.agent_id == agent_id)).first()
    if tree is None:
        raise not_found("Skill tree not found")
    if viewer.id != tree.owner_id and not viewer.is_admin:
        raise forbidden("Only the agent's owner (or an admin) can remove its skill tree.")
    course_id = db.get(Agent, agent_id).course_id
    db.delete(tree)
    db.commit()
    return hx_redirect(f"/classes/{course_id}?tab=agents")


# --- Running the organizer --------------------------------------------------------------------------------


@router.post("/market/organize")
def organize_now(viewer: Viewer = Depends(checked_admin), db: Session = Depends(get_session)):
    """An admin organizes the market now. With the AI organizer this uses one of
    the day's few free requests, so it's limited to MAX_AI_RUNS_PER_DAY a day."""
    record = market.run(db, "manual")
    return toast("success" if record.ok else "warning", record.message, 200)


@router.post("/internal/organize-market", include_in_schema=False)
def scheduled_organize(x_organizer_token: str | None = Header(None), db: Session = Depends(get_session)):
    """Called by the daily timer (see .github/workflows/organize-market.yml) at
    15:00 JST, which also wakes a sleeping Render service. Runs only if an
    update is due, so calling it more often does no harm. Needs ORGANIZER_TOKEN."""
    if not secret_matches(x_organizer_token, os.getenv("ORGANIZER_TOKEN")):
        raise HTTPException(401, "Missing or wrong organizer token")
    record = market.run_if_due(db, None)
    return {"ran": record is not None, "message": record.message if record else "No update is due right now."}
