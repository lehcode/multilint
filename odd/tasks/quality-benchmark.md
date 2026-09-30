# Code Quality Benchmark pre-commit/pre-push hook

## Objective

Add a `Code Quality Benchmark` hook (pre-commit and pre-push) modelled on `pi-router-build/scripts/quality-benchmark.sh`, corrected and scoped to this project.

## Problem

The pi-router script cannot be copied: its ShellCheck stage never fails (`|| echo "[]"` makes the JSON a two-value stream, `jq` prints `1\n0`, the count parser returns 0), `SC_CODE=$?` always captures 0, and `A || B && C` precedence would mask failures once that is fixed. It is also shell-only and depends on Semgrep's network registry.

## Decisions (user, 2026-10-01)

| Topic | Decision |
| --- | --- |
| Checks | vulture, xenon, import-linter only. Semgrep dropped. Checks multilint already runs (ShellCheck, Bashate, ...) are excluded. |
| import-linter | Contracts rooted at `tests` (only package; `server`/`mcp_server` are modules, which import-linter rejects as roots): `tests.unit`/`tests.integration` independent; neither imports `tests.fixtures`. Both verified KEPT on current code. |
| Existing violations | Fix the code, not the thresholds. |
| vulture policy | `--min-confidence 80` |
| xenon policy | `--max-absolute B --max-modules A --max-average A` |

## Constraints

- Minimal tests: essential regressions only, no Docker-dependent tests.
- TDD: not configured; ordinary functional checks. Runner: `.venv/bin/pytest`.
- Refactors are behaviour-preserving; existing tests are the proof.

## Tasks

- [x] **T1** Remove vulture findings: `server.py:132` `log_request(*args)`, `tests/unit/test_lint_changed.py:191` lambda `*a, **k`. *(route: delegated writer — 2+ non-trivial files with T2/T3)*
- [x] **T2** Refactor `claude-plugin/scripts/lint_changed.py` `main` (D, 25) and `cached_git_root` (C, 12) to grade B or better. *(route: same writer)*
- [x] **T3** `.importlinter`, `scripts/quality-benchmark.sh`, `quality-benchmark` hook in `.pre-commit-config.yaml` (`repo: local`, `language: python`, pinned `additional_dependencies`, stages pre-commit + pre-push). *(route: same writer)*

## Acceptance criteria

- `scripts/quality-benchmark.sh` exits 0 on the fixed tree and non-zero when any stage finds an issue or a tool fails to run.
- `pre-commit run quality-benchmark --all-files` passes.
- Full pytest suite passes.

## Progress

- Branch `feat/quality-benchmark` from `feat/json-config-checks` HEAD `014b77b`.
- T1+T2 committed in `beafdaf`. T3 committed with this document. Writer pass: T1-T3 implemented. `lint_changed.py` refactor: `main` D(25) -> B(10) via `read_payload`, `resolve_edited_file`, `changed_scope_root`, `summarize_results`, `build_notices`; `cached_git_root` C(12) -> B(8) via `touch_folder`, `store_folder`.

## Verification evidence

| Check | Result |
| --- | --- |
| `bash scripts/quality-benchmark.sh` | exit 0, 3/3 stages clean |
| Negative: tracked-intent bad file (unused import + CC>10) | exit 1, vulture 1 finding, xenon 2 findings; import-linter violation counted as 1 broken; removed afterwards |
| Negative: empty PATH | exit 1, all 3 stages `ERR` (tool not found) |
| `pre-commit run quality-benchmark --all-files` | Passed |
| `pytest -q` | 155 passed |
| `shellcheck -S style`, `bashate -i E006`, `shfmt -i 4 -d` | clean |
| `lint.sh` on script and on `lint_changed.py` | all checks passed |
| `pre-commit run --all-files` | markdownlint fails (pre-existing, `--fix` rewrites CHANGELOG.md; reverted); `fail_fast` stops later hooks; gitleaks and quality-benchmark pass when run alone |

## Review

- Commits `beafdaf`, `5e14f9b`: assessed high risk, review granted, 4 lenses approved and acknowledged (lineage `review-b9d3a7446015924e`).
- Non-blocking follow-ups: vulture stage counts stderr/crash lines as findings instead of `ERR` (still fails closed); stage count duplicated; no automated test for the script.

## Next step

Push and PR are the user's decision.
