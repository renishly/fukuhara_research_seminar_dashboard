"""The Members page (admins only): accounts, gens, and file storage use."""

from fastapi import APIRouter, Depends, Form, Request
from sqlmodel import Session, delete, select, update

from db import (
    DATABASE_LIMIT,
    Agent,
    ClassVisit,
    Course,
    CourseEnrolledUser,
    CourseHiddenUser,
    FeedFilter,
    Folder,
    Gen,
    LoginSession,
    MonitorNotifySetting,
    NotifySetting,
    Skill,
    SkillTree,
    StoredFile,
    Submission,
    Task,
    User,
    database_size,
    get_session,
    utc_now,
)
from names import assign_guest_number, placeholder, split_other_names
from routes.auth import full_name_error, other_names_error
from routes.common import delete_agent, delete_submission, not_found
from security import (
    Viewer,
    checked_admin,
    end_all_sessions,
    hash_password,
    password_matches,
    require_admin,
    temporary_password,
)
import storage
from uploads import STORAGE_TOTAL_LIMIT, total_stored
from usage import database_breakdown, file_breakdown
from web import render

router = APIRouter()


def members_page(request: Request, db: Session, viewer: Viewer, status_code: int = 200, **messages):
    """Accounts grouped by role and gen (newest gen first), plus storage use."""
    gens = list(db.exec(select(Gen).order_by(Gen.number.desc())).all())
    gen_numbers = {gen.id: gen.number for gen in gens}
    users = db.exec(select(User)).all()
    admins = sorted((u for u in users if u.role == "admin"), key=lambda u: u.full_name.casefold())
    groups = []
    for gen in gens:
        students = sorted((u for u in users if u.role == "student" and u.gen_id == gen.id),
                          key=lambda u: u.full_name.casefold())
        groups.append({"gen": gen, "students": students})
    used = total_stored(db)
    db_used = database_size(db)
    return render(request, db, viewer, "members.html", status_code=status_code, admins=admins, groups=groups,
                  gens=gens, placeholders={u.id: placeholder(u, gen_numbers.get(u.gen_id)) for u in users},
                  storage_used=used, storage_limit=STORAGE_TOTAL_LIMIT,
                  storage_percent=min(100, round(used * 100 / STORAGE_TOTAL_LIMIT)),
                  storage_status=storage.check(), database_used=db_used, database_limit=DATABASE_LIMIT,
                  database_percent=min(100, round(db_used * 100 / DATABASE_LIMIT)) if db_used is not None else None,
                  **messages)


