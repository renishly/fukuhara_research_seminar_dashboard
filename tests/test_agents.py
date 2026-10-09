"""Agent dashboards, the agent API, and the connector."""

import html
import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from requests.structures import CaseInsensitiveDict
from sqlmodel import Session, select

from conftest import PASSWORD, api, key_for
from db import Agent, Command, Event, InboxFile, Monitor, Output, StoredFile, utc_now
from routes.agent_tools import is_online
from security import decrypt_api_key, hash_api_key


@pytest.fixture
def course(create_class):
    return create_class(enrolled_gens=[9])


@pytest.fixture
def taro(make_person):
    return make_person("student", "taro", "Yamada Taro", gen=9)


@pytest.fixture
def hanako(make_person):
    return make_person("student", "hanako", "Sato Hanako", gen=9)


def agent_row(engine, agent_id) -> Agent:
    with Session(engine) as db:
        return db.get(Agent, agent_id)


def rows(engine, model, **where):
    with Session(engine) as db:
        statement = select(model)
        for name, value in where.items():
            statement = statement.where(getattr(model, name) == value)
        return db.exec(statement).all()


def opt_in(client, name, **state):
    return client.post("/api/state", json=state, headers=api(name))


# --- Who sees and does what on the dashboard -------------------------------------------------


def test_only_the_owner_gets_add_agent_controls_settings_and_inbox(admin, taro, hanako, guest, course, create_agent):
    agent = create_agent(taro, course, "helper")
    grid = f"/classes/{course}/agents/{taro.id}"
    own = taro.get(grid).text
    assert "open-add-agent" in own and "/connector/code" in own and "+ Add folder" in own
    for viewer in (hanako, admin):
        page = viewer.get(grid).text
        assert "open-add-agent" not in page and "/connector/code" not in page and "+ Add folder" not in page
        assert "read-only" in page
        agent_page = viewer.get(f"/agents/{agent}").text
        assert "Agent settings" not in agent_page and 'aria-label="Start"' not in agent_page and "Inbox" not in agent_page
    own_page = taro.get(f"/agents/{agent}").text
    assert "Agent settings" in own_page and 'aria-label="Start"' in own_page and "Inbox" in own_page


def test_owner_only_routes_refuse_everyone_else(engine, client, admin, taro, hanako, course, create_agent):
    agent = create_agent(taro, course, "helper")
    opt_in(client, "helper", accepts_inbox=True)
    taro.post(f"/agents/{agent}/inbox", files={"files": ("a.txt", b"hi", "text/plain")})
    item = rows(engine, InboxFile)[0].id
    attempts = [
        ("post", f"/agents/{agent}/command", {"command": "start"}),
        ("post", f"/agents/{agent}/rename", {"name": "stolen"}),
        ("post", f"/agents/{agent}/description", {"description": "x"}),
        ("post", f"/agents/{agent}/monitors/link", {"name": "Sheet", "url": "https://example.com"}),
        ("post", f"/agents/{agent}/monitors/pin", {"name": "x"}),
        ("post", f"/agents/{agent}/outputs/pin", {"name": "x"}),
        ("post", f"/agents/{agent}/key/view", {"password": PASSWORD}),
        ("post", f"/agents/{agent}/key/replace", {"password": PASSWORD}),
        ("post", f"/agents/{agent}/delete", {"password": PASSWORD}),
        ("get", f"/agents/{agent}/inbox", None),
        ("get", f"/inbox/{item}/file", None),
        ("post", f"/inbox/{item}/delete", {}),
    ]
    for viewer in (hanako, admin):
        for method, url, data in attempts:
            response = viewer.post(url, data=data) if method == "post" else viewer.get(url)
            assert response.status_code == 403, (url, response.status_code)
    assert agent_row(engine, agent).name == "helper" and not agent_row(engine, agent).paused
    assert rows(engine, Command) == []
    # Only enrolled students add agents, and only on their own dashboard.
    assert admin.post(f"/classes/{course}/agents", data={"name": "admin-bot"}).status_code == 403


def test_members_can_read_other_agents_but_guests_only_see_the_grid(taro, hanako, guest, course, create_agent, client):
    agent = create_agent(taro, course, "helper", description="Finds papers for Yamada Taro.")
    client.post("/api/outputs", json={"category": "papers", "content": "A paper"}, headers=api("helper"))
    assert hanako.get(f"/agents/{agent}").status_code == 200
    assert "A paper" in hanako.get(f"/agents/{agent}/outputs", params={"all": "true"}).text
    assert guest.get(f"/agents/{agent}").status_code == 403
    assert guest.get(f"/agents/{agent}/outputs", params={"all": "true"}).status_code == 403
    assert guest.get(f"/agents/{agent}/events").status_code == 403
    assert guest.get(f"/agents/{agent}/monitors").status_code == 403
    grid = guest.get(f"/classes/{course}/agents/{taro.id}").text
    assert "helper" in grid and "Finds papers for" in grid and "online" in grid
    assert "Yamada Taro" not in grid  # names hidden from guests, even in descriptions
    assert f'href="/agents/{agent}"' not in grid  # no links to agent pages


def test_non_owners_cannot_post_controls_as_guests(guest, taro, course, create_agent):
    agent = create_agent(taro, course, "helper")
    assert guest.post(f"/agents/{agent}/command", data={"command": "start"}).status_code == 403
    assert guest.post(f"/agents/{agent}/outputs/notify", data={"category": "x"}).status_code == 403


