"""Laying out the classes timeline: pure Python, no database, no browser code.

The timeline is a horizontal axis of months. Each class is a box placed in a
"lane" above or below the axis, as wide as its months on the axis (or a
minimum readable width, whichever is wider). Lanes are numbered outwards:

        lane +2   ...
        lane +1   [ box ][ box ]
        ======== axis (months) ========
        lane -1      [  box  ]
        lane -2   ...

Classes are placed in start order, each into the nearest free lane, trying
+1, -1, +2, -2, ... in turn. "Free" means no box already in that lane overlaps
this box's footprint. So boxes never overlap, and when many classes run at
the same time, more lanes are added and the timeline grows taller.

The axis also has a tick on every Friday, the day the seminar meets, placed
on its exact date (so 4 or 5 ticks a month).

Everything is measured in pixels, so the page only has to place each box
where this module says (with CSS "left" and "top").
"""

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta

# Zoom levels: how many pixels one month takes. Index 1 is the default.
ZOOM_LEVELS = [60, 130, 210, 320]
DEFAULT_ZOOM = 1

MIN_BOX_WIDTH = 200  # a box is never narrower than this, so its text stays readable
BOX_GAP_X = 12  # space between two boxes side by side in one lane
LANE_HEIGHT = 124  # one lane: a box (BOX_HEIGHT) plus space between lanes
BOX_HEIGHT = 108
AXIS_HEIGHT = 56  # the band with the month names and the Friday ticks
PADDING = 16  # space above the top lane and below the bottom lane
MONTHS_BACK = 12  # the timeline starts 12 months before today, or at the oldest class if that's earlier
MONTHS_FORWARD = 36  # ...and reaches 3 years after today (or the end of a class that goes further)
DAY_MARGIN = 10  # days sit this far inside their month, so dates near the 1st or 31st clear the month border
SEMINAR_WEEKDAY = calendar.FRIDAY  # the seminar meets on Fridays: one tick per Friday


def month_number(day: date) -> int:
    """Months counted from year 0, so months can be subtracted: Jan 2026 - Dec 2025 = 1."""
    return day.year * 12 + day.month - 1


