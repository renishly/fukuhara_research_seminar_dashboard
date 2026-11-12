"""The Agent Skills market: shared skill files, skill trees, and the organizer.

Skills
  A skill is a Markdown file (like "skills.md") that a member, or one of their
  agents through the API, shares on the market. Each has a stable identifier,
  "skill-<number>", and an address (/api/skills/<number>/content) that always
  gives its latest version, so an agent can be told "use skill-42" and will
  always read the current text, even after its author updates it.

  Where a skill shows up depends on how it was shared:
    - "class":   only in the market of the class it was posted in;
    - "seminar": in every class's "Whole seminar" view, and also in the
                 default view of the class it was posted in.
  The market is for members only (never guests).

The organizer
  Sorts skills into categories by what they do (data retrieval, decision
  frameworks...), writes a description of each category, and for each skill a
  note on what sets it apart from the others. It uses an AI model through
  OpenRouter when OPENROUTER_API_KEY is set, and simple keyword rules
  otherwise. It runs at most twice a day (MAX_AI_RUNS_PER_DAY): always once
  at 15:00 JST, and once around 03:00 JST if skills changed since.
"""

import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

import skill_sorter
from fastapi import UploadFile
from sqlmodel import Session, delete, func, or_, select

from db import (Agent, Course, OrganizerRun, Skill, SkillCategory, SkillSeen, SkillTree, SkillVote, StoredFile, User, as_utc,
                utc_now)
from permissions import can_see_tree, can_view_course, course_name
from security import Viewer
from uploads import UploadRefused, delete_stored_file, save_upload
from web import JST, templates

MAX_SKILL_BYTES = 100 * 1024  # a skill or skill tree file: up to 100 KB of text
MAX_SKILL_NAME = 100
MAX_SUMMARY = 500
SHARES = {"class": "This class only", "seminar": "Whole seminar"}
SORTS = {"category": "By category", "updated": "Recently updated", "downloads": "Most downloaded",
         "name": "Name (A–Z)", "newest": "Newest"}

# --- Identifiers -------------------------------------------------------------------


def identifier(skill: Skill) -> str:
    """e.g. "skill-42": what to give an agent (read_skill("skill-42") in the connector).
    Built-in skills have a name instead of a number ("skill-connector"), the
    same on every copy of the dashboard."""
    return f"skill-{skill.builtin or skill.id}"


def address(skill: Skill, site: str = "") -> str:
    """The address an agent reads the latest version from (with its API key)."""
    return f"{site}/api/skills/{skill.builtin or skill.id}/content"


def parse_ref(ref: str) -> int | str | None:
    """ "skill-42" or "42" -> 42; "skill-connector" or "connector" -> "connector"
    (a built-in skill); None if it isn't any of these."""
    match = re.fullmatch(r"(?:skill-)?(\d+|[a-z][a-z-]*)", ref.strip())
    if not match:
        return None
    return int(match.group(1)) if match.group(1).isdigit() else match.group(1)


def find_skill(db: Session, ref: str) -> Skill | None:
    """A skill by any of its references (see parse_ref()), visible or not."""
    key = parse_ref(ref)
    if isinstance(key, int):
        return db.get(Skill, key)
    return db.exec(select(Skill).where(Skill.builtin == key)).first() if key else None


def skill_text(skill: Skill, site: str = "") -> str:
    """The skill's text as served: built-in skills get this site's address filled in."""
    return skill.content.replace("{{DOWNLOADED_FROM}}", site) if skill.builtin else skill.content


def credit(skill: Skill, owner: User | None) -> str:
    """ "Yamada Taro", or "from Yamada Taro's agent, Researcher" if an agent posted it."""
    if skill.builtin:
        return BUILT_IN_CREDIT
    who = owner.full_name if owner else "a deleted account"
    if skill.agent_name:
        return f"from {who}'s agent, {skill.agent_name}"
    return who


# --- Who can see what ----------------------------------------------------------------


def can_see_skill(db: Session, viewer: Viewer, skill: Skill) -> bool:
    """Members only. Seminar-wide skills: every member. Class-only skills:
    members who can see the class they were posted in."""
    if not viewer.is_member:
        return False
    if skill.share == "seminar":
        return True
    course = db.get(Course, skill.course_id)
    return course is not None and can_view_course(db, viewer, course)


