"""Dashboard connector: lets any Python agent report to the Fukuhara Research
Seminar Dashboard (your agent dashboard in one class).

Works wherever the dashboard runs: on your own computer, on Render, or on
any other website. Drop this file next to your agent's code. It needs Python
3.10+ and the `requests` package (pip install requests). Nothing else.

SETUP: two values (see the PLACEHOLDERS section below)
  1. DASHBOARD_URL      the address of the dashboard website (just the address,
                        no page), e.g. https://your-service.onrender.com
  2. DASHBOARD_API_KEY  this agent's key, from the "Add Agent" card on your agent
                        dashboard in the class (or later: the agent's settings,
                        the gear next to its name, then "View key").
  Set them as environment variables (recommended), e.g.
      DASHBOARD_URL=https://your-site.onrender.com DASHBOARD_API_KEY=the-key python your_agent.py
  or replace the placeholders below.

QUICK START: everything this file offers, in the order you'd usually use it

    from pathlib import Path
    from dashboard_connector import (
        accept_inbox, accept_messages, check_in, flush, listen, output, report,
        set_monitor, start_check_ins, wait_if_paused,
    )

    # 1. Optional: let the dashboard send this agent text messages. Until you
    #    call this, the dashboard's message box stays switched off.
    def on_message(text):            # e.g. pass the text to your agent's LLM
        ...
    accept_messages(on_message)

    # 1b. Optional: take files you upload to this agent's Inbox on the dashboard.
    def on_file(item, path):         # path: a temporary copy; store it your own way
        shutil.copy(path, Path("data") / item["filename"])   # (import shutil)
    accept_inbox(on_file)            # each file is deleted from the dashboard once stored

    # 2. Optional: run your own code when Start or Pause is pressed.
    def on_command(command):         # command["command"] is "start" or "pause"
        report("Paused" if command["command"] == "pause" else "Started", "status")
    listen(on_command)

    # 3. Check in, at a rhythm that suits this agent (see CHECK-INS below).
    #    An agent that runs all the time checks in in the background:
    start_check_ins(every=30)        # seconds; shorter = quicker reactions
    #    ...an agent that runs on a schedule instead calls, at each run:
    #    check_in(every=3600)        # e.g. an hourly agent

    # 4. Say hello (a status line in the agent's Activity log).
    report("Agent online", "status")

    # 5. Optional: the Monitor, showing either a link to where your data lives,
    #    or a PDF shown inside the dashboard (only the latest one is kept).
    set_monitor("https://docs.google.com/spreadsheets/d/...", "Results sheet")

    # 6. Your agent's work loop.
    while True:
        wait_if_paused()             # a safe point: waits here while paused
        ...                          # your agent's real work
        # output(content, category) is like print(), but to the dashboard's
        # Outputs section. Only what you wrap in output() is sent, so choose
        # what matters, or send a short summary of a lot of data.
        output("Found 12 new articles", "daily summary")       # text
        output(12, "articles found")                           # a number
        output("https://example.com/story", "links")           # a clickable link
        output({"top": "story", "count": 12}, "stats")         # a list or dict
        output(Path("reports/today.pdf"), "reports")           # a file, up to 5 MB
        set_monitor(Path("daily_summary.pdf"), "Daily summary")  # a PDF on schedule

    # A short script that runs on a schedule and then exits should end with:
    flush()                          # try to send anything still waiting

    # 7. Optional: read what any agent in this class has shared, and tidy
    #    up this agent's own data (agents can only delete their own).
    from dashboard_connector import agents, read_outputs, download, delete_outputs
    for other in agents() or []:                      # every agent in the class, A to Z
        for item in read_outputs(other["name"], category="reports", limit=5) or []:
            if item["kind"] == "file":
                download(item, save_to=item["file"]["filename"])
    delete_outputs(category="drafts")                 # this agent's own only

FUNCTIONS
  output(content, category)  Show a result in Outputs, sorted by category (1-50
                             characters; the dashboard can filter by it and pin
                             one). content: text (up to 20,000 characters), a
                             number, an http(s) link, a list/dict, or a file
                             (pathlib.Path, open file, or bytes; up to 5 MB).
  report(message, kind)      A status line in Activity. kind: "log" (blue),
                             "status" (green) or "error" (red).
  set_monitor(link_or_pdf, label)
                             The Monitor: an http(s) link, a PDF (up to 5 MB) shown
                             inside the page, or None to remove it. The label is up
                             to 50 characters.
  start_check_ins(every)     Check in automatically every `every` seconds, in the
                             background (for agents that run all the time).
  check_in(every=None)       Check in once, now (for agents that run on a schedule:
                             call it at the start of each run). `every` says how
                             often to expect check-ins; it's remembered.
  accept_messages(on_message)
                             Switch on the dashboard's message box; on_message(text)
                             receives each message, at check-ins.
  listen(on_command, every=None)
                             Run on_command(command) when Start/Pause is pressed,
                             at check-ins. (Messages go to on_message instead, if
                             set.) Passing `every` also starts background check-ins.
  wait_if_paused()           Wait while the dashboard has this agent paused.
  is_paused()                True while paused.
  flush(), pending()         Send what's waiting in the offline buffer now / count it.
  accept_inbox(on_file)      Pick up files you upload to this agent's Inbox:
                             on_file(item, path) gets each one at check-ins; return
                             normally once stored and it's deleted from the dashboard.
  inbox(), download_inbox(item, save_to=None), confirm_inbox(item)
                             The same, by hand: list, download, confirm.

  Reading (any agent in the same class; `agent` is "me", an agent's id, or its
  exact name; if two agents in the class share a name, use the id):
  agents()                   Every agent in the class: id, name, owner, description,
                             online, paused, monitor, ...
  read_agent(agent="me")     One agent, plus its output categories.
  read_outputs(agent="me", category=None, after=None, before=None, limit=50, offset=0)
                             Its outputs, newest first, as dicts (content as sent).
  read_activity(agent="me", kind=None, after=None, before=None, limit=50, offset=0)
                             Its Activity log, newest first.
  read_monitor(agent="me")   What its Monitor shows (a link or a PDF), or None.
  download(item, save_to=None)
                             The bytes of a file output or a PDF monitor.
  Deleting (THIS agent's own data only; each delete is noted in its Activity):
  delete_output(item)        One output (from read_outputs(), or its id).
  delete_outputs(category=None, before=None, everything=False)
                             Many outputs; everything=True for all of them.
  delete_event(item)         One Activity line.
  delete_activity(kind=None, before=None, everything=False)
                             Many Activity lines.
  set_monitor(None)          Removes the Monitor (see above).
  The dashboard keeps each agent's newest 1,000 outputs and 2,000 Activity
  lines; older ones are deleted automatically.
  All of them return quickly and never raise: problems are printed as one-line
  "[dashboard] ..." warnings, and your agent carries on.

CHECK-INS
  Each agent decides how often it checks in: every few seconds, every few
  minutes, hourly, daily (1 second to 1 week). A check-in says "I'm here",
  tells the dashboard how often to expect the next one, and picks up any
  waiting Start/Pause/messages. The dashboard shows the agent as "online" while
  it keeps to its own rhythm (plus half an interval of slack), so an hourly
  agent stays "online" between runs. Agents that never say how often they
  check in are expected every 60 seconds. Any call to the dashboard also counts
  as being seen.

START AND PAUSE (the dashboard's buttons)
  They reach the agent at its next check-in. While paused, output() and
  set_monitor() hold what you send and deliver it, in order and with its
  original time, when Start is pressed. report() still goes through, so status lines and errors
  stay visible. This needs no extra code. To pause the agent's *work* too,
  call wait_if_paused() at safe points in your loop.

OFFLINE BUFFER
  If the dashboard can't be reached for a while (no internet, a restart, a
  sleeping free site, a server error), output(), report() and set_monitor()
  save what they couldn't send in a "dashboard_buffer" folder next to this file
  (or wherever DASHBOARD_BUFFER_DIR points). It's sent automatically, in order
  and with its original times, once the dashboard is back, even if your agent
  was restarted in between. Mistakes retrying can't fix (a wrong key, a file
  over 5 MB) aren't saved; they're printed once instead.
"""