def test_agents_tab_shows_last_connection(client, admin, taro, hanako, course, create_agent):
    create_agent(taro, course, "helper")
    window = admin.get(f"/classes/{course}/window/agents").text
    assert window.count("Never connected") == 2
    opt_in(client, "helper", check_in_every=60)
    window = admin.get(f"/classes/{course}/window/agents").text
    assert "Last connected" in window and "JST" in window and "online" in window
    assert window.count("Never connected") == 1


# --- Adding agents and keys -----------------------------------------------------------------


def test_blank_key_generates_one_and_typed_keys_follow_the_rules(engine, client, taro, course):
    response = taro.post(f"/classes/{course}/agents", data={"name": "auto", "api_key": ""})
    assert "Agent added" in response.text
    agent = rows(engine, Agent, name="auto")[0]
    generated = decrypt_api_key(agent.api_key_encrypted)
    assert len(generated) == 64 and generated in response.text
    assert client.post("/api/state", json={}, headers={"X-API-Key": generated}).status_code == 200
    short = taro.post(f"/classes/{course}/agents", data={"name": "short", "api_key": "too-short"})
    spaces = taro.post(f"/classes/{course}/agents", data={"name": "spaces", "api_key": "has some spaces in it ok"})
    for response in (short, spaces):
        assert "at least 20 characters with no spaces" in response.text
    duplicate = taro.post(f"/classes/{course}/agents", data={"name": "dup", "api_key": generated})
    assert "already used" in duplicate.text
    assert taro.post(f"/classes/{course}/agents", data={"name": "auto"}).text.count("already have an agent") == 1


def test_key_is_stored_hashed_and_encrypted_and_can_be_viewed(engine, taro, course, create_agent):
    agent_id = create_agent(taro, course, "helper")
    agent = agent_row(engine, agent_id)
    assert agent.api_key_hash == hash_api_key(key_for("helper"))
    assert key_for("helper") not in agent.api_key_encrypted
    assert "incorrect" in taro.post(f"/agents/{agent_id}/key/view", data={"password": "wrong"}).text
    shown = taro.post(f"/agents/{agent_id}/key/view", data={"password": PASSWORD})
    assert key_for("helper") in shown.text and shown.headers["cache-control"] == "no-store"


def test_replace_key_stops_the_old_one(client, taro, course, create_agent):
    agent_id = create_agent(taro, course, "helper")
    reply = taro.post(f"/agents/{agent_id}/key/replace", data={"password": PASSWORD, "new_key": "a-brand-new-key-0123456789"})
    assert "Key replaced" in reply.text
    assert client.post("/api/state", json={}, headers=api("helper")).status_code == 401
    assert client.post("/api/state", json={}, headers={"X-API-Key": "a-brand-new-key-0123456789"}).status_code == 200


def test_renaming_keeps_the_key(engine, client, taro, course, create_agent):
    agent_id = create_agent(taro, course, "helper")
    create_agent(taro, course, "other")
    assert "already have an agent" in html.unescape(taro.post(f"/agents/{agent_id}/rename", data={"name": "other"}).text)
    taro.post(f"/agents/{agent_id}/rename", data={"name": "renamed"})
    assert agent_row(engine, agent_id).name == "renamed"
    assert client.post("/api/state", json={}, headers=api("helper")).status_code == 200


def test_names_are_unique_per_owner_per_class(taro, hanako, course, create_agent, create_class):
    create_agent(taro, course, "bot", api_key=key_for("bot-1"))
    create_agent(hanako, course, "bot", api_key=key_for("bot-2"))  # another student may use the same name
    create_agent(taro, create_class(title="Other", enrolled_gens=[9]), "bot", api_key=key_for("bot-3"))  # and another class


def test_delete_agent_needs_the_password_and_deletes_everything(engine, client, taro, course, create_agent):
    agent_id = create_agent(taro, course, "helper")
    client.post("/api/outputs/file", data={"category": "files"}, files={"file": ("a.txt", b"x", "text/plain")},
                headers=api("helper"))
    client.post("/api/events", json={"message": "hi"}, headers=api("helper"))
    assert "incorrect" in taro.post(f"/agents/{agent_id}/delete", data={"password": "wrong"}).text
    assert agent_row(engine, agent_id) is not None
    response = taro.post(f"/agents/{agent_id}/delete", data={"password": PASSWORD})
    assert response.headers["HX-Redirect"].startswith(f"/classes/{course}/agents/{taro.id}")
    assert agent_row(engine, agent_id) is None
    assert rows(engine, Output) == [] and rows(engine, Event) == [] and rows(engine, StoredFile) == []


# --- The agent API ------------------------------------------------------------------------


def test_unknown_empty_or_missing_keys_get_401(client):
    for headers in ({"X-API-Key": "not-a-real-key-0123456789"}, {"X-API-Key": ""}, {}):
        assert client.post("/api/events", json={"message": "x"}, headers=headers).status_code == 401
        assert client.get("/api/agents", headers=headers).status_code == 401


def test_check_in_every_limits(client, taro, course, create_agent):
    create_agent(taro, course, "helper")
    assert opt_in(client, "helper", check_in_every=0).status_code == 422
    assert opt_in(client, "helper", check_in_every=7 * 24 * 3600 + 1).status_code == 422
    reply = opt_in(client, "helper", check_in_every=3600).json()
    assert reply["check_in_every"] == 3600 and reply["paused"] is False