def can_change_skill(viewer: Viewer, skill: Skill) -> bool:
    """The member it's credited to (also for skills their agents posted).
    Built-in skills: nobody (they change with the dashboard)."""
    return not skill.builtin and viewer.is_member and viewer.id == skill.owner_id


def can_delete_skill(viewer: Viewer, skill: Skill) -> bool:
    """Its member, or an admin (moderation). Built-in skills: nobody."""
    return not skill.builtin and (can_change_skill(viewer, skill) or viewer.is_admin)


def viewer_for(db: Session, user: User) -> Viewer:
    """A Viewer for a member without a browser (used for their agents' API calls)."""
    return Viewer(session=None, user=user)


# --- Listing -------------------------------------------------------------------------------


@dataclass
class MarketQuery:
    scope: str = "class"  # "class": this class's market; "seminar": the whole seminar
    q: str = ""
    category: str = ""  # a category id, "none" (not organized yet), or "" (all)
    source: str = ""  # "member", "agent" or "" (both)
    sort: str = "category"
    mine: bool = False
    reuse: str = ""  # "common", "personal" or "" (both): Masuda's flag


def listed_skills(db: Session, viewer: Viewer, course: Course, query: MarketQuery) -> list[Skill]:
    """The skills to show in a class's market for these choices, sorted."""
    statement = select(Skill)
    if query.scope == "seminar":
        # Seminar-wide skills from every class, plus this class's own.
        statement = statement.where(or_(Skill.share == "seminar", Skill.course_id == course.id))
    else:
        # This class's own, and the built-in skills (in every class's market).
        statement = statement.where(or_(Skill.course_id == course.id, Skill.builtin.is_not(None)))
    if query.source == "agent":
        statement = statement.where(Skill.agent_name.is_not(None))
    elif query.source == "member":
        statement = statement.where(Skill.agent_name.is_(None), Skill.builtin.is_(None))
    if query.mine:
        statement = statement.where(Skill.owner_id == viewer.id)
    if query.reuse in SCOPES:
        statement = statement.where(Skill.scope == query.reuse, Skill.builtin.is_(None))
    if query.category == "none":
        statement = statement.where(Skill.category_id.is_(None))
    elif query.category.isdigit():
        statement = statement.where(Skill.category_id == int(query.category))
    skills = [s for s in db.exec(statement).all() if can_see_skill(db, viewer, s)]
    if query.q.strip():
        words = query.q.casefold().split()
        owners = owner_names(db, skills)
        def haystack(s: Skill) -> str:
            return " ".join([s.name, s.summary or "", s.specialty or "", s.agent_name or "",
                             owners.get(s.owner_id, ""), credit(s, None) if s.builtin else "", identifier(s)]).casefold()
        skills = [s for s in skills if all(word in haystack(s) for word in words)]
    keys = {
        "updated": lambda s: (-as_utc(s.updated_at).timestamp(),),
        "newest": lambda s: (-as_utc(s.created_at).timestamp(),),
        "downloads": lambda s: (-s.downloads, s.name.casefold()),
        "name": lambda s: (s.name.casefold(),),
        "category": lambda s: (-as_utc(s.updated_at).timestamp(),),  # grouped later; newest first inside
    }
    key = keys.get(query.sort, keys["category"])
    return sorted(skills, key=lambda s: (s.builtin is None, *key(s)))  # built-in skills first


def owner_names(db: Session, skills: list[Skill]) -> dict[int, str]:
    ids = {s.owner_id for s in skills}
    if not ids:
        return {}
    return {u.id: u.full_name for u in db.exec(select(User).where(User.id.in_(ids))).all()}


def categories(db: Session) -> list[SkillCategory]:
    return list(db.exec(select(SkillCategory).order_by(SkillCategory.position, SkillCategory.name)).all())


