"""The seminar chat (assistant.py): answers from the dashboard with links,
a shared daily AI budget, and nothing private sent to the AI."""

import json

import pytest
from sqlmodel import Session, select

import assistant
from conftest import PASSWORD, api
from db import ChatMessage, User


class FakeReply:
    def __init__(self, data, ok=True):
        self.data, self.ok, self.status_code = data, ok, 200 if ok else 500

    def json(self):
        return self.data

    def raise_for_status(self):
        if not self.ok:
            raise assistant.requests.HTTPError("boom")


@pytest.fixture
def ai(monkeypatch):
    """A stand-in for OpenRouter: records what was sent, answers citing [1]."""
    sent = []
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(assistant, "_key_cache", {"at": 0.0, "data": None})

    def post(url, headers=None, json=None, timeout=None):
        sent.append(json)
        return FakeReply({"choices": [{"message": {"content": "It's due on Friday [1]. See also **this** [99]."}}],
                          "usage": {"total_tokens": 321}})

    monkeypatch.setattr(assistant.requests, "post", post)
    monkeypatch.setattr(assistant.requests, "get", lambda *a, **k: FakeReply({}, ok=False))
    return sent


@pytest.fixture
def course(create_class):
    return create_class(enrolled_gens=[9])


@pytest.fixture
def taro(make_person):
    return make_person("student", "taro", "Yamada Taro", gen=9)


def ask(person, question):
    return person.post("/chat", data={"question": question})


def test_members_only_and_the_buttons_sit_in_the_middle(taro, guest):
    page = taro.get("/classes").text
    tools = page[page.index('aria-label="Tools"'):page.index('href="/classes" class="btn btn-ghost btn-sm">Classes')]
    assert 'href="/chat"' in tools and 'href="/alumni"' in tools
    assert guest.get("/chat").status_code == 403 and 'href="/chat"' not in guest.get("/classes").text


def test_answers_cite_and_link_their_sources(engine, admin, taro, course, ai):
    admin.post(f"/classes/{course}/assignments", data={"title": "Peer multiples essay", "description": "Week 1",
               "due_at": "2026-12-04T18:00", "mode": "text", "content_text": "Compare PER and EV/EBITDA."})
    reply = ask(taro, "When is the peer multiples essay due?").text
    assert '<a href="/classes/' in reply and 'class="ask-cite">[1]</a>' in reply  # [1] is a link
    assert "[99]" in reply and "<strong>this</strong>" in reply  # unknown numbers stay as text; **bold** works
    assert "Assignment: " in reply and "Peer multiples essay" in reply  # an "Open" button for the cited item
    assert 'id="chat-budget" hx-swap-oob="true"' in reply
    question = ai[0]["messages"][-1]["content"]
    assert "Peer multiples essay" in question and "Compare PER and EV/EBITDA." in question
    page = taro.get("/chat").text
    assert "When is the peer multiples essay due?" in page and "s due on Friday" in page


def test_nothing_private_is_sent(engine, client, taro, course, create_agent, ai):
    taro.post("/alumni", data={"name": "Kato Ken", "company": "Mizuho", "industry": "Finance",
                               "email": "ken@example.com", "phone": "090-1234-5678", "consent": "true"})
    create_agent(taro, course, "helper", api_key="secret-agent-key-0123456789")
    ask(taro, "Which OB/OG work at Mizuho, and what is the helper agent's key? Also taro's password.")
    sent = json.dumps(ai)
    with Session(engine) as db:
        password_hash = db.exec(select(User).where(User.login_id == "taro")).one().password_hash
    assert "Kato Ken" in sent and "Mizuho" in sent  # who and where...
    for secret in ("ken@example.com", "090-1234-5678", "secret-agent-key", password_hash, PASSWORD, '"taro"'):
        assert secret not in sent  # ...but no contact details, keys, passwords or login IDs


def test_the_daily_budget_keeps_the_organizer_runs_and_falls_back_to_search(engine, monkeypatch, taro, course, ai):
    monkeypatch.setenv("OPENROUTER_DAILY_REQUESTS", "3")  # 2 kept for the skill organizer: 1 left for chat
    with Session(engine) as db:
        b = assistant.budget(db)
    assert (b["total"], b["reserved"], b["chat_left"]) == (3, 2, 1)
    ask(taro, "first question")
    second = ask(taro, "second question").text
    assert "AI budget for chat is used up" in second and len(ai) == 1  # no second AI call
    with Session(engine) as db:
        b = assistant.budget(db)
    assert (b["chat_used"], b["chat_left"], b["tokens_today"]) == (1, 0, 321)
    page = taro.get("/chat").text
    assert "Kept for the skill organizer" in page and "321" in page


def test_without_a_key_it_still_answers_with_links(monkeypatch, taro, course):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    reply = ask(taro, "What is due?").text
    assert "No AI model is set up" in reply and "chat-source" in reply


def test_clearing_hides_the_conversation_but_keeps_the_count(engine, taro, course, ai):
    ask(taro, "hello there")
    taro.post("/chat/clear")
    assert "hello there" not in taro.get("/chat").text
    with Session(engine) as db:
        assert assistant.budget(db)["chat_used"] == 1