def test_online_status_uses_the_interval_plus_slack(engine, client, taro, course, create_agent):
    agent = agent_row(engine, create_agent(taro, course, "helper"))
    assert not is_online(agent)  # never connected
    now = utc_now()
    agent.last_seen_at = now - timedelta(seconds=85)
    assert is_online(agent, now)  # default 60 s + 30 s slack
    agent.last_seen_at = now - timedelta(seconds=95)
    assert not is_online(agent, now)
    agent.check_in_seconds = 3600
    agent.last_seen_at = now - timedelta(minutes=89)
    assert is_online(agent, now)  # an hourly agent: + half an hour
    agent.last_seen_at = now - timedelta(minutes=91)
    assert not is_online(agent, now)
    agent.check_in_seconds = 5
    agent.last_seen_at = now - timedelta(seconds=34)
    assert is_online(agent, now)  # at least 30 s slack


def test_late_items_land_at_their_real_time(engine, client, taro, course, create_agent):
    create_agent(taro, course, "helper")
    made = datetime.now(timezone.utc) - timedelta(minutes=10)
    client.post("/api/outputs", json={"category": "news", "content": "Old news", "created_at": made.isoformat()},
                headers=api("helper"))
    future = datetime.now(timezone.utc) + timedelta(days=1)
    client.post("/api/events", json={"message": "From the future", "created_at": future.isoformat()}, headers=api("helper"))
    output = rows(engine, Output)[0]
    assert abs(output.created_at.replace(tzinfo=timezone.utc) - made) < timedelta(seconds=1)
    lines = {e.message: e for e in rows(engine, Event)}
    assert any("(delivered late)" in message for message in lines)
    assert lines["From the future"].created_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)
    bad = client.post("/api/events", json={"message": "x", "created_at": "yesterday"}, headers=api("helper"))
    assert bad.status_code == 422


def test_late_monitor_update_older_than_the_current_one_is_skipped(client, taro, course, create_agent, engine):
    create_agent(taro, course, "helper")
    client.put("/api/monitors", json={"name": "Sheet", "content": "https://example.com/new"}, headers=api("helper"))
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    reply = client.put("/api/monitors", json={"name": "Sheet", "content": "https://example.com/old", "created_at": old},
                       headers=api("helper"))
    assert reply.json()["applied"] is False
    assert rows(engine, Monitor)[0].value == "https://example.com/new"


def test_commands_start_pause_and_messages(engine, client, taro, course, create_agent):
    agent_id = create_agent(taro, course, "helper")
    taro.post(f"/agents/{agent_id}/command", data={"command": "pause"})
    assert opt_in(client, "helper").json()["paused"] is True
    refused = taro.post(f"/agents/{agent_id}/command", data={"command": "message", "text": "hello"})
    assert refused.status_code == 409 and "doesn't accept messages" in html.unescape(refused.text)
    opt_in(client, "helper", accepts_messages=True)
    assert taro.post(f"/agents/{agent_id}/command", data={"command": "message", "text": "hello"}).status_code == 200
    commands = client.get("/api/commands", headers=api("helper")).json()
    assert [c["command"] for c in commands] == ["pause", "message"]
    assert commands[1]["payload"] == {"text": "hello"}
    done = client.patch(f"/api/commands/{commands[0]['id']}", json={"status": "done"}, headers=api("helper"))
    assert done.status_code == 200
    assert [c["command"] for c in client.get("/api/commands", headers=api("helper")).json()] == ["message"]


def test_agents_only_see_and_update_their_own_commands(client, taro, hanako, course, create_agent):
    a = create_agent(taro, course, "a-agent")
    create_agent(hanako, course, "b-agent")
    taro.post(f"/agents/{a}/command", data={"command": "start"})
    assert client.get("/api/commands", headers=api("b-agent")).json() == []
    command = client.get("/api/commands", headers=api("a-agent")).json()[0]
    assert client.patch(f"/api/commands/{command['id']}", json={"status": "done"}, headers=api("b-agent")).status_code == 404


def test_messages_ready_check_reloads_the_page_when_it_changes(client, taro, course, create_agent):
    agent_id = create_agent(taro, course, "helper")
    assert taro.get(f"/agents/{agent_id}/messages-ready?ready=false").status_code == 204
    opt_in(client, "helper", accepts_messages=True)
    assert taro.get(f"/agents/{agent_id}/messages-ready?ready=false").headers.get("HX-Refresh") == "true"


def test_inbox_is_refused_until_opted_in_and_confirming_deletes_the_file(engine, client, taro, course, create_agent):
    agent_id = create_agent(taro, course, "helper")
    refused = taro.post(f"/agents/{agent_id}/inbox", files={"files": ("data.csv", b"a,b\n1,2", "text/csv")})
    assert refused.status_code == 409 and rows(engine, InboxFile) == []
    assert opt_in(client, "helper", accepts_inbox=True).json()["inbox_waiting"] == 0
    upload = taro.post(f"/agents/{agent_id}/inbox", files={"files": ("data.csv", b"a,b\n1,2", "text/csv")},
                       data={"note": "For you"})
    assert "Uploaded data.csv" in upload.text
    assert opt_in(client, "helper").json()["inbox_waiting"] == 1
    items = client.get("/api/inbox", headers=api("helper")).json()
    assert items[0]["filename"] == "data.csv" and items[0]["note"] == "For you"
    file = client.get(items[0]["file_url"], headers=api("helper"))
    assert file.content == b"a,b\n1,2"
    stored = rows(engine, StoredFile)[0]
    path = Path(os.environ["LOCAL_FILES_DIR"]) / stored.storage_path
    assert client.post(f"/api/inbox/{items[0]['id']}/received", headers=api("helper")).json() == {"deleted": 1}
    assert rows(engine, InboxFile) == [] and rows(engine, StoredFile) == [] and not path.exists()
    assert any("agent received data.csv" in e.message for e in rows(engine, Event))