def grouped(db: Session, skills: list[Skill]) -> list[dict]:
    """Skills grouped by category, each group with its category: built-in
    skills first (builtin: True), and None for skills the organizer hasn't
    looked at yet, listed last."""
    built_in = [s for s in skills if s.builtin]
    groups = [{"category": None, "builtin": True, "skills": built_in}] if built_in else []
    skills = [s for s in skills if not s.builtin]
    for category in categories(db):
        members = [s for s in skills if s.category_id == category.id]
        if members:
            groups.append({"category": category, "skills": members})
    rest = [s for s in skills if s.category_id is None
            or not any(g["category"] and g["category"].id == s.category_id for g in groups)]
    if rest:
        groups.append({"category": None, "skills": rest})
    return groups


def visible_tree(db: Session, viewer: Viewer, agent_id: int | None) -> SkillTree | None:
    """An agent's skill tree, if there is one and this member can see that agent's class
    (or the agent has shared a skill with the whole seminar)."""
    if not can_see_tree(db, viewer, agent_id):
        return None
    return db.exec(select(SkillTree).where(SkillTree.agent_id == agent_id)).first()


# --- Posting and updating ---------------------------------------------------------------------


class SkillRefused(Exception):
    """A skill or skill tree that can't be accepted; the message says why."""


def clean_skill_text(raw: str | bytes | None) -> str:
    """The text of a skill file: UTF-8, at most MAX_SKILL_BYTES, not empty."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_SKILL_BYTES:
            raise SkillRefused(f"Skill files can be at most {MAX_SKILL_BYTES // 1024} KB.")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise SkillRefused("Skill files must be text (Markdown) in UTF-8.") from None
    text = (raw or "").replace("\r\n", "\n").strip()
    if not text:
        raise SkillRefused("The skill file is empty.")
    if len(text.encode("utf-8")) > MAX_SKILL_BYTES:
        raise SkillRefused(f"Skill files can be at most {MAX_SKILL_BYTES // 1024} KB.")
    return text


def guess_name(content: str) -> str:
    """A name for a skill without one: its first Markdown heading, or its first line."""
    for line in content.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:MAX_SKILL_NAME]
    return "Untitled skill"


def save_skill(db: Session, *, course: Course, owner: User, content: str, name: str | None = None,
               summary: str | None = None, share: str = "class", agent: Agent | None = None) -> tuple[Skill, bool]:
    """Post a skill, or update it if the same member (or agent) already shared one
    with this name in this class: the content is replaced and its version goes up,
    keeping its identifier. Returns (skill, created?). Commits."""
    name = " ".join((name or "").split())[:MAX_SKILL_NAME] or guess_name(content)
    summary = " ".join((summary or "").split())[:MAX_SUMMARY] or None
    share = share if share in SHARES else "class"
    statement = select(Skill).where(Skill.course_id == course.id, Skill.owner_id == owner.id, Skill.name == name)
    statement = statement.where(Skill.agent_id == agent.id) if agent else statement.where(Skill.agent_id.is_(None))
    skill = db.exec(statement).first()
    created = skill is None
    if created:
        skill = Skill(course_id=course.id, owner_id=owner.id, agent_id=agent.id if agent else None,
                      agent_name=agent.name if agent else None, name=name, content=content)
    else:
        if skill.content != content:
            skill.version += 1
        skill.content = content
        if agent:
            skill.agent_name = agent.name
    skill.summary = summary if (summary or created) else skill.summary
    skill.share = share
    skill.updated_at = utc_now()
    db.add(skill)
    db.flush()
    file_skill(db, skill, fixed_categories(db))  # filed straight away by the rules (cheap)
    db.commit()
    db.refresh(skill)
    return skill, created


# The user-friendly version of a skill tree: a PDF, an HTML page or an image.
VIEW_FILE_LIMIT = 20 * 1024 * 1024  # bigger files couldn't be previewed in the page
VIEW_TYPES = {".pdf": "application/pdf", ".html": "text/html", ".htm": "text/html", ".png": "image/png",
              ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
VIEW_ACCEPT = ",".join(VIEW_TYPES)  # for the file chooser


def view_kind(stored: StoredFile | None) -> str | None:
    """How a skill tree's user-friendly version is shown: "pdf", "html", "image" or None."""
    if stored is None:
        return None
    if stored.content_type.startswith("image/"):
        return "image"
    return "pdf" if stored.content_type == "application/pdf" else "html"


