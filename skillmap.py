"""Hoang Long's class skill map: which skills each student has built, by the
class's categories, so gaps show across the class and between two students.

A student's skills are the ones credited to them in the class (shared by
them or by their agents). Each skill counts as
  live     its agent is live right now (it posts to the dashboard),
  built    on the market, but its agent isn't live (or it has no agent),
  planned  marked "status: planned" in its SKILL.md (not built yet).
A category with nothing built or live is a gap.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from sqlmodel import Session, func, select

import market
from db import Agent, Course, Skill, SkillCategory, SkillVote, User, as_utc, utc_now
from routes.agent_tools import states_for
from routes.common import student_groups
from security import Viewer

STATUSES = ("live", "built", "planned")
FILTERS = {"all": "All students", "gap": "Has an empty category", "silent": "Not posting yet",
           "new": "New skills this week"}


@dataclass
class Cell:
    live: list[Skill] = field(default_factory=list)
    built: list[Skill] = field(default_factory=list)
    planned: list[Skill] = field(default_factory=list)

    @property
    def count(self) -> int:  # built + live: what the student actually has
        return len(self.live) + len(self.built)

    @property
    def skills(self) -> list[Skill]:
        return self.live + self.built + self.planned


@dataclass
class Row:
    student: object
    cells: dict[int, Cell]  # category number -> cell
    new_this_week: int = 0

    @property
    def total(self) -> int:
        return sum(cell.count for cell in self.cells.values())

    @property
    def live(self) -> int:
        return sum(len(cell.live) for cell in self.cells.values())

    @property
    def gaps(self) -> list[int]:
        return [number for number, cell in self.cells.items() if cell.count == 0]


def status_of(skill: Skill, live_agents: set[int]) -> str:
    if skill.planned:
        return "planned"
    return "live" if skill.agent_id in live_agents else "built"


def build(db: Session, viewer: Viewer, course: Course) -> dict:
    """Everything the map shows, for this class."""
    rows_by_number = market.fixed_categories(db)
    number_of_id = {row.id: number for number, row in rows_by_number.items()}
    agents = list(db.exec(select(Agent).where(Agent.course_id == course.id)).all())
    states = states_for(db, agents)
    live_agents = {agent.id for agent in agents if states[agent.id].key == "live"}
    skills = list(db.exec(select(Skill).where(Skill.course_id == course.id, Skill.builtin.is_(None))).all())
    week_ago = utc_now() - timedelta(days=7)
    rows = []
    for group in student_groups(db, viewer, course):
        for cell in group.cells:
            student = cell["student"]
            cells = {number: Cell() for number in rows_by_number}
            new = 0
            for skill in skills:
                if skill.owner_id != student.id:
                    continue
                number = number_of_id.get(skill.category_id)
                if number is None:
                    continue
                getattr(cells[number], status_of(skill, live_agents)).append(skill)
                if as_utc(skill.created_at) >= week_ago and not skill.planned:
                    new += 1
            rows.append(Row(student=student, cells=cells, new_this_week=new))
    categories = [(number, rows_by_number[number]) for number in sorted(rows_by_number)]
    nothing = {number: sum(1 for row in rows if row.cells[number].count == 0) for number in rows_by_number}
    built_or_live = sum(row.total for row in rows)
    live = sum(row.live for row in rows)
    biggest_gap = max(nothing, key=lambda n: (nothing[n], -n)) if rows else None
    counts = {"all": len(rows), "gap": sum(1 for r in rows if r.gaps),
              "silent": sum(1 for r in rows if r.total and not r.live),
              "new": sum(1 for r in rows if r.new_this_week)}
    return {"rows": rows, "categories": categories, "nothing": nothing, "built_or_live": built_or_live,
            "live_share": round(100 * live / built_or_live) if built_or_live else None,
            "biggest_gap": rows_by_number[biggest_gap] if biggest_gap is not None and nothing[biggest_gap] else None,
            "biggest_gap_count": nothing[biggest_gap] if biggest_gap is not None else 0,
            "filter_counts": counts, "max_count": max([c.count for r in rows for c in r.cells.values()] + [1])}


def keep(row: Row, chosen: str) -> bool:
    if chosen == "gap":
        return bool(row.gaps)
    if chosen == "silent":
        return bool(row.total and not row.live)
    if chosen == "new":
        return row.new_this_week > 0
    return True


def compare(a: Row, b: Row, categories: list[tuple[int, SkillCategory]]) -> list[dict]:
    """Two students side by side, per category, with what it means."""
    lines = []
    for number, category in categories:
        x, y = a.cells[number].count, b.cells[number].count
        if x and y:
            verdict = "Both covered"
        elif x:
            verdict = f"{b.student.full_name} has nothing here: {a.student.full_name} could share"
        elif y:
            verdict = f"{a.student.full_name} has nothing here: {b.student.full_name} could share"
        else:
            verdict = "Gap for both: a joint build?"
        lines.append({"number": number, "category": category, "a": x, "b": y, "verdict": verdict,
                      "both_gap": not x and not y})
    return lines


# ---------------------------------------------------------------------------
# The graph (the Skill map's picture): category nodes on one path, skill
# nodes inside them on "inner connections", miscellaneous skills outside.
# Positions are worked out here; the page draws them as SVG.
# ---------------------------------------------------------------------------

import math  # noqa: E402
import random  # noqa: E402

import skill_sorter  # noqa: E402

SKILL_R = 11  # a skill node's radius (score 0)
STEP = 42  # space between skill nodes along an inner connection
LANE_GAP = 38  # space between two inner connections in a category node
MIN_R = 46  # an empty category node
PAD = 22
GAP = 46  # between two category nodes
TOP = 110  # room above and below the path for miscellaneous skills


def scores(db: Session, skill_ids: list[int]) -> dict[int, int]:
    """Each skill's vote total (ups minus downs)."""
    if not skill_ids:
        return {}
    found = db.exec(select(SkillVote.skill_id, func.sum(SkillVote.value))
                    .where(SkillVote.skill_id.in_(skill_ids)).group_by(SkillVote.skill_id)).all()
    return {skill_id: int(total or 0) for skill_id, total in found}


