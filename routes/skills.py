"""The Agent Skills market in the browser: the section under the Agents tab's
student window, skill pages, uploads, skill trees, and running the organizer.

Members only. See market.py for who sees which skills.
"""

import io
import json
import os
import re
import secrets
import zipfile

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlmodel import Session, select

import market
import skillmap
from db import Agent, Course, Skill, SkillCategory, SkillTree, SkillVote, User, get_session, utc_now
from routes.agents import site_url
from permissions import course_name
from routes.common import get_course, not_found
from security import Viewer, checked_admin, checked_member, forbidden, require_member, secret_matches
from uploads import chosen_files, has_file
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
                source: str = "", sort: str = "category", mine: bool = False, reuse: str = "",
                viewer: Viewer = Depends(require_member), db: Session = Depends(get_session)):
    """The market's results for the filters (loaded when the Agents tab opens,
    and again whenever a filter changes)."""
    course = market_course(db, viewer, course_id)
    query = market.MarketQuery(scope="seminar" if scope == "seminar" else "class", q=q[:200], category=category,
                               source=source, sort=sort if sort in market.SORTS else "category", mine=mine,
                               reuse=reuse)
    skills = market.listed_skills(db, viewer, course, query)
    categories = market.categories(db)
    chosen = next((c for c in categories if str(c.id) == category), None)
    return render(request, db, viewer, "partials/skills_list.html", course=course, query=query, skills=skills,
                  votes=skillmap.scores(db, [skill.id for skill in skills]), unseen=market.unseen(db, viewer, skills),
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
    market.mark_seen(db, viewer, [skill])  # opening it counts as checking it (clears its "new" mark)
    owner = db.get(User, skill.owner_id) if skill.owner_id else None
    category = db.get(SkillCategory, skill.category_id) if skill.category_id else None
    course = db.get(Course, skill.course_id) if skill.course_id else None
    tree = market.visible_tree(db, viewer, skill.agent_id)
    return render(request, db, viewer, "partials/skill_detail.html", skill=skill, owner=owner, category=category,
                  course=course, credit=market.credit(skill, owner), identifier=market.identifier(skill),
                  address=market.address(skill, site_url(request)), tree=tree,
                  content=market.skill_text(skill, site_url(request)), view_file=market.tree_view_file(db, tree),
                  can_change=market.can_change_skill(viewer, skill), can_delete=market.can_delete_skill(viewer, skill),
                  **vote_context(db, viewer, skill))


def vote_context(db: Session, viewer: Viewer, skill: Skill) -> dict:
    mine = db.exec(select(SkillVote).where(SkillVote.skill_id == skill.id, SkillVote.user_id == viewer.id)).first()
    return {"vote_skill": skill, "vote_score": skillmap.scores(db, [skill.id]).get(skill.id, 0),
            "my_vote": mine.value if mine else 0}


@router.post("/skills/{skill_id}/vote")
def vote(request: Request, skill_id: int, value: int = Form(0), viewer: Viewer = Depends(checked_member),
         db: Session = Depends(get_session)):
    """Up (+1) or down (-1) vote; the same vote again takes it back. Votes size
    the skill's node on the skill map."""
    skill = get_skill(db, viewer, skill_id)
    mine = db.exec(select(SkillVote).where(SkillVote.skill_id == skill.id, SkillVote.user_id == viewer.id)).first()
    if value not in (1, -1) or (mine and mine.value == value):
        if mine:
            db.delete(mine)
    elif mine:
        mine.value = value
        db.add(mine)
    else:
        db.add(SkillVote(skill_id=skill.id, user_id=viewer.id, value=value))
    db.commit()
    context = vote_context(db, viewer, skill)
    reply = render(request, db, viewer, "partials/skill_vote.html", **context)
    # Tell the page straight away (ui.js): the skill map resizes its node, the market updates its total.
    reply.headers["HX-Trigger"] = json.dumps({"skill-voted": {
        "id": skill.id, "score": context["vote_score"], "r": round(skillmap.node_radius(context["vote_score"]), 1)}})
    return reply


@router.get("/skills/{skill_id}/download")
def download_skill(skill_id: int, request: Request, viewer: Viewer = Depends(require_member),
                   db: Session = Depends(get_session)):
    skill = get_skill(db, viewer, skill_id)
    skill.downloads += 1
    db.add(skill)
    db.commit()
    return Response(market.skill_text(skill, site_url(request)), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{market.identifier(skill)}.md"',
                             "X-Content-Type-Options": "nosniff"})


