"""The seminar chat (Yuri's Knowledge Hub): ask about anything on the
dashboard and get an answer with links to where it is.

What it may use: everything a member can see in the classes they can open,
namely classes, assignments and exams, submissions (titles, descriptions,
text), agents (state, description, latest outputs and monitors), recent
agent changes, forecasts and the Agent skills market, plus the OB/OG list
(names, companies, industries, notes). Never: passwords, API keys, login
IDs, sign-ins, or the alumni's email addresses and phone numbers (the
answer links to the OB/OG page instead).

How it answers: it picks the items that match the question best (shared
words; for Japanese, shared pairs of characters), adds the upcoming
deadlines, and asks the AI model (OpenRouter) to answer only from those,
citing them as [1], [2]... The page shows buttons to open each cited item.

The budget: OpenRouter's daily allowance is shared by the whole seminar
(OPENROUTER_DAILY_REQUESTS a day). The skill organizer's runs
(market.MAX_AI_RUNS_PER_DAY) are kept free for it; chat uses the rest, with
a cap per member. Without an AI key, or once the budget is used up, the chat
still answers with the matching items as search results.
"""

import html
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta

import os
import requests
from markupsafe import Markup
from sqlmodel import Session, func, or_, select

import forecasts
import market
from db import (Agent, Alumnus, Attachment, ChatMessage, Course, Monitor, OrganizerRun, Output, Skill, StateChange,
                StoredFile, User, as_utc, utc_now)
from permissions import visible_courses
from routes.agent_tools import newest_per, states_for
from routes.common import visible_submissions, visible_tasks
from security import Viewer
from web import JST, jst, relative_time

MAX_SOURCES = 12  # items sent to the model with each question
SOURCE_CHARS = 1200  # at most this much of each item's text
HISTORY = 6  # earlier messages sent along, so follow-up questions work
QUESTIONS_PER_DAY = 20  # per member (Japan day)
DEFAULT_DAILY_REQUESTS = 50  # OpenRouter's free allowance; set OPENROUTER_DAILY_REQUESTS to change it
TIMEOUT = 60
SUGGESTIONS = ["Brief me on this week", "What's due in the next two weeks?",
               "Which agents need attention right now?", "Who forecasts real estate, and what do they say?"]
BRIEFING = "brief me on this week"

STOP_WORDS = {"the", "and", "for", "what", "which", "who", "when", "where", "how", "are", "is", "was", "were",
              "this", "that", "with", "from", "about", "any", "our", "your", "you", "me", "my", "we", "of", "to",
              "in", "on", "it", "do", "does", "did", "can", "should", "a", "an", "be", "there", "have", "has",
              "show", "tell", "give", "find", "please", "they", "say"}


@dataclass
class Source:
    kind: str  # "Class", "Assignment", "Exam", "Submission", "Agent", "Forecast", "Skill", "OB/OG", ...
    title: str
    text: str
    url: str
    weight: float = 1.0

    def words(self) -> set[str]:
        return tokens(f"{self.kind} {self.title} {self.title} {self.text}")

    def ref(self) -> dict:
        return {"kind": self.kind, "title": self.title, "url": self.url}


def tokens(text: str) -> set[str]:
    """Lowercase words (2+ letters), plus pairs of Japanese characters."""
    text = text.lower()
    found = {w.rstrip("s") or w for w in re.findall(r"[a-z0-9][a-z0-9\-]+", text) if w not in STOP_WORDS}
    cjk = re.sub(r"[^぀-ヿ㐀-鿿]", " ", text)
    for run in cjk.split():
        found |= {run[i:i + 2] for i in range(len(run) - 1)} or {run}
    return found


