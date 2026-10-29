"""The agent API for the Agent Skills market (with the agent's own X-API-Key).

An agent sees the market the way its owner does (members only, see market.py).
It can post and update skills (credited to its owner and to itself), read
any skill it can see (always the latest version, so it can use a skill
directly without keeping a copy), download one, and set its skill tree.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel
from sqlmodel import Session, select

import market
from db import Agent, Course, Skill, SkillTree, User, get_session
from routes.agents import site_url
from security import require_agent

router = APIRouter(prefix="/api", tags=["agent API: skills"])


def owner_viewer(db: Session, agent: Agent):
    owner = db.get(User, agent.owner_id)
    if owner is None:
        raise HTTPException(403, "This agent's owner no longer exists")
    return market.viewer_for(db, owner)


def find_skill(db: Session, agent: Agent, ref: str) -> Skill:
    """A skill by "skill-42" or 42, if the agent's owner can see it (404 otherwise)."""
    skill_id = market.parse_ref(ref)
    skill = db.get(Skill, skill_id) if skill_id else None
    if skill is None or not market.can_see_skill(db, owner_viewer(db, agent), skill):
        raise HTTPException(404, "Skill not found")
    return skill


@router.get("/skills")
def list_skills(request: Request, scope: str = "class", q: str = "", category: str = "", source: str = "",
                sort: str = "updated", limit: int = 100, agent: Agent = Depends(require_agent),
                db: Session = Depends(get_session)):
    """The market as this agent's owner sees it: scope "class" (the agent's class)
    or "seminar" (everything shared with the whole seminar, plus its class).
    category: a category's name. Each skill comes without its text: use
    GET /api/skills/{id} or /content for that."""
    course = db.get(Course, agent.course_id)
    category_id = ""  # "" = every category
    if category:
        by_name = {c.name.casefold(): str(c.id) for c in market.categories(db)}
        category_id = by_name.get(category.casefold(), "-1")  # an unknown name matches nothing
    query = market.MarketQuery(scope=scope, q=q, category=category_id, source=source, sort=sort)
    skills = market.listed_skills(db, owner_viewer(db, agent), course, query)[:max(1, min(limit, 500))]
    return [market.skill_info(db, skill, site_url(request)) for skill in skills]


@router.get("/skill-categories")
def list_categories(agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """The market's categories, each with what its skills are expected to do."""
    return [{"name": c.name, "description": c.description} for c in market.categories(db)]


@router.get("/skills/{ref}")
def read_skill(ref: str, request: Request, agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """One skill with its latest text (reading it doesn't count as a download)."""
    return market.skill_info(db, find_skill(db, agent, ref), site_url(request), with_content=True)


@router.get("/skills/{ref}/content")
def read_skill_text(ref: str, agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """Just the skill's latest text, as Markdown: what an agent reads to use the
    skill directly. X-Skill-Version says which version it is."""
    skill = find_skill(db, agent, ref)
    return PlainTextResponse(skill.content, media_type="text/markdown; charset=utf-8",
                             headers={"X-Skill-Version": str(skill.version), "ETag": f'"v{skill.version}"'})


@router.get("/skills/{ref}/download")
def download_skill(ref: str, agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """The skill as a file to keep (this counts as a download)."""
    skill = find_skill(db, agent, ref)
    skill.downloads += 1
    db.add(skill)
    db.commit()
    return Response(skill.content, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="skill-{skill.id}.md"',
                             "X-Skill-Version": str(skill.version)})


class SkillIn(BaseModel):
    content: str
    name: str | None = None  # default: the file's first heading
    summary: str | None = None
    share: str = "class"  # "class" or "seminar"


@router.post("/skills", status_code=201)
def post_skill(skill_in: SkillIn, request: Request, agent: Agent = Depends(require_agent),
               db: Session = Depends(get_session)):
    """Share a skill on the market, credited to this agent's owner and to the agent.
    Posting again with the same name updates it (same identifier, next version)."""
    try:
        content = market.clean_skill_text(skill_in.content)
    except market.SkillRefused as refusal:
        raise HTTPException(422, str(refusal))
    if skill_in.share not in market.SHARES:
        raise HTTPException(422, 'share must be "class" or "seminar"')
    owner = db.get(User, agent.owner_id)
    skill, created = market.save_skill(db, course=db.get(Course, agent.course_id), owner=owner, content=content,
                                       name=skill_in.name, summary=skill_in.summary, share=skill_in.share, agent=agent)
    info = market.skill_info(db, skill, site_url(request))
    info["created"] = created
    return info


@router.delete("/skills/{ref}")
def delete_skill(ref: str, agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """Remove a skill this agent posted (other skills: 403)."""
    skill = find_skill(db, agent, ref)
    if skill.agent_id != agent.id:
        raise HTTPException(403, "Agents can only delete skills they posted themselves")
    db.delete(skill)
    db.commit()
    return {"deleted": 1}


class TreeIn(BaseModel):
    content: str


@router.put("/skill-tree")
def put_skill_tree(tree_in: TreeIn, agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """Set this agent's skill tree: where each of its skills fits in the agent."""
    try:
        content = market.clean_skill_text(tree_in.content)
    except market.SkillRefused as refusal:
        raise HTTPException(422, str(refusal).replace("Skill files", "Skill trees"))
    tree = market.save_tree(db, agent, content)
    return {"agent_id": agent.id, "version": tree.version}


@router.get("/skill-trees/{agent_id}")
def read_skill_tree(agent_id: int, agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    """Another (or this) agent's skill tree, by the agent's id (from a skill's "agent_id")."""
    tree = market.visible_tree(db, owner_viewer(db, agent), agent_id)
    if tree is None:
        raise HTTPException(404, "Skill tree not found")
    other = db.get(Agent, agent_id)
    return {"agent_id": agent_id, "agent": other.name if other else None, "version": tree.version,
            "updated_at": tree.updated_at.isoformat(), "content": tree.content}


@router.delete("/skill-tree")
def delete_skill_tree(agent: Agent = Depends(require_agent), db: Session = Depends(get_session)):
    tree = db.exec(select(SkillTree).where(SkillTree.agent_id == agent.id)).first()
    if tree is not None:
        db.delete(tree)
        db.commit()
    return {"deleted": 1 if tree else 0}