def test_inbox_files_are_only_for_that_agent(engine, client, taro, hanako, course, create_agent):
    a = create_agent(taro, course, "a-agent")
    create_agent(hanako, course, "b-agent")
    opt_in(client, "a-agent", accepts_inbox=True)
    taro.post(f"/agents/{a}/inbox", files={"files": ("secret.txt", b"s", "text/plain")})
    item = rows(engine, InboxFile)[0]
    assert client.get("/api/inbox", headers=api("b-agent")).json() == []
    assert client.get(f"/api/inbox/{item.id}/file", headers=api("b-agent")).status_code == 404
    assert client.post(f"/api/inbox/{item.id}/received", headers=api("b-agent")).status_code == 404
    assert hanako.get(f"/files/{item.file_id}").status_code == 404
    assert taro.get(f"/inbox/{item.id}/file").status_code == 200


def test_output_kinds_and_file_limits(engine, client, taro, course, create_agent, monkeypatch):
    create_agent(taro, course, "helper")
    for content in ("text", 12, "https://example.com", {"a": 1}):
        assert client.post("/api/outputs", json={"category": "c", "content": content}, headers=api("helper")).status_code == 201
    assert {o.kind for o in rows(engine, Output)} == {"text", "number", "link", "json"}
    assert client.post("/api/outputs", json={"category": "", "content": "x"}, headers=api("helper")).status_code == 422
    assert client.post("/api/outputs", json={"category": "c", "content": "x" * 20_001}, headers=api("helper")).status_code == 422
    import routes.agent_api
    monkeypatch.setattr(routes.agent_api, "AGENT_FILE_LIMIT", 5)
    too_big = client.post("/api/outputs/file", data={"category": "c"}, files={"file": ("a.bin", b"123456", "x/y")},
                          headers=api("helper"))
    assert too_big.status_code == 413
    not_pdf = client.post("/api/monitor/file", files={"file": ("a.pdf", b"nope", "application/pdf")}, headers=api("helper"))
    assert not_pdf.status_code == 422


def test_monitor_pdf_is_shown_inline_and_replaced(engine, client, taro, course, create_agent):
    agent_id = create_agent(taro, course, "helper")
    send = {"name": "Summary"}
    client.post("/api/monitors/file", data=send, files={"file": ("one.pdf", b"%PDF-1.4 one", "application/pdf")},
                headers=api("helper"))
    first = rows(engine, StoredFile)[0]
    monitor = rows(engine, Monitor)[0]
    assert monitor.kind == "pdf"
    pdf = taro.get(f"/agents/{agent_id}/monitors/{monitor.id}/view")
    assert pdf.headers["content-type"] == "application/pdf" and pdf.headers["content-disposition"].startswith("inline")
    client.post("/api/monitors/file", data=send, files={"file": ("two.pdf", b"%PDF-1.4 two", "application/pdf")},
                headers=api("helper"))
    assert [f.filename for f in rows(engine, StoredFile)] == ["two.pdf"]  # replaced, and the old file deleted
    assert not (Path(os.environ["LOCAL_FILES_DIR"]) / first.storage_path).exists()
    page = taro.get(f"/agents/{agent_id}/monitors", params={"c": "Summary"}).text
    assert "two.pdf" in page and 'hx-preserve="true"' in page
    # A file that only claims to be a PDF is kept as a plain download, never shown inline.
    client.post("/api/monitors/file", data={"name": "Fake"}, files={"file": ("x.pdf", b"<html>", "application/pdf")},
                headers=api("helper"))
    fake = next(m for m in rows(engine, Monitor) if m.name == "Fake")
    assert fake.kind == "file" and taro.get(f"/agents/{agent_id}/monitors/{fake.id}/view").status_code == 404


# --- Reading and deleting through the API ---------------------------------------------------


def test_reading_by_me_id_and_name_within_the_class(client, taro, hanako, course, create_agent, create_class):
    a = create_agent(taro, course, "reader")
    b = create_agent(hanako, course, "writer", description="Writes summaries")
    elsewhere = create_agent(taro, create_class(title="Other", enrolled_gens=[9]), "far-away")
    client.post("/api/outputs", json={"category": "notes", "content": "Hi"}, headers=api("writer"))
    listing = client.get("/api/agents", headers=api("reader")).json()
    assert [agent["name"] for agent in listing] == ["reader", "writer"]  # not the other class's agent
    assert listing[1]["owner"] == {"full_name": "Sato Hanako", "gen": 9}
    assert all("api_key" not in str(agent) and "login" not in str(agent) for agent in listing)
    assert client.get("/api/agents/me", headers=api("reader")).json()["id"] == a
    assert client.get(f"/api/agents/{b}", headers=api("reader")).json()["description"] == "Writes summaries"
    by_name = client.get("/api/agents/writer", headers=api("reader")).json()
    assert by_name["categories"][0]["name"] == "notes"
    outputs = client.get("/api/agents/writer/outputs", headers=api("reader")).json()
    assert outputs[0]["content"] == "Hi"
    assert client.get(f"/api/agents/{elsewhere}", headers=api("reader")).status_code == 404
    assert client.get("/api/agents/far-away/outputs", headers=api("reader")).status_code == 404


def test_shared_name_in_a_class_gets_409_with_ids(client, taro, hanako, course, create_agent):
    first = create_agent(taro, course, "bot", api_key=key_for("taro-bot"))
    second = create_agent(hanako, course, "bot", api_key=key_for("hanako-bot"))
    reply = client.get("/api/agents/bot", headers=api("taro-bot"))
    assert reply.status_code == 409 and reply.json()["detail"]["ids"] == sorted([first, second])