def short(text: str | None, limit: int = SOURCE_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "..."


# ---------------------------------------------------------------------------
# What it knows: everything the member may see (minus private details)
# ---------------------------------------------------------------------------


def sources_for(db: Session, viewer: Viewer) -> list[Source]:
    now = utc_now()
    found: list[Source] = []
    upcoming: list[str] = []
    users = {u.id: u for u in db.exec(select(User)).all()}
    name = lambda user_id: users[user_id].full_name if user_id in users else "a former member"  # noqa: E731
    courses = visible_courses(db, viewer)
    agents_seen: list[Agent] = []
    for course in courses:
        label = course.title or "a class"
        found.append(Source("Class", label, f"{course.description}\nRuns {course.start_month:%B %Y} to "
                            f"{course.end_month:%B %Y}." + (f"\nDocuments folder: {course.documents_url}"
                                                            if course.documents_url else ""),
                            f"/classes/{course.id}", weight=0.6))
        for kind, tab in (("assignment", "assignments"), ("exam", "exams")):
            if kind == "exam" and not course.has_exams:
                continue
            for task in visible_tasks(db, viewer, course, kind):
                files = db.exec(select(StoredFile.filename).join(Attachment, Attachment.file_id == StoredFile.id)
                                .where(Attachment.task_id == task.id)).all()
                found.append(Source(kind.capitalize(), f"{task.title} ({label})",
                                    f"Due {jst(task.due_at)} ({relative_time(task.due_at)}). {task.description or ''}\n"
                                    f"{short(task.content_text)}" + (f"\nFiles: {', '.join(files)}" if files else ""),
                                    f"/classes/{course.id}?tab={tab}"))
                if now - timedelta(days=7) <= as_utc(task.due_at) <= now + timedelta(days=14):
                    upcoming.append(f"{label}: {kind} \"{task.title}\" due {jst(task.due_at)} "
                                    f"({relative_time(task.due_at)})")
            for sub in visible_submissions(db, viewer, course, kind)[:200]:
                found.append(Source("Submission", f"\"{sub.title or 'Untitled'}\" by {name(sub.student_id)} ({label})",
                                    f"{kind.capitalize()} submission, {jst(sub.created_at)}. {sub.description or ''}\n"
                                    f"{short(sub.content_text, 600)}",
                                    f"/classes/{course.id}/{tab}/students/{sub.student_id}", weight=0.8))
        if course.has_agents:
            agents_seen += list(db.exec(select(Agent).where(Agent.course_id == course.id)).all())
    found.append(Source("Deadlines", "Deadlines from a week ago to two weeks ahead",
                        "Today is " + now.astimezone(JST).strftime("%A %Y-%m-%d") + ".\n"
                        + ("\n".join(upcoming) or "No deadlines in that time."), "/classes", weight=3.0))

    if agents_seen:
        ids = [a.id for a in agents_seen]
        states = states_for(db, agents_seen)
        latest_outputs: dict[int, list[Output]] = {i: [] for i in ids}
        for output in newest_per(db, Output, [Output.agent_id, Output.category], [Output.agent_id.in_(ids)]):
            latest_outputs[output.agent_id].append(output)
        monitors: dict[int, list[Monitor]] = {i: [] for i in ids}
        for monitor in db.exec(select(Monitor).where(Monitor.agent_id.in_(ids))).all():
            monitors[monitor.agent_id].append(monitor)
        for agent in agents_seen:
            state = states[agent.id]
            values = [f"{o.category}: {short(o.value, 200)}" for o in latest_outputs[agent.id] if o.kind != "file"]
            values += [f"monitor {m.name}: {short(m.value, 200)}" for m in monitors[agent.id] if m.kind != "file"]
            found.append(Source("Agent", f"{agent.name} by {name(agent.owner_id)}",
                                f"State: {state.label}{' (' + state.why + ')' if state.why else ''}; {state.when}; "
                                f"checks in {state.rhythm or 'without a set interval'}.\n{agent.description or ''}\n"
                                + short("\n".join(values), 900), f"/agents/{agent.id}"))
        changes = db.exec(select(StateChange).where(StateChange.agent_id.in_(ids), StateChange.kind != "start",
                                                    StateChange.at >= now - timedelta(days=7))
                          .order_by(StateChange.at.desc()).limit(40)).all()
        by_id = {a.id: a for a in agents_seen}
        found.append(Source("Agent changes", "Agent changes in the last 7 days",
                            "\n".join(f"{jst(c.at)} {by_id[c.agent_id].name}: {c.message}" for c in changes)
                            or "No changes.", "/classes", weight=1.2))
        for row in forecasts.overview(db, agents_seen):
            this = row["this"]
            found.append(Source("Forecast", f"{this.target} by {name(row['agent'].owner_id)} ({row['agent'].name})",
                                f"{this.week_label}: {forecasts.number(this.value)} {this.unit} {this.view}"
                                + (f"; last week {forecasts.number(row['last'].value)}" if row["last"] else "")
                                + f". {short(this.why_changed, 500)}", f"/agents/{row['agent'].id}#forecast"))

    course_ids = [c.id for c in courses]
    first_class = {c.id: c for c in courses}
    for skill in db.exec(select(Skill).where(or_(Skill.course_id.in_(course_ids), Skill.share == "seminar",
                                                 Skill.builtin.is_not(None)))).all():
        home = skill.course_id if skill.course_id in first_class else (course_ids[0] if course_ids else None)
        found.append(Source("Skill", f"{skill.name} ({market.identifier(skill)})",
                            f"{skill.summary or ''}\n{short(skill.content, 500)}",
                            f"/classes/{home}?tab=skills&skill={skill.id}" if home else "/classes", weight=0.9))
    if viewer.is_member:  # OB/OG: names and where they work, never their email or phone
        for alum in db.exec(select(Alumnus)).all():
            found.append(Source("OB/OG", alum.name, f"Graduated {alum.year or '?'}; {alum.company or '?'} "
                                f"({alum.industry or '?'}). {alum.notes}", "/alumni"))
    return found


def best_sources(question: str, sources: list[Source]) -> list[Source]:
    """The items that share the most words with the question (deadlines always)."""
    wanted = tokens(question)
    briefing = BRIEFING in question.lower()

    def score(source: Source) -> float:
        if briefing and source.kind in ("Deadlines", "Agent changes"):
            return 100 * source.weight
        if briefing and source.kind == "Agent" and "State: Live" not in source.text:
            return 50
        return len(wanted & source.words()) * source.weight

    ranked = sorted(sources, key=score, reverse=True)
    picked = [s for s in ranked if score(s) > 0][:MAX_SOURCES]
    deadlines = next((s for s in sources if s.kind == "Deadlines"), None)
    if deadlines and deadlines not in picked:
        picked = picked[:MAX_SOURCES - 1] + [deadlines]
    return picked


# ---------------------------------------------------------------------------
# The shared daily budget
# ---------------------------------------------------------------------------


def daily_requests() -> int:
    try:
        return max(1, int(os.getenv("OPENROUTER_DAILY_REQUESTS") or DEFAULT_DAILY_REQUESTS))
    except ValueError:
        return DEFAULT_DAILY_REQUESTS


def today_start():
    return utc_now().astimezone(JST).replace(hour=0, minute=0, second=0, microsecond=0)


_key_cache: dict = {"at": 0.0, "data": None}
_key_lock = threading.Lock()


def key_info() -> dict | None:
    """OpenRouter's own figures for the key (credit used and left), cached for
    10 minutes; None if there's no key or it can't be read."""
    if not market.ai_key():
        return None
    with _key_lock:
        if time.time() - _key_cache["at"] < 600:
            return _key_cache["data"]
        data = None
        try:
            response = requests.get("https://openrouter.ai/api/v1/key", timeout=10,
                                    headers={"Authorization": f"Bearer {market.ai_key()}"})
            if response.ok:
                data = response.json().get("data")
        except (requests.RequestException, ValueError):
            data = None
        _key_cache.update(at=time.time(), data=data)
        return data


def budget(db: Session, viewer: Viewer | None = None) -> dict:
    """Today's (Japan time) OpenRouter requests for the whole seminar: what
    chat and the skill organizer used, what's kept for the organizer, and
    what's left for chat."""
    start = today_start()
    total = daily_requests()
    chat_used = db.exec(select(func.count(ChatMessage.id)).where(ChatMessage.used_ai.is_(True),
                                                                 ChatMessage.created_at >= start)).one()
    tokens_today = db.exec(select(func.coalesce(func.sum(ChatMessage.tokens), 0))
                           .where(ChatMessage.created_at >= start)).one()
    organizer_used = db.exec(select(func.count(OrganizerRun.id)).where(
        OrganizerRun.jst_date == str(start.date()), OrganizerRun.method == "ai")).one()
    reserved = max(0, market.MAX_AI_RUNS_PER_DAY - organizer_used)
    chat_left = max(0, total - chat_used - organizer_used - reserved)
    mine = 0
    if viewer is not None:
        mine = db.exec(select(func.count(ChatMessage.id)).where(
            ChatMessage.user_id == viewer.id, ChatMessage.role == "user", ChatMessage.created_at >= start)).one()
    return {"total": total, "chat_used": chat_used, "organizer_used": organizer_used, "reserved": reserved,
            "chat_left": chat_left, "tokens_today": tokens_today, "mine_left": max(0, QUESTIONS_PER_DAY - mine),
            "per_member": QUESTIONS_PER_DAY, "has_key": bool(market.ai_key()), "key": key_info()}


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------

_ask_lock = threading.Lock()


def prompt_for(question: str, sources: list[Source], history: list[ChatMessage]) -> list[dict]:
    listing = "\n\n".join(f"[{n}] {s.kind}: {s.title}\n{s.text}" for n, s in enumerate(sources, 1))
    system = ("You are the chat assistant of a university research seminar's dashboard (the Fukuhara Research "
              "Seminar). Answer members' questions using ONLY the numbered sources in the latest message and the "
              "conversation so far. Cite each fact with its source number in square brackets, like [2]; the page "
              "turns citations into buttons that open that item, so cite the item a member should open when they "
              "ask where something is. If the sources don't contain the answer, say so plainly and suggest where on "
              "the dashboard to look. Never invent contact details: for OB/OG contacts, point to the OB/OG page. "
              "Be brief (at most about 8 short lines or bullet points) and answer in the language of the question.")
    messages = [{"role": "system", "content": system}]
    for message in history[-HISTORY:]:
        messages.append({"role": message.role, "content": message.content[:2000]})
    messages.append({"role": "user", "content": f"SOURCES\n{listing}\n\nQUESTION\n{question}"})
    return messages


def linked(text: str, sources: list[dict]) -> Markup:
    """An answer as safe HTML: escaped, with [n] linking to source n."""
    escaped = html.escape(text.strip())

    def cite(match: re.Match) -> str:
        n = int(match.group(1))
        if 1 <= n <= len(sources):
            return f'<a href="{html.escape(sources[n - 1]["url"])}" class="ask-cite">[{n}]</a>'
        return match.group(0)

    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)  # the model's **bold**
    return Markup(re.sub(r"\[(\d{1,2})\]", cite, escaped))