import json
import mimetypes
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import requests

# =============================================================================
# PLACEHOLDERS: replace these two values, OR set the environment variables
# DASHBOARD_URL and DASHBOARD_API_KEY (environment variables win if both are set).
# =============================================================================

# >>> REPLACE THIS PLACEHOLDER with the dashboard's address (no page at the end):
#       "http://127.0.0.1:8000"               if the dashboard runs on this same computer
#       "https://your-service.onrender.com"   if it's deployed on Render
#       "https://your-own-domain.com"         if it's on a custom domain or another host
#     This file was downloaded from: {{DOWNLOADED_FROM}}
#     (that may be a local test address; use the address your agent can actually reach)
DASHBOARD_URL_PLACEHOLDER = "REPLACE_WITH_YOUR_DASHBOARD_URL"

# >>> REPLACE THIS PLACEHOLDER with this agent's API key, from "Add Agent" or "View key".
#     Prefer the DASHBOARD_API_KEY environment variable instead: a key typed in here
#     must never be committed to Git or shared.
DASHBOARD_API_KEY_PLACEHOLDER = "REPLACE_WITH_YOUR_AGENT_API_KEY"

# =============================================================================

REPORT_TIMEOUT_SECONDS = 10  # report() runs in your agent's own code, so keep it short
LISTEN_TIMEOUT_SECONDS = 30  # the listener runs in the background; allow sleepy servers
UPLOAD_TIMEOUT_SECONDS = 60  # sending a file can take a while
MAX_FILE_BYTES = 5 * 1024 * 1024  # the dashboard accepts files up to 5 MB
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

# Offline buffer: where unsent items wait, how often to retry, and how much to keep.
# Set DASHBOARD_BUFFER_DIR to keep the buffer somewhere else.
BUFFER_DIR = os.getenv("DASHBOARD_BUFFER_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "dashboard_buffer"
)
RETRY_SECONDS = 15
MIN_CHECK_IN, MAX_CHECK_IN = 1, 7 * 24 * 60 * 60  # check-in interval limits: 1 second to a week
BUFFER_MAX_ITEMS = 1000  # beyond this, the oldest waiting items are dropped
BUFFER_MAX_FILE_BYTES = 100 * 1024 * 1024  # 100 MB of waiting files at most


def normalize_url(url: str) -> str:
    """Tidy a dashboard address: trim spaces, a trailing "/", and a page such as
    "/classes" or "/login" (easy to copy from the browser's address bar)."""
    url = url.strip().rstrip("/")
    for page in ("/classes", "/login", "/dashboard"):
        if url.endswith(page):
            url = url[: -len(page)]
    return url


def config_problem(url: str, api_key: str) -> str | None:
    """Explain what's wrong with the settings, or return None if they look fine."""
    if not url or url.startswith("REPLACE_WITH_"):
        return "Set DASHBOARD_URL (or replace DASHBOARD_URL_PLACEHOLDER) with your dashboard's address."
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return f"DASHBOARD_URL must start with http:// or https://, got {url!r}."
    if not api_key or api_key.startswith("REPLACE_WITH_"):
        return "Set DASHBOARD_API_KEY (or replace DASHBOARD_API_KEY_PLACEHOLDER) with this agent's key."
    return None


DASHBOARD_URL = normalize_url(os.getenv("DASHBOARD_URL") or DASHBOARD_URL_PLACEHOLDER)
API_KEY = (os.getenv("DASHBOARD_API_KEY") or DASHBOARD_API_KEY_PLACEHOLDER).strip()
PROBLEM = config_problem(DASHBOARD_URL, API_KEY)

