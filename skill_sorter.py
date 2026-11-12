# Vendored from Raiyaan Kunimoto's skill-sorter proposal (Nov 2026), unchanged:
# the class's 9 categories + foundation lane, the keyword rules and the zip checks.
# The dashboard calls read_zip(), sort_skill() and build_tree(); it never uses the AI fallback here.
#!/usr/bin/env python3
"""
sort_skills.py - read an agent's skills zip and sort every skill into the
9 class categories of the Fukuhara Seminar skill tree.

Proposal draft by Raiyaan Kunimoto (skill-tree sub-team, Nov 2026).
The rules this script applies are written out in skill-sorter.md.

Usage
  python sort_skills.py UPLOAD.zip --owner "Raiyaan Kunimoto" --agent "SAN-9628" --out skill_tree.json
  python sort_skills.py --test test_set.csv          # accuracy against the class skill tree

Design
  * Standard library only (no pip install needed on the dashboard server).
  * Never runs uploaded code. It only reads text files.
  * Sorting order, cheapest first:
        1. category written in the skill's frontmatter  (free)
        2. keyword rules below                           (free, runs on the server)
        3. optional AI fallback for low-confidence skills (costs tokens; off by default)
    Every result is shown to the owner to confirm before it is published.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import PurePosixPath

SCHEMA_VERSION = "1.0"

# ---------------------------------------------------------------------------
# 1. The 9 class categories (from the class skill tree, Oct 2026)
# ---------------------------------------------------------------------------
CATEGORIES = [
    {"id": 1, "key": "public-data", "name": "Public data collection",
     "question": "Filings, IR pages and share prices into a sourced record"},
    {"id": 2, "key": "alt-data", "name": "Alternative & field data",
     "question": "Our own observations: web prices, searches, rankings, visits"},
    {"id": 3, "key": "checks-stats", "name": "Data checks & statistics",
     "question": "Is the data right, and how sure can we be?"},
    {"id": 4, "key": "market-competitor", "name": "Market, competitor & 3C/4P",
     "question": "How the company stands against customers and rivals"},
    {"id": 5, "key": "forecast", "name": "Revenue & earnings forecast",
     "question": "KPIs -> revenue -> EPS (the bridge)"},
    {"id": 6, "key": "valuation", "name": "Valuation",
     "question": "Multiples, cost of capital, DCF -> value per share"},
    {"id": 7, "key": "debate", "name": "Debate & judge",
     "question": "Agents argue; one final target price"},
    {"id": 8, "key": "position", "name": "Position & portfolio",
     "question": "Expected return -> alpha x confidence -> active weight"},
    {"id": 9, "key": "operations", "name": "Operations & publishing",
     "question": "Run on a schedule, report, send to the class dashboard"},
]
# Lane 0 is not a 10th category: it holds rulebooks that other skills read first
# (e.g. altdata-core, guardrails) and is drawn as the root of the tree.
FOUNDATION = {"id": 0, "key": "foundation", "name": "Shared foundation",
              "question": "Rules, schemas and guardrails other skills read first"}
CAT_BY_ID = {c["id"]: c for c in [FOUNDATION] + CATEGORIES}
CAT_BY_KEY = {c["key"]: c for c in CATEGORIES}

# ---------------------------------------------------------------------------
# 2. Keyword rules. (regex, weight). Matched case-insensitively against
#    the skill name (counted twice) + description + headings of SKILL.md.
#    Edit these lists when the class adds new kinds of skills.
# ---------------------------------------------------------------------------
RULES: dict[int, list[tuple[str, float]]] = {
    1: [  # Public data collection
        (r"有報|有価証券|短信|決算|edinet|tdnet|xbrl", 3),
        (r"\bfilings?\b|disclosures?|annual report|securities report|integrated report", 2.5),
        (r"\bir\b|ir extract|ir page|investor relations|monthly release|monthly kpi release", 2),
        (r"data acquisition|data sourcing|data extraction|data collection|extract", 2),
        (r"source extraction|public[- ]information|baseline", 2),
        (r"share price|stock price|live prices?|yahoo prices|consensus eps|market data", 1.5),
        (r"workbook|input register|restatements?|guidance hit", 1.5),
        (r"pdfs?\b|page & unit|with page", 1),
        (r"\brates?\b.*\b(deposit|mortgage)|\b(deposit|mortgage)\b.*rates?", 1),
        (r"results\b|\bplan\b", 0.5),
    ],
    2: [  # Alternative & field data
        (r"alternative data|alt[- ]data|alt data|field data|field layer|field counts?|field protocol", 3),
        (r"google trends|search(es)?\b|keyword batch|share of search", 2),
        (r"rankings?|ranks?\b|top[- ]?\d+|@cosme|rakuten|app store|apple rss", 2),
        (r"store visits?|shelf|stock-?outs?|street counts?|panel\b|lots every|scrap", 2.5),
        (r"nowcast|this month'?s sales|monthly sales", 2.5),
        (r"weather|calendar|visitors|jnto|hotel index|reviews\b|screenshots?", 1.5),
        (r"e-?stat|deaths per|hall map|plan prices|velocity|basket", 1.5),
        (r"signals?\b", 0.5),
    ],
    3: [  # Data checks & statistics
        (r"pass/warn/fail|quality|data[_ -]?qc|\bqc\b|hygiene|integrity|health check", 3),
        (r"bootstrap|\bcis?\b|confidence interval|kappa|reliability|significance|statistic", 3),
        (r"\bn\s*[<>≥≤]=?\s*\d+|pre-registered|negative-results|gate\b|auto-gate", 2.5),
        (r"validat|cross-?check|cross-source|dedup|verified|check the run|stops the run", 2),
        (r"transfer test|\btest(s|ed)?\b|backtest", 1),
        (r"refuses|complete months|mixed-basis", 1.5),
    ],
    4: [  # Market, competitor & 3C/4P
        (r"3c|4p|3c/4p|analyse_3c|analyse_4p", 3.5),
        (r"competitor|rivals?|peer landscape|competitive|moat|pricing power", 3),
        (r"market analysis|market_analysis|business analysis|business & competitors", 2.5),
        (r"price gap|competitor gap|basket vs|scorecard|share signal|demand-decomposition|lasting vs temporary", 2),
        (r"segments?\b|so what|priorities|durability|event classes|news scorer", 1),
    ],
    5: [  # Revenue & earnings forecast
        (r"forecast|predict|estimate\b|revenue agent|revenue tree|driver tree", 3),
        (r"→\s*eps|-> ?eps|to eps|eps bridge|kpi.*eps|eps & fcf|profit to", 3),
        (r"revenue|sales & profit|segment build|nii|net interest", 2),
        (r"bear/base/bull|low/high|fy20\d\d|regression", 1.5),
        (r"×|\bx\b", 0.5),
    ],
    6: [  # Valuation
        (r"valuation|value per share|target p/?e|p/e decomposition|sotp", 3),
        (r"\bdcf\b|capm|fama-french|wacc|residual income|\bri\b|ddm|coe", 3),
        (r"p/e|\bper\b|pbr|p/b|ev/ebitda|multiples?\b|peer median|6-peer", 2.5),
        (r"accounting model|anchor-and-update|own-history|market analyst|benchmarking", 1.5),
        (r"peers?\b|blended", 0.5),
    ],
    7: [  # Debate & judge
        (r"debate|judge|committee|consensus", 4),
        (r"final target|final price|set the (final|tp)|\btp\b|verdict|final-valuation", 3),
        (r"challenge each other|agents argue|argue|claims|weights models|evidence quality", 2.5),
        (r"human adopts|adopts or rejects|analyst decision|orchestrator", 2),
        (r"red[- ]team", 1),
    ],
    8: [  # Position & portfolio
        (r"portfolio|position\b|active weight|over/?underweight|sizes the active", 3.5),
        (r"alpha|expected return|benchmark|topix|turnover|weight\b", 2.5),
        (r"rf\+mrp|capped|price range|p5|p95|walk-forward|value blend|blend", 1.5),
        (r"confidence", 0.5),
    ],
    9: [  # Operations & publishing
        (r"dashboard connector|publish|posts? (agent )?cards|json payloads|run_all", 3.5),
        (r"cron|schedul|timer|systemd|daily \d|every \d+ ?h|weekly timer|cloudflare|oracle vm", 3),
        (r"\boperations?\b|monitor|change detection|fallback|versioned|never overwritten", 2),
        (r"weekly report|reports?\b|summary pdf|html monitor|drive files|excel", 1.5),
        (r"by hand|manually|status\b|refresh", 1),
    ],
}
_COMPILED = {cid: [(re.compile(p, re.I), w) for p, w in rules] for cid, rules in RULES.items()}

# Tie-break rules from skill-sorter.md, applied after scoring.
# Each returns a category id if it fires, else None.
def _tiebreak(text: str, scores: dict[int, float]) -> tuple[int | None, str]:
    t = text.lower()
    # R5: a debate / judge step always wins over the steps it combines.
    if re.search(r"debate|judge|committee", t):
        return 7, "R5: has a debate/judge step"
    # R1: 'price' in a competitor context is competitor analysis, not valuation.
    if re.search(r"competitor|rival|peer landscape|3c|4p", t) and scores[4] >= scores[6]:
        return 4, "R1: compares against rivals (main output is a competitor view)"
    # R3: alt data turned into this month's sales = nowcast signal, stays in 2;
    #     turned into fiscal-year revenue/EPS = forecast (5).
    if re.search(r"nowcast|this month", t) and not re.search(r"eps|fy20", t):
        return 2, "R3: nowcast signal, not a fiscal-year forecast"
    return None, ""


def score_text(name: str, description: str, body: str = "") -> dict[int, float]:
    text_name = name.replace("_", " ").replace("-", " ") + " " + name
    scores = {cid: 0.0 for cid in RULES}
    for cid, rules in _COMPILED.items():
        for rx, w in rules:
            if rx.search(text_name):
                scores[cid] += 2 * w
            if rx.search(description):
                scores[cid] += w
            if body and rx.search(body):
                scores[cid] += 0.5 * w
    return scores


def classify_by_rules(name: str, description: str, body: str = "") -> dict:
    scores = score_text(name, description, body)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, s1), (second, s2) = ranked[0], ranked[1]
    total = sum(scores.values()) or 1.0
    reason = f"keyword score {s1:.1f} vs next {CAT_BY_ID[second]['name']} {s2:.1f}"

    tb, tb_reason = _tiebreak(f"{name} {description}", scores)
    if tb is not None and tb != best:
        best, reason = tb, tb_reason
        s1 = scores[tb]
    elif tb is not None:
        reason = f"{tb_reason}; {reason}"

    if s1 == 0:
        return {"category": None, "confidence": 0.0, "reason": "no keyword matched",
                "scores": scores}
    # confidence: share of the evidence + gap to runner-up, clipped to 0..1
    gap = (s1 - s2) / (s1 or 1)
    confidence = round(min(1.0, 0.5 * (s1 / total) + 0.5 * gap + (0.15 if tb else 0)), 2)
    return {"category": best, "confidence": confidence, "reason": reason, "scores": scores}


# ---------------------------------------------------------------------------
# 3. Optional AI fallback (OFF by default; NOT tested - needs network + key)
# ---------------------------------------------------------------------------
AI_PROMPT = """You sort one AI-agent skill into exactly one of these 9 categories.
{cats}
Rules: classify by the skill's MAIN OUTPUT, not by words in its name.
A skill with a debate/judge step goes in 7. Competitor price comparisons go in 4, not 6.
Alternative data turned into this month's sales is 2; into fiscal-year revenue/EPS is 5.
Reply with JSON only: {{"category": <1-9>, "confidence": <0-1>, "reason": "<one line>"}}

