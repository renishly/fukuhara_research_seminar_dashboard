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
from fastapi import UploadFile
from sqlmodel import Session, func, or_, select

from db import Agent, Course, OrganizerRun, Skill, SkillCategory, SkillTree, StoredFile, User, as_utc, utc_now
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
        "category": category.name if category else None, "specialty": skill.specialty,
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

# The starting categories, used by the keyword rules (and offered to the AI,
# which may reword them or add others).
RULES = [
    ("Data retrieval", "Find and fetch information: searching the web, APIs, databases or papers, and "
     "collecting the results.", "search fetch retrieve scrape crawl query api download lookup collect source "
     "arxiv database sql http rss gather find"),
    ("Data processing and analysis", "Turn raw data into something usable: cleaning, transforming, "
     "summarising, measuring and comparing.", "analyze analyse analysis clean transform parse summarize "
     "summarise statistics aggregate extract classify cluster compare pandas csv metric"),
    ("Decision frameworks", "Help an agent choose what to do: criteria, rubrics, trade-offs, priorities and "
     "when to stop or ask.", "decide decision choose evaluate criteria rubric prioritize prioritise tradeoff "
     "trade-off judge score risk strategy should option weigh"),
    ("Planning and orchestration", "Break work into steps and coordinate them: plans, task lists, schedules, "
     "and working with other agents.", "plan planning step steps workflow orchestrate coordinate schedule "
     "task tasks subtask delegate pipeline loop agents sequence"),
    ("Writing and communication", "Produce text for people: reports, emails, summaries for readers, "
     "translations and presentations.", "write writing report email draft translate tone audience present "
     "presentation slides explain message letter essay"),
    ("Coding and tools", "Write, run or fix code, and use developer tools and file formats.",
     "code coding python javascript function test debug script tool tools git repository compile run "
     "command shell"),
]
GENERAL = ("General", "Skills that don't fit one of the other categories yet.")

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


# --- Organizing with keyword rules (no AI) ---------------------------------------------------------


def _words(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z][a-zA-Z\-]+", text.casefold())


def organize_with_rules(db: Session) -> int:
    """Put each skill in the category whose keywords it uses most. Commits."""
    lookup = {}
    for position, (name, description, _) in enumerate(RULES + [GENERAL + ("",)]):
        lookup[name] = upsert_category(db, name, description, position)
    keywords = {name: set(words.split()) for name, _, words in RULES}
    trees = {t.agent_id: t.content for t in db.exec(select(SkillTree)).all()}
    skills = list(db.exec(select(Skill).where(Skill.builtin.is_(None))).all())
    for skill in skills:
        text = " ".join([skill.name, skill.summary or "", skill.content[:4000], trees.get(skill.agent_id, "")[:1000]])
        words = _words(text)
        scores = {name: sum(1 for word in words if word in keys) for name, keys in keywords.items()}
        best = max(scores, key=scores.get)
        category = lookup[best] if scores[best] > 0 else lookup[GENERAL[0]]
        skill.category_id = category.id
        # The category's keywords this skill uses, in the order it uses them.
        found = [word for word in dict.fromkeys(words) if word in keywords.get(category.name, set())]
        skill.specialty = ("Keywords: " + ", ".join(found[:5]) + ". (Sorted by keyword rules; the AI organizer "
                           "writes a fuller note when it's set up.)") if found else None
        skill.organized_version = skill.version
        db.add(skill)
    remove_unused_categories(db, {lookup[name].id for name in lookup})
    db.commit()
    return len(skills)


def upsert_category(db: Session, name: str, description: str, position: int) -> SkillCategory:
    category = db.exec(select(SkillCategory).where(SkillCategory.name == name)).first()
    category = category or SkillCategory(name=name)
    category.description, category.position, category.updated_at = description, position, utc_now()
    db.add(category)
    db.flush()
    return category


def remove_unused_categories(db: Session, keep_ids: set[int]) -> None:
    """Delete categories that are no longer offered and hold no skills."""
    for category in db.exec(select(SkillCategory)).all():
        if category.id in keep_ids:
            continue
        if db.exec(select(Skill.id).where(Skill.category_id == category.id)).first() is None:
            db.delete(category)


# --- Organizing with AI (OpenRouter) -------------------------------------------------------------------


