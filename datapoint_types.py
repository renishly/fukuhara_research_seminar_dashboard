"""Suggesting a type for each data point name (datapoints.py).

Agents name their data points, and may leave the type out: the dashboard
then files each name under a type itself. Where an agent did give a type,
the dashboard's suggestion is only shown to the agent's owner when it
disagrees, so they can fix the agent; the agent's own type is what counts.

Two ways to suggest, as with the skill organizer (market.py):
  - keyword rules (rule_type()): immediate, for every name, always available;
  - the AI model (OpenRouter), which replaces the rules' guess once it has
    looked at a name. Its answers are kept per name (DataPointType), so each
    name is sent once, ever. Every name not yet looked at goes in one request
    (at most MAX_NAMES_PER_RUN).

The budget: OpenRouter's daily requests are shared by the whole seminar
(assistant.budget()). The sorter has its own share, MAX_AI_RUNS_PER_DAY
requests a day (Japan time), kept free for it like the skill organizer's;
chat uses what's left. It runs from the background schedule (main.py) when
there are names to sort, at least MIN_HOURS_BETWEEN hours apart, so names
added later in the day still get the second run.
"""

import json
import re
import threading
from datetime import timedelta

from sqlmodel import Session, func, select

import market
import web
from db import DataPointRun, DataPointType, Monitor, as_utc, utc_now

# The fixed list, in display order. Internal values are what the agent works
# out itself (fair values, its own probabilities...); every other type is data
# from outside. A type the dashboard doesn't know counts as Other.
TYPES = [
    ("internal", "Internal values"),
    ("market", "Market prices"),
    ("company", "Company data"),
    ("economic", "Economic indicators"),
    ("analyst", "Analyst & consensus"),
    ("news", "News & text"),
    ("alternative", "Alternative data"),
    ("other", "Other"),
]
LABELS = dict(TYPES)
MEANINGS = {
    "internal": "values the agent constructs or predicts itself: its own fair value, DCF, model output, "
                "probability, score or signal",
    "market": "prices and quotes from markets: share prices, indices, FX rates, bond yields, futures, "
              "volatility, a prediction market's odds",
    "company": "data published by or about one company: earnings, revenue, filings, guidance, IR releases, "
               "balance sheet, dividends",
    "economic": "macroeconomic and official statistics: CPI, GDP, BOJ Tankan, PMI, unemployment, policy rates, "
                "visitor numbers, trade data",
    "analyst": "analysts' views: consensus estimates, ratings, target prices, estimate revisions",
    "news": "text: news articles, press releases, headlines, social media, transcripts, sentiment from text",
    "alternative": "non-traditional data: foot traffic, satellite images, search trends, app downloads, web "
                   "traffic, card spending, job postings, weather",
    "other": "anything that fits none of the above",
}

MAX_AI_RUNS_PER_DAY = 2  # this function's share of OpenRouter's daily requests
MIN_HOURS_BETWEEN = 6
MAX_NAMES_PER_RUN = 300
MAX_NAME = 200

_lock = threading.Lock()  # one sorter run at a time


def type_key(name) -> str | None:
    """ "Market prices", "market_prices", "MARKET" -> "market"; None if it isn't a known type."""
    text = " ".join(str(name).replace("_", " ").replace("-", " ").split()).lower()
    for key, label in TYPES:
        if text in (key, label.lower(), label.lower().replace(" & ", " and ")):
            return key
    return None


def clean_name(raw) -> str:
    return " ".join(str(raw).split()).lower()[:MAX_NAME]


# --- Keyword rules ----------------------------------------------------------------------------

# Tried in this order; the first that matches wins. Internal comes first only
# on words that clearly mean "made by the agent", then the more specific
# outside types before the general ones (consensus EPS is Analyst, not Company).
RULES = [
    ("internal", r"\b(our|own|my|internal|in-house|implied|fair value|dcf|intrinsic|model(led)?|"
                 r"nowcast|estimate[ds]? by)\b"),
    ("analyst", r"\b(consensus|analyst|ratings?|target price|price target|estimate revisions?|ibes|"
                r"recommendations?|broker)\b"),
    ("alternative", r"\b(foot ?traffic|store traffic|traffic|footfall|satellite|search trends?|google trends|app downloads?|web traffic|"
                    r"card (spend|spending|data)|credit card|job postings?|weather|footfall|alt(ernative)? data)\b"),
    ("economic", r"\b(cpi|gdp|boj|tankan|pmi|unemployment|inflation|policy rate|interest rates?|jnto|visitors?|"
                 r"tourists?|trade balance|wages?|economic|macro|industrial production|retail sales|"
                 r"housing starts|fed|ecb|fomc)\b"),
    ("news", r"\b(news|articles?|press|headlines?|tweets?|twitter|social|reddit|sentiment|blog|"
             r"transcripts?|reports? text|nikkei asia|bloomberg news|reuters)\b"),
    ("company", r"\b(earnings|revenue|sales|filings?|10-k|10-q|yuho|tanshin|ir\b|guidance|balance sheet|"
                r"annual report|margins?|dividends?|eps|cash flow|segment|disclosure|buyback|tob)\b"),
    ("market", r"\b(price|prices|share|stock|quote|fx|usd/?jpy|yen|dollar|index|indices|nikkei|topix|"
               r"s&p|yield|yields|futures|options|odds|volatility|vix|market cap|volume|spread)\b"),
]
_COMPILED = [(key, re.compile(pattern, re.I)) for key, pattern in RULES]


def rule_type(name: str) -> str:
    for key, pattern in _COMPILED:
        if pattern.search(name):
            return key
    return "other"


# --- Suggestions ------------------------------------------------------------------------------