def month_start(number: int) -> date:
    """The first day of a month, from month_number()."""
    return date(number // 12, number % 12 + 1, 1)


def lane_order():
    """+1, -1, +2, -2, +3, ... : the order lanes are tried in."""
    lane = 1
    while True:
        yield lane
        yield -lane
        lane += 1


def assign_lanes(footprints: list[tuple[object, float, float]]) -> dict[object, int]:
    """Give each item a lane so that items in the same lane never overlap.

    footprints: (key, left, right) for each item, in pixels. Items are placed
    in order of their left edge (then right edge), each in the first lane of
    lane_order() whose items all end at or before this item's left edge.
    Returns {key: lane}.
    """
    lane_ends: dict[int, float] = {}  # lane -> right edge of the last item placed in it
    lanes = {}
    for key, left, right in sorted(footprints, key=lambda item: (item[1], item[2])):
        for lane in lane_order():
            if lane_ends.get(lane, float("-inf")) <= left:
                lanes[key] = lane
                lane_ends[lane] = right
                break
    return lanes


@dataclass
class Box:
    """One class on the timeline, in pixels."""

    key: object  # the class's id
    left: float  # where the box starts (its first month)
    width: float  # the box's width (at least MIN_BOX_WIDTH)
    span_width: float  # how far its months reach on the axis
    lane: int  # +1, -1, +2, ...
    top: float = 0  # the box's top edge
    connector_top: float = 0  # the dashed lines joining the box to the axis
    connector_height: float = 0

    @property
    def above(self) -> bool:
        return self.lane > 0


@dataclass
class FridayMark:
    """One Friday (a seminar day) on the axis."""

    left: float
    day: date


@dataclass
class MonthMark:
    left: float
    label: str  # "Apr"
    year: int | None  # set on January (and the first month), to show the year


@dataclass
class Timeline:
    zoom: int
    px_per_month: int
    first_month: int  # month_number() of the left edge
    width: float
    height: float
    axis_top: float
    today_x: float
    center_x: float  # the position the page should scroll to the middle of the screen
    months: list[MonthMark] = field(default_factory=list)
    fridays: list[FridayMark] = field(default_factory=list)
    boxes: list[Box] = field(default_factory=list)


def position_of(day_or_month: float, first_month: int, px_per_month: int) -> float:
    return (day_or_month - first_month) * px_per_month


def fraction_of_month(day: date) -> float:
    """How far into its month a day is: the 1st is 0, the 16th of a 30-day month is 0.5."""
    days_in_month = calendar.monthrange(day.year, day.month)[1]
    return (day.day - 1) / days_in_month


def day_position(day: date, first_month: int, px_per_month: int) -> float:
    """Where a date sits on the axis: inside its month, keeping DAY_MARGIN pixels
    clear of the month's borders on both sides. Today's line and the Friday
    ticks both use this, so they always agree."""
    month_left = position_of(month_number(day), first_month, px_per_month)
    return month_left + DAY_MARGIN + fraction_of_month(day) * (px_per_month - 2 * DAY_MARGIN)


def fridays_between(first_month: int, last_month: int) -> list[date]:
    """Every Friday from the start of first_month to the end of last_month."""
    day = month_start(first_month)
    day += timedelta(days=(SEMINAR_WEEKDAY - day.weekday()) % 7)  # the first Friday
    end = month_start(last_month + 1)
    fridays = []
    while day < end:
        fridays.append(day)
        day += timedelta(days=7)
    return fridays


def build_timeline(
    spans: list[tuple[object, date, date]],
    today: date,
    zoom: int = DEFAULT_ZOOM,
    center: float | None = None,
) -> Timeline:
    """Lay out classes on the timeline.

    spans:  (key, start month, end month) for each class (end month included).
    today:  today's date in Japan time; the "Today (JST)" line goes here.
    zoom:   an index into ZOOM_LEVELS.
    center: a fractional month number to keep in the middle of the screen
            (when zooming); None means today.
    """
    zoom = min(max(zoom, 0), len(ZOOM_LEVELS) - 1)
    ppm = ZOOM_LEVELS[zoom]
    today_month = month_number(today)

    starts = [month_number(start) for _, start, _ in spans]
    ends = [month_number(end) for _, _, end in spans]
    # Back: 12 months before today, or the oldest class's first month if that
    # is earlier. Forward: 3 years after today, or further if a class ends
    # later (so no box ever runs off the end).
    first = min([today_month - MONTHS_BACK] + starts)
    last = max([today_month + MONTHS_FORWARD] + ends)

    # Footprints: from the first month to whichever is wider, the months or
    # the minimum box width (plus a small gap so boxes don't touch).
    boxes = []
    for key, start, end in spans:
        left = position_of(month_number(start), first, ppm)
        span_width = (month_number(end) - month_number(start) + 1) * ppm
        boxes.append(Box(key=key, left=left, width=max(span_width, MIN_BOX_WIDTH), span_width=span_width, lane=0))
    lanes = assign_lanes([(box.key, box.left, box.left + box.width + BOX_GAP_X) for box in boxes])
    for box in boxes:
        box.lane = lanes[box.key]

    lanes_above = max([box.lane for box in boxes if box.lane > 0], default=1)
    lanes_below = max([-box.lane for box in boxes if box.lane < 0], default=1)
    axis_top = PADDING + lanes_above * LANE_HEIGHT
    axis_bottom = axis_top + AXIS_HEIGHT
    height = axis_bottom + lanes_below * LANE_HEIGHT + PADDING

    for box in boxes:
        steps = abs(box.lane)
        if box.above:
            box.top = axis_top - steps * LANE_HEIGHT + (LANE_HEIGHT - BOX_HEIGHT)
            box.connector_top = box.top + BOX_HEIGHT
            box.connector_height = axis_top - box.connector_top
        else:
            box.top = axis_bottom + (steps - 1) * LANE_HEIGHT
            box.connector_top = axis_bottom
            box.connector_height = box.top - axis_bottom

    months = []
    for number in range(first, last + 1):
        day = month_start(number)
        months.append(MonthMark(
            left=position_of(number, first, ppm),
            label=day.strftime("%b"),
            year=day.year if day.month == 1 or number == first else None,
        ))

    # Each Friday tick sits on its own date, measured the same way as "Today".
    fridays = [FridayMark(left=day_position(day, first, ppm), day=day) for day in fridays_between(first, last)]

    today_x = day_position(today, first, ppm)
    center_x = today_x if center is None else position_of(center, first, ppm)
    width = (last - first + 1) * ppm
    return Timeline(
        zoom=zoom, px_per_month=ppm, first_month=first, width=width, height=height,
        axis_top=axis_top, today_x=today_x, center_x=min(max(center_x, 0), width),
        months=months, fridays=fridays, boxes=boxes,
    )
