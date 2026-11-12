---
name: my-skill-name
description: One sentence on what the skill produces and when to use it.
category: 0-9 or key            # optional - see the table below; saves a guess
reusability: generic            # optional - generic | sector | company
depends_on: [other-skill]       # optional - skills this one reads first
inputs: [what it needs]         # optional
outputs: [files it writes]      # optional
version: 1                      # optional
role: skill                     # optional - skill | foundation
---

# My skill name

What it does, step by step.

<!--
Category keys
  0 foundation          rules other skills read first
  1 public-data         filings, IR, prices
  2 alt-data            our own observations, nowcasts
  3 checks-stats        data checks, confidence, tests
  4 market-competitor   3C/4P, rivals, moat
  5 forecast            revenue / profit / EPS for future years
  6 valuation           value per share
  7 debate              debate, judge, final target
  8 position            weight, alpha, portfolio
  9 operations          schedule, report, publish

Zip layout
  my-agent.zip
  └── skills/
      └── my-skill-name/
          ├── SKILL.md      (this file)
          └── scripts/      (optional)

Never put API keys or .env files in the zip. They are removed or the skill is blocked.
-->
