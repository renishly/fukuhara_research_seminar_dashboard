"""Raiyaan's skill sorter on the dashboard: an agent's owner uploads its
skills folder as one zip; the dashboard checks it (skill_sorter.read_zip:
structure rules S1-S7, no secrets, never runs code), suggests a category for
every skill, and shows a "Check your skills" screen. Nothing is published
until the owner confirms; then each skill goes on the market under that
agent (uploading again updates them, as new versions).
"""

import io
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlmodel import Session, delete, select

import market
import skill_sorter
from db import Course, SkillUpload, User, get_session
from routes.agents import own_agent
from routes.common import not_found
from security import Viewer, checked_member
from web import hx_redirect, render

router = APIRouter()

MAX_ZIP_BYTES = 20 * 1024 * 1024  # the zip as uploaded (the sorter also limits it unpacked)
COMMON_HINTS = {"generic"}  # the sorter's reusability guess that suggests "common candidate"


def sort_zip(path: str, agent_name: str, owner_name: str) -> tuple[list[dict], list[str]]:
    """The sorter's suggestions plus each skill's full SKILL.md text.
    Raises skill_sorter.UploadRejected (with .errors) for a zip that breaks the rules."""
    raw, _ = skill_sorter.read_zip(path, strict=True)
    tree = skill_sorter.build_tree(path, owner_name, agent_name, use_ai=False)
    texts = {}
    with zipfile.ZipFile(path) as zf:
        for item in raw:
            texts[item["folder"]] = zf.read(item["path"] + "SKILL.md").decode("utf-8", errors="replace")
    skills = []
    for skill, item in zip(tree["skills"], raw):
        skills.append({"name": skill["name"], "description": skill["description"][:200],
                       "content": texts[item["folder"]],  # the sorter already limits SKILL.md to 100 KB
                       "category": skill["category"], "confidence": skill["confidence"],
                       "needs_review": skill["needs_review"], "reason": skill["reason"],
                       "reusability": skill["reusability"], "blocked": skill["status"] == "blocked",
                       "warnings": skill["warnings"],
                       "planned": str(item["meta"].get("status", "")).lower() == "planned"})
    return skills, tree["upload"]["warnings"]


@router.post("/agents/{agent_id}/skills/zip")
async def upload_zip(request: Request, agent_id: int, file: UploadFile = File(...),
                     viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    agent = own_agent(db, viewer, agent_id)
    data = await file.read(MAX_ZIP_BYTES + 1)
    errors = []
    if len(data) > MAX_ZIP_BYTES:
        errors = ["The zip is over 20 MB."]
    elif not zipfile.is_zipfile(io.BytesIO(data)):
        errors = ["That isn't a zip file."]
    skills, warnings = [], []
    if not errors:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "skills.zip"
            path.write_bytes(data)
            try:
                skills, warnings = sort_zip(str(path), agent.name, viewer.user.full_name)
            except skill_sorter.UploadRejected as rejected:
                errors = list(rejected.errors)
            except (ValueError, zipfile.BadZipFile) as problem:
                errors = [str(problem)]
    if not errors and not skills:
        errors = ["No skills found: put each one in skills/<skill-name>/SKILL.md."]
    if errors:
        return render(request, db, viewer, "partials/skill_zip.html", agent=agent, errors=errors, upload=None,
                      fixed_categories=market.FIXED, scopes=market.SCOPES)
    db.exec(delete(SkillUpload).where(SkillUpload.agent_id == agent.id))  # one pending upload per agent
    upload = SkillUpload(agent_id=agent.id, owner_id=viewer.id, filename=file.filename or "", skills=skills,
                         warnings=warnings)
    db.add(upload)
    db.commit()
    db.refresh(upload)
    return render(request, db, viewer, "partials/skill_zip.html", agent=agent, errors=None, upload=upload,
                  fixed_categories=market.FIXED, scopes=market.SCOPES, common_hints=COMMON_HINTS)


@router.post("/agents/{agent_id}/skills/zip/{upload_id}/publish")
async def publish(request: Request, agent_id: int, upload_id: int, viewer: Viewer = Depends(checked_member),
                  db: Session = Depends(get_session)):
    """Put the ticked skills on the market under this agent, with the owner's
    categories (which the organizer then leaves alone)."""
    agent = own_agent(db, viewer, agent_id)
    upload = db.get(SkillUpload, upload_id)
    if upload is None or upload.agent_id != agent.id:
        raise not_found("That upload is gone; upload the zip again.")
    form = await request.form()
    course = db.get(Course, agent.course_id)
    owner = db.get(User, agent.owner_id)
    rows = market.fixed_categories(db)
    published = 0
    for n, item in enumerate(upload.skills):
        if item["blocked"] or form.get(f"include_{n}") != "true":
            continue
        skill, _ = market.save_skill(db, course=course, owner=owner, content=item["content"], name=item["name"],
                                     summary=item["description"], share="class", agent=agent)
        chosen = str(form.get(f"category_{n}", ""))
        if chosen.isdigit() and int(chosen) in rows:
            skill.category_id, skill.category_source = rows[int(chosen)].id, "owner"
        skill.scope = "common" if form.get(f"scope_{n}") == "common" else "personal"
        skill.planned = bool(item.get("planned"))
        db.add(skill)
        published += 1
    db.delete(upload)
    db.commit()
    return hx_redirect(f"/classes/{agent.course_id}?tab=skills")