if PROBLEM:
    print(f"[dashboard] Not connected: {PROBLEM}")
elif urlparse(DASHBOARD_URL).scheme == "http" and urlparse(DASHBOARD_URL).hostname not in LOCAL_HOSTS:
    print(
        "[dashboard] Warning: DASHBOARD_URL uses http://, so the API key travels unencrypted. "
        "Use https:// for a deployed dashboard."
    )

_http = requests.Session()
_http.headers["X-API-Key"] = API_KEY


def _send(method: str, path: str, timeout: float, **kwargs) -> requests.Response:
    """Make one request to the dashboard API.

    Redirects are not followed: a redirect (e.g. http:// to https://) would turn a
    POST into a GET and silently lose the report, so it's reported as a setup problem.
    """
    response = _http.request(
        method, f"{DASHBOARD_URL}{path}", timeout=timeout, allow_redirects=False, **kwargs
    )
    if response.is_redirect:
        where = response.headers.get("Location", "another address")
        raise requests.HTTPError(f"redirected to {where}", response=response)
    response.raise_for_status()
    return response


def _warn(what: str, error: Exception) -> None:
    """Print a one-line warning with a hint, instead of crashing."""
    response = getattr(error, "response", None)
    status = response.status_code if response is not None else None
    detail = None
    if response is not None and status in (403, 404, 422):
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        if not isinstance(detail, str) or detail == "Not Found":  # "Not Found" = wrong address
            detail = None
    if detail:
        reason = f"HTTP {status}: {detail}"
    elif status in (301, 302, 303, 307, 308):
        reason = f"{error}. Set DASHBOARD_URL to that address (often https:// instead of http://)"
    elif status == 401:
        reason = "HTTP 401: the API key was refused. Check DASHBOARD_API_KEY (it may have been replaced)"
    elif status == 404:
        reason = "HTTP 404: check DASHBOARD_URL points at the dashboard website itself"
    elif status == 413:
        reason = "HTTP 413: the file is too big (max 5 MB)"
    elif status == 422:
        reason = "HTTP 422: the dashboard rejected the content (check the category and content)"
    elif status is not None:
        reason = f"HTTP {status}"
    elif isinstance(error, requests.Timeout):
        reason = "timed out (a sleeping free Render site can take about a minute to wake)"
    elif isinstance(error, requests.ConnectionError):
        reason = f"can't reach {DASHBOARD_URL} (is it running, and is the address right?)"
    else:
        reason = f"{type(error).__name__}: {error}"
    print(f"[dashboard] Could not {what}: {reason}.")


# =============================================================================
# Check-ins, Start/Pause and messages
#
# A check-in tells the dashboard "I'm here" (keeping the agent "online"), says
# how often to expect the next one and whether this agent accepts messages,
# learns whether the agent is paused, and delivers any waiting Start/Pause/
# messages to your handlers. How often to check in is up to each agent:
#   - an agent that runs all the time: start_check_ins(every=30) once;
#   - an agent that runs on a schedule (e.g. hourly): check_in(every=3600) at
#     the start of each run.
# =============================================================================

_paused = False  # what the dashboard said at the last check-in
_state_known = False  # True once a check-in has succeeded
_accepts_messages = False  # set by accept_messages()
_declared_every = None  # seconds between check-ins, as this agent declared it
_message_handler = None
_accepts_inbox = False  # set by accept_inbox()
_inbox_handler = None
_inbox_waiting = 0  # how many Inbox files the dashboard said are waiting
_inbox_lock = threading.Lock()  # one Inbox pick-up at a time
_command_handler = None
_check_in_thread = None
_announced_hold = False


def _set_paused(paused: bool) -> None:
    """Remember the pause state; on Start, send whatever was held."""
    global _paused, _announced_hold
    was_paused, _paused = _paused, paused
    if was_paused and not paused:
        _announced_hold = False
        _retry_now.set()  # wake the buffer's sender: held items can go now


def _check_in() -> bool:
    """Send the check-in itself (POST /api/state) and learn the pause state."""
    global _state_known
    global _inbox_waiting
    body = {"accepts_messages": _accepts_messages, "accepts_inbox": _accepts_inbox}
    if _declared_every is not None:
        body["check_in_every"] = _declared_every
    try:
        state = _send("POST", "/api/state", REPORT_TIMEOUT_SECONDS, json=body).json()
    except (requests.RequestException, ValueError):
        return False  # unreachable: keep the last known state and try again later
    _state_known = True
    _set_paused(bool(state.get("paused")))
    try:
        _inbox_waiting = int(state.get("inbox_waiting") or 0)
    except (TypeError, ValueError):
        _inbox_waiting = 0
    return True


def _declare_every(every) -> None:
    """Remember how often this agent checks in (seconds, 1 second to a week).

    Values outside that range are brought into it, with a warning (never an error).
    """
    global _declared_every
    try:
        wanted = int(every)
    except (TypeError, ValueError):
        print(f"[dashboard] Ignoring check-in interval {every!r}: it must be a number of seconds.")
        return
    allowed = min(max(wanted, MIN_CHECK_IN), MAX_CHECK_IN)
    if allowed != wanted:
        print(f"[dashboard] Check-in interval {wanted}s is out of range; using {allowed}s.")
    _declared_every = allowed


def check_in(every: float | None = None) -> bool:
    """Check in with the dashboard once, now.

    every: how often this agent checks in, in seconds (e.g. 3600 for an hourly
           agent). The dashboard shows the agent as online while it keeps to
           this rhythm, plus some slack. Set it once; it's remembered.
    Also learns whether the agent is paused (see is_paused()) and hands any
    waiting Start/Pause/messages to your listen()/accept_messages() handlers,
    and any waiting Inbox files to your accept_inbox() handler.
    Returns True if the dashboard was reached.

    For an agent that runs on a schedule, call it at the start of each run.
    """
    if PROBLEM:
        return False
    if every is not None:
        _declare_every(every)
    reached = _check_in()
    if reached and (_command_handler is not None or _message_handler is not None):
        _check_commands()
    if reached and _inbox_handler is not None and _inbox_waiting:
        _check_inbox()
    return reached