def cited(text: str, sources: list[dict]) -> list[tuple[int, dict]]:
    """The sources an answer cites, in order (all of them if it cites none)."""
    numbers = []
    for match in re.finditer(r"\[(\d{1,2})\]", text or ""):
        n = int(match.group(1))
        if 1 <= n <= len(sources) and n not in numbers:
            numbers.append(n)
    return [(n, sources[n - 1]) for n in numbers] if numbers else list(enumerate(sources, 1))[:6]


def ask(db: Session, viewer: Viewer, question: str) -> ChatMessage:
    """Answer one question (saving both messages) and return the answer."""
    question = " ".join(question.split())[:1000]
    history = list(db.exec(select(ChatMessage).where(ChatMessage.user_id == viewer.id)
                           .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(HISTORY)).all())[::-1]
    earlier = " ".join(m.content for m in history if m.role == "user")[-300:]
    sources = best_sources(f"{question} {earlier}", sources_for(db, viewer))
    refs = [s.ref() for s in sources]
    db.add(ChatMessage(user_id=viewer.id, role="user", content=question))
    db.commit()
    answer = ChatMessage(user_id=viewer.id, role="assistant", content="", sources=refs)
    with _ask_lock:
        left = budget(db, viewer)
        if not left["has_key"]:
            answer.note = "No AI model is set up (OPENROUTER_API_KEY), so here's what matches on the dashboard."
        elif left["mine_left"] <= 0:
            answer.note = f"You've asked {QUESTIONS_PER_DAY} questions today, the most per member; here's what matches."
        elif left["chat_left"] <= 0:
            answer.note = "The seminar's AI budget for chat is used up today; here's what matches on the dashboard."
        else:
            answer.used_ai = True
            db.add(answer)
            db.commit()  # counts against the budget straight away
    if answer.used_ai:
        try:
            response = requests.post(
                market.OPENROUTER_URL,
                headers={"Authorization": f"Bearer {market.ai_key()}", "Content-Type": "application/json",
                         "X-Title": "Fukuhara Research Seminar Dashboard"},
                json={"model": market.model_name(), "temperature": 0.2,
                      "messages": prompt_for(question, sources, history)},
                timeout=TIMEOUT)
            response.raise_for_status()
            data = response.json()
            answer.content = data["choices"][0]["message"]["content"] or ""
            answer.tokens = int((data.get("usage") or {}).get("total_tokens") or 0)
        except (requests.RequestException, KeyError, IndexError, ValueError, TypeError):
            answer.note = "The AI model didn't answer just now; here's what matches on the dashboard."
    db.add(answer)
    db.commit()
    db.refresh(answer)
    return answer
