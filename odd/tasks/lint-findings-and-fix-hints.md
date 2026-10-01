# Report violated rules and fix hints, not just failing check names

## Objective

When a check fails, the agent receives each finding (file, line, rule, message), the deduplicated list of violated rules, and one dense fix line per check, so it can fix the code without re-running linters.

## Problem

Agents using multilint report they "cannot get the actual error". Verified 2026-10-01 on a 3-line Python file:

- The JSON contract (`checks.<name>`) carries counts only: `failed`, `total`, `status`, no messages.
- Both plugins reduce lint.sh's human stderr report to lines containing `✗`/`⚠` (`claude-plugin/scripts/lint_changed.py` `failing_lines()`, `.opencode/plugins/multilint-lint.js`). Those lines say *that* flake8 failed, never *why*; `a.py:1:1: F401 'os' imported but unused` is dropped.

## Why this approach

The user chose structured findings in the JSON (option B) over the hooks passing more human text (option A): both plugins already consume the JSON, so a field there is a stable contract for every client, while text scraping breaks whenever the report wording changes. Rule IDs and fix hints were added at the user's request ("list violated rules and provide dense guidelines on what should be fixed") with the notice format below approved by the user.

## Target notice (approved)

```text
multilint: a.py failed flake8, black, pylint

flake8 — fix the code (policy: checks.flake8.args)
  F401 a.py:1  'os' imported but unused
  E302 a.py:2  expected 2 blank lines, found 0
black — formatting only; run: black --line-length=120 a.py
pylint
  W0311 bad-indentation a.py:3  Bad indentation. Found 2 spaces, expected 4
  W0611 unused-import   a.py:1  Unused import os

Rules: F401 E302 W0311 W0611
```

## Scope

- `lint.sh`: capture each tool's output per failing file and check; parse into findings in the JSON emitter.
- JSON contract, additive only (no existing field changes name, type or position):
  - `checks.<name>.findings`: `[{"file", "line", "rule", "message"}]`, `line`/`rule` null when the tool gives none; capped at 50 per check with `findings_truncated: true` when cut.
  - `checks.<name>.fix`: one-line hint, present when the check failed.
  - `summary.rules_violated`: sorted unique rule IDs across all checks.
- Fix hints: formatters (black, shfmt, yaml/json prettier, toml_sort) give the exact auto-fix command using the effective policy flags; shellcheck rules link `https://www.shellcheck.net/wiki/SCxxxx`; markdownlint rules link `https://github.com/DavidAnson/markdownlint/blob/main/doc/mdxxx.md`; others say "fix the code" and name the `checks.<name>.args` override.
- `lint_changed.py` and `multilint-lint.js`: build the notice from `findings`/`fix`/`rules_violated`; fall back to the old marker lines when a document has no findings (older image). Keep the 4000-char cap.
- README JSON-contract section and `claude-plugin/agents/multilint.md`: document the new fields.

Out of scope: changing text-mode output; pylint's `/.cache` permission noise (separate finding).

## Constraints

- Minimal tests: one per core behaviour, none Docker-dependent, code first, tests last.
- Human text output stays unchanged except where a harness flag must change to make output parseable (record it in the task notes).
- Repo gates: black/flake8 line-length 120, pylint `--disable=C,R,E0401,E1123,W1510`, `shellcheck -e SC1091,SC2155,SC2086 -S style lint.sh`, `bash -n`, `node --check`.
- TDD: off (strict_tdd false, user rule: tests last). Runner: `python -m pytest tests/unit -q`.
- Branch `feat/lint-json-messages` off `origin/develop`; Conventional Commits, no AI attribution.

## Tasks

| ID | Task | Route | Status |
| --- | --- | --- | --- |
| T1 | lint.sh: capture per-check tool output, parse findings, add `findings`/`fix`/`rules_violated` to JSON | delegated writer (writer trigger: 3+ non-trivial files) | [x] f058132 |
| T2 | Both plugins render the approved notice from the JSON, with fallback | same writer | [x] 06347aa |
| T3 | Essential tests: flake8 finding parsed (rule/line/file), formatter fix hint, hook notice from a findings document | same writer | [x] f058132 (lint.sh pair), 06347aa (notice) |
| T4 | Docs: README contract + agent prompt | same writer | [x] a656ee0 |