def start_check_ins(every: float = 30) -> None:
    """Check in automatically every `every` seconds, in the background.

    For agents that run continuously. Start/Pause and messages reach the agent
    at each check-in, so a shorter interval means quicker reactions (e.g. 5);
    a longer one means fewer requests (e.g. 300). Calling it again changes the
    interval.
    """
    global _check_in_thread
    if PROBLEM:
        print("[dashboard] Not checking in until the settings are fixed.")
        return
    _declare_every(every)
    if _check_in_thread is not None:
        return  # already running; it picks up the new interval

    def loop():
        while True:
            check_in()
            time.sleep(_declared_every)

    _check_in_thread = threading.Thread(target=loop, name="dashboard-check-in", daemon=True)
    _check_in_thread.start()


def is_paused() -> bool:
    """True while the dashboard has this agent paused (as of the last check-in)."""
    return _paused


def wait_if_paused(check_every: float = 5.0) -> bool:
    """If the dashboard has this agent paused, wait here until Start is pressed.

    Call it at safe points in your agent's loop, e.g. before starting the next
    task, so Pause stops the agent's work and not just its outputs. While it
    waits, it checks in every `check_every` seconds to notice Start.
    Returns True if it had to wait.
    """
    if PROBLEM:
        return False
    if not _state_known:
        _check_in()  # make sure we know the state before deciding
    if not _paused:
        return False
    print("[dashboard] Paused from the dashboard; waiting for Start...")
    while _paused:
        time.sleep(check_every)
        check_in()
    print("[dashboard] Started again from the dashboard.")
    return True


def accept_messages(on_message) -> None:
    """Switch on the dashboard's message box for this agent.

    on_message(text) is called with each message you send from the dashboard
    (e.g. to pass it to the agent's LLM). Messages arrive at the agent's check-ins,
    so the agent must also check in: start_check_ins() or check_in().
    Until an agent calls this, the dashboard keeps its message box switched off.
    """
    global _accepts_messages, _message_handler
    if PROBLEM:
        print("[dashboard] Not accepting messages until the settings are fixed.")
        return
    _message_handler = on_message
    _accepts_messages = True
    _check_in()  # tell the dashboard straight away


# =============================================================================
# Offline buffer
#
# Each thing to send is an "item": a small dict saying what it is (kind), when
# it was made (created_at), and what to send. If sending fails for a reason
# that should pass (the dashboard is unreachable, asleep, or has a server
# error), the item is appended to dashboard_buffer/queue.jsonl (one item per
# line; files are copied into dashboard_buffer/files/). A background thread
# retries every RETRY_SECONDS and sends waiting items oldest first.
# Mistakes that retrying can't fix (a wrong key, a file that's too big...)
# are never buffered; they're reported once, as before.
# =============================================================================

_buffer_lock = threading.Lock()  # one change to the queue file at a time
_flush_lock = threading.Lock()  # one sender at a time, so nothing is sent twice
_retry_now = threading.Event()  # set to make the background sender try straight away
_sender_started = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _queue_path() -> str:
    return os.path.join(BUFFER_DIR, "queue.jsonl")


def _load_queue() -> list[dict]:
    try:
        with open(_queue_path(), encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        print(f"[dashboard] The offline buffer couldn't be read ({error}); starting a new one.")
        return []


def _save_queue(items: list[dict]) -> None:
    """Rewrite the queue file safely (write a new copy, then swap it in)."""
    os.makedirs(BUFFER_DIR, exist_ok=True)
    temporary = _queue_path() + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item) + "\n")
    os.replace(temporary, _queue_path())


def _delete_item_file(item: dict) -> None:
    if item.get("file"):
        try:
            os.remove(os.path.join(BUFFER_DIR, "files", item["file"]))
        except FileNotFoundError:
            pass


def _item_data(item: dict) -> bytes:
    """The bytes of a file item: still in memory, or saved in the buffer folder."""
    if "_data" in item:
        return item["_data"]
    with open(os.path.join(BUFFER_DIR, "files", item["file"]), "rb") as handle:
        return handle.read()


def _deliver(item: dict) -> None:
    """Send one item to the dashboard. Raises requests.RequestException on failure."""
    kind, made = item["kind"], item["created_at"]
    if kind == "report":
        _send("POST", "/api/events", REPORT_TIMEOUT_SECONDS, json={**item["body"], "created_at": made})
    elif kind == "output":
        _send("POST", "/api/outputs", REPORT_TIMEOUT_SECONDS, json={**item["body"], "created_at": made})
    elif kind == "output_file":
        _send(
            "POST",
            "/api/outputs/file",
            UPLOAD_TIMEOUT_SECONDS,
            data={"category": item["category"], "created_at": made},
            files={"file": (item["filename"], _item_data(item), item["content_type"])},
        )
    elif kind == "monitor":
        _send("PUT", "/api/monitor", REPORT_TIMEOUT_SECONDS, json={**item["body"], "created_at": made})
    elif kind == "monitor_file":
        _send(
            "POST",
            "/api/monitor/file",
            UPLOAD_TIMEOUT_SECONDS,
            data={"label": item["label"] or "", "created_at": made},
            files={"file": (item["filename"], _item_data(item), "application/pdf")},
        )


