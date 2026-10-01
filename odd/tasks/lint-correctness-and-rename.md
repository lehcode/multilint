# Fix silent lint misses, harden findings, rename plugin files

## Objective

Make every check able to fail when it should, keep findings from being lost on the way to the agent, and give the plugin entry points the `multilint` name.

## Problem

Verified 2026-10-01 while building findings reporting (PR #24):

1. **shfmt never fails.** `lint.sh` runs `if shfmt ... -d "$f" | grep -q .` under `set -o pipefail`; on a diff shfmt exits 1, so the pipeline is false and the pass branch runs. Reproduced: `shfmt -d` rc=1, check `ok`/0.
2. **Secret and dangerous-pattern scans miss files.** `secret_rc`, `py_secret_rc`, `dangerous_rc` are set only on a grep non-match and never reset per file, so after one clean file every later match is skipped.
3. **Every TOML file fails.** The default `toml_sort_args` is `--sort-keys`, which toml-sort 0.25.0 in the image rejects.
4. **pylint cache noise.** The container runs as the invoking user with no writable home; pylint prints `Unable to create directory /.cache/pylint`, which now lands in findings as rule-less entries.
5. **Review advisories on PR #24 (WARNING):** `.opencode/plugins/multilint-lint.js:205-208` truncation can drop every finding; `lint.sh:536-539` findings can be lost silently when recording fails; `lint.sh:1497-1521` and `1579-1589` gitleaks raw-output fallback and parsing.
6. **Naming.** The user wants the plugin entry points named after the product: `.opencode/plugins/multilint-lint.js` → `multilint.js`; `claude-plugin/scripts/lint-changed.sh` → `multilint.sh`; `claude-plugin/scripts/lint_changed.py` → `multilint.py`.

## Scope

- `lint.sh` fixes 1–4 and the two lint.sh advisories in 5.
- JS truncation advisory in 5.
- Rename 6 with `git mv`, updating every reference: `hooks.json`, `opencode.json`, `README.md`, `claude-plugin/agents/multilint.md`, the tests, `lint.sh`, and the scripts themselves. Historical docs under `odd/` and `openspec/` stay as written.

## Constraints

- Minimal tests: one regression test per fixed defect (1–3), none Docker-dependent; code first, tests last.
- Do not change unrelated code; match surrounding comment density.
- Repo gates: black/flake8 at 120, pylint `--disable=C,R,E0401,E1123,W1510`, `shellcheck -e SC1091,SC2155,SC2086 -S style lint.sh`, `bash -n`, `node --check`, plus the pre-commit xenon complexity gate.
- TDD off; runner `python -m pytest tests/unit -q`.
- Branch `fix/lint-correctness-and-rename`, stacked on `feat/lint-json-messages` (PR #24). PR #23 also edits `lint_changed.py` and the JS plugin; expect a rename-aware rebase after it merges.
- Conventional Commits, no AI attribution, logical chunks: rename separate from behaviour fixes.

## Tasks

| ID | Task | Route | Status |
| --- | --- | --- | --- |
| T1 | Rename plugin files and every reference (commit alone) | delegated writer (writer trigger: 10+ files) | [x] |
| T2 | shfmt pipefail fix + regression test | same writer | [x] |
| T3 | Per-file reset of secret/dangerous rc + regression test | same writer | [x] |
| T4 | toml_sort default flags valid for 0.25.0 + regression test | same writer | [x] |
| T5 | pylint cache to a writable temp dir | same writer | [x] |
| T6 | Review advisories: JS truncation, silent findings loss, gitleaks parsing | same writer | [x] |

## Acceptance criteria

- A misformatted shell file fails `shfmt`; a secret in the second of two shell files fails `security_secrets`; a sorted TOML file passes `toml_sort`.
- No `/.cache/pylint` lines in pylint findings.
- No reference to the old file names outside `odd/` and `openspec/`; the hook runs from `hooks.json`.
- All gates and `python -m pytest tests/unit -q` pass.

## Progress

- 2026-10-01: defects verified; user chose to fix all and rename; branch created.
- 2026-10-01: T1-T6 implemented by one writer.

| Task | Commit | Evidence |
| --- | --- | --- |
| T1 | eae16db `refactor(plugin)!: rename plugin entry points to multilint` | `git grep` for old names outside odd/openspec: no output; 153 tests passed; node --check ok |
| T2, T3, T4, T5, lint.sh half of T6 | 1f818c3 `fix(lint): ...` | container runs: misformatted .sh -> shfmt failed/1; secret in a later .sh -> security_secrets failed/1 (unit test with 4 clean + 4 leaking files: 4); bad.toml -> toml_sort failed, sorted TOML -> ok (toml-sort 0.25.0 in image); pylint findings: only `W0611 Unused import os`, no `/.cache` lines. New tests fail on the old lint.sh (2 failed, toml test skips: no host toml-sort) |
| T6 (JS, Python guard) | 58adc86 `fix(plugin): ...` | JS cuts at a line break only when one exists; Python hook ignores non-list `findings`; node --check ok |

Deviations:

- `tests/unit/test_lint_sh.py::test_threshold_config_file_read` expected `shfmt: 0 failures` for a fixture that is really misformatted (it only passed because shfmt could never fail); now expects 1.
- T4: default is `--sort-table-keys` (valid in toml-sort 0.25.0); README/agent tables updated, stale "known CLI bug" README note removed.
- T5: `PYLINTHOME` defaults to `${ML_MSG_DIR:-/tmp}/.pylint` (cleaned with the temp dir) unless already set.
- T6: record_output now adds a warning (text and JSON `warnings`) when it cannot write; gitleaks fallback strips ANSI and skips `Secret:`/`Match:`/`Finding:` lines so a secret is never echoed into a finding. `git mv` of the test file was not done (name kept).

## Next step

Review the three commits, then push and update PR #24 / open the stacked PR.