def build_prompt(db: Session) -> tuple[str, list[Skill]]:
    """The request to the model: the current categories, every skill (shortened
    to fit PROMPT_BUDGET), and the skill trees that place them."""
    skills = list(db.exec(select(Skill).where(Skill.builtin.is_(None)).order_by(Skill.id)).all())
    trees = {t.agent_id: t.content for t in db.exec(select(SkillTree)).all()}
    per_skill = max(400, min(2000, PROMPT_BUDGET // max(1, len(skills))))
    existing = [{"name": c.name, "description": c.description} for c in categories(db)] or \
               [{"name": n, "description": d} for n, d, _ in RULES]
    lines = [
        "You organize a marketplace of 'skill' files (Markdown instructions) that AI agents use.",
        "Group the skills into categories BY FUNCTION: what the skill makes an agent able to do. For example, a skill",
        "about retrieving data belongs apart from one that is a decision framework. Reuse the existing categories",
        "when they fit (you may improve their descriptions); add or merge categories only when that clearly helps.",
        "Use 3 to 10 categories. For each category write a description (1-2 sentences) of what skills in it are",
        "expected to do. For each skill write a 'specialty' (1-2 sentences): what sets it apart from the other",
        "skills in its category. A skill tree says where each skill sits inside its agent; use it as context.",
        "Answer with JSON only, exactly in this shape:",
        '{"categories": [{"name": "...", "description": "..."}], "skills": [{"id": 1, "category": "...", "specialty": "..."}]}',
        "",
        "EXISTING CATEGORIES:", json.dumps(existing, ensure_ascii=False),
        "", "SKILLS:",
    ]
    for skill in skills:
        lines.append(json.dumps({"id": skill.id, "name": skill.name, "summary": skill.summary,
                                 "by": "agent" if skill.agent_name else "member",
                                 "text": skill.content[:per_skill]}, ensure_ascii=False))
        if skill.agent_id in trees:
            lines.append(json.dumps({"skill_tree_of_skill": skill.id,
                                     "tree": trees[skill.agent_id][:per_skill // 2]}, ensure_ascii=False))
    return "\n".join(lines), skills


def ask_model(prompt: str) -> str:
    """Send one request to OpenRouter and return the model's reply text."""
    response = requests.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {ai_key()}", "Content-Type": "application/json",
                 "X-Title": "Fukuhara Research Seminar Dashboard"},
        json={"model": (os.getenv("OPENROUTER_MODEL") or DEFAULT_MODEL).strip(),
              "messages": [{"role": "user", "content": prompt}], "temperature": 0.2},
        timeout=180,
    )
    if not response.ok:
        raise RuntimeError(f"OpenRouter answered HTTP {response.status_code}: {response.text[:200]}")
    return response.json()["choices"][0]["message"]["content"]


def parse_reply(reply: str) -> dict:
    """The JSON object in the model's reply (models sometimes wrap it in ```json fences or text)."""
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end < start:
        raise ValueError("the model's reply had no JSON in it")
    data = json.loads(reply[start:end + 1])
    if not isinstance(data.get("categories"), list) or not isinstance(data.get("skills"), list):
        raise ValueError("the model's reply didn't have categories and skills")
    return data


def organize_with_ai(db: Session) -> int:
    """Organize the market with one OpenRouter request. Commits.

    If the reply can't be used, nothing changes (the error is reported)."""
    prompt, skills = build_prompt(db)
    if not skills:
        return 0
    data = parse_reply(ask_model(prompt))
    lookup = {}
    for position, item in enumerate(data["categories"][:12]):
        name = " ".join(str(item.get("name") or "").split())[:60]
        if name and name not in lookup:
            lookup[name] = upsert_category(db, name, " ".join(str(item.get("description") or "").split())[:600],
                                           position)
    general = None
    by_id = {skill.id: skill for skill in skills}
    for item in data["skills"]:
        skill = by_id.get(item.get("id")) if isinstance(item, dict) else None
        if skill is None:
            continue
        category = lookup.get(" ".join(str(item.get("category") or "").split())[:60])
        if category is None:
            general = general or upsert_category(db, *GENERAL, len(lookup))
            category = general
        skill.category_id = category.id
        skill.specialty = " ".join(str(item.get("specialty") or "").split())[:600] or None
        skill.organized_version = skill.version
        db.add(skill)
    remove_unused_categories(db, {c.id for c in lookup.values()} | ({general.id} if general else set()))
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