def _worth_retrying(error: requests.RequestException) -> bool:
    """True if the problem should pass by itself: no connection, a timeout, or a
    server-side error (5xx, e.g. a restart, an outage, or a full database)."""
    if isinstance(error, (requests.ConnectionError, requests.Timeout)):
        return True
    response = getattr(error, "response", None)
    return response is not None and (response.status_code >= 500 or response.status_code == 429)


def _add_to_buffer(item: dict) -> int:
    """Save an item to send later. Returns how many items are now waiting."""
    with _buffer_lock:
        items = _load_queue()
        if item["kind"] in ("monitor", "monitor_file"):
            # Only the latest monitor matters: drop older ones still waiting.
            for old in [i for i in items if i["kind"] in ("monitor", "monitor_file")]:
                _delete_item_file(old)
            items = [i for i in items if i["kind"] not in ("monitor", "monitor_file")]
        if "_data" in item:  # copy the file into the buffer folder
            os.makedirs(os.path.join(BUFFER_DIR, "files"), exist_ok=True)
            item["file"] = uuid.uuid4().hex
            with open(os.path.join(BUFFER_DIR, "files", item["file"]), "wb") as handle:
                handle.write(item.pop("_data"))
            item["size"] = os.path.getsize(os.path.join(BUFFER_DIR, "files", item["file"]))
        items.append(item)
        # Keep the buffer from growing without limit: drop the oldest first.
        dropped = 0
        while len(items) > BUFFER_MAX_ITEMS or sum(i.get("size", 0) for i in items) > BUFFER_MAX_FILE_BYTES:
            _delete_item_file(items.pop(0))
            dropped += 1
        if dropped:
            print(f"[dashboard] Offline buffer full: dropped the {dropped} oldest waiting item(s).")
        _save_queue(items)
        waiting = len(items)
    _start_sender()
    return waiting


def _remove_from_buffer(item_id: str) -> None:
    with _buffer_lock:
        items = _load_queue()
        for item in items:
            if item["id"] == item_id:
                _delete_item_file(item)
        _save_queue([i for i in items if i["id"] != item_id])


def flush() -> int:
    """Try to send everything waiting in the offline buffer now.

    Returns how many items are still waiting (0 = all sent). Handy at the end
    of a short script; long-running agents don't need it, as waiting items are
    retried automatically every RETRY_SECONDS. While the dashboard has the
    agent paused, nothing is sent: held items wait for Start.
    """
    if PROBLEM or _paused:
        return len(_load_queue())
    sent = 0
    with _flush_lock:
        with _buffer_lock:
            items = _load_queue()
        for item in items:
            # Skip anything dropped from the buffer since we read it (e.g. when
            # the buffer was full), so it's never sent after all.
            with _buffer_lock:
                still_waiting = any(i["id"] == item["id"] for i in _load_queue())
            if not still_waiting:
                continue
            try:
                _deliver(item)
            except requests.RequestException as error:
                if _worth_retrying(error):
                    break  # still unreachable: keep this and everything after it, in order
                _warn(f"send a saved {item['kind'].replace('_', ' ')} (dropping it)", error)
            except OSError as error:  # its saved file went missing
                print(f"[dashboard] Dropping a saved {item['kind']}: {error}")
            else:
                sent += 1
            _remove_from_buffer(item["id"])
    waiting = len(_load_queue())
    if sent:
        print(f"[dashboard] Sent {sent} saved item(s) from the offline buffer ({waiting} still waiting).")
    return waiting


def pending() -> int:
    """How many items are waiting in the offline buffer."""
    return len(_load_queue())


def _start_sender() -> None:
    """Start the background thread that retries the buffer (once per program)."""
    global _sender_started
    if _sender_started or PROBLEM:
        return
    _sender_started = True

    def loop():
        while True:
            _retry_now.wait(RETRY_SECONDS)
            _retry_now.clear()
            if pending() and not _paused:
                flush()

    threading.Thread(target=loop, name="dashboard-buffer", daemon=True).start()


def _send_or_buffer(item: dict, what: str) -> bool:
    """Send an item now, or save it for later if the dashboard is unreachable.

    Returns True if the dashboard got it now. While older items are waiting,
    new ones queue up behind them so everything arrives in order.
    """
    global _announced_hold
    item.setdefault("id", uuid.uuid4().hex)
    item.setdefault("created_at", _now())
    if _paused and item["kind"] != "report":
        # Paused from the dashboard: hold it (reports still go through).
        waiting = _add_to_buffer(item)
        if not _announced_hold:
            print("[dashboard] Paused from the dashboard: holding outputs and monitor updates until Start.")
            _announced_hold = True
        print(f"[dashboard] Held {what} ({waiting} waiting).")
        return False
    if pending() and not (_paused and item["kind"] == "report"):
        waiting = _add_to_buffer(item)
        _retry_now.set()
        print(f"[dashboard] Queued {what} behind {waiting - 1} waiting item(s).")
        return False
    try:
        _deliver(item)
        return True
    except requests.RequestException as error:
        if not _worth_retrying(error):
            _warn(what, error)
            return False
        waiting = _add_to_buffer(item)
        _warn(what, error)
        print(f"[dashboard] Saved it to the offline buffer; will retry every {RETRY_SECONDS}s ({waiting} waiting).")
        return False


def report(message: str, kind: str = "log", data: dict | None = None) -> bool:
    """Show a message on this agent's dashboard card and activity log.

    kind: "log" (blue), "status" (green) or "error" (red).
    data: optional extra details, e.g. {"url": ...} or {"path": ...}.
    Returns True if the dashboard received it now. If the dashboard couldn't
    be reached, it's saved and sent later (see "Offline buffer" above).
    """
    if PROBLEM:
        return False
    body = json.loads(json.dumps({"message": str(message), "type": kind, "data": data}, default=str))
    return _send_or_buffer({"kind": "report", "body": body}, "send report")


def _is_file(content) -> bool:
    """True for things output() sends as a file: a path, bytes, or an open file."""
    return isinstance(content, (os.PathLike, bytes, bytearray)) or hasattr(content, "read")