def test_reading_outputs_and_activity_with_filters(client, taro, course, create_agent):
    create_agent(taro, course, "helper")
    early = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    client.post("/api/outputs", json={"category": "a", "content": "early", "created_at": early}, headers=api("helper"))
    client.post("/api/outputs", json={"category": "b", "content": "now"}, headers=api("helper"))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    after = client.get("/api/agents/me/outputs", params={"after": cutoff}, headers=api("helper")).json()
    before = client.get("/api/agents/me/outputs", params={"before": cutoff}, headers=api("helper")).json()
    assert [o["content"] for o in after] == ["now"] and [o["content"] for o in before] == ["early"]
    assert [o["content"] for o in client.get("/api/agents/me/outputs", params={"category": "a"}, headers=api("helper")).json()] == ["early"]
    assert client.get("/api/agents/me/outputs", params={"limit": 501}, headers=api("helper")).status_code == 422
    events = client.get("/api/agents/me/events", params={"type": "output"}, headers=api("helper")).json()
    assert len(events) == 2


def test_file_outputs_can_be_downloaded_by_agents_in_the_class(client, taro, hanako, course, create_agent):
    create_agent(taro, course, "maker")
    create_agent(hanako, course, "reader")
    made = client.post("/api/outputs/file", data={"category": "files"}, files={"file": ("r.txt", b"report", "text/plain")},
                       headers=api("maker")).json()
    url = made["file"]["url"]
    reply = client.get(url, headers=api("reader"))
    assert reply.content == b"report" and reply.headers["content-disposition"].startswith("attachment")


def test_agents_delete_only_their_own_data(engine, client, taro, hanako, course, create_agent):
    create_agent(taro, course, "a-agent")
    create_agent(hanako, course, "b-agent")
    output = client.post("/api/outputs", json={"category": "c", "content": "mine"}, headers=api("a-agent")).json()
    event = client.post("/api/events", json={"message": "line"}, headers=api("a-agent")).json()
    assert client.delete(f"/api/outputs/{output['id']}", headers=api("b-agent")).status_code == 403
    assert client.delete(f"/api/events/{event['id']}", headers=api("b-agent")).status_code == 403
    assert len(rows(engine, Output)) == 1
    assert client.delete("/api/outputs", headers=api("a-agent")).status_code == 422  # needs a filter or all=true
    assert client.delete(f"/api/outputs/{output['id']}", headers=api("a-agent")).json() == {"deleted": 1}
    assert client.delete(f"/api/events/{event['id']}", headers=api("a-agent")).json() == {"deleted": 1}
    messages = [e.message for e in rows(engine, Event) if e.type == "delete"]
    assert any("Deleted 1 output" in m for m in messages) and any("Deleted 1 activity line" in m for m in messages)
    client.post("/api/outputs", json={"category": "x", "content": "1"}, headers=api("a-agent"))
    client.post("/api/outputs", json={"category": "y", "content": "2"}, headers=api("a-agent"))
    assert client.delete("/api/outputs", params={"category": "x"}, headers=api("a-agent")).json() == {"deleted": 1}
    assert client.delete("/api/outputs", params={"all": "true"}, headers=api("a-agent")).json() == {"deleted": 1}
    client.put("/api/monitors", json={"name": "m", "content": 1}, headers=api("a-agent"))
    assert client.delete("/api/monitors", params={"name": "m"}, headers=api("b-agent")).json() == {"deleted": 0}
    assert client.delete("/api/monitors", params={"name": "m"}, headers=api("a-agent")).json() == {"deleted": 1}


def test_reads_never_include_commands_or_inbox(client, taro, course, create_agent):
    a = create_agent(taro, course, "helper")
    taro.post(f"/agents/{a}/command", data={"command": "start"})
    everything = str(client.get("/api/agents/me", headers=api("helper")).json())
    everything += str(client.get("/api/agents/me/events", headers=api("helper")).json())
    assert "start" not in everything.lower().replace("started", "")


# --- Outputs filter, pins and bells ---------------------------------------------------------


def test_outputs_filter_pin_and_bell(engine, client, taro, hanako, course, create_agent):
    agent_id = create_agent(taro, course, "helper")
    client.post("/api/outputs", json={"category": "news", "content": "Headline"}, headers=api("helper"))
    client.post("/api/outputs", json={"category": "stats", "content": 7}, headers=api("helper"))
    first_visit = taro.get(f"/agents/{agent_id}").text
    assert "2 outputs available" in first_visit and "Headline" not in first_visit
    chosen = taro.get(f"/agents/{agent_id}/outputs", params={"c": "news"}).text
    assert "Headline" in chosen and ">7<" not in chosen
    taro.post(f"/agents/{agent_id}/outputs/pin", data={"name": "news"})
    assert agent_row(engine, agent_id).pinned_category == "news"
    assert "Headline" in taro.get(f"/agents/{agent_id}").text  # a pinned category starts ticked
    assert "Headline" in hanako.get(f"/classes/{course}/agents/{taro.id}").text  # and shows on the card
    hanako.post(f"/agents/{agent_id}/outputs/notify", data={"name": "news"})  # bells are per viewer
    assert "Notifications on" in hanako.get(f"/agents/{agent_id}/outputs", params={"c": "news", "known": "x"}).text
    assert "Notifications on" not in taro.get(f"/agents/{agent_id}/outputs", params={"c": "news", "known": "x"}).text


# --- Monitors: named datapoints, replaced by each new value --------------------------------


def put_monitor(client, name, content, agent="helper", **extra):
    return client.put("/api/monitors", json={"name": name, "content": content, **extra}, headers=api(agent))


