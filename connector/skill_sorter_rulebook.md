---
name: skill-sorter
description: Sort the skills in an uploaded agent zip into the 9 class categories of the Fukuhara Seminar skill tree, tag each one, find dependencies, and return skill_tree.json for the owner to confirm. Use when an agent's skills are uploaded or re-uploaded to the class dashboard.
version: 1
category: 9
role: foundation
---

# Skill sorter - rulebook

This file is the rulebook. `sort_skills.py` applies it automatically; an AI
(or a person) can apply it by hand when the script is unsure. Draft by
Raiyaan Kunimoto, skill-tree sub-team (Anthony), Nov 2026.

## 1. Goal

Every skill on the class dashboard sits in exactly one of the 9 class
categories, carries the same set of tags, and shows which skills it depends
on. The owner always confirms the result before it is published.

## 2. What an upload looks like

The zip is attached in the **Add agent** form (new field "Skills (.zip)",
under "Agent API key") and can be replaced later from the agent's card
("Update skills"). It must follow this structure exactly:

```
<agent-name>.zip
├── README.md                 optional
└── skills/
    ├── <skill-name>/
    │   ├── SKILL.md          required - one folder = one skill
    │   ├── scripts/          optional - stored, never run
    │   ├── references/       optional
    │   └── assets/           optional
    └── <skill-name>/
        └── SKILL.md
```

Structure rules. Breaking S1-S4, S6 or S7 rejects the whole upload with a
list of what to fix; nothing is saved. S5 is only a warning.

- **S1 Root.** Only `skills/` (and `README.md` / `agent.md`) at the top. One
  wrapping folder is allowed, because macOS "Compress" adds one.
- **S2 One folder per skill.** Every folder directly in `skills/` is one skill and
  has a file named exactly `SKILL.md` at its top. No loose files in `skills/`.
- **S3 Folder names.** Lowercase letters, digits and hyphens, 2-64 characters.
- **S4 Frontmatter.** SKILL.md starts with `---` frontmatter containing `name:`
  and `description:`; `name` equals the folder name. Max 100 KB.
- **S5 Subfolders.** Inside a skill, use `scripts/`, `references/`, `assets/`.
- **S6 No nesting.** No SKILL.md below the top of a skill folder.
- **S7 Unique.** No two skills with the same name in one upload.

Limits: 20 MB unpacked, 500 files.

## 3. The 9 categories (and lane 0)

Read them in order 1 -> 9: that is how one research run flows.

| # | Category | Put a skill here when its MAIN OUTPUT is... | Class examples |
|---|----------|---------------------------------------------|----------------|
| 0 | Shared foundation (lane, not a category) | rules, schemas or guardrails that other skills read first | altdata-core, guardrails |
| 1 | Public data collection | numbers taken from filings, IR pages, TDnet/EDINET or share prices, with a source | data_collection, fr-ir-extract, Market data + Filings |
| 2 | Alternative & field data | our own observations: web prices, searches, rankings, store visits, counts, nowcasts of this month's sales | App rankings, Parking panel, @cosme, competitor-nowcast |
| 3 | Data checks & statistics | a verdict on the data (PASS/WARN/FAIL, gate) or a measure of how sure we are (CIs, kappa, significance, backtest) | data_qc, statistics, Auto-gate checks |
| 4 | Market, competitor & 3C/4P | a view of the company against customers and rivals: 3C/4P, competitor prices, moat, peer landscape | market_analysis, Park24-CompetitorPrice, Moat score |
| 5 | Revenue & earnings forecast | revenue, profit or EPS for future fiscal years (the KPI -> EPS bridge) | forecast, Rate -> EPS bridge, Driver tree |
| 6 | Valuation | a value per share from one or more methods: multiples, CAPM, DCF, RI | valuation, fr-valuation, DCF · P/E decomposition |
| 7 | Debate & judge | one final target price after agents argue, or a challenge to the thesis | committee, A6 Judge, consensus-debate |
| 8 | Position & portfolio | an active weight or position from expected return, alpha and confidence | portfolio, Position rule |
| 9 | Operations & publishing | a scheduled run, a report, or a post to the dashboard | Dashboard connector, Weekly timer + publish |