def _read_file(content) -> tuple[str, bytes]:
    """Return (file name, file bytes) for a path, bytes, or an open file."""
    if isinstance(content, os.PathLike):
        with open(content, "rb") as handle:
            return os.path.basename(os.fspath(content)), handle.read()
    if isinstance(content, (bytes, bytearray)):
        return "output.bin", bytes(content)
    data = content.read()
    if isinstance(data, str):
        data = data.encode("utf-8")
    return os.path.basename(getattr(content, "name", "") or "output.bin"), data


def output(content, category: str) -> bool:
    """Send a result to this agent's Outputs section on the dashboard, like print().

    content:  what to show. Text, a number, a link ("https://..."), a list or
              dict, or a file: a pathlib.Path, an open file, or bytes (max 5 MB).
    category: a short name for this output channel, e.g. "articles" or "reports".
              The dashboard can filter by it, and you can pin one category so
              its latest output shows on the agent's card.

    Only what you pass to output() is sent. To log everything, send a short
    summary of it rather than the raw data. Returns True if the dashboard got
    it now. If the dashboard couldn't be reached, it's saved and sent later,
    with its original time (see "Offline buffer" above).
    """
    if PROBLEM:
        return False
    category = " ".join(str(category).split())
    what = f"send output to {category!r}"
    if _is_file(content):
        try:
            name, data = _read_file(content)
        except OSError as error:
            _warn(what, error)
            return False
        if len(data) > MAX_FILE_BYTES:
            print(f"[dashboard] Not sending {name!r} to {category!r}: it's over 5 MB.")
            return False
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        item = {"kind": "output_file", "category": category, "filename": name,
                "content_type": content_type, "_data": data}
    else:
        # default=str turns anything JSON can't handle (dates, objects...) into text.
        body = json.loads(json.dumps({"category": category, "content": content}, default=str))
        item = {"kind": "output", "body": body}
    return _send_or_buffer(item, what)


def set_monitor(link_or_pdf, label: str | None = None) -> bool:
    """Set what this agent's Monitor section shows. Only the latest one is shown.

    link_or_pdf:
      - a web address (http:// or https://) of where the agent's data lives,
        e.g. a Google Sheet or a Grafana board; or
      - a PDF (a pathlib.Path, an open file, or bytes; max 5 MB), shown inside
        the dashboard page. Call it again on a schedule (e.g. a daily summary)
        and the page swaps in the new PDF within about 10 seconds; or
      - None, to remove the monitor.
    Returns True if the dashboard got it now. If the dashboard couldn't be
    reached, the latest monitor is saved and sent later (see "Offline buffer").
    """
    if PROBLEM:
        return False
    if _is_file(link_or_pdf):
        try:
            name, data = _read_file(link_or_pdf)
        except OSError as error:
            _warn("set the monitor", error)
            return False
        if not data.startswith(b"%PDF-"):
            print(f"[dashboard] Not setting the monitor: {name!r} isn't a PDF.")
            return False
        if len(data) > MAX_FILE_BYTES:
            print(f"[dashboard] Not setting the monitor: {name!r} is over 5 MB.")
            return False
        item = {"kind": "monitor_file", "filename": name, "label": label, "_data": data}
    else:
        item = {"kind": "monitor", "body": {"url": link_or_pdf, "label": label}}
    return _send_or_buffer(item, "set the monitor")


# =============================================================================
# Reading the dashboard, and deleting this agent's own data
#
# Any agent can read the outputs, activity and monitor of every agent in the
# same class (to build on each other's work). An agent can only delete its OWN data; trying to delete
# another agent's gets a warning and changes nothing. Deletes are noted in this
# agent's Activity log on the dashboard.
#
# `agent` is "me" (this agent), another agent's id, or its exact name.
# These talk to the dashboard straight away: they aren't held while paused or
# saved in the offline buffer. They return None (reads) or None/False
# (deletes) if the dashboard couldn't be reached, after printing a warning.
# Deletes only affect what the dashboard already has, not outputs still
# waiting in this agent's offline buffer.
# =============================================================================


def _agent_path(agent) -> str:
    return "/api/agents/" + quote(str(agent), safe="")


def _as_time(moment) -> str | None:
    """A datetime (or ISO text) as the ISO text the dashboard expects."""
    if moment is None or isinstance(moment, str):
        return moment
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _read(path: str, what: str, params: dict | None = None):
    """GET some JSON from the dashboard, or None (with a warning) if that fails."""
    if PROBLEM:
        return None
    params = {name: value for name, value in (params or {}).items() if value is not None}
    try:
        return _send("GET", path, REPORT_TIMEOUT_SECONDS, params=params).json()
    except (requests.RequestException, ValueError) as error:
        _warn(what, error)
        return None


def _delete(path: str, what: str, params: dict | None = None) -> int | None:
    """DELETE on the dashboard; returns how many things were deleted, or None."""
    if PROBLEM:
        return None
    params = {name: value for name, value in (params or {}).items() if value is not None}
    try:
        return int(_send("DELETE", path, REPORT_TIMEOUT_SECONDS, params=params).json().get("deleted", 0))
    except (requests.RequestException, ValueError, AttributeError, TypeError) as error:
        _warn(what, error)
        return None


def agents() -> list[dict] | None:
    """Every agent in this agent's class, A to Z: id, name, owner ({"full_name",
    "gen"}), description (what it does, written by its owner; None if not set),
    "you" (True for this agent), folders, online, paused, accepts_messages,
    check_in_every, created_at, last_seen, and monitor (or None)."""
    return _read("/api/agents", "list the agents")


def read_agent(agent="me") -> dict | None:
    """One agent, as in agents(), plus its output "categories" (name, count, latest_at)."""
    return _read(_agent_path(agent), f"read agent {agent!r}")