templates.env.globals["tree_view_kind"] = view_kind


def save_tree(db: Session, agent: Agent, content: str) -> SkillTree:
    """Set (or replace) an agent's skill tree. Commits."""
    tree = db.exec(select(SkillTree).where(SkillTree.agent_id == agent.id)).first()
    if tree is None:
        tree = SkillTree(agent_id=agent.id, owner_id=agent.owner_id, content=content)
    elif tree.content != content:
        tree.content, tree.version = content, tree.version + 1
    tree.updated_at = utc_now()
    db.add(tree)
    db.commit()
    db.refresh(tree)
    return tree


# --- Built-in skills -------------------------------------------------------------------------

BUILT_IN_CREDIT = "built into the dashboard"
CONNECTOR_PROMPT = Path(__file__).resolve().parent / "connector" / "connector_prompt.md"
CONNECTOR_PREFACE = """# Dashboard connector (built-in skill)

This skill comes with the dashboard itself and always describes how to connect
to it **as it is now**: it is rebuilt from the dashboard's own connector
instructions every time the dashboard is updated. Its identifier never changes:
`skill-connector` (address: `{{DOWNLOADED_FROM}}/api/skills/connector/content`,
read with this agent's API key in the `X-API-Key` header).

- Read it when setting up the connection, and read it again whenever a call to
  the dashboard starts failing in a new way (for example 404, 405 or 422), or
  when its version (`X-Skill-Version`) is higher than the one you last used.
  Then update the connection code to match, keeping what already works.
- Never put the API key into code, logs or anything you share.

---

"""


def builtin_skills() -> dict[str, dict]:
    """What each built-in skill should be now: {key: {name, summary, content}}."""
    return {
        "connector": {
            "name": "Dashboard connector",
            "summary": "How an agent connects to this dashboard: outputs, monitors, status, commands, the Inbox "
                       "and skills. Always up to date with the dashboard.",
            "content": CONNECTOR_PREFACE + CONNECTOR_PROMPT.read_text(encoding="utf-8"),
        },
    }


def sync_builtin_skills(db: Session) -> None:
    """Make sure every built-in skill exists, seminar-wide, with its current text
    (its version goes up when the text changed). Runs at every start. Commits."""
    for key, wanted in builtin_skills().items():
        skill = db.exec(select(Skill).where(Skill.builtin == key)).first()
        if skill is None:
            skill = Skill(builtin=key, name=wanted["name"], content=wanted["content"])
        elif skill.content != wanted["content"]:
            skill.content, skill.version, skill.updated_at = wanted["content"], skill.version + 1, utc_now()
        skill.name, skill.summary, skill.share = wanted["name"], wanted["summary"], "seminar"
        skill.course_id = skill.owner_id = skill.agent_id = skill.agent_name = None
        db.add(skill)
    db.commit()


def save_tree_view(db: Session, tree: SkillTree, upload: UploadFile, uploaded_by: int | None) -> StoredFile:
    """Set (or replace) a skill tree's user-friendly version. Commits.
    Raises SkillRefused for a file type that isn't allowed, or a fake PDF."""
    name = (upload.filename or "").lower()
    extension = name[name.rfind("."):] if "." in name else ""
    if extension not in VIEW_TYPES:
        raise SkillRefused("The user-friendly version must be a PDF, an HTML page or an image (PNG, JPEG, GIF, WebP).")
    if extension == ".pdf" and not upload.file.read(5).startswith(b"%PDF-"):
        raise SkillRefused("That file isn't a real PDF.")
    upload.file.seek(0)
    try:
        stored = save_upload(db, upload, VIEW_FILE_LIMIT, uploaded_by)
    except UploadRefused as refusal:
        raise SkillRefused(str(refusal)) from refusal
    stored.content_type = VIEW_TYPES[extension]  # by its name, not what the browser claimed
    old_file_id, tree.view_file_id = tree.view_file_id, stored.id
    tree.view_updated_at = utc_now()
    db.add(tree)
    db.flush()
    delete_stored_file(db, old_file_id)
    db.commit()
    return stored