def suggestions(db: Session, names: set[str]) -> dict[str, tuple[str, str]]:
    """Each name's suggested type and how it was found: {name: (key, "ai" | "rules")}."""
    found = {}
    if names:
        listed = list(names)
        for start in range(0, len(listed), 500):
            for row in db.exec(select(DataPointType).where(DataPointType.name.in_(listed[start:start + 500]))).all():
                found[row.name] = (row.suggested, "ai")
    return {name: found.get(name) or (rule_type(name), "rules") for name in names}


def all_names(db: Session) -> set[str]:
    """Every data point name in any agent's "Data points" monitor."""
    from datapoints import MONITOR, parse  # here, as datapoints uses this module
    names: set[str] = set()
    for value in db.exec(select(Monitor.value).where(func.lower(Monitor.name) == MONITOR, Monitor.kind == "json")).all():
        declaration = parse(value)
        if declaration is not None:
            names |= declaration.untyped
            for points in declaration.names.values():
                names |= points
    return names


def unsorted_names(db: Session) -> list[str]:
    """Names the AI hasn't looked at yet, alphabetically."""
    return sorted(name for name, (_, how) in suggestions(db, all_names(db)).items() if how == "rules")


# --- The AI run and its budget ------------------------------------------------------------------


def jst_today() -> str:
    return market.jst_now().date().isoformat()


def ai_runs_today(db: Session) -> int:
    return db.exec(select(func.count(DataPointRun.id)).where(DataPointRun.jst_date == jst_today(),
                                                             DataPointRun.method == "ai")).one()


def ai_runs_left(db: Session) -> int:
    return max(0, MAX_AI_RUNS_PER_DAY - ai_runs_today(db))


def last_run(db: Session) -> DataPointRun | None:
    return db.exec(select(DataPointRun).order_by(DataPointRun.started_at.desc())).first()


def due(db: Session) -> bool:
    """Should the sorter ask the AI now? Only with a key, names to sort, a
    request left of today's share, and the last run long enough ago."""
    if not market.ai_key() or ai_runs_left(db) <= 0:
        return False
    previous = last_run(db)
    if previous and utc_now() - as_utc(previous.started_at) < timedelta(hours=MIN_HOURS_BETWEEN):
        return False
    return bool(unsorted_names(db))


def prompt(names: list[str]) -> str:
    kinds = "\n".join(f'- "{key}" ({LABELS[key]}): {MEANINGS[key]}' for key, _ in TYPES)
    return ("You sort the data points that AI agents in a university finance seminar use to make their forecasts "
            "into types. The types:\n" + kinds + "\n\n"
            "Each data point below is just its name, as the agent's author wrote it (any language). Give each one "
            "the single best type key. \"internal\" is only for values the agent works out itself; everything "
            "taken from outside is one of the other types.\n\n"
            "Reply with JSON only, no other text: {\"types\": {\"<name exactly as given>\": \"<type key>\", ...}}\n\n"
            "Data points:\n" + json.dumps(names, ensure_ascii=False))


def parse_types(reply: str, names: list[str]) -> dict[str, str]:
    """The model's {name: key}, keeping only names we sent and known keys."""
    start, end = reply.find("{"), reply.rfind("}")  # models sometimes wrap it in ```json fences or text
    if start < 0 or end < start:
        raise ValueError("the model's reply had no JSON in it")
    data = json.loads(reply[start:end + 1])
    given = data.get("types") if isinstance(data, dict) else None
    if not isinstance(given, dict):
        raise ValueError("the model's reply had no \"types\" in it")
    wanted = set(names)
    result = {}
    for name, key in given.items():
        name = clean_name(name)
        key = type_key(key) or (str(key).strip().lower() if str(key).strip().lower() in LABELS else None)
        if name in wanted and key:
            result[name] = key
    return result


def run(db: Session) -> DataPointRun | None:
    """Ask the AI for the types of every name it hasn't seen (one request).
    Refuses (None) without a key or with today's share used up. Never raises."""
    with _lock:
        if not market.ai_key() or ai_runs_left(db) <= 0:
            return None
        names = unsorted_names(db)[:MAX_NAMES_PER_RUN]
        if not names:
            return None
        record = DataPointRun(jst_date=jst_today(), method="ai", names=len(names))
        db.add(record)
        db.commit()  # counts against the budget straight away
        try:
            found = parse_types(market.ask_model(prompt(names)), names)
            now = utc_now()
            existing = {row.name: row for row in db.exec(select(DataPointType).where(
                DataPointType.name.in_(list(found)))).all()} if found else {}
            for name, key in found.items():
                row = existing.get(name) or DataPointType(name=name, suggested=key)
                row.suggested, row.updated_at = key, now
                db.add(row)
            record.ok = True
            record.message = f"Sorted {len(found)} of {len(names)} data point names with the AI model."
        except Exception as error:  # noqa: BLE001 - a failed run is recorded, never raised
            db.rollback()
            record = db.get(DataPointRun, record.id) or record
            record.message = f"The AI model didn't answer usefully: {str(error)[:200]}"
        db.add(record)
        db.commit()
        db.refresh(record)
        return record


def run_if_due(db: Session) -> DataPointRun | None:
    return run(db) if due(db) else None


def status_line(db: Session) -> str:
    """One line on how the sorting is going, for the Data points tab."""
    if not market.ai_key():
        return "Types without a type given are sorted by keyword rules (no AI model is set up)."
    previous = last_run(db)
    when = f"last AI run {web.jst(previous.started_at)}" if previous else "the AI model hasn't run yet"
    return (f"Names without a type are sorted by the AI model, up to {MAX_AI_RUNS_PER_DAY} times a day "
            f"(keyword rules until then); {when}.")