def read_outputs(agent="me", category=None, after=None, before=None, limit: int = 50,
                 offset: int = 0) -> list[dict] | None:
    """An agent's outputs, newest first.

    category: one category name, or a list of them (default: all).
    after:    only outputs made at or after this time (a datetime or ISO text).
    before:   only outputs made before this time.
    limit/offset: how many (1-500) and how many newest ones to skip, for paging.
    Each output is a dict: id, agent_id, category, kind ("text", "number",
    "link", "json" or "file"), content (as sent: text, a number, or a
    list/dict; a file's name for files), created_at, and for files "file":
    {filename, content_type, size, url}. Get a file's bytes with download().
    """
    params = {"category": [category] if isinstance(category, str) else category,
              "after": _as_time(after), "before": _as_time(before), "limit": limit, "offset": offset}
    return _read(_agent_path(agent) + "/outputs", f"read outputs of {agent!r}", params)


def read_activity(agent="me", kind=None, after=None, before=None, limit: int = 50,
                  offset: int = 0) -> list[dict] | None:
    """An agent's Activity log, newest first: id, agent_id, type, message, data,
    created_at. kind: only one type, e.g. "error". after/before/limit/offset: as in read_outputs()."""
    params = {"type": kind, "after": _as_time(after), "before": _as_time(before), "limit": limit, "offset": offset}
    return _read(_agent_path(agent) + "/events", f"read activity of {agent!r}", params)


def read_monitor(agent="me") -> dict | None:
    """What an agent's Monitor shows: {"kind": "link", "url", "label", "updated_at"}
    or {"kind": "pdf", "filename", "label", "updated_at", "file_url"}. None if it
    has no monitor (or it couldn't be read: then a warning is printed).
    Get a PDF's bytes with download()."""
    reply = _read(_agent_path(agent) + "/monitor", f"read the monitor of {agent!r}")
    return reply.get("monitor") if isinstance(reply, dict) else None


def download(item, save_to=None) -> bytes | None:
    """The bytes of a file output or a PDF monitor, from any agent.

    item:    an output from read_outputs(), a monitor from read_monitor(), or the id
             of one of THIS agent's file outputs.
    save_to: optional path to also save the file to.
    Returns None (with a warning) if there's no file or it couldn't be fetched.
    """
    if PROBLEM:
        return None
    if isinstance(item, dict):
        path = (item.get("file") or {}).get("url") or item.get("file_url")
    elif isinstance(item, int):
        path = f"/api/agents/me/outputs/{item}/file"
    else:
        path = None
    if not path:
        print("[dashboard] Nothing to download: that has no file.")
        return None
    try:
        data = _send("GET", path, UPLOAD_TIMEOUT_SECONDS).content
    except requests.RequestException as error:
        _warn("download the file", error)
        return None
    if save_to is not None:
        try:
            with open(save_to, "wb") as handle:
                handle.write(data)
        except OSError as error:
            _warn(f"save the file to {os.fspath(save_to)!r}", error)
    return data


def _id_of(item) -> int | None:
    """The id of an output/activity line (a dict from a read), or the id itself."""
    try:
        return int(item.get("id") if isinstance(item, dict) else item)
    except (TypeError, ValueError):
        print(f"[dashboard] Not deleting: {item!r} isn't an output/activity line or its id.")
        return None


def delete_output(item) -> bool:
    """Delete one of THIS agent's outputs (an output from read_outputs(), or its id).
    Returns True if it was deleted."""
    output_id = _id_of(item)
    return output_id is not None and bool(_delete(f"/api/outputs/{output_id}", f"delete output {output_id}"))


def delete_outputs(category: str | None = None, before=None, everything: bool = False) -> int | None:
    """Delete many of THIS agent's outputs: those in `category`, and/or made
    before `before` (a datetime or ISO text). Deleting all of them needs
    everything=True, so a forgotten filter can't wipe everything.
    Returns how many were deleted, or None if the dashboard couldn't be reached."""
    if category is None and before is None and not everything:
        print("[dashboard] Not deleting: give a category, a before time, or everything=True.")
        return None
    params = {"category": category, "before": _as_time(before), "all": "true" if everything else None}
    return _delete("/api/outputs", "delete outputs", params)


def delete_event(item) -> bool:
    """Delete one of THIS agent's Activity lines (from read_activity(), or its id)."""
    event_id = _id_of(item)
    return event_id is not None and bool(_delete(f"/api/events/{event_id}", f"delete activity line {event_id}"))


def delete_activity(kind=None, before=None, everything: bool = False) -> int | None:
    """Delete THIS agent's Activity lines of one type (`kind`) and/or made before
    `before`, or all of them with everything=True. One line saying how many
    were deleted is left behind.
    Returns how many were deleted, or None if the dashboard couldn't be reached."""
    if kind is None and before is None and not everything:
        print("[dashboard] Not deleting: give a kind, a before time, or everything=True.")
        return None
    params = {"type": kind, "before": _as_time(before), "all": "true" if everything else None}
    return _delete("/api/events", "delete activity", params)


# =============================================================================
# The Inbox: files the owner uploads for this agent on its dashboard page
#
# They wait in the dashboard's file storage until this agent takes them. accept_inbox(on_file)
# makes every check-in download each waiting file to a temporary place, hand it
# to on_file(item, path), and then confirm it, which deletes it from the
# dashboard. Store the data your own way inside on_file (copy the file, load it
# into your database...): the temporary copy is removed afterwards.
# If on_file raises an error or returns False, the file is NOT confirmed: it
# stays in the Inbox and is offered again at the next check-in.
# Inbox files are only for this agent; other agents can't see them.
# =============================================================================


