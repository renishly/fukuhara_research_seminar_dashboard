"""Classes: who can see them, who can manage them, the timeline and the class page."""

import html
import re
from datetime import date, datetime, timezone

from sqlmodel import Session, select

from conftest import class_form
from db import Course, CourseEnrolledGen
from permissions import course_name, timespan_label
from timeline import DAY_MARGIN, MIN_BOX_WIDTH, ZOOM_LEVELS, assign_lanes, build_timeline
from web import jst, parse_jst_input, relative_time


def visible(person, course_id) -> bool:
    return person.get(f"/classes/{course_id}").status_code == 200


# --- Who can see a class ------------------------------------------------------------


def test_class_visibility_for_every_kind_of_viewer(engine, admin, make_person, create_class):
    enrolled = make_person("student", "enrolled", gen=9)
    by_gen = make_person("student", "hiddengen", gen=8)
    by_name = make_person("student", "hiddenname", gen=7)
    both = make_person("student", "both", gen=6)  # enrolled by name AND hidden by gen
    other = make_person("student", "other", gen=5)
    guest = make_person("guest")
    course = create_class(enrolled_gens=[9], enrolled_users=[both.id], hidden_gens=[8, 6],
                          hidden_users=[by_name.id])
    assert visible(admin, course)
    assert visible(enrolled, course)
    assert not visible(by_gen, course)
    assert not visible(by_name, course)
    assert visible(both, course)  # enrolment wins over hiding
    assert visible(other, course)  # nothing hidden by default
    assert visible(guest, course)  # not hidden from guests

    secret = create_class(title="Secret", hidden_from_guests=True)
    assert not visible(guest, secret)
    assert visible(other, secret)
    # Hidden classes are also left off the timeline.
    assert "Secret" not in guest.get("/classes").text
    assert "Secret" in other.get("/classes").text
    assert "Research Methods" not in by_gen.get("/classes").text


def test_gen_enrolment_includes_students_who_register_later(engine, admin, make_person, create_class):
    course = create_class(enrolled_gens=[10], hidden_gens=[10])
    late = make_person("student", "latecomer", gen=10)
    assert visible(late, course)
    window = late.get(f"/classes/{course}?tab=assignments").text
    assert "latecomer" not in window  # Login IDs aren't shown...
    assert "Student Person" in window  # ...but the new student's cell is


def test_hidden_class_looks_like_a_missing_one(make_person, create_class):
    hidden_from = make_person("student", "hidden", gen=8)
    course = create_class(hidden_gens=[8])
    assert hidden_from.get(f"/classes/{course}").status_code == 404
    assert hidden_from.get(f"/classes/{course}/assignments").status_code == 404


# --- Managing classes -----------------------------------------------------------------


def test_only_admins_create_edit_and_delete_classes(engine, admin, make_person, create_class):
    student = make_person("student", "taro")
    guest = make_person("guest")
    form = class_form(engine, title="Sneaky")
    assert student.post("/classes", data=form).status_code == 403
    assert guest.post("/classes", data=form).status_code == 403
    assert student.get("/classes/new").status_code == 403

    course = create_class(title="Real")
    assert student.post(f"/classes/{course}/edit", data=form).status_code == 403
    assert student.post(f"/classes/{course}/delete", data={"password": "x"}).status_code == 403
    assert admin.post(f"/classes/{course}/edit", data=class_form(engine, title="Renamed")).status_code == 200
    with Session(engine) as db:
        assert db.get(Course, course).title == "Renamed"

    wrong = admin.post(f"/classes/{course}/delete", data={"password": "wrong-password"})
    assert "incorrect" in wrong.text
    from conftest import PASSWORD
    assert admin.post(f"/classes/{course}/delete", data={"password": PASSWORD}).headers["HX-Redirect"] == "/classes"
    with Session(engine) as db:
        assert db.get(Course, course) is None
        assert db.exec(select(CourseEnrolledGen).where(CourseEnrolledGen.course_id == course)).all() == []


def test_class_form_checks_its_fields(engine, admin):
    no_description = admin.post("/classes", data=class_form(engine, description="  "))
    assert "description is required" in no_description.text and "HX-Redirect" not in no_description.headers
    backwards = admin.post("/classes", data=class_form(engine, start=(9, 2026), end=(4, 2026)))
    assert "can&#39;t end before it starts" in backwards.text


def test_automatic_label_when_there_is_no_title(engine, admin, create_class):
    course_id = create_class(title="", start=(4, 2026), end=(9, 2026))
    with Session(engine) as db:
        course = db.get(Course, course_id)
        assert course.title is None
        assert course_name(course) == "Apr 2026 – Sep 2026"
    one_month = Course(description="x", start_month=date(2026, 4, 1), end_month=date(2026, 4, 1))
    assert timespan_label(one_month) == "Apr 2026"
    assert "Apr 2026 – Sep 2026" in admin.get(f"/classes/{course_id}").text


def test_class_page_shows_info_tabs_and_enrolment_summary(engine, admin, make_person, create_class):
    extra = make_person("student", "extra", gen=3)
    course = create_class(enrolled_gens=[9, 10], enrolled_users=[extra.id])
    page = admin.get(f"/classes/{course}").text
    assert "Gen 9, Gen 10 + 1 student" in page
    for tab in ("Assignments", "Exams", "Agents"):
        assert tab in page
    assert "Edit" in page and "Delete" in page
    assert admin.get(f"/classes/{course}?tab=exams").status_code == 200
    fragment = admin.get(f"/classes/{course}/tab/agents").text
    assert 'id="tab-area"' in fragment and "<html" not in fragment
    student_view = extra.get(f"/classes/{course}").text
    assert "Delete permanently" not in student_view