## 4. Tie-break rules (use in this order)

- **R1 - Main output, not keywords.** Decide by what the skill produces.
  "Park24-CompetitorPrice" is about prices but produces a competitor comparison -> 4, not 6.
- **R2 - Who collected the data.** From the company or the exchange (filings, IR,
  prices) -> 1. Collected by us (scraping, searches, rankings, visits) -> 2.
- **R3 - Nowcast vs forecast.** Alt data turned into this month's sales or a
  signal -> 2. Turned into fiscal-year revenue/EPS -> 5.
- **R4 - Checks.** A check on data or a confidence measure -> 3. A challenge to the
  analyst's conclusion -> 7. A check that a report quotes the digest -> 3.
- **R5 - Debate wins.** If a skill contains a debate or judge step, it goes in 7
  even if it also sets a position or writes a report.
- **R6 - Value vs judge.** Methods that produce a value -> 6. Combining several
  agents' views into one final target -> 7.
- **R7 - Weight vs value.** Anything that ends in a weight or over/underweight -> 8,
  even if it includes a backtest or price range.
- **R8 - One skill, one category.** If a skill really does two jobs, put it where
  its main output belongs and suggest splitting it into two skills.
- **R9 - Foundation.** A skill that 2+ other skills read first, or that says
  `role: foundation`, goes in lane 0 unless the author set a category.

## 5. Tags on every skill

| Tag | Values | Set by |
|-----|--------|--------|
| scope | built-in · common · individual | dashboard. Uploads start as `individual`; `common` only through promotion |
| reusability | generic · sector · company | author (script guesses "company" when one ticker is named) |
| depends_on | list of skill names or `skill-N` ids | author, plus lines like `Read ../x/SKILL.md first` |
| status | awaiting_owner_confirm · published · blocked · deprecated | dashboard |

## 6. Frontmatter standard

Only `name` and `description` are required. Every extra field the author fills
in saves tokens and removes a guess.

```yaml
---
name: data-qc
description: PASS/WARN/FAIL checks on arithmetic, sources and peer periods.
category: 3                 # 0-9, or the key e.g. checks-stats
reusability: generic        # generic | sector | company
depends_on: [core-rules]
inputs: [baseline workbook]
outputs: [qc_report.json]
version: 1
role: skill                 # or foundation
---
```

## 7. Sorting order (cheapest first)

1. **Frontmatter** - if `category` is set, use it. Confidence 1.0, no tokens.
2. **Rules** - `sort_skills.py` scores the name, description and headings against
   keyword lists for each category, then applies R1-R9. Free, runs on the server.
3. **AI fallback** - only when confidence < 0.45. Send the name, description, the
   first part of SKILL.md and this section 3-4 to a small model. Off by default.
4. **Owner confirms** - every skill, every time. Flagged ones are highlighted.

## 8. Safety rules

- Never execute anything from an upload. Read text only.
- Remove `.env`, `*.pem`, `*.key`, `credentials.json`, `id_rsa*` before saving.
- If a SKILL.md or script contains something that looks like an API key or
  password, mark the skill `blocked` and tell the owner. Do not publish its text.
- Reject paths with `..`, absolute paths and symlinks (zip-slip).
- Re-uploading a skill with the same name creates a new version; it never
  overwrites the author or credit.

## 9. Output

`skill_tree.json` with `categories` (0-9 with counts), `skills` (one record per
skill, with category, source, confidence, reason, tags, warnings) and `edges`
(`from` skill -> `to` skill, type `depends_on`). The UI draws the tree from this
file alone; no tokens are needed to draw it.

## 10. Must not

- Invent a category the class has not agreed. New categories need Anthony's OK.
- Publish without the owner's confirmation.
- Change the owner's category choice back after they confirm it.