def accept_inbox(on_file) -> None:
    """Pick up files you upload to this agent's Inbox on the dashboard.

    on_file(item, path) is called for each one, at check-ins (so the agent
    must also check in: start_check_ins() or check_in()):
      item: {"id", "filename", "content_type", "size", "note", "uploaded_at"}
            ("note" is what you typed with the upload, or None);
      path: a pathlib.Path to a temporary copy of the file. Read it, or copy
            it somewhere permanent, inside on_file: it's deleted afterwards.
    Return normally (or anything but False) once the data is safely stored;
    the file is then deleted from the dashboard. Raise an error or return
    False to leave it in the Inbox for next time.
    Until an agent calls this, the dashboard doesn't accept Inbox uploads for
    it (the Inbox window says so).
    """
    global _accepts_inbox, _inbox_handler
    if PROBLEM:
        print("[dashboard] Not accepting Inbox files until the settings are fixed.")
        return
    _inbox_handler = on_file
    _accepts_inbox = True
    if _check_in() and _inbox_waiting:  # tell the dashboard straight away, and take what's waiting
        _check_inbox()


def inbox() -> list[dict] | None:
    """The files waiting in this agent's Inbox, oldest first (None if unreachable)."""
    return _read("/api/inbox", "read the inbox")


def download_inbox(item, save_to=None):
    """Download one Inbox file (an item from inbox()) to `save_to`, or to a new
    temporary file. Returns the pathlib.Path, or None (with a warning) if it
    couldn't be fetched. Downloading doesn't remove it: see confirm_inbox()."""
    from pathlib import Path

    if PROBLEM or not isinstance(item, dict) or "id" not in item:
        return None
    if save_to is None:
        name = os.path.basename(str(item.get("filename") or "file")) or "file"
        save_to = os.path.join(tempfile.mkdtemp(prefix="dashboard_inbox_"), name)
    try:
        response = _http.get(f"{DASHBOARD_URL}/api/inbox/{int(item['id'])}/file",
                             timeout=UPLOAD_TIMEOUT_SECONDS, allow_redirects=False, stream=True)
        if response.is_redirect:
            raise requests.HTTPError(f"redirected to {response.headers.get('Location')}", response=response)
        response.raise_for_status()
        with open(save_to, "wb") as handle:
            for piece in response.iter_content(chunk_size=1024 * 1024):  # never the whole file in memory
                handle.write(piece)
    except (requests.RequestException, OSError, ValueError) as error:
        _warn(f"download inbox file {item.get('filename')!r}", error)
        return None
    return Path(save_to)


def confirm_inbox(item) -> bool:
    """Tell the dashboard this agent has stored an Inbox file: it's deleted there."""
    item_id = _id_of(item)
    if item_id is None or PROBLEM:
        return False
    try:
        _send("POST", f"/api/inbox/{item_id}/received", REPORT_TIMEOUT_SECONDS)
    except requests.RequestException as error:
        _warn(f"confirm inbox file {item_id}", error)
        return False
    return True


def _check_inbox() -> None:
    """Hand each waiting Inbox file to the accept_inbox() handler, then confirm it."""
    if not _inbox_lock.acquire(blocking=False):
        return  # another check-in is already doing this
    try:
        items = inbox()
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict) or "id" not in item:
                continue
            path = download_inbox(item)
            if path is None:
                continue  # try again at the next check-in
            try:
                keep = _inbox_handler(item, path) is False
            except Exception as error:  # a bug in the handler shouldn't stop the agent
                report(f"Failed to handle inbox file {item.get('filename')!r}: {error}", "error")
                keep = True
            finally:
                try:
                    os.remove(path)
                    os.rmdir(os.path.dirname(path))
                except OSError:
                    pass
            if not keep:
                confirm_inbox(item)
    finally:
        _inbox_lock.release()


def _check_commands(on_command=None) -> None:
    """Fetch waiting commands once, hand each to its handler, then mark it done.

    Start/Pause also update is_paused() straight away. Messages go to the
    accept_messages() handler if there is one, otherwise to on_command.
    """
    on_command = on_command or _command_handler
    try:
        commands = _send("GET", "/api/commands", LISTEN_TIMEOUT_SECONDS).json()
    except (requests.RequestException, ValueError) as error:
        _warn("check for commands", error)
        return
    if not isinstance(commands, list):
        return  # not the reply we expect (e.g. a proxy's page): ignore it
    for command in commands:
        if not isinstance(command, dict) or "id" not in command:
            continue
        name = command.get("command")
        if name in ("start", "pause"):
            _set_paused(name == "pause")
        try:
            if name == "message" and _message_handler is not None:
                _message_handler((command.get("payload") or {}).get("text", ""))
            elif on_command is not None:
                on_command(command)
        except Exception as error:  # a bug in a handler shouldn't stop the agent
            report(f"Failed to handle {name!r}: {error}", "error")
        try:
            _send("PATCH", f"/api/commands/{command['id']}", LISTEN_TIMEOUT_SECONDS, json={"status": "done"})
        except requests.RequestException as error:
            _warn(f"mark command {command['id']} done", error)


def listen(on_command, every: float | None = None) -> None:
    """Run on_command(command) when Start or Pause is pressed on the dashboard.

    command is a dict like {"id": 7, "command": "start", "payload": None, ...}.
    Commands arrive at check-ins, so the agent must check in: pass `every`
    (seconds) to start background check-ins at that rhythm (same as
    start_check_ins(every)), or call check_in() yourself, e.g. in a scheduled run.
    Messages go to the accept_messages() handler instead, if you set one.
    """
    global _command_handler
    if PROBLEM:
        print("[dashboard] Not listening for commands until the settings are fixed.")
        return
    _command_handler = on_command
    if every is not None:
        start_check_ins(every)


# Items left in the offline buffer by an earlier run are sent in the background.
if not PROBLEM and os.path.exists(_queue_path()) and pending():
    print(f"[dashboard] {pending()} item(s) waiting in the offline buffer from an earlier run; sending in the background.")
    _start_sender()
    _retry_now.set()