# --- Uploading and changing skills -----------------------------------------------------------------


def skill_form(request, db, viewer, course, skill=None, values=None, error=None, status_code=200):
    owner_chose = skill is not None and skill.category_source == "owner"
    values = values or {"name": skill.name if skill else "", "summary": (skill.summary or "") if skill else "",
                        "share": skill.share if skill else "class", "mode": "file", "content_text": "",
                        "category": str(market.number_of(db, skill)) if owner_chose else "",
                        "scope": skill.scope if skill else "personal"}
    return render(request, db, viewer, "partials/skill_form.html", status_code=status_code, course=course,
                  skill=skill, values=values, error=error, shares=market.SHARES, scopes=market.SCOPES,
                  fixed_categories=market.FIXED)


def apply_choices(db: Session, skill, category: str, scope: str) -> None:
    """The owner's category (or the organizer's, if none) and common/personal. The caller commits."""
    skill.scope = scope if scope in market.SCOPES else "personal"
    rows = market.fixed_categories(db)
    if category.isdigit() and int(category) in rows:
        skill.category_id, skill.category_source = rows[int(category)].id, "owner"
        skill.specialty = None
    else:
        if skill.category_source == "owner":
            skill.category_source = None
        market.file_skill(db, skill, rows)
    db.add(skill)


@router.get("/classes/{course_id}/skills/new")
def new_skill_form(request: Request, course_id: int, viewer: Viewer = Depends(require_member),
                   db: Session = Depends(get_session)):
    return skill_form(request, db, viewer, market_course(db, viewer, course_id))


@router.post("/classes/{course_id}/skills")
async def upload_skill(request: Request, course_id: int, name: str = Form(""), summary: str = Form(""),
                       share: str = Form("class"), mode: str = Form("file"), content_text: str = Form(""),
                       category: str = Form(""), scope: str = Form("personal"),
                       files: list[UploadFile] | None = File(None), viewer: Viewer = Depends(checked_member),
                       db: Session = Depends(get_session)):
    """A member shares skills directly (credited to them): typed, or one or
    more files, each becoming its own skill (several files: each is named
    after its first heading). Uploading a skill with the same name again
    updates it (same identifier, next version)."""
    course = market_course(db, viewer, course_id)
    values = {"name": name, "summary": summary, "share": share, "mode": mode, "content_text": content_text,
              "category": category, "scope": scope}
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
    for skill in skills:
        apply_choices(db, skill, category, scope)
    db.commit()
    if len({skill.id for skill in skills}) == 1:
        return hx_redirect(f"/classes/{course.id}?tab=skills&skill={skills[0].id}")
    return hx_redirect(f"/classes/{course.id}?tab=skills")


@router.get("/skills/{skill_id}/edit")
def edit_skill_form(request: Request, skill_id: int, viewer: Viewer = Depends(require_member),
                    db: Session = Depends(get_session)):
    skill = get_skill(db, viewer, skill_id)
    if not market.can_change_skill(viewer, skill):
        raise forbidden("Only the member a skill is credited to can change it.")
    return skill_form(request, db, viewer, db.get(Course, skill.course_id), skill=skill)