def remove_tree_view(db: Session, tree: SkillTree) -> None:
    """Remove a skill tree's user-friendly version (the AI version stays). The caller commits."""
    old_file_id, tree.view_file_id, tree.view_updated_at = tree.view_file_id, None, None
    db.add(tree)
    db.flush()
    delete_stored_file(db, old_file_id)


def delete_tree(db: Session, tree: SkillTree) -> None:
    """Delete a skill tree with its user-friendly version. The caller commits."""
    file_id = tree.view_file_id
    db.delete(tree)
    db.flush()
    delete_stored_file(db, file_id)


def tree_view_file(db: Session, tree: SkillTree | None) -> StoredFile | None:
    return db.get(StoredFile, tree.view_file_id) if tree is not None and tree.view_file_id else None


def skill_info(db: Session, skill: Skill, site: str = "", with_content: bool = False) -> dict:
    """A skill as JSON for the agent API (never anyone's Login ID)."""
    owner = db.get(User, skill.owner_id) if skill.owner_id else None
    category = db.get(SkillCategory, skill.category_id) if skill.category_id else None
    course = db.get(Course, skill.course_id) if skill.course_id else None
    info = {
        "id": skill.id, "identifier": identifier(skill), "address": address(skill, site), "name": skill.name,
        "summary": skill.summary, "credit": credit(skill, owner),
        "owner": owner.full_name if owner else None, "agent": skill.agent_name, "agent_id": skill.agent_id,
        "class": course_name(course) if course else None, "share": skill.share,
        "category": category.name if category else None, "category_number": category.position if category else None,
        "scope": skill.scope, "planned": skill.planned, "votes": skill_votes(db, skill.id), "specialty": skill.specialty,
        "version": skill.version, "downloads": skill.downloads,
        "created_at": as_utc(skill.created_at).isoformat(), "updated_at": as_utc(skill.updated_at).isoformat(),
        "skill_tree": f"{site}/api/skill-trees/{skill.agent_id}" if skill.agent_id else None,
    }
    if with_content:
        info["content"] = skill_text(skill, site)
    if skill.builtin:
        info["builtin"] = True
    return info


# --- The organizer ------------------------------------------------------------------------------

MAX_AI_RUNS_PER_DAY = 2  # OpenRouter's free tier allows very few requests a day
THREE_PM = 15  # the update that always happens: 15:00 JST
MORNING = 3  # the optional second update: 03:00 JST, only if skills changed
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openrouter/free"  # OpenRouter picks an available free model
PROMPT_BUDGET = 60_000  # characters of skill text sent to the model in one request

# The categories: the class's 9 from its skill tree (Oct 2026) plus the
# "Shared foundation" lane, as in Raiyaan's skill sorter (skill_sorter.py).
# Every skill is filed into one of them; the organizer never invents others.
FIXED = [(c["id"], c["name"], c["question"]) for c in [skill_sorter.FOUNDATION] + skill_sorter.CATEGORIES]
SCOPES = {"personal": "Personal", "common": "Common candidate"}

_lock = threading.Lock()  # one organizer run at a time


def jst_now() -> datetime:
    return utc_now().astimezone(JST)


def ai_key() -> str:
    return (os.getenv("OPENROUTER_API_KEY") or "").strip()


def runs_on(db: Session, jst_date: str, method: str | None = None) -> list[OrganizerRun]:
    statement = select(OrganizerRun).where(OrganizerRun.jst_date == jst_date)
    if method:
        statement = statement.where(OrganizerRun.method == method)
    return list(db.exec(statement.order_by(OrganizerRun.started_at)).all())


def last_run(db: Session) -> OrganizerRun | None:
    return db.exec(select(OrganizerRun).order_by(OrganizerRun.started_at.desc())).first()


def changed_since(db: Session, moment: datetime | None) -> bool:
    """Has any skill or skill tree been posted or changed since `moment`?"""
    members = Skill.builtin.is_(None)  # built-in skills aren't organized
    if moment is None:
        return db.exec(select(func.count(Skill.id)).where(members)).one() > 0
    newer_skill = db.exec(select(Skill.id).where(members, Skill.updated_at > moment)).first()
    newer_tree = db.exec(select(SkillTree.id).where(SkillTree.updated_at > moment)).first()
    unorganized = db.exec(select(Skill.id).where(members, Skill.category_id.is_(None))).first()
    return bool(newer_skill or newer_tree or unorganized)