Skill name: {name}
Description: {description}
First part of SKILL.md:
{body}
"""


def classify_by_ai(name: str, description: str, body: str) -> dict | None:
    """Call an LLM only for skills the rules could not sort confidently.
    Ren: point this at whatever API the dashboard already uses. Returns None on any failure."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    import urllib.request
    cats = "\n".join(f"{c['id']}. {c['name']} - {c['question']}" for c in CATEGORIES)
    prompt = AI_PROMPT.format(cats=cats, name=name, description=description, body=body[:3000])
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({
            "model": os.environ.get("SORTER_MODEL", "claude-haiku-5-5"),
            "max_tokens": 200,
            "messages": [{"role": "user", "content": prompt}],
        }).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = json.loads(r.read())["content"][0]["text"]
        out = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        if int(out["category"]) in CAT_BY_ID:
            return {"category": int(out["category"]),
                    "confidence": float(out.get("confidence", 0.5)),
                    "reason": "AI: " + str(out.get("reason", ""))[:200]}
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# 4. Reading the zip safely
# ---------------------------------------------------------------------------
MAX_FILES = 500
MAX_TOTAL_BYTES = 20 * 1024 * 1024      # 20 MB unpacked
MAX_SKILL_MD_BYTES = 100 * 1024         # same limit as "Share a skill" today
TEXT_EXT = {".md", ".py", ".txt", ".json", ".yaml", ".yml", ".csv", ".toml", ".js", ".ts", ".sh"}