def test_an_agent_has_many_monitors_each_replaced_not_added(engine, client, taro, course, create_agent):
    create_agent(taro, course, "helper")
    put_monitor(client, "Temperature", 21.5)
    put_monitor(client, "Status", "Collecting papers")
    put_monitor(client, "Sheet", "https://example.com/sheet")
    put_monitor(client, "Counts", {"papers": 12})
    put_monitor(client, "Temperature", 22.0)  # replaces, doesn't add
    monitors = {m.name: m for m in rows(engine, Monitor)}
    assert set(monitors) == {"Temperature", "Status", "Sheet", "Counts"}
    assert (monitors["Temperature"].kind, monitors["Temperature"].value) == ("number", "22.0")
    assert monitors["Sheet"].kind == "link" and monitors["Counts"].kind == "json"
    assert rows(engine, Output) == []  # monitors are not outputs
    events = [e.message for e in rows(engine, Event)]
    assert events.count('New monitor "Temperature"') == 1  # creating is logged; updates aren't
    put_monitor(client, "Status", None)  # null content removes it
    assert "Status" not in {m.name for m in rows(engine, Monitor)}
    assert any('Monitor "Status" removed' in e for e in [e.message for e in rows(engine, Event)])
    assert put_monitor(client, "", 1).status_code == 422 and put_monitor(client, "x" * 51, 1).status_code == 422


def test_monitors_have_the_same_filter_pins_and_bells_as_outputs(engine, client, taro, hanako, course, create_agent):
    agent_id = create_agent(taro, course, "helper")
    put_monitor(client, "Temperature", 21.5)
    put_monitor(client, "Status", "All good")
    client.post("/api/outputs", json={"category": "news", "content": "Headline"}, headers=api("helper"))
    first_visit = taro.get(f"/agents/{agent_id}").text
    assert "2 monitors available" in first_visit and "All good" not in first_visit  # nothing until chosen
    chosen = taro.get(f"/agents/{agent_id}/monitors", params={"c": "Status"}).text
    assert "All good" in chosen and "21.5" not in chosen
    everything = taro.get(f"/agents/{agent_id}/monitors", params={"all": "true", "sort": "name", "order": "asc"}).text
    assert everything.index("Status") < everything.index("Temperature")
    # One pin per agent, shared with outputs: pinning a monitor unpins the category.
    taro.post(f"/agents/{agent_id}/outputs/pin", data={"name": "news"})
    reply = taro.post(f"/agents/{agent_id}/monitors/pin", data={"name": "Status"})
    agent = agent_row(engine, agent_id)
    assert agent.pinned_monitor_name == "Status" and agent.pinned_category is None
    assert 'id="pin-outputs-' in reply.text and 'hx-swap-oob="true"' in reply.text  # the outputs pin, unpinned
    assert "All good" in taro.get(f"/agents/{agent_id}").text  # a pinned monitor starts ticked
    card = hanako.get(f"/classes/{course}/agents/{taro.id}").text
    assert "Status" in card and "All good" in card  # and shows its current value on the card
    put_monitor(client, "Status", "Paused for lunch")
    assert "Paused for lunch" in hanako.get(f"/classes/{course}/agents/{taro.id}").text
    hanako.post(f"/agents/{agent_id}/monitors/notify", data={"name": "Status"})  # bells are per viewer
    assert "Notifications on" in hanako.get(f"/agents/{agent_id}/monitors", params={"known": "x"}).text
    assert "Notifications on" not in taro.get(f"/agents/{agent_id}/monitors", params={"known": "x"}).text
    assert hanako.post(f"/agents/{agent_id}/monitors/pin", data={"name": "Status"}).status_code == 403


def test_owner_sets_and_removes_link_monitors_in_settings(engine, taro, course, create_agent):
    agent_id = create_agent(taro, course, "helper")
    taro.post(f"/agents/{agent_id}/monitors/link", data={"name": "Sheet", "url": "https://example.com/a"})
    taro.post(f"/agents/{agent_id}/monitors/link", data={"name": "Board", "url": "https://example.com/b"})
    assert {m.name: m.value for m in rows(engine, Monitor)} == {"Sheet": "https://example.com/a",
                                                                "Board": "https://example.com/b"}
    bad = taro.post(f"/agents/{agent_id}/monitors/link", data={"name": "Bad", "url": "javascript:alert(1)"})
    assert "must start with http" in bad.text
    taro.post(f"/agents/{agent_id}/monitors/link", data={"name": "Sheet", "url": ""})  # empty link removes it
    assert [m.name for m in rows(engine, Monitor)] == ["Board"]


def test_agents_read_monitors_of_their_class_only(client, taro, hanako, course, create_agent, create_class):
    create_agent(taro, course, "reader")
    create_agent(hanako, course, "writer")
    elsewhere = create_agent(taro, create_class(title="Other", enrolled_gens=[9]), "far-away")
    put_monitor(client, "Temperature", 21.5, agent="writer")
    client.post("/api/monitors/file", data={"name": "Report"}, files={"file": ("r.pdf", b"%PDF-1.4 r", "application/pdf")},
                headers=api("writer"))
    monitors = client.get("/api/agents/writer/monitors", headers=api("reader")).json()
    assert [m["name"] for m in monitors] == ["Report", "Temperature"]
    assert monitors[1]["content"] == 21.5 and monitors[0]["kind"] == "pdf"
    assert client.get(monitors[0]["file"]["url"], headers=api("reader")).content == b"%PDF-1.4 r"
    one = client.get("/api/agents/writer/monitors", params={"name": "Temperature"}, headers=api("reader")).json()
    assert len(one) == 1
    assert client.get("/api/agents/writer", headers=api("reader")).json()["monitors"] == ["Report", "Temperature"]
    assert client.get(f"/api/agents/{elsewhere}/monitors", headers=api("reader")).status_code == 404