def due(db: Session, now: datetime | None = None) -> str | None:
    """Which scheduled update is due now, if any: "3 pm", "morning" or None.

    - "3 pm": every day, once it's 15:00 JST or later, if no update has run
      since 15:00 today (so a sleeping server catches up when it wakes).
    - "morning": from 03:00 JST, if no update has run since 03:00 today and
      skills changed since the last one, and today's limit isn't used up.
    """
    now = (now or jst_now()).astimezone(JST)
    today = now.date().isoformat()
    todays = runs_on(db, today)
    if len([r for r in todays if r.method == "ai"]) >= MAX_AI_RUNS_PER_DAY and ai_key():
        return None
    def ran_since(hour: int) -> bool:
        since = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        return any(as_utc(r.started_at) >= since for r in todays)
    if now.hour >= THREE_PM and not ran_since(THREE_PM):
        return "3 pm"
    if MORNING <= now.hour < THREE_PM and not ran_since(MORNING):
        previous = last_run(db)
        if changed_since(db, as_utc(previous.started_at) if previous else None):
            return "morning"
    return None


def ai_runs_left(db: Session) -> int:
    return MAX_AI_RUNS_PER_DAY - len(runs_on(db, jst_now().date().isoformat(), "ai"))


def run(db: Session, trigger: str) -> OrganizerRun:
    """Organize the market now (AI if a key is set, keyword rules otherwise).

    The caller decides whether it's due; this only refuses when today's AI
    limit is used up (then it says so instead). Never raises.
    """
    with _lock:
        method = "ai" if ai_key() else "rules"
        now = jst_now()  # one clock for the run's day and start time
        record = OrganizerRun(jst_date=now.date().isoformat(), trigger=trigger, method=method,
                              started_at=now.astimezone(timezone.utc))
        if method == "ai" and ai_runs_left(db) <= 0:
            record.message = (f"Not run: the AI organizer already ran {MAX_AI_RUNS_PER_DAY} times today "
                              "(OpenRouter's free limit). The next update is at 15:00 JST.")
            record.finished_at = utc_now()
            return record  # not saved: it didn't use a request
        db.add(record)
        db.commit()
        try:
            count = organize_with_ai(db) if method == "ai" else organize_with_rules(db)
            how = "with the AI model" if method == "ai" else "with keyword rules (no OPENROUTER_API_KEY set)"
            record.ok, record.message = True, f"Organized {count} skills {how}."
        except Exception as error:  # noqa: BLE001 - any problem is reported, and the old categories stay
            db.rollback()
            record.message = f"Failed: {error}"[:500]
            print(f"Market organizer: {record.message}")
        record.finished_at = utc_now()
        db.add(record)
        db.commit()
        db.refresh(record)
        return record


def run_if_due(db: Session, trigger_name: str | None = None) -> OrganizerRun | None:
    """Run a scheduled update if one is due (see due())."""
    reason = due(db)
    if reason is None:
        return None
    return run(db, trigger_name or reason)


# --- Organizing: the class's fixed categories, rules first, AI only when unsure -----------------------


def fixed_categories(db: Session) -> dict[int, SkillCategory]:
    """The 10 fixed categories (number -> row), created or brought up to date.
    Categories from before (invented by the old organizer) are removed; their
    skills are filed again."""
    existing = list(db.exec(select(SkillCategory)).all())
    wanted = {(number, name, description) for number, name, description in FIXED}
    if {(c.position, c.name, c.description) for c in existing} == wanted and len(existing) == len(FIXED):
        return {c.position: c for c in existing}  # already right: nothing to write
    rows = {}
    for number, name, description in FIXED:
        rows[number] = upsert_category(db, name, description, number)
    keep = {row.id for row in rows.values()}
    for old in db.exec(select(SkillCategory)).all():
        if old.id not in keep:
            for skill in db.exec(select(Skill).where(Skill.category_id == old.id)).all():
                skill.category_id = None
                db.add(skill)
            db.flush()
            db.delete(old)
    db.flush()
    return rows


