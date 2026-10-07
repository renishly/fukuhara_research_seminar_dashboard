"""Demo agent: shows the dashboard connector in action.

It uses the very same connector/dashboard_connector.py that "Implement
connector" lets students download, so it behaves exactly like a real agent.

What it does:
  1. Reports "Agent online" (in its Activity log).
  2. Checks in every 5 seconds, says it accepts messages (so the message box
     switches on) and Inbox files.
  3. Replies to Start, Pause and messages in handle() below: the place where
     a student's real AI logic goes.
  4. Does a small piece of "work" every 20 seconds: a "ticks" output. Press
     Pause and the work stops (wait_if_paused); outputs made just before are
     held until Start.
  5. Keeps files uploaded to its Inbox in agent/received/.

Before running it: sign in as an enrolled student, open the class's Agents
tab, click your own cell, and use the "Add Agent" card to get an API key.
Then, with the website running:
  macOS/Linux:  DASHBOARD_API_KEY=the-key python agent/agent.py
  Windows:      set DASHBOARD_API_KEY=the-key
                python agent\\agent.py
(or put DASHBOARD_API_KEY in the .env file). It talks to
http://127.0.0.1:8000 unless you set DASHBOARD_URL, e.g. to
https://your-service.onrender.com.
"""

import os
import shutil
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent.parent

# Read DASHBOARD_API_KEY (and DASHBOARD_URL) from the .env file next to
# main.py if they're there, then default to the local website. Both must
# happen before the connector is imported, because it reads them then.
load_dotenv(APP_DIR / ".env")
os.environ.setdefault("DASHBOARD_URL", "http://127.0.0.1:8000")

# Use the connector file from this project's connector/ folder.
sys.path.insert(0, str(APP_DIR / "connector"))
import dashboard_connector  # noqa: E402
from dashboard_connector import (  # noqa: E402
    accept_inbox,
    accept_messages,
    listen,
    output,
    report,
    start_check_ins,
    wait_if_paused,
)

RECEIVED_DIR = Path(__file__).resolve().parent / "received"
starts = 0  # how many times Start was pressed, sent as an output


# ===========================================================================
# YOUR AI LOGIC GOES HERE
# ---------------------------------------------------------------------------
# handle() is called for every Start, Pause and message sent from the
# dashboard. Replace the example replies with what your real agent should do
# (for example, pass a message to your LLM). Use report() for status lines
# and output(content, category) for results.
# ===========================================================================
def handle(command: dict) -> None:
    global starts
    name = command["command"]
    if name == "start":
        starts += 1
        report("Starting work.", "status")
        output(starts, "starts")
    elif name == "pause":
        report("Pausing.", "status")  # the connector holds outputs by itself while paused
    elif name == "message":
        text = (command.get("payload") or {}).get("text", "")
        report(f"You said: {text}")
        output(f"Message received: {text}", "messages")
    else:
        report(f"Unknown command: {name}", "error")


def handle_message(text: str) -> None:
    """Messages arrive here (accept_messages); pass them to handle() like the rest."""
    handle({"command": "message", "payload": {"text": text}})


def handle_inbox_file(item: dict, path: Path) -> None:
    """A file uploaded to this agent's Inbox. Keep it (a real agent might load
    it into its own database instead). Once this returns, the dashboard
    deletes its copy."""
    RECEIVED_DIR.mkdir(exist_ok=True)
    target = RECEIVED_DIR / Path(item["filename"]).name  # just the name, never another folder
    shutil.copy(path, target)
    note = f' (note: "{item["note"]}")' if item.get("note") else ""
    report(f"Stored inbox file {item['filename']} in agent/received/{note}")


def main() -> None:
    if dashboard_connector.PROBLEM:
        sys.exit("Fix the setting above, then run the agent again.")

    print(f"Agent connecting to {dashboard_connector.DASHBOARD_URL} (Ctrl+C to stop)")
    listen(handle)  # Start and Pause
    accept_messages(handle_message)  # switches on the dashboard's message box
    accept_inbox(handle_inbox_file)  # takes files uploaded to its Inbox
    # This demo runs all the time, so it checks in in the background. Every 5
    # seconds keeps Start/Pause and messages quick; a real agent might use 30-300.
    start_check_ins(every=5)
    report("Agent online", "status")
    output("https://github.com/", "links")  # an example link output

    tick = 0
    while True:
        wait_if_paused()  # a safe point: while paused, the agent waits here
        tick += 1
        output(f"Tick {tick}: did a small piece of work", "ticks")
        time.sleep(20)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAgent stopped.")