## Acceptance criteria

- The 3-line `a.py` above yields findings with rules F401, E302, E201, E202 (flake8) and W0311, W0611 (pylint), plus a black fix command.
- The hook notice for that file matches the approved format and stays under 4000 chars.
- `python -m pytest tests/unit -q` passes; all repo gates clean.

## Progress

- 2026-10-01: problem verified, design approved, branch created.
- 2026-10-01: T1-T4 implemented by one writer; commits f058132, 06347aa, a656ee0.

## Implementation notes

- Mechanism: `record_output <check> <file> <raw output>` in lint.sh writes `<seq>.<check>.file/.out` pairs to a `mktemp -d` dir (trap-cleaned); the JSON emitter parses them per tool and exports of `ML_MSG_DIR`/`ML_ARGS_<formatter>` feed it. Findings are collected only for checks whose status is `failed`.
- Harness-flag change: `shellcheck` now runs with `-f gcc`, placed after the policy flags so it always wins. Text-mode shellcheck output is therefore one `file:line:col: severity: message [SCxxxx]` line per finding instead of the multi-line tty format. No other tool output changed.
- Deviation (additive): findings from pylint and markdownlint carry an extra `symbol` key (`bad-indentation`, `no-trailing-spaces`), needed to render the approved `W0311 bad-indentation` notice line.
- Formatters give rule `null` and message `formatting required` (so `rules_violated` stays real rule IDs); an output line mentioning `error` becomes its own finding instead. The notice omits the bare `formatting required` lines.
- The notice header is rendered for every failed check (`pylint — fix the code (policy: checks.pylint.args)`); the approved sample showed pylint without it.
- Notice cap: `MAX_CONTEXT_CHARS` applies to the whole notice; findings are cut at a line boundary, header and Rules footer are kept.
- Unparsed lines (e.g. pylint `/.cache` noise) stay as rule-null findings, sorted after parsed ones. Decorative pylint/bashate lines (module banner, dashes, score, error count) are skipped.
- gitleaks findings never copy the secret: file, line, RuleID and Description only. security_secrets findings carry no matched text.

## Verification (2026-10-01)

| Command | Result |
| --- | --- |
| `python -m pytest tests/unit -q` | 153 passed |
| `black --line-length=120 --check claude-plugin/scripts tests/unit` | clean |
| `flake8 ...` (repo gate flags) | clean |
| `pylint ... lint_changed.py` | 10.00/10 |
| `shellcheck ... lint.sh && bash -n lint.sh` | clean |
| `node --check .opencode/plugins/multilint-lint.js` | clean |
| acceptance sample through `multilint:json-config` with repo lint.sh mounted | flake8 F401/E302/E201/E202, pylint W0311/W0611, black `black --line-length=120 a.py`; notice 699 chars matches the approved format |

JS notice output verified identical to the Python notice for the same document (bun).

## Pre-existing defects found, not fixed (out of scope)

- shfmt check never fails when a diff exists: `shfmt -d | grep -q .` under `pipefail` returns shfmt's exit 1, so the `if` takes the pass branch.
- security_secrets / security_dangerous_patterns: `secret_rc`, `py_secret_rc`, `dangerous_rc` are set only on grep non-match and never reset, so after one clean file later matches are missed.
- Image's toml-sort 0.25.0 rejects the default `--sort-keys` policy flag (`unrecognized arguments`), so every TOML file fails toml_sort.
- The image's markdownlint has no MD060 (table-separator rule), so the `|---|` doc-link case could not be reproduced there; the link rendering is generic per rule ID.

## Next step

Review, then push and open the PR (user decision). Rebuild the image so the plugins get findings from the published `lint.sh`.