def test_monitor_cap_per_agent(client, taro, course, create_agent, monkeypatch):
    import routes.agent_tools
    monkeypatch.setattr(routes.agent_tools, "MAX_MONITORS_PER_AGENT", 2)
    create_agent(taro, course, "helper")
    assert put_monitor(client, "a", 1).status_code == 200 and put_monitor(client, "b", 2).status_code == 200
    assert put_monitor(client, "c", 3).status_code == 422
    assert put_monitor(client, "a", 9).status_code == 200  # replacing an existing one is always fine


def test_older_connectors_single_monitor_still_works(engine, client, taro, course, create_agent):
    create_agent(taro, course, "helper")
    client.put("/api/monitor", json={"url": "https://example.com/old-style"}, headers=api("helper"))
    assert [(m.name, m.value) for m in rows(engine, Monitor)] == [("Monitor", "https://example.com/old-style")]
    assert client.get("/api/agents/me/monitor", headers=api("helper")).json()["monitor"]["kind"] == "link"
    assert client.post("/api/monitor/file", files={"file": ("a.pdf", b"nope", "application/pdf")},
                       headers=api("helper")).status_code == 422
    assert client.delete("/api/monitor", headers=api("helper")).json() == {"deleted": 1}


def test_a_legacy_single_monitor_is_moved_into_the_monitor_table(engine, taro, course, create_agent):
    from db import move_legacy_monitors

    agent_id = create_agent(taro, course, "helper")
    with Session(engine) as db:
        agent = db.get(Agent, agent_id)
        agent.monitor_url, agent.pinned_monitor = "https://example.com/legacy", True
        db.add(agent)
        db.commit()
        move_legacy_monitors(db)
        move_legacy_monitors(db)  # running it again changes nothing
        agent = db.get(Agent, agent_id)
        assert agent.monitor_url is None and agent.pinned_monitor is False
        assert agent.pinned_monitor_name == "Monitor"
    assert [(m.name, m.kind, m.value) for m in rows(engine, Monitor)] == [("Monitor", "link", "https://example.com/legacy")]


def test_deleting_an_agent_deletes_its_monitors_and_their_files(engine, client, taro, course, create_agent):
    agent_id = create_agent(taro, course, "helper")
    client.post("/api/monitors/file", data={"name": "Report"}, files={"file": ("r.pdf", b"%PDF-1.4 r", "application/pdf")},
                headers=api("helper"))
    taro.post(f"/agents/{agent_id}/delete", data={"password": PASSWORD})
    assert rows(engine, Monitor) == [] and rows(engine, StoredFile) == []


# --- The connector -------------------------------------------------------------------------