def skill_votes(db: Session, skill_id: int) -> int:
    """A skill's vote total (ups minus downs)."""
    return int(db.exec(select(func.coalesce(func.sum(SkillVote.value), 0)).where(SkillVote.skill_id == skill_id)).one())


def forget_votes(db: Session, skill_ids) -> None:
    """Delete the votes (and "checked" marks) on these skills, before the
    skills go. The caller commits."""
    ids = list(skill_ids)
    if ids:
        db.exec(delete(SkillVote).where(SkillVote.skill_id.in_(ids)))
        db.exec(delete(SkillSeen).where(SkillSeen.skill_id.in_(ids)))


def unseen(db: Session, viewer: Viewer, skills: list[Skill]) -> dict[int, str]:
    """The skills that are new ("new") or updated ("updated") for this member
    since they last opened them. Their own skills and built-in ones don't count."""
    candidates = [s for s in skills if not s.builtin and s.owner_id != viewer.id]
    if not candidates or not viewer.is_member:
        return {}
    seen = {row.skill_id: row.version for row in db.exec(select(SkillSeen).where(
        SkillSeen.user_id == viewer.id, SkillSeen.skill_id.in_([s.id for s in candidates]))).all()}
    return {s.id: ("new" if s.id not in seen else "updated") for s in candidates
            if s.id not in seen or seen[s.id] < s.version}


def mark_seen(db: Session, viewer: Viewer, skills: list[Skill]) -> None:
    """This member has checked these skills (their current versions). Commits."""
    if not viewer.is_member:
        return
    for skill in skills:
        row = db.get(SkillSeen, (viewer.id, skill.id)) or SkillSeen(user_id=viewer.id, skill_id=skill.id)
        row.version = skill.version
        db.add(row)
    db.commit()


def market_skills(db: Session, course: Course) -> list[Skill]:
    """Every member's skill on this class's market (its own and seminar-wide ones)."""
    return list(db.exec(select(Skill).where(Skill.builtin.is_(None),
                                            or_(Skill.course_id == course.id, Skill.share == "seminar"))).all())


def adopt_fixed_categories(db: Session) -> None:
    """At startup: if the stored categories aren't the class's fixed ones yet
    (a database from before), file every skill into them by the rules, once."""
    names = [(c.position, c.name) for c in categories(db)]
    if names != [(number, name) for number, name, _ in FIXED]:
        organize_with_rules(db)
        print("Skill market: skills filed into the class's 9 categories + Shared foundation")


def number_of(db: Session, skill: Skill) -> int | None:
    """A skill's category number (0-9), or None."""
    category = db.get(SkillCategory, skill.category_id) if skill.category_id else None
    return category.position if category else None


def file_skill(db: Session, skill: Skill, rows: dict[int, SkillCategory]) -> dict:
    """File one skill by Raiyaan's rules (its frontmatter first), unless its
    owner chose the category. Returns the sorter's result. The caller commits."""
    meta, body = skill_sorter.parse_frontmatter(skill.content or "")
    skill.planned = str(meta.get("status", "")).strip().lower() == "planned" or skill.planned
    if skill.category_source == "owner":
        return {"category": number_of(db, skill), "needs_review": False}
    description = str(meta.get("description") or skill.summary or "")
    result = skill_sorter.sort_skill(skill.name, description, skill_sorter._headings(body) + " " + body, meta)
    if result["category"] is None and str(meta.get("role", "")).lower() == "foundation":
        result.update(category=0, category_source="frontmatter", confidence=1.0, needs_review=False)
    skill.category_id = rows[result["category"]].id if result["category"] is not None else None
    skill.category_source = result["category_source"]
    skill.category_confidence = result["confidence"]
    skill.specialty = f"Why here: {result['reason']}." if result.get("reason") else None
    skill.organized_version = skill.version
    db.add(skill)
    return result


def organize_with_rules(db: Session) -> int:
    """File every skill into the fixed categories by the rules. Commits."""
    rows = fixed_categories(db)
    skills = list(db.exec(select(Skill).where(Skill.builtin.is_(None))).all())
    for skill in skills:
        file_skill(db, skill, rows)
    db.commit()
    return len(skills)