SECRET_FILES = re.compile(r"(^|/)(\.env(\..*)?|.*\.pem|.*\.key|id_rsa.*|credentials\.json|"
                          r"secrets?\.(json|ya?ml|toml)|.*\.p12)$", re.I)
SECRET_TEXT = re.compile(
    r"(sk-[A-Za-z0-9_\-]{20,}|sk-ant-[A-Za-z0-9_\-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}|"
    r"ghp_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9\-]{10,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"\s]{12,}['\"])", re.I)


def _safe_members(zf: zipfile.ZipFile) -> tuple[list[zipfile.ZipInfo], list[str]]:
    warnings, ok, total = [], [], 0
    infos = zf.infolist()
    if len(infos) > MAX_FILES:
        raise ValueError(f"zip has {len(infos)} files (limit {MAX_FILES})")
    for info in infos:
        p = PurePosixPath(info.filename)
        if info.is_dir():
            continue
        if p.is_absolute() or ".." in p.parts:                    # zip-slip
            warnings.append(f"skipped unsafe path: {info.filename}")
            continue
        if (info.external_attr >> 16) & 0o170000 == 0o120000:     # symlink
            warnings.append(f"skipped symlink: {info.filename}")
            continue
        if p.parts and p.parts[0] in {"__MACOSX"} or p.name == ".DS_Store":
            continue
        if SECRET_FILES.search(info.filename):
            warnings.append(f"removed secret file: {info.filename}")
            continue
        total += info.file_size
        if total > MAX_TOTAL_BYTES:                                # zip bomb
            raise ValueError("zip is larger than 20 MB unpacked")
        ok.append(info)
    return ok, warnings


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Tiny YAML-subset parser: key: value, key: [a, b], and '- item' lists."""
    m = re.match(r"^﻿?---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    meta, key = {}, None
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item and key:
            if not isinstance(meta.get(key), list):
                meta[key] = []
            meta[key].append(item.group(1).strip().strip("'\""))
            continue
        kv = re.match(r"^([A-Za-z_][\w\-]*)\s*:\s*(.*)$", line)
        if kv:
            key = kv.group(1).strip()
            val = re.sub(r"\s+#.*$", "", kv.group(2)).strip()   # drop inline comments
            if val.startswith("[") and val.endswith("]"):
                meta[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
            elif val in ("", "|", ">"):
                meta[key] = ""
            else:
                meta[key] = val.strip("'\"")
        elif key and isinstance(meta.get(key), str):              # folded continuation
            meta[key] = (meta[key] + " " + line.strip()).strip()
    return meta, m.group(2)


DEP_PATTERNS = [
    re.compile(r"\.\./([\w\-]+)/SKILL\.md", re.I),                       # Read ../altdata-core/SKILL.md
    re.compile(r"read_skill\(\s*['\"](skill-[\w\-]+)['\"]\s*\)", re.I),  # read_skill("skill-19")
    re.compile(r"\bread\s+`?([\w\-]+)`?\s+(?:skill\s+)?first\b", re.I),  # read altdata-core first
    re.compile(r"\b(?:uses|depends on|requires)\s+(?:the\s+)?`([\w\-]+)`", re.I),
]


def find_dependencies(body: str, meta: dict, known: set[str]) -> list[str]:
    deps = list(meta.get("depends_on") or []) if isinstance(meta.get("depends_on"), list) else []
    for rx in DEP_PATTERNS:
        for d in rx.findall(body):
            if d.startswith("skill-") or d in known:
                deps.append(d)
    seen, out = set(), []
    for d in deps:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def guess_reusability(meta: dict, text: str) -> tuple[str, str]:
    if meta.get("reusability") in {"generic", "sector", "company"}:
        return meta["reusability"], "frontmatter"
    if re.search(r"\b\d{4}\b(?!\s*(?:-|年|/))", text) and re.search(r"default|ticker|covered company|\(\d{4}\)", text, re.I):
        return "company", "guess: names one ticker - owner to confirm"
    return "generic", "guess - owner to confirm"


class UploadRejected(ValueError):
    """The zip breaks the required structure. Nothing is saved; show .errors to the student."""
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
ALLOWED_SUBDIRS = {"scripts", "references", "assets"}
ALLOWED_ROOT_FILES = {"readme.md", "agent.md"}


def validate_structure(members: list[zipfile.ZipInfo]) -> tuple[str, list[str], list[str]]:
    """Strict structure check (rules S1-S7 in skill-sorter.md).
    Returns (prefix, errors, warnings). prefix is "" or one wrapping folder such as
    "my-agent/" that the zip tool added; everything is read relative to it."""
    errors, warnings = [], []
    paths = [PurePosixPath(i.filename) for i in members]
    if not paths:
        return "", ["S1: the zip is empty"], warnings
    # Allow exactly one wrapping folder (macOS 'Compress' on the agent folder adds one)
    tops = {p.parts[0] for p in paths}
    prefix = ""
    if len(tops) == 1 and next(iter(tops)) != "skills" and any(len(p.parts) > 2 for p in paths):
        prefix = next(iter(tops)) + "/"
        paths = [PurePosixPath(*p.parts[1:]) for p in paths]
    # S1: at the root, only skills/ plus README.md or agent.md
    for p in paths:
        if len(p.parts) == 1 and p.name.lower() not in ALLOWED_ROOT_FILES:
            errors.append(f"S1: '{p}' is at the top of the zip; only skills/ (and README.md) may be there")
        elif len(p.parts) > 1 and p.parts[0] != "skills":
            errors.append(f"S1: '{p.parts[0]}/' is at the top of the zip; put everything inside skills/")
    skill_files: dict[str, list[PurePosixPath]] = {}
    for p in paths:
        if p.parts[0] != "skills":
            continue
        if len(p.parts) == 2:
            errors.append(f"S2: '{p}' is loose in skills/; each skill needs its own folder")
            continue
        if len(p.parts) >= 3:
            skill_files.setdefault(p.parts[1], []).append(PurePosixPath(*p.parts[2:]))
    if not skill_files and not errors:
        errors.append("S2: no skill folders found; expected skills/<skill-name>/SKILL.md")
    seen_lower = {}
    for name, files in sorted(skill_files.items()):
        # S3: folder name
        if not SKILL_NAME.match(name):
            errors.append(f"S3: folder 'skills/{name}/' - use lowercase letters, digits and hyphens (2-64 chars)")
        low = name.lower()
        if low in seen_lower:
            errors.append(f"S7: two skills named '{name}' and '{seen_lower[low]}'")
        seen_lower[low] = name
        # S2: SKILL.md directly in the folder (exact name)
        if PurePosixPath("SKILL.md") not in files:
            errors.append(f"S2: 'skills/{name}/' has no SKILL.md at its top (file name must be exactly SKILL.md)")
        for f in files:
            # S6: no skills inside skills
            if len(f.parts) > 1 and f.name.lower() == "skill.md":
                errors.append(f"S6: 'skills/{name}/{f}' - a skill cannot contain another skill; move it to skills/<its-name>/")
            # S5: only scripts/, references/, assets/ as subfolders
            if len(f.parts) > 1 and f.parts[0] not in ALLOWED_SUBDIRS:
                warnings.append(f"S5: 'skills/{name}/{f.parts[0]}/' - expected scripts/, references/ or assets/; kept but not standard")
    return prefix, sorted(set(errors)), sorted(set(warnings))


def read_zip(path: str, strict: bool = True) -> tuple[list[dict], list[str]]:
    """Read every skills/<name>/SKILL.md. With strict=True (the default) a zip that
    breaks rules S1-S7 raises UploadRejected and nothing is read."""
    skills = []
    with zipfile.ZipFile(path) as zf:
        members, warnings = _safe_members(zf)
        prefix, errors, structure_warnings = validate_structure(members)
        warnings += structure_warnings
        texts = {}
        # S4: frontmatter with name + description, and name == folder name
        for info in members:
            rel = info.filename[len(prefix):]
            parts = PurePosixPath(rel).parts
            if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md":
                if info.file_size > MAX_SKILL_MD_BYTES:
                    errors.append(f"S4: 'skills/{parts[1]}/SKILL.md' is over 100 KB")
                    continue
                text = zf.read(info).decode("utf-8", errors="replace")
                meta, body = parse_frontmatter(text)
                if not meta:
                    errors.append(f"S4: 'skills/{parts[1]}/SKILL.md' must start with --- frontmatter ---")
                else:
                    if not meta.get("name"):
                        errors.append(f"S4: 'skills/{parts[1]}/SKILL.md' has no name:")
                    elif str(meta["name"]) != parts[1]:
                        errors.append(f"S4: 'skills/{parts[1]}/SKILL.md' says name: {meta['name']} - it must match the folder name")
                    if not meta.get("description"):
                        errors.append(f"S4: 'skills/{parts[1]}/SKILL.md' has no description:")
                texts[parts[1]] = (text, meta, body)
        if errors and strict:
            raise UploadRejected(sorted(set(errors)))
        warnings += errors                     # non-strict mode: report and carry on
        for name, (text, meta, body) in sorted(texts.items()):
            skill_warnings, scripts, blocked = [], [], False
            if SECRET_TEXT.search(text):
                blocked = True
                skill_warnings.append("possible API key or password in SKILL.md - remove it and re-upload")
            base = f"{prefix}skills/{name}/"
            for f in members:
                if not f.filename.startswith(base) or f.filename == base + "SKILL.md":
                    continue
                rel = f.filename[len(base):]
                if PurePosixPath(rel).suffix.lower() in TEXT_EXT and f.file_size < 2 * 1024 * 1024:
                    if SECRET_TEXT.search(zf.read(f).decode("utf-8", errors="replace")):
                        blocked = True
                        skill_warnings.append(f"possible secret in {rel} - not published")
                if rel.endswith(".py"):
                    scripts.append(rel)
            skills.append({"folder": name, "path": base, "meta": meta, "body": body,
                           "scripts": sorted(scripts), "blocked": blocked,
                           "warnings": skill_warnings})
    return skills, warnings


# ---------------------------------------------------------------------------
# 5. Sorting one skill (frontmatter -> rules -> AI) and building the tree
# ---------------------------------------------------------------------------
REVIEW_BELOW = 0.45   # confidence under this -> needs_review (and AI fallback if enabled)


def _category_from_meta(meta: dict) -> int | None:
    raw = str(meta.get("category", "")).strip().lower()
    if not raw or re.match(r"^\d\s*-\s*\d", raw):     # empty or template left as "0-9"
        return None
    m = re.match(r"^(\d)\b", raw)
    if m and int(m.group(1)) in CAT_BY_ID:
        return int(m.group(1))
    if raw in CAT_BY_KEY:
        return CAT_BY_KEY[raw]["id"]
    for c in CATEGORIES:
        if raw == c["name"].lower():
            return c["id"]
    return None


def sort_skill(name: str, description: str, body: str = "", meta: dict | None = None,
               use_ai: bool = False) -> dict:
    meta = meta or {}
    cid = _category_from_meta(meta)
    if cid is not None:
        return {"category": cid, "category_source": "frontmatter", "confidence": 1.0,
                "reason": "set by the author in frontmatter", "needs_review": False}
    r = classify_by_rules(name, description, body[:4000])
    result = {"category": r["category"], "category_source": "rules",
              "confidence": r["confidence"], "reason": r["reason"]}
    if (r["category"] is None or r["confidence"] < REVIEW_BELOW) and use_ai:
        ai = classify_by_ai(name, description, body)
        if ai:
            result.update(ai, category_source="ai")
    result["needs_review"] = result["category"] is None or result["confidence"] < REVIEW_BELOW
    return result


def _headings(body: str) -> str:
    return " ".join(re.findall(r"^#{1,3}\s+(.*)$", body, re.M))


def build_tree(zip_path: str, owner: str, agent: str, use_ai: bool = False,
               known_skills: set[str] | None = None, strict: bool = True) -> dict:
    """Raises UploadRejected (with .errors) when strict and the zip breaks S1-S7."""
    raw, zip_warnings = read_zip(zip_path, strict)
    names = {str(s["meta"].get("name") or s["folder"]) for s in raw} | (known_skills or set())
    skills, edges = [], []
    for s in raw:
        meta, body = s["meta"], s["body"]
        name = str(meta.get("name") or s["folder"])
        desc = str(meta.get("description") or "")
        res = sort_skill(name, desc, _headings(body) + " " + body, meta, use_ai)
        reuse, reuse_src = guess_reusability(meta, f"{desc} {body[:2000]}")
        deps = find_dependencies(body, meta, names)
        cat = CAT_BY_ID.get(res["category"]) if res["category"] is not None else None
        skills.append({
            "id": f"{agent}/{name}",
            "name": name,
            "agent": agent,
            "owner": owner,
            "description": desc,
            "category": cat["id"] if cat else None,
            "category_name": cat["name"] if cat else None,
            "category_source": res["category_source"],
            "confidence": res["confidence"],
            "reason": res["reason"],
            "needs_review": res["needs_review"],
            "status": "blocked" if s["blocked"] else "awaiting_owner_confirm",
            "scope": "individual",                      # 'common' only via promotion
            "reusability": reuse,
            "reusability_source": reuse_src,
            "version": str(meta.get("version", "1")),
            "depends_on": deps,
            "inputs": meta.get("inputs", []),
            "outputs": meta.get("outputs", []),
            "scripts": s["scripts"],
            "warnings": s["warnings"],
        })
        for d in deps:
            edges.append({"from": d, "to": f"{agent}/{name}", "type": "depends_on"})
    # Foundation skills: rulebooks other skills read first (e.g. altdata-core,
    # guardrails). Marked when the author says so, or when 2+ skills depend on it.
    indegree: dict[str, int] = {}
    for e in edges:
        indegree[e["from"]] = indegree.get(e["from"], 0) + 1
    for sk, s in zip(skills, raw):
        role = str(s["meta"].get("role", "")).lower()
        sk["foundation"] = role == "foundation" or indegree.get(sk["name"], 0) >= 2
        if sk["foundation"] and sk["category"] is None:
            sk.update(category=0, category_name=FOUNDATION["name"], category_source="dependencies",
                      confidence=1.0, needs_review=False,
                      reason="read first by other skills -> foundation lane")
        # point edges at full ids when the dependency is in this upload
        for e in edges:
            if e["from"] == sk["name"]:
                e["from"] = sk["id"]
    counts = {c["id"]: 0 for c in [FOUNDATION] + CATEGORIES}
    for sk in skills:
        if sk["category"] is not None:
            counts[sk["category"]] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "upload": {"file": os.path.basename(zip_path), "owner": owner, "agent": agent,
                   "warnings": zip_warnings},
        "categories": [dict(c, count=counts[c["id"]]) for c in [FOUNDATION] + CATEGORIES],
        "skills": skills,
        "edges": edges,
    }


# ---------------------------------------------------------------------------
# 6. Test against the class skill tree (hand-sorted ground truth)
# ---------------------------------------------------------------------------
def run_test(csv_path: str) -> dict:
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    results, confusion = [], {}
    for r in rows:
        res = sort_skill(r["skill"], r["description"])
        exp, got = int(r["expected_category"]), res["category"]
        results.append({**r, "got": got, "confidence": res["confidence"],
                        "needs_review": res["needs_review"], "reason": res["reason"]})
        confusion[(exp, got)] = confusion.get((exp, got), 0) + 1
    n = len(results)
    correct = sum(1 for x in results if int(x["expected_category"]) == x["got"])
    auto = [x for x in results if not x["needs_review"]]
    auto_correct = sum(1 for x in auto if int(x["expected_category"]) == x["got"])
    per_cat = {}
    for c in CATEGORIES:
        rows_c = [x for x in results if int(x["expected_category"]) == c["id"]]
        per_cat[c["id"]] = (sum(1 for x in rows_c if x["got"] == c["id"]), len(rows_c))
    return {"n": n, "correct": correct, "accuracy": correct / n,
            "auto_sorted": len(auto), "auto_correct": auto_correct,
            "flagged": n - len(auto),
            "per_category": per_cat, "results": results}


def _print_test(rep: dict) -> None:
    print(f"Skills tested: {rep['n']}")
    print(f"Correct category: {rep['correct']}/{rep['n']} = {rep['accuracy']:.0%}")
    print(f"Sorted automatically (confidence >= {REVIEW_BELOW}): {rep['auto_sorted']} "
          f"-> {rep['auto_correct']} correct "
          f"({rep['auto_correct'] / max(1, rep['auto_sorted']):.0%})")
    print(f"Flagged for owner review: {rep['flagged']}")
    print("\nPer category (correct / total):")
    for c in CATEGORIES:
        ok, tot = rep["per_category"][c["id"]]
        print(f"  {c['id']} {c['name']:<30} {ok}/{tot}")
    print("\nMisses:")
    for x in rep["results"]:
        if int(x["expected_category"]) != x["got"]:
            flag = " (flagged)" if x["needs_review"] else ""
            print(f"  {x['owner']:<8} {x['skill'][:38]:<38} expected {x['expected_category']} "
                  f"got {x['got']}{flag}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sort an agent's skills zip into the 9 class categories.")
    ap.add_argument("zip", nargs="?", help="uploaded skills zip")
    ap.add_argument("--owner", default="unknown")
    ap.add_argument("--agent", default="agent")
    ap.add_argument("--out", default="skill_tree.json")
    ap.add_argument("--use-ai", action="store_true", help="AI fallback for low-confidence skills")
    ap.add_argument("--test", metavar="CSV", help="measure accuracy on a labelled CSV")
    ap.add_argument("--lenient", action="store_true", help="report structure errors instead of rejecting")
    a = ap.parse_args(argv)
    if a.test:
        _print_test(run_test(a.test))
        return 0
    if not a.zip:
        ap.error("give a zip file or --test CSV")
    try:
        tree = build_tree(a.zip, a.owner, a.agent, a.use_ai, strict=not a.lenient)
    except UploadRejected as e:
        print("UPLOAD REJECTED - fix these and upload again:")
        for err in e.errors:
            print("  x " + err)
        return 2
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(tree, f, ensure_ascii=False, indent=2)
    for s in tree["skills"]:
        mark = "?" if s["needs_review"] else " "
        print(f"{mark} {s['name']:<32} -> {s['category']} {s['category_name']} "
              f"({s['category_source']}, {s['confidence']:.2f}) [{s['status']}]")
    for w in tree["upload"]["warnings"]:
        print("! " + w)
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