# --- The timeline --------------------------------------------------------------------


def test_overlapping_classes_never_share_a_lane():
    lanes = assign_lanes([("a", 0, 100), ("b", 50, 150)])
    assert lanes["a"] != lanes["b"]
    assert {lanes["a"], lanes["b"]} == {1, -1}  # above, then below the axis


def test_three_or_more_overlapping_classes_add_lanes():
    lanes = assign_lanes([(k, 0, 100) for k in "abcde"])
    assert sorted(lanes.values()) == [-2, -1, 1, 2, 3]
    assert len(set(lanes.values())) == 5


def test_non_overlapping_classes_reuse_lanes():
    lanes = assign_lanes([("a", 0, 100), ("b", 100, 200), ("c", 250, 300)])
    assert lanes == {"a": 1, "b": 1, "c": 1}


def test_timeline_boxes_never_overlap_at_any_zoom():
    spans = [(i, date(2026, 1 + i % 6, 1), date(2026, 6 + i % 6, 1)) for i in range(8)]
    spans.append((99, date(2026, 3, 1), date(2026, 3, 1)))  # a one-month class: minimum width
    for zoom in range(len(ZOOM_LEVELS)):
        layout = build_timeline(spans, date(2026, 10, 8), zoom)
        for first in layout.boxes:
            assert first.width >= MIN_BOX_WIDTH
            for second in layout.boxes:
                if first is second or first.lane != second.lane:
                    continue
                assert first.left + first.width <= second.left or second.left + second.width <= first.left


def test_timeline_reaches_back_to_the_oldest_class_and_three_years_forward():
    today = date(2026, 10, 8)
    empty = build_timeline([], today)
    assert (empty.months[0].label, empty.months[0].year) == ("Oct", 2025)  # 12 months back
    assert empty.months[-1].label == "Oct" and len(empty.months) == 12 + 36 + 1  # Oct 2029
    old = build_timeline([("old", date(2020, 1, 1), date(2020, 3, 1))], today)
    assert (old.months[0].label, old.months[0].year) == ("Jan", 2020)  # the oldest class
    long = build_timeline([("long", date(2026, 4, 1), date(2031, 6, 1))], today)
    assert long.months[-1].label == "Jun"  # a class ending after 3 years is never cut off
    assert 0 < empty.today_x < empty.width


def test_a_tick_on_every_friday_on_its_exact_date():
    layout = build_timeline([], date(2026, 10, 8), zoom=2)
    fridays = [mark.day for mark in layout.fridays]
    assert all(day.weekday() == 4 for day in fridays)  # all Fridays...
    assert all((b - a).days == 7 for a, b in zip(fridays, fridays[1:]))  # ...and none missing
    per_month = {}
    for day in fridays:
        per_month[(day.year, day.month)] = per_month.get((day.year, day.month), 0) + 1
    assert set(per_month.values()) <= {4, 5} and len(per_month) == len(layout.months)
    # Fri 2026-10-09 sits 8/31 of the way through October (inside the month's margins).
    october = next(m.left for m in layout.months if m.label == "Oct" and m.left > 0 and m.year is None)
    tick = next(mark for mark in layout.fridays if mark.day == date(2026, 10, 9))
    inner = layout.px_per_month - 2 * DAY_MARGIN
    assert abs(tick.left - (october + DAY_MARGIN + 8 / 31 * inner)) < 0.001
    # Every tick keeps clear of its month's borders, even on the 1st and the 31st.
    for mark in layout.fridays:
        month_left = next(m.left for m in reversed(layout.months) if m.left <= mark.left)
        assert month_left + DAY_MARGIN <= mark.left <= month_left + layout.px_per_month - DAY_MARGIN


def test_timeline_page_and_zoom(admin, create_class):
    create_class(title="Spring <b>Seminar</b>")
    page = admin.get("/classes").text
    # The "today" line says which day and time it is in Japan, e.g. "Thu 8 Oct 2026 08:31 JST".
    assert re.search(r">(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{1,2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2} JST<", page)
    assert "Spring &lt;b&gt;Seminar&lt;/b&gt;" in page
    zoomed = admin.get("/classes/timeline?zoom=3&center=24321.5")
    assert zoomed.status_code == 200 and 'hx-swap-oob="true"' in zoomed.text and "Zoom 4/4" in zoomed.text


def test_enrolled_and_ended_badges_on_the_timeline(make_person, create_class):
    student = make_person("student", "taro", gen=9)
    create_class(title="Mine", enrolled_gens=[9])
    create_class(title="Long ago", start=(1, 2025), end=(3, 2025))
    page = student.get("/classes").text
    assert "Enrolled" in page and "Ended" in page


# --- Times ------------------------------------------------------------------------------


def test_times_are_shown_in_jst():
    moment = datetime(2026, 10, 7, 22, 10, tzinfo=timezone.utc)
    assert jst(moment) == "2026-10-08 07:10 JST"
    assert jst(moment.replace(tzinfo=None)) == "2026-10-08 07:10 JST"  # SQLite forgets the zone
    assert parse_jst_input("2026-10-08T07:10") == moment


def test_relative_times():
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    assert relative_time(datetime(2026, 10, 10, tzinfo=timezone.utc), now) == "in 2 days"
    assert relative_time(datetime(2026, 10, 7, 21, tzinfo=timezone.utc), now) == "3 hours ago"
    assert relative_time(now, now) == "just now"


def test_html_in_class_text_is_escaped(admin, create_class):
    course = create_class(title="<script>alert(1)</script>", description="<img src=x onerror=alert(1)>")
    page = admin.get(f"/classes/{course}").text
    assert "<script>alert(1)</script>" not in page and "<img src=x" not in page
    assert html.escape("<script>alert(1)</script>") in page