@pytest.fixture
def connector(client, taro, course, create_agent, tmp_path, monkeypatch):
    """The real connector file, talking to the test site through the TestClient."""
    create_agent(taro, course, "connected")
    monkeypatch.setenv("DASHBOARD_URL", "http://testserver")
    monkeypatch.setenv("DASHBOARD_API_KEY", key_for("connected"))
    monkeypatch.setenv("DASHBOARD_BUFFER_DIR", str(tmp_path / "buffer"))
    path = Path(__file__).resolve().parent.parent / "connector" / "dashboard_connector.py"
    spec = importlib.util.spec_from_file_location("dashboard_connector_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class ThroughTestClient:
        """Stands in for requests.Session, sending requests to the test site."""

        headers = {}

        def request(self, method, url, timeout=None, allow_redirects=False, stream=False, **kwargs):
            reply = client.request(method, url, headers={"X-API-Key": key_for("connected")},
                                   follow_redirects=False, **kwargs)
            response = requests.Response()
            response.status_code, response._content = reply.status_code, reply.content
            response.headers, response.url = CaseInsensitiveDict(reply.headers), url
            response._content_consumed = True  # so iter_content() reads _content
            return response

        def get(self, url, **kwargs):
            return self.request("GET", url, **kwargs)

    monkeypatch.setattr(module, "_http", ThroughTestClient())
    monkeypatch.setattr(module, "_start_sender", lambda: None)  # no background thread in tests
    return module


def test_connector_holds_outputs_while_paused_and_sends_them_on_start(engine, taro, connector):
    agent_id = rows(engine, Agent)[0].id
    assert connector.report("Agent online", "status") is True
    taro.post(f"/agents/{agent_id}/command", data={"command": "pause"})
    connector.check_in(every=5)
    assert connector.is_paused()
    assert connector.output("held result", "results") is False  # held, not sent
    assert connector.report("still reporting", "status") is True  # reports still go through
    assert connector.pending() == 1 and rows(engine, Output) == []
    # Pretend the pause lasted a while: the held item keeps its original time.
    queue = connector._load_queue()
    queue[0]["created_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    connector._save_queue(queue)
    taro.post(f"/agents/{agent_id}/command", data={"command": "start"})
    connector.check_in()
    assert not connector.is_paused()
    assert connector.flush() == 0
    output = rows(engine, Output)[0]
    assert output.value == "held result"
    assert datetime.now(timezone.utc) - output.created_at.replace(tzinfo=timezone.utc) > timedelta(minutes=4)
    assert any("(delivered late)" in e.message for e in rows(engine, Event))


def test_connector_opts_in_to_messages_and_inbox(engine, taro, connector):
    received = []
    connector.accept_messages(received.append)
    agent = rows(engine, Agent)[0]
    assert agent.accepts_messages
    taro.post(f"/agents/{agent.id}/command", data={"command": "message", "text": "hello agent"})
    connector.check_in()
    assert received == ["hello agent"]
    stored = []
    connector.accept_inbox(lambda item, path: stored.append((item["filename"], Path(path).read_bytes())))
    taro.post(f"/agents/{agent.id}/inbox", files={"files": ("in.txt", b"payload", "text/plain")})
    connector.check_in()
    assert stored == [("in.txt", b"payload")] and rows(engine, InboxFile) == []


# --- Caps on outputs and activity ------------------------------------------------------------


def test_each_agent_keeps_only_the_newest_outputs_and_activity(engine, client, taro, course, create_agent, monkeypatch):
    import routes.agent_tools
    monkeypatch.setattr(routes.agent_tools, "MAX_OUTPUTS_PER_AGENT", 3)
    monkeypatch.setattr(routes.agent_tools, "MAX_EVENTS_PER_AGENT", 4)
    create_agent(taro, course, "chatty")
    client.post("/api/outputs/file", data={"category": "files"}, files={"file": ("old.txt", b"old", "text/plain")},
                headers=api("chatty"))
    old_file = Path(os.environ["LOCAL_FILES_DIR"]) / rows(engine, StoredFile)[0].storage_path
    for number in range(5):
        client.post("/api/outputs", json={"category": "n", "content": f"output {number}"}, headers=api("chatty"))
    assert [o.value for o in rows(engine, Output)] == ["output 2", "output 3", "output 4"]
    assert rows(engine, StoredFile) == [] and not old_file.exists()  # the oldest file went with its output
    for number in range(6):
        client.post("/api/events", json={"message": f"line {number}"}, headers=api("chatty"))
    assert [e.message for e in rows(engine, Event)] == ["line 2", "line 3", "line 4", "line 5"]


def test_connector_sets_named_monitors_and_buffers_only_the_newest_value_each(engine, taro, connector):
    agent_id = rows(engine, Agent)[0].id
    assert connector.set_monitor(3, "Queue") is True
    assert connector.set_monitor("All good", "Status") is True
    assert connector.set_monitor(b"%PDF-1.4 daily", "Summary") is True
    monitors = {m.name: m for m in rows(engine, Monitor)}
    assert monitors["Queue"].value == "3" and monitors["Summary"].kind == "pdf"
    # While paused, updates are held, and only each monitor's newest value waits.
    taro.post(f"/agents/{agent_id}/command", data={"command": "pause"})
    connector.check_in()
    connector.set_monitor(4, "Queue")
    connector.set_monitor(5, "Queue")
    connector.set_monitor("Busy", "Status")
    assert connector.pending() == 2
    taro.post(f"/agents/{agent_id}/command", data={"command": "start"})
    connector.check_in()
    assert connector.flush() == 0
    monitors = {m.name: m.value for m in rows(engine, Monitor)}
    assert monitors["Queue"] == "5" and monitors["Status"] == "Busy"
    read = connector.read_monitor("me", "Queue")
    assert read["content"] == 5
    assert [m["name"] for m in connector.read_monitors()] == ["Queue", "Status", "Summary"]
    assert connector.download(connector.read_monitor("me", "Summary")) == b"%PDF-1.4 daily"
    assert connector.set_monitor(None, "Status") is True  # None removes it
    assert connector.delete_monitors("Queue") == 1
    assert [m.name for m in rows(engine, Monitor)] == ["Summary"]


# --- Remembered Filter choices --------------------------------------------------------------


def test_filter_choices_are_remembered_per_member_agent_and_feed(engine, client, taro, hanako, course, create_agent):
    agent_id = create_agent(taro, course, "helper")
    client.post("/api/outputs", json={"category": "news", "content": "Headline"}, headers=api("helper"))
    client.post("/api/outputs", json={"category": "stats", "content": 7}, headers=api("helper"))
    put_monitor(client, "Status", "All good")
    assert "Headline" not in taro.get(f"/agents/{agent_id}").text  # nothing chosen yet
    # Choosing in the Filter (the form always sends its sort) is remembered...
    taro.get(f"/agents/{agent_id}/outputs", params={"c": "news", "sort": "name", "order": "asc"})
    taro.get(f"/agents/{agent_id}/monitors", params={"all": "true", "sort": "date", "order": "desc"})
    page = taro.get(f"/agents/{agent_id}").text
    assert "Headline" in page and "All good" in page  # ...so it's all back on the next visit
    assert '<option value="name" selected>' in page
    # ...for that member only.
    other = hanako.get(f"/agents/{agent_id}").text
    assert "Headline" not in other and "All good" not in other
    # Unticking everything is remembered too.
    taro.get(f"/agents/{agent_id}/outputs", params={"sort": "date", "order": "desc"})
    assert "Headline" not in taro.get(f"/agents/{agent_id}").text


def test_background_refreshes_without_the_filter_change_nothing(engine, client, taro, course, create_agent):
    from db import FeedFilter

    agent_id = create_agent(taro, course, "helper")
    client.post("/api/outputs", json={"category": "news", "content": "Headline"}, headers=api("helper"))
    taro.get(f"/agents/{agent_id}/outputs", params={"c": "news", "sort": "date", "order": "desc"})
    saved = rows(engine, FeedFilter)[0].updated_at
    taro.get(f"/agents/{agent_id}/outputs", params={"c": "news", "sort": "date", "order": "desc"})  # same choices
    taro.get(f"/agents/{agent_id}/outputs", params={"known": "news"})  # no Filter values at all
    assert rows(engine, FeedFilter)[0].updated_at == saved and rows(engine, FeedFilter)[0].selected == ["news"]