def node_radius(score: int) -> float:
    """Votes change a skill node's size: bigger with a positive total, smaller
    with a negative one (between 6 and 18)."""
    return max(6.0, min(18.0, SKILL_R + math.copysign(2.4 * math.sqrt(abs(score)), score)))


def initials(name: str) -> str:
    parts = [p for p in (name or "?").replace("・", " ").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def dependencies(skills: list[Skill]) -> dict[int, set[int]]:
    """Which skills each skill reads first (its frontmatter's depends_on, or
    naming another skill of the same owner in its text), as skill ids."""
    by_owner: dict[int | None, dict[str, Skill]] = {}
    for skill in skills:
        by_owner.setdefault(skill.owner_id, {})[skill.name] = skill
    needs: dict[int, set[int]] = {skill.id: set() for skill in skills}
    for skill in skills:
        names = by_owner.get(skill.owner_id, {})
        meta, body = skill_sorter.parse_frontmatter(skill.content or "")
        for name in skill_sorter.find_dependencies(body, meta, set(names)):
            other = names.get(name)
            if other is not None and other.id != skill.id:
                needs[skill.id].add(other.id)
    return needs


def ordered(lane: list[Skill], needs: dict[int, set[int]]) -> list[Skill]:
    """A lane's skills with each one after the skills it reads first (by name otherwise)."""
    left = sorted(lane, key=lambda s: s.name.lower())
    done: list[Skill] = []
    while left:
        ready = next((s for s in left if not (needs[s.id] & {x.id for x in left})), left[0])
        done.append(ready)
        left.remove(ready)
    return done


def graph(db: Session, viewer: Viewer, course: Course, layout: str = "") -> dict:
    """The picture. `layout` seeds the randomness: a new one each page load,
    kept while the same page goes back and forth (category view, compare)."""
    rows = market.fixed_categories(db)
    number_of_id = {row.id: number for number, row in rows.items()}
    agents = {a.id: a for a in db.exec(select(Agent).where(Agent.course_id == course.id)).all()}
    states = states_for(db, list(agents.values()))
    live_agents = {i for i, a in agents.items() if states[i].key == "live"}
    skills = list(db.exec(select(Skill).where(Skill.course_id == course.id, Skill.builtin.is_(None))).all())
    users = {u.id: u for u in db.exec(select(User)).all()}
    needs = dependencies(skills)
    votes = scores(db, [s.id for s in skills])
    numbers = sorted(rows)

    # Lanes: one inner connection per agent (or per member, for skills they shared themselves).
    lanes: dict[int, dict[tuple, list[Skill]]] = {n: {} for n in numbers}
    misc: list[Skill] = []
    for skill in skills:
        number = number_of_id.get(skill.category_id)
        if number is None:
            misc.append(skill)
            continue
        lanes[number].setdefault((skill.agent_id or 0, skill.owner_id), []).append(skill)

    nodes, categories, x, height_r = {}, [], 0.0, MIN_R
    for number in numbers:
        lane_list = [ordered(lanes[number][key], needs) for key in sorted(lanes[number], key=str)]
        count = sum(len(lane) for lane in lane_list)
        longest = max([len(lane) for lane in lane_list] + [0])
        inner_w = (longest + 1) * STEP
        inner_h = max(0, len(lane_list) - 1) * LANE_GAP + 2 * SKILL_R
        fit = math.hypot(inner_w / 2, inner_h / 2) + PAD if count else MIN_R
        r = max(MIN_R, fit, MIN_R + 9 * math.sqrt(count))  # grows with its share of skills
        cx = x + r
        categories.append({"number": number, "row": rows[number], "count": count, "cx": cx, "r": r,
                           "lanes": [], "skills": []})
        for j, lane in enumerate(lane_list):
            y_off = (j - (len(lane_list) - 1) / 2) * LANE_GAP
            span = (len(lane) + 1) * STEP
            points = []
            for k, skill in enumerate(lane):
                sx = cx - span / 2 + (k + 1) * STEP
                nodes[skill.id] = {"skill": skill, "x": sx, "y_off": y_off, "category": number}
                points.append(skill.id)
            categories[-1]["lanes"].append({"ids": points, "y_off": y_off})
        x = cx + r + GAP * (1.0 + 0.8 * wobble(layout, "gap", number))  # uneven spacing
        height_r = max(height_r, r)
    width = x - GAP

    # Miscellaneous skills: near the skills they work with (same agent, or a dependency).
    placed_misc, unplaced = [], []
    for n, skill in enumerate(misc):
        partners = [nodes[i] for i in nodes
                    if (nodes[i]["skill"].agent_id and nodes[i]["skill"].agent_id == skill.agent_id)
                    or i in needs[skill.id] or skill.id in needs[i]]
        if partners:
            px = sum(p["x"] for p in partners) / len(partners)
            category = next(c for c in categories if c["number"] == partners[0]["category"])
            side = -1 if n % 2 == 0 else 1
            placed_misc.append({"skill": skill, "x": px + (n % 3 - 1) * 26, "side": side,
                                "category": category["number"], "partners": [p["skill"].id for p in partners]})
        else:
            unplaced.append(skill)
    jitter = min(110.0, 0.75 * height_r)  # how far category nodes drift off a straight line
    base_cy = TOP + height_r + jitter + 10
    total_h = base_cy + height_r + jitter + TOP + 30

    def node(skill: Skill, x: float, y: float) -> dict:
        owner = users.get(skill.owner_id)
        status = status_of(skill, live_agents)
        agent = agents.get(skill.agent_id)
        score = votes.get(skill.id, 0)
        return {"id": skill.id, "x": round(x, 1), "y": round(y, 1), "status": status, "name": skill.name,
                "score": score, "r": round(node_radius(score), 1),
                "owner_id": skill.owner_id, "owner": owner.full_name if owner else "a former member",
                "initials": initials(owner.full_name if owner else "?"), "agent": agent.name if agent else "",
                "category": number_of_id.get(skill.category_id)}

    drawn, paths = {}, []
    for category in categories:
        cy = base_cy + wobble(layout, "category", category["number"]) * jitter
        left, right = (category["cx"] - category["r"], cy), (category["cx"] + category["r"], cy)
        category["left"], category["right"], category["cy"] = left, right, cy
        for lane in category["lanes"]:
            points = [left]
            for skill_id in lane["ids"]:
                spot = nodes[skill_id]
                drawn[skill_id] = node(spot["skill"], spot["x"], cy + lane["y_off"])
                # A little randomness, but always inside its category node.
                # Scattered over much of the circle (x stays within its step, so the path still runs left to right).
                spread = max(category["r"] - drawn[skill_id]["r"] - 6, 0)
                x, y = inside(spot["x"] + wobble(layout, "x", skill_id) * STEP * 0.45,
                              cy + lane["y_off"] + wobble(layout, "y", skill_id) * spread * 0.75,
                              category["cx"], cy, category["r"] - drawn[skill_id]["r"] - 6)
                drawn[skill_id]["x"], drawn[skill_id]["y"] = round(x, 1), round(y, 1)
                points.append((drawn[skill_id]["x"], drawn[skill_id]["y"]))
                category["skills"].append(drawn[skill_id])
            points.append(right)
            paths.append({"category": category["number"], "points": points, "ids": list(lane["ids"])})
    misc_nodes, misc_edges = [], []
    for item in placed_misc:
        home = next(c for c in categories if c["number"] == item["category"])
        spot = node(item["skill"], 0, 0)
        x = item["x"] + wobble(layout, "mx", item["skill"].id) * 45
        y = home["cy"] + item["side"] * (home["r"] + spot["r"] + 26 + abs(wobble(layout, "my", item["skill"].id)) * 45)
        x, y = outside(x, y, spot["r"], categories, total_h)
        spot["x"], spot["y"] = round(x, 1), round(y, 1)
        misc_nodes.append(spot)
        for partner in item["partners"]:
            if partner in drawn:
                misc_edges.append({"a": spot["id"], "b": partner, "x1": spot["x"], "y1": spot["y"],
                                   "x2": drawn[partner]["x"], "y2": drawn[partner]["y"]})
    main = [{"from": a["number"], "to": b["number"], "x1": a["right"][0], "y1": a["cy"], "x2": b["left"][0],
             "y2": b["cy"]} for a, b in zip(categories, categories[1:])]
    following = steps(categories, [(m["id"], item["partners"]) for m, item in zip(misc_nodes, placed_misc)])
    owners_list = sorted({(d["owner_id"], d["owner"]) for d in list(drawn.values()) + misc_nodes}, key=lambda o: o[1])
    return {"categories": categories, "paths": paths, "main": main, "misc": misc_nodes, "misc_edges": misc_edges,
            "unplaced": [node(s, 0, 0) for s in unplaced], "width": round(width + 20), "height": round(total_h),
            "owners": owners_list, "next": following}


def steps(categories: list[dict], misc: list[tuple[int, list[int]]]) -> dict[int, list[int]]:
    """Where a drawn path may go from each skill, following the graph's lines
    left to right: the next skill on the same inner connection; from the end
    of one, out along the main path (past any categories, by their bypass) to
    the start of an inner connection in a later category; and between a
    miscellaneous skill and the skills its dotted lines join."""
    following: dict[int, list[int]] = {}
    for i, category in enumerate(categories):
        later_starts = [lane["ids"][0] for later in categories[i + 1:] for lane in later["lanes"] if lane["ids"]]
        for lane in category["lanes"]:
            ids = lane["ids"]
            for k, skill_id in enumerate(ids):
                following[skill_id] = [ids[k + 1]] if k + 1 < len(ids) else list(later_starts)
    for misc_id, partners in misc:
        following.setdefault(misc_id, [])
        for partner in partners:
            following.setdefault(partner, []).append(misc_id)
            following[misc_id].append(partner)
    return following


def traversable(following: dict[int, list[int]], path: list[int]) -> bool:
    """True if each skill in the path can be reached from the one before."""
    return all(b in following.get(a, []) for a, b in zip(path, path[1:]))


def wobble(layout: str, kind: str, key) -> float:
    """A "random" number from -1 to 1 for this thing in this layout (the same
    while the page shows the same layout, different on the next page load)."""
    return random.Random(f"{layout}:{kind}:{key}").uniform(-1, 1)


def inside(x: float, y: float, cx: float, cy: float, room: float) -> tuple[float, float]:
    """(x, y) moved toward the centre, if needed, to stay within `room` of it."""
    dx, dy = x - cx, y - cy
    distance = math.hypot(dx, dy)
    if distance <= max(room, 0) or distance == 0:
        return x, y
    scale = max(room, 0) / distance
    return cx + dx * scale, cy + dy * scale


def outside(x: float, y: float, r: float, categories: list[dict], height: float) -> tuple[float, float]:
    """(x, y) moved up or down, if needed, so a node of radius r is clear of
    every category node (miscellaneous skills never sit inside one)."""
    for _ in range(12):
        clash = next((c for c in categories if math.hypot(x - c["cx"], y - c["cy"]) < c["r"] + r + 8), None)
        if clash is None:
            break
        reach = math.sqrt(max((clash["r"] + r + 10) ** 2 - (x - clash["cx"]) ** 2, 0))
        above, below = clash["cy"] - reach, clash["cy"] + reach
        if y <= clash["cy"] and above - r > 4:
            y = above
        elif below + r < height - 4:
            y = below
        else:
            y = above
    return x, y


def smooth(points: list[tuple[float, float]]) -> str:
    """An SVG path through the points with gentle curves."""
    if not points:
        return ""
    d = f"M{points[0][0]:.1f},{points[0][1]:.1f}"
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        mid = (x0 + x1) / 2
        d += f" C{mid:.1f},{y0:.1f} {mid:.1f},{y1:.1f} {x1:.1f},{y1:.1f}"
    return d