def get_member(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise not_found("Account not found")
    return user


@router.get("/members")
def members(request: Request, viewer: Viewer = Depends(require_admin), db: Session = Depends(get_session)):
    return members_page(request, db, viewer)


@router.get("/members/usage/{part}")
def usage_breakdown(request: Request, part: str, viewer: Viewer = Depends(require_admin),
                    db: Session = Depends(get_session)):
    """What takes up file storage ("files") or the database ("database"): pie charts and tables.

    Loaded only when an admin opens a breakdown, so the Members page stays fast.
    """
    if part not in ("files", "database"):
        raise not_found()
    views = file_breakdown(db) if part == "files" else database_breakdown(db)
    return render(request, db, viewer, "partials/usage.html", part=part, views=views)


@router.post("/members/{user_id}/gen")
def change_gen(request: Request, user_id: int, gen_id: int = Form(...), viewer: Viewer = Depends(checked_admin),
               db: Session = Depends(get_session)):
    """Move a student to another gen. They get the next placeholder number of
    that gen, so guests never see two "Gen 9 Student 3"s."""
    user = get_member(db, user_id)
    gen = db.get(Gen, gen_id)
    if user.role != "student" or gen is None:
        return members_page(request, db, viewer, 400, error="Choose a gen for a student.")
    if user.gen_id != gen.id:
        user.gen_id = gen.id
        assign_guest_number(db, user)
        db.add(user)
        db.commit()
    return members_page(request, db, viewer, notice=f"{user.full_name} is now in Gen {gen.number}.")


@router.post("/members/{user_id}/name")
def change_name(request: Request, user_id: int, full_name: str = Form(""), viewer: Viewer = Depends(checked_admin),
                db: Session = Depends(get_session)):
    user = get_member(db, user_id)
    full_name = " ".join(full_name.split())
    error = full_name_error(full_name)
    if error:
        return members_page(request, db, viewer, 400, error=error)
    user.full_name = full_name
    db.add(user)
    db.commit()
    return members_page(request, db, viewer, notice=f"Name changed to {full_name}.")


@router.post("/members/{user_id}/other-names")
def change_other_names(request: Request, user_id: int, other_names: str = Form(""),
                       viewer: Viewer = Depends(checked_admin), db: Session = Depends(get_session)):
    user = get_member(db, user_id)
    error = other_names_error(other_names)
    if error:
        return members_page(request, db, viewer, 400, error=error)
    user.other_names = "\n".join(split_other_names(other_names)) or None
    db.add(user)
    db.commit()
    return members_page(request, db, viewer, notice=f"Other names of {user.full_name} saved.")


@router.post("/members/{user_id}/reset-password")
def reset_password(request: Request, user_id: int, viewer: Viewer = Depends(checked_admin),
                   db: Session = Depends(get_session)):
    """Give a member a new temporary password (shown once) and sign them out everywhere."""
    user = get_member(db, user_id)
    new_password = temporary_password()
    user.password_hash = hash_password(new_password)
    user.credentials_changed_at = utc_now()
    db.add(user)
    db.commit()
    end_all_sessions(db, user.id)
    return members_page(request, db, viewer, reset_for=user, temporary=new_password)


@router.post("/members/{user_id}/delete")
def delete_member(request: Request, user_id: int, password: str = Form(""), viewer: Viewer = Depends(checked_admin),
                  db: Session = Depends(get_session)):
    """Delete an account with its submissions, agents and their data. Needs the admin's password."""
    user = get_member(db, user_id)
    if user.id == viewer.id:
        return members_page(request, db, viewer, 400, error="You can't delete your own account here.")
    if not password_matches(password, viewer.user.password_hash):
        return members_page(request, db, viewer, 400, error="Your password is incorrect. Nothing was deleted.")
    name = user.full_name
    for submission in db.exec(select(Submission).where(Submission.student_id == user.id)).all():
        delete_submission(db, submission)
    db.exec(delete(Skill).where(Skill.owner_id == user.id))
    for agent in db.exec(select(Agent).where(Agent.owner_id == user.id)).all():
        delete_agent(db, agent)
    db.exec(delete(SkillTree).where(SkillTree.owner_id == user.id))
    for model, column in ((Folder, Folder.owner_id), (NotifySetting, NotifySetting.user_id),
                          (MonitorNotifySetting, MonitorNotifySetting.user_id),
                          (FeedFilter, FeedFilter.user_id),
                          (LoginSession, LoginSession.user_id), (ClassVisit, ClassVisit.user_id),
                          (CourseEnrolledUser, CourseEnrolledUser.user_id),
                          (CourseHiddenUser, CourseHiddenUser.user_id)):
        db.exec(delete(model).where(column == user.id))
    # Classes, assignments and files this person posted stay; they just lose the name.
    db.exec(update(Course).where(Course.created_by == user.id).values(created_by=None))
    db.exec(update(Task).where(Task.created_by == user.id).values(created_by=None))
    db.exec(update(StoredFile).where(StoredFile.uploaded_by == user.id).values(uploaded_by=None))
    db.delete(user)
    db.commit()
    return members_page(request, db, viewer, notice=f"Deleted the account of {name}.")


@router.post("/gens")
def add_gen(request: Request, viewer: Viewer = Depends(checked_admin), db: Session = Depends(get_session)):
    """Add the next gen (e.g. Gen 13 after Gen 12)."""
    numbers = db.exec(select(Gen.number)).all()
    gen = Gen(number=max(numbers, default=0) + 1)
    db.add(gen)
    db.commit()
    return members_page(request, db, viewer, notice=f"Added Gen {gen.number}.")