def upsert_category(db: Session, name: str, description: str, position: int) -> SkillCategory:
    category = db.exec(select(SkillCategory).where(SkillCategory.position == position)).first() or \
        db.exec(select(SkillCategory).where(SkillCategory.name == name)).first()
    category = category or SkillCategory(name=name)
    category.name, category.description, category.position, category.updated_at = name, description, position, utc_now()
    db.add(category)
    db.flush()
    return category


def build_prompt(skills: list[Skill]) -> str:
    """The request to the model: the fixed categories and the skills the rules
    weren't sure about."""
    per_skill = max(400, min(2000, PROMPT_BUDGET // max(1, len(skills))))
    lines = [
        "You file 'skill' files (Markdown instructions that AI agents use) into a research seminar's fixed",
        "categories. Pick exactly one category number for each skill, by its MAIN OUTPUT. For each skill also",
        "write a 'specialty' (1 sentence): what sets it apart. Answer with JSON only, exactly in this shape:",
        '{"skills": [{"id": 1, "category": 4, "specialty": "..."}]}',
        "", "CATEGORIES:",
        json.dumps([{"number": n, "name": name, "main output": d} for n, name, d in FIXED], ensure_ascii=False),
        "", "SKILLS:",
    ]
    for skill in skills:
        lines.append(json.dumps({"id": skill.id, "name": skill.name, "summary": skill.summary,
                                 "text": skill.content[:per_skill]}, ensure_ascii=False))
    return "\n".join(lines)


def ask_model(prompt: str) -> str:
    """Send one request to OpenRouter and return the model's reply text."""
    response = requests.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {ai_key()}", "Content-Type": "application/json",
                 "X-Title": "Fukuhara Research Seminar Dashboard"},
        json={"model": model_name(), "messages": [{"role": "user", "content": prompt}], "temperature": 0.2},
        timeout=180,
    )
    if not response.ok:
        raise RuntimeError(f"OpenRouter answered HTTP {response.status_code}: {response.text[:200]}")
    return response.json()["choices"][0]["message"]["content"]


def model_name() -> str:
    return (os.getenv("OPENROUTER_MODEL") or DEFAULT_MODEL).strip()


def parse_reply(reply: str) -> dict:
    """The JSON object in the model's reply (models sometimes wrap it in ```json fences or text)."""
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end < start:
        raise ValueError("the model's reply had no JSON in it")
    data = json.loads(reply[start:end + 1])
    if not isinstance(data.get("skills"), list):
        raise ValueError("the model's reply didn't list the skills")
    return data


def organize_with_ai(db: Session) -> int:
    """File every skill by the rules, then ask the AI (one request) about the
    ones the rules weren't sure of. Commits. If the AI's reply can't be used,
    the rules' filing stays."""
    rows = fixed_categories(db)
    skills = list(db.exec(select(Skill).where(Skill.builtin.is_(None)).order_by(Skill.id)).all())
    unsure = [skill for skill in skills if file_skill(db, skill, rows).get("needs_review")]
    db.commit()
    if not unsure:
        return len(skills)
    data = parse_reply(ask_model(build_prompt(unsure)))
    by_id = {skill.id: skill for skill in unsure}
    for item in data["skills"]:
        skill = by_id.get(item.get("id")) if isinstance(item, dict) else None
        number = item.get("category") if isinstance(item, dict) else None
        if skill is None or not isinstance(number, int) or number not in rows:
            continue
        skill.category_id, skill.category_source = rows[number].id, "ai"
        skill.specialty = " ".join(str(item.get("specialty") or "").split())[:600] or skill.specialty
        db.add(skill)
    db.commit()
    return len(skills)


# --- The schedule: a background check every few minutes -------------------------------------------------


def schedule_message(db: Session) -> str:
    """What admins see about the organizer in the market."""
    previous = last_run(db)
    how = "AI (OpenRouter)" if ai_key() else "keyword rules (no OPENROUTER_API_KEY set)"
    text = f"Organized by {how}, at 15:00 JST every day (and around 03:00 if skills changed)."
    if previous:
        when = as_utc(previous.started_at).astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
        text += f" Last run {when}: {previous.message}"
    return text

