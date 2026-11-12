"""The seminar chat page (assistant.py): a conversation per member, with the
seminar's shared daily AI budget beside it. Members only."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, delete, select

import assistant
from db import ChatMessage, get_session
from security import Viewer, checked_member, require_member
from web import render, render_text, templates

router = APIRouter()
SHOWN = 100  # the latest messages shown on the page

templates.env.globals["chat_linked"] = assistant.linked
templates.env.globals["chat_cited"] = assistant.cited


@router.get("/chat")
def chat_page(request: Request, viewer: Viewer = Depends(require_member), db: Session = Depends(get_session)):
    messages = list(db.exec(select(ChatMessage).where(ChatMessage.user_id == viewer.id, ChatMessage.note != "cleared")
                            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(SHOWN)).all())[::-1]
    return render(request, db, viewer, "chat.html", messages=messages, budget=assistant.budget(db, viewer),
                  suggestions=assistant.SUGGESTIONS)


@router.post("/chat")
def send(question: str = Form(""), viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    """One question: replies with the two new messages (added to the end of the
    conversation) and the budget panel, updated in place."""
    if not question.strip():
        return HTMLResponse("")
    answer = assistant.ask(db, viewer, question)
    asked = db.exec(select(ChatMessage).where(ChatMessage.user_id == viewer.id, ChatMessage.role == "user")
                    .order_by(ChatMessage.id.desc()).limit(1)).one()
    html = render_text(db, viewer, "partials/chat_messages.html", messages=[asked, answer])
    html += render_text(db, viewer, "partials/chat_budget.html", budget=assistant.budget(db, viewer), oob=True)
    return HTMLResponse(html)


@router.post("/chat/clear")
def clear(viewer: Viewer = Depends(checked_member), db: Session = Depends(get_session)):
    """Forget this member's conversation (the budget counts stay: they were used)."""
    db.exec(delete(ChatMessage).where(ChatMessage.user_id == viewer.id, ChatMessage.used_ai.is_(False)))
    for message in db.exec(select(ChatMessage).where(ChatMessage.user_id == viewer.id)).all():
        message.content, message.sources, message.note = "", None, "cleared"  # kept only to count today's budget
        db.add(message)
    db.commit()
    return RedirectResponse("/chat", status_code=303)