@router.post("/skills/{skill_id}/edit")
async def edit_skill(request: Request, skill_id: int, summary: str = Form(""), share: str = Form("class"),
                     mode: str = Form("file"), content_text: str = Form(""), category: str = Form(""),
                     scope: str = Form("personal"),
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
            values = {"name": skill.name, "summary": summary, "share": share, "mode": mode, "content_text": content_text,
                      "category": category, "scope": scope}
            return skill_form(request, db, viewer, db.get(Course, skill.course_id), skill=skill, values=values,
                              error=str(refusal), status_code=400)
        if content != skill.content:
            skill.content, skill.version = content, skill.version + 1
    skill.summary = " ".join(summary.split())[:market.MAX_SUMMARY] or None
    skill.share = share if share in market.SHARES else "class"
    skill.updated_at = utc_now()
    apply_choices(db, skill, category, scope)
    db.commit()
    return hx_redirect(f"/classes/{skill.course_id}?tab=skills&skill={skill.id}")


@router.post("/skills/{skill_id}/delete")
def remove_skill(skill_id: int, viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    """Remove a skill from the market: its member, or an admin (moderation)."""
    skill = get_skill(db, viewer, skill_id)
    if not market.can_delete_skill(viewer, skill):
        raise forbidden("Only the member a skill is credited to (or an admin) can remove it.")
    course_id = skill.course_id
    market.forget_votes(db, [skill.id])
    db.delete(skill)
    db.commit()
    return hx_redirect(f"/classes/{course_id}?tab=skills")


# --- Skill trees ----------------------------------------------------------------------------------------


def tree_form(request, db, viewer, course, agent_id=None, values=None, error=None, status_code=200):
    """The skill tree form. trees: which of the viewer's agents already have one
    (for them the AI version is optional, to add or replace just the user-friendly one)."""
    agents = my_agents(db, viewer, course)
    trees = {t.agent_id: t for t in db.exec(select(SkillTree).where(
        SkillTree.agent_id.in_([a.id for a in agents] or [-1]))).all()}
    return render(request, db, viewer, "partials/skill_tree_form.html", status_code=status_code, course=course,
                  agents=agents, trees=trees, chosen_agent=agent_id or (agents[0].id if agents else None),
                  error=error, values=values or {"mode": "file", "content_text": ""}, view_accept=market.VIEW_ACCEPT)


@router.get("/classes/{course_id}/skill-trees/new")
def new_tree_form(request: Request, course_id: int, agent: int | None = None,
                  viewer: Viewer = Depends(require_member), db: Session = Depends(get_session)):
    return tree_form(request, db, viewer, market_course(db, viewer, course_id), agent_id=agent)


@router.post("/classes/{course_id}/skill-trees")
async def upload_tree(request: Request, course_id: int, agent_id: int = Form(0), mode: str = Form("file"),
                      content_text: str = Form(""), file: UploadFile | None = File(None),
                      view: UploadFile | None = File(None),
                      viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    """Set (or replace) the skill tree of one of your agents in this class: the
    AI version (.md, required the first time) and/or the user-friendly version."""
    course = market_course(db, viewer, course_id)
    agent = db.get(Agent, agent_id)
    if agent is None or agent.owner_id != viewer.id or agent.course_id != course.id:
        raise forbidden("Choose one of your own agents in this class.")
    tree = db.exec(select(SkillTree).where(SkillTree.agent_id == agent.id)).first()
    has_text = (mode == "file" and has_file(file)) or (mode == "text" and content_text.strip())
    has_view = has_file(view)
    try:
        if tree is None and not has_text:
            raise market.SkillRefused("The AI version (.md) comes first: the organizer reads it. "
                                      "Add the user-friendly version with it, or later.")
        if not has_text and not has_view:
            raise market.SkillRefused("Choose a new AI version, a user-friendly version, or both.")
        content = await read_text_input(mode, content_text, file) if has_text else None
        if content is not None:
            tree = market.save_tree(db, agent, content)
        if has_view:
            market.save_tree_view(db, tree, view, viewer.id)
    except market.SkillRefused as refusal:
        return tree_form(request, db, viewer, course, agent_id=agent.id, error=str(refusal), status_code=400,
                         values={"mode": mode, "content_text": content_text})
    return hx_redirect(f"/classes/{course.id}?tab=skills")


@router.get("/skill-trees/{agent_id}")
def tree_page(request: Request, agent_id: int, viewer: Viewer = Depends(require_member),
              db: Session = Depends(get_session)):
    """A skill tree: its user-friendly version first, if it has one; the AI version behind a button."""
    tree = market.visible_tree(db, viewer, agent_id)
    if tree is None:
        raise not_found("Skill tree not found")
    agent = db.get(Agent, agent_id)
    return render(request, db, viewer, "partials/skill_tree_detail.html", tree=tree, agent=agent,
                  owner=db.get(User, tree.owner_id), view_file=market.tree_view_file(db, tree),
                  is_owner=viewer.id == tree.owner_id, can_delete=viewer.id == tree.owner_id or viewer.is_admin)


def owned_tree(db: Session, viewer: Viewer, agent_id: int) -> SkillTree:
    tree = db.exec(select(SkillTree).where(SkillTree.agent_id == agent_id)).first()
    if tree is None:
        raise not_found("Skill tree not found")
    if viewer.id != tree.owner_id and not viewer.is_admin:
        raise forbidden("Only the agent's owner (or an admin) can remove its skill tree.")
    return tree


@router.post("/skill-trees/{agent_id}/delete")
def remove_tree(agent_id: int, viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    tree = owned_tree(db, viewer, agent_id)
    course_id = db.get(Agent, agent_id).course_id
    market.delete_tree(db, tree)
    db.commit()
    return hx_redirect(f"/classes/{course_id}?tab=skills")


@router.post("/skill-trees/{agent_id}/view/delete")
def remove_tree_view(agent_id: int, viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    """Remove just the user-friendly version (the AI version stays)."""
    tree = owned_tree(db, viewer, agent_id)
    course_id = db.get(Agent, agent_id).course_id
    market.remove_tree_view(db, tree)
    db.commit()
    return hx_redirect(f"/classes/{course_id}?tab=skills")


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


@router.get("/classes/{course_id}/skill-map")
def skill_map(request: Request, course_id: int, show: str = "all", category: int | None = None,
              a: int | None = None, b: int | None = None, layout: str = "", viewer: Viewer = Depends(require_member),
              db: Session = Depends(get_session)):
    """Hoang Long's class skill map (skillmap.py): the matrix (filtered), one
    category across the class, or two students compared."""
    course = market_course(db, viewer, course_id)
    data = skillmap.build(db, viewer, course)
    show = show if show in skillmap.FILTERS else "all"
    rows = [row for row in data["rows"] if skillmap.keep(row, show)]
    by_id = {row.student.id: row for row in data["rows"]}
    compared = None
    if a in by_id and b in by_id and a != b:
        compared = {"a": by_id[a], "b": by_id[b], "lines": skillmap.compare(by_id[a], by_id[b], data["categories"])}
    chosen = next(((n, c) for n, c in data["categories"] if n == category), None)
    # A new random layout on every page load (no "layout" yet); the same one while this page goes back and forth.
    layout = layout[:16] if re.fullmatch(r"[0-9a-f]{1,16}", layout or "") else secrets.token_hex(4)
    picture = skillmap.graph(db, viewer, course, layout) if not chosen and not compared else None
    return render(request, db, viewer, "partials/skill_map.html", course=course, map=data, rows=rows, show=show,
                  filters=skillmap.FILTERS, chosen=chosen, compared=compared, a=a, b=b, graph=picture,
                  unseen=market.unseen(db, viewer, market.market_skills(db, course)), smooth=skillmap.smooth,
                  layout=layout)


@router.post("/classes/{course_id}/skills/bundle")
async def bundle(request: Request, course_id: int, viewer: Viewer = Depends(checked_member),
                 db: Session = Depends(get_session)):
    """The skills of a path drawn on the skill map, as one zip: a folder per
    category (e.g. 06-valuation/dcf-value/SKILL.md), and README.md listing the
    path in order. Each skill counts as downloaded."""
    course = market_course(db, viewer, course_id)
    form = await request.form()
    picked, seen = [], set()
    for raw in form.getlist("ids")[:200]:
        if str(raw).isdigit() and int(raw) not in seen:
            skill = db.get(Skill, int(raw))
            if skill is not None and market.can_see_skill(db, viewer, skill):
                picked.append(skill)
                seen.add(skill.id)
    if not picked:
        raise not_found("Pick at least one skill on the map.")
    following = skillmap.graph(db, viewer, course)["next"]
    if not skillmap.traversable(following, [skill.id for skill in picked]):
        raise HTTPException(400, "That path can't be followed on the skill map: each skill must be reachable "
                                 "from the one before, along the map's lines.")
    names = {row.id: (row.position, row.name) for row in market.categories(db)}
    buffer = io.BytesIO()
    lines = [f"# Skill path from the {course_name(course)} skill map", "",
             "The skills in the order they were picked (each folder is one class category):", ""]
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, skill in enumerate(picked, 1):
            number, label = names.get(skill.category_id, (None, "Not in a category"))
            folder = f"{number:02d}-{slug(label)}" if number is not None else "misc"
            path = f"{folder}/{slug(skill.name) or f'skill-{skill.id}'}/SKILL.md"
            zf.writestr(path, market.skill_text(skill, site_url(request)))
            lines.append(f"{n}. {skill.name} ({market.identifier(skill)}) - {label} - {path}")
            skill.downloads += 1
            db.add(skill)
        zf.writestr("README.md", "\n".join(lines) + "\n")
    db.commit()
    return Response(buffer.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="skill-path.zip"'})


def slug(text: str) -> str:
    """ "Market, competitor & 3C/4P" -> "market-competitor-3c-4p" """
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60]


@router.post("/classes/{course_id}/skills/seen")
def mark_all_seen(course_id: int, viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    """Clear every "new / updated" mark on this class's market for this member."""
    course = market_course(db, viewer, course_id)
    market.mark_seen(db, viewer, market.market_skills(db, course))
    return hx_redirect(f"/classes/{course.id}?tab=skills")
