"""Fill a LOCAL demo database with sample data, to try the site quickly.

Never run this against the real (Supabase) database: it refuses unless
DATABASE_URL points at a SQLite file.

Usage (from the app folder, with the same settings as the demo server):
    DATABASE_URL=sqlite:///./demo/demo.db LOCAL_FILES_DIR=./demo/files python scripts/seed_demo.py

It creates, if they don't exist yet (these are demo-only test values):
    demo-admin   admin                       password: demo-password-123
    demo-taro    student, Gen 9 (Demo Taro)   password: demo-password-123
    demo-hanako  student, Gen 10 (Demo Hanako) password: demo-password-123
a class enrolling Gen 9 and 10, an agent for Demo Taro, a few skills shared
by members and by the agent (some class-only, some seminar-wide), a skill
tree, and runs the market organizer once (keyword rules).
"""

import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SEMINAR_TESTING", "1")  # never read a real .env here

if not (os.getenv("DATABASE_URL") or "").startswith("sqlite"):
    sys.exit("Refusing: set DATABASE_URL to a local SQLite file, e.g. sqlite:///./demo/demo.db")

from sqlmodel import Session, select  # noqa: E402

import market  # noqa: E402
from db import Agent, Course, CourseEnrolledGen, Gen, Skill, User, create_db_and_tables, engine  # noqa: E402
from names import assign_guest_number  # noqa: E402
from routes.agent_tools import store_api_key  # noqa: E402
from security import hash_password  # noqa: E402

PASSWORD = "demo-password-123"
SKILLS = [
    ("Paper finder", "Find recent papers on a topic.", "seminar", None,
     "# Paper finder\n\nSearch arXiv and Google Scholar for a topic, fetch the 10 newest papers through their API, "
     "and collect title, authors, link and abstract.\n\n## Steps\n1. Build the query from the topic.\n"
     "2. Search arXiv first, then Scholar.\n3. Drop duplicates and return a list."),
    ("Method chooser", "Pick a research method with explicit criteria.", "class", None,
     "# Method chooser\n\nDecide which research method to use. Weigh these criteria: time, data available, "
     "risk, and what the question needs. Score each option 1-5 per criterion, choose the highest, and say why.\n\n"
     "Stop and ask the user when two options score within 1 point."),
    ("Weekly report writer", "Turns notes into a short report.", "seminar", "agent",
     "# Weekly report writer\n\nWrite a one-page weekly report for the seminar from the agent's notes: "
     "what was done, results, and next steps. Keep the tone plain; write for an audience of students."),
    ("CSV cleaner", "Cleans and summarises survey CSV files.", "class", "agent",
     "# CSV cleaner\n\nClean a survey CSV with pandas: parse dates, drop empty rows, fix column names, then "
     "summarize each column (counts, mean, missing) and compare groups."),
]


def user(db: Session, login_id: str, name: str, role: str, gen_number: int | None) -> User:
    existing = db.exec(select(User).where(User.login_id == login_id)).first()
    if existing:
        return existing
    gen = db.exec(select(Gen).where(Gen.number == gen_number)).first() if gen_number else None
    person = User(login_id=login_id, full_name=name, role=role, gen_id=gen.id if gen else None,
                  password_hash=hash_password(PASSWORD))
    if gen:
        assign_guest_number(db, person)
    db.add(person)
    db.commit()
    db.refresh(person)
    return person


def main() -> None:
    create_db_and_tables()
    with Session(engine) as db:
        admin = user(db, "demo-admin", "Demo Admin", "admin", None)
        taro = user(db, "demo-taro", "Demo Taro", "student", 9)
        hanako = user(db, "demo-hanako", "Demo Hanako", "student", 10)
        course = db.exec(select(Course).where(Course.title == "Demo class")).first()
        if course is None:
            today = date.today().replace(day=1)
            course = Course(title="Demo class", description="A class to try the dashboard.\nIt has all three tabs.",
                            start_month=today, end_month=today.replace(year=today.year + 1), created_by=admin.id)
            db.add(course)
            db.flush()
            for number in (9, 10):
                gen = db.exec(select(Gen).where(Gen.number == number)).one()
                db.add(CourseEnrolledGen(course_id=course.id, gen_id=gen.id))
            db.commit()
            db.refresh(course)
        agent = db.exec(select(Agent).where(Agent.owner_id == taro.id, Agent.name == "Research helper")).first()
        if agent is None:
            agent = Agent(owner_id=taro.id, course_id=course.id, name="Research helper", api_key_hash="",
                          description="Finds papers and writes weekly reports.")
            store_api_key(agent, "demo-agent-key-0123456789")  # demo only
            db.add(agent)
            db.commit()
            db.refresh(agent)
        if db.exec(select(Skill)).first() is None:
            for name, summary, share, by, content in SKILLS:
                owner = taro if by == "agent" or name == "Paper finder" else hanako
                market.save_skill(db, course=course, owner=owner, content=content, name=name, summary=summary,
                                  share=share, agent=agent if by == "agent" else None)
            market.save_tree(db, agent, "# Research helper's skills\n\n- Paper finder (shared): step 1, every Monday.\n"
                             "- CSV cleaner (shared): when the survey data arrives.\n- Weekly report writer "
                             "(shared): last step on Fridays.\n- A private 'note taker' skill (not shared) "
                             "runs all week.")
        record = market.run(db, "manual")
        print(f"Demo ready. Organizer: {record.message}")


if __name__ == "__main__":
    main()
