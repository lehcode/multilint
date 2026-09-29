# Ephemeral-container execution model

## Objective

Replace multilint's long-running container + HTTP API with one ephemeral container per lint invocation, so the plugin works for any project directory and for several agents at once.

## Problem

1. Bind mounts and `MULTILINT_ALLOWED_ROOTS` are fixed at container start and ports `8591`/`8592` are host-published, so N agents in N directories cannot be served. Adding a project means recreating the container and killing the other agents' in-flight lints. `server.py:143-148` already carries an `EADDRINUSE` branch acknowledging this.
2. `docker-compose.yml:17-18` mounts `~/lan-hosts` and `~/docker-compose.d/multilint` — paths no other user has.
3. The host↔container path boundary produces false greens. `.opencode/plugins/multilint-lint.js:57` posts the host path with no `cwd`; reproduced against the live service, it reports `All checks passed ✓` with `return_code: 0` having linted nothing.

## Why this approach

An identity mount (`source=<root>,target=<root>`) makes host path == container path, deleting the translation layer rather than fixing it. Per-invocation containers have no fixed mounts, no ports and no long-running privileged process, while keeping the pinned 12-tool toolchain and bash 5 that motivated using Docker at all.

Host-native was rejected: `lint.sh` needs bash 4+ (`declare -A` at `:168`, `mapfile` at `:196`/`:285`/`:403`) and six tools have no `command -v` guard. Recorded as rationale only — containerising makes host bash irrelevant, so there is deliberately no macOS follow-up.

## Constraints

- **TDD: off.** No project or session configuration enables it; the presence of a test suite does not. Ordinary functional checks apply per task. Runner: `python -m pytest`.
- `release-please.yml` must not be touched. Releases stay on pushes to `master`.
- `master` keeps linear history: `develop` → `master` is rebase or fast-forward only.
- `server.py` / `mcp_server.py` stay in repo and image (MCP-registry product surface); they only leave the plugin hot path.
- Change-detection state stays outside `~/.claude/` and OpenCode's config dirs.
- Delivery strategy: `ask-on-risk`. Forecast ~675 authored lines total, sliced as three branches of ~200 / ~145 / ~330 — each under the ~400 heuristic, so no further chaining is planned.

## Tasks

### Branch 0 — `chore/develop-ci` → `develop`

- [x] **T0.1** Benchmark `docker run` against the 1.39 s warm-HTTP baseline; abort if over ~1 s overhead. *(route: inline)*
- [x] **T0.2** Create `develop` from `origin/master`. *(route: inline)*
- [x] **T0.3** Add `.github/workflows/develop-checks.yml` — unit tests, flake8/black/shellcheck, npm build. *(route: inline)*
- [x] **T0.4** Add `develop` to `gitleaks.yml` trigger branches. *(route: inline)*
- [x] **T0.5** Verify the workflow locally: yamllint, the exact linter commands, `npm run build`. *(route: inline)*
- [x] **T0.6** Commit, push `develop` and `chore/develop-ci`, open PR → **PR #13**, all four checks green.
- [x] **T0.7** Retarget PR #12 from `master` to `develop`. Done via REST; `gh pr edit` fails on this repo with a Projects-classic GraphQL deprecation error, so use `gh api -X PATCH .../pulls/12 -f base=develop`.
- [x] **T0.8** Confirm whether CodeQL default setup covers `develop` (GitHub UI, not a repo file). **It does not** — see below.

### Branch 2 — `fix/lint-correctness` → `develop`

- [ ] **T2.1** `lint.sh:754-755` — `"passed"` and `"failed"` both hold the failure count; `passed` is wrong.
- [ ] **T2.2** `lint.sh:723` — emitter omits `mypy`, `bandit`, `yaml_prettier`, `json_prettier`, `toml_sort`; `ML_CHECKS_COUNT=11` (`:740`) is hardcoded, derive it.
- [ ] **T2.3** Add `"status": "ok" | "failed" | "skipped"` to the JSON, set `skipped` wherever `warn "… (not installed, skipping)"` fires.
- [ ] **T2.4** Guard the 6 unguarded tools (`shellcheck`, `flake8`, `black`, `pylint`, `mypy`, `bandit`) like the other 6.
- [ ] **T2.5** `claude-plugin/scripts/lint_changed.py` — add `from __future__ import annotations`; PEP 604 unions at `:88`, `:102`, `:117`, `:213` currently force Python 3.10+.
- [ ] **T2.6** `Dockerfile:19` — `markdownlint` is copied without its `node_modules` and has never run; `lint.sh:351-352` reads `$?` after `|| true`, masking the crash as `✓`.
- [ ] **T2.7** Update `tests/unit/test_lint_sh.py` and `test_lint_sh_extended.py` for the changed JSON contract.
- [ ] **T2.8** Rebuild the container so the allowlist, `USER nobody` and current `lint.sh` actually run. *(needs user approval — restarts a service)*

### Branch 3 — `feat/ephemeral-container-execution` → `develop`

- [ ] **T3.1** Replace `lint()` in `lint_changed.py` with a `docker run` argv builder; delete `path_map()`, `to_container_path()`, `DEFAULT_LINT_URL`, the `urllib` imports and the `# nosec B310` guard.
- [ ] **T3.2** Resolve the image from `MULTILINT_IMAGE`; no-op silently when `docker` is absent.
- [ ] **T3.3** Surface `status: "skipped"` to the agent even when `return_code == 0` (`lint_changed.py:284` returns early today).
- [ ] **T3.4** Emit a one-line notice when the retired `MULTILINT_URL` / `MULTILINT_PATH_MAP` are set.
- [ ] **T3.5** Delete `claude-plugin/.mcp.json`.
- [ ] **T3.6** Fix `.opencode/plugins/multilint-lint.js` false green.
- [ ] **T3.7** `docker-compose.yml` — env-driven mounts, drop personal paths, ports and `MULTILINT_ALLOWED_ROOTS`.
- [ ] **T3.8** Rewrite `claude-plugin/agents/multilint.md` for the wrapper contract.
- [ ] **T3.9** New tests for the argv builder — assert on constructed arguments without executing Docker, so CI needs no image.
- [ ] **T3.10** Confirm `tests/integration/test_http_api.py` still applies (`server.py` survives); do not delete it merely because the plugin stopped using that path.
- [ ] **T3.11** ROADMAP — reclassify item 4, Docker Registry Publishing, from `❌ Deferred` to required; update Summary Table and Active Work.

## Acceptance criteria

| Case | Expected |
| --- | --- |
| Two sessions editing two unrelated repos at once | Both get their own findings; neither blocks or misroutes |
| A project in a directory named in no config file | Linted, no compose edit, no restart |
| A 10-file changeset | One container, not ten |
| `markdownlint` deliberately broken | Agent told the check was skipped, not a silent `✓` |
| `docker` absent from `PATH` | Hook exits 0 silently |
| Non-git scratch directory | SQLite path still suppresses an unchanged second edit |
| `gitleaks` on a mounted git root | Runs, not `(.git not found, skipping)` |

Preserved baseline: `tests/test_files/` → `return_code: 1` with the known failures; `agents/` → `return_code: 0`.

## Verification evidence

- **T0.1 passed 2026-09-29.** Startup floor ~0.18 s steady. Single file mean of 10: **1.747 s** vs **1.39 s** warm-HTTP baseline → **0.357 s** overhead, under the 1 s abort threshold. 10 files in one container **6.35 s** vs ten containers **8.05 s**, confirming amortisation.
- Verified invocation uses `--mount`, not `-v`: in zsh, `"$PWD:$PWD:ro"` becomes `…/multilint:…/multilinto` because `:r` is a parameter modifier that applies even inside double quotes. This produced a bogus 0.213 s reading by linting an empty directory.
- Pre-change baseline on the tracked tree: `python -m pytest tests/unit` → 59 passed; flake8, `black --check --line-length=120` and shellcheck with `lint.sh`'s exact flags → all rc=0 on 11 Python and 4 shell files.
- **T0.5 passed 2026-09-29.** `yamllint -c .yamllint` clean on the new workflow; the exact `git ls-files -z | xargs -0 -r` commands run under bash give rc=0 for flake8, black and shellcheck; `npm run build` completes (esbuild → `dist/dual.js` 2.2 kB, then `tsc`). Full multilint pass on `develop-checks.yml` → `All checks passed ✓`.
- **T2.6 independently reconfirmed.** In the container, `markdownlint --version` fails with `Cannot find package 'commander' imported from /usr/local/bin/markdownlint`, yet `lint.sh` still reports `✓ markdownlint`. The false pass is real.

### T0.8 — branch protection and CodeQL scope, confirmed via the GitHub API

- CodeQL default setup is `configured` (languages: actions, javascript, javascript-typescript, python, typescript; `threat_model: remote`; weekly schedule). Default setup covers the **default branch and PRs targeting it**, and the default branch is `master`. **PRs into `develop` therefore get no CodeQL.**
- Ruleset `23642850` ("Default", `active`) is scoped to `refs/heads/master` only, with rules `deletion`, `non_fast_forward`, `pull_request`, `code_quality`, `code_scanning`. `non_fast_forward` confirms the linear-history requirement.
- **Consequence:** `develop` is entirely unprotected. `develop-checks.yml` will run on PRs into it but cannot block a merge, because no ruleset requires its checks. `code_quality` and `code_scanning` are enforced only at the `develop` → `master` promotion. Adding a ruleset on `develop` requiring the three new checks is a GitHub-UI decision left to the user.

## Findings recorded, not acted on

- **`yaml_prettier` fails on all four pre-existing workflows.** prettier defaults to double-quoted scalars while every existing workflow uses single quotes, so `gitleaks.yml` fails the repo's own check independently of this change. `develop-checks.yml` was written double-quoted so it does not add to the violation; the existing files were deliberately left alone rather than restyled.
- **`gitleaks.yml` has no `---` document start**, which yamllint warns about. Pre-existing, untouched.
- **`pytest.ini` declares `unit` and `integration` markers that no test applies.** `-m unit` would collect nothing. Selecting by path works; the unused markers remain.

### T0.6 — what CI caught that local runs did not

The workflow needed two rounds of fixing, both because the maintainer's environment hid missing dependencies. Worth keeping, because it is the same failure mode this whole change is about — a check that appears to pass without having run.

1. **`ModuleNotFoundError: No module named 'pytest_mock'`.** `tests/conftest.py:14` imports it. Locally 59 tests passed because conda provides it.
2. **`ModuleNotFoundError: No module named 'fastmcp'`** (5 tests). `tests/unit/test_mcp_server.py` → `mcp_server.py:7`. A clean venv gives 54 passed, not 59.
3. **Two tests have an undeclared toolchain dependency** and fail with 57 passed:
   - `test_lint_sh.py::TestThresholds::test_threshold_config_file_read` asserts `bashate: 1 failures`; without `bashate`, `lint.sh` warns "not installed, skipping" and the counter stays 0.
   - `test_lint_sh_extended.py::TestGitleaks::test_gitleaks_no_git` asserts `gitleaks (.git not found, skipping)`; without `gitleaks`, the "not installed" branch fires instead.

   Resolved by installing those two tools (gitleaks pinned to `Dockerfile:44`'s version) rather than editing the tests. Arguably the tests should `skipif` on a missing tool — that is a real defect, left alone because altering existing tests was not in scope here.

**Method that worked:** replicate CI in a throwaway venv (`python3 -m venv`, install only the declared set, run the exact command) instead of trusting a local pass. Verified set: `pytest pytest-asyncio pytest-mock fastmcp` plus `bashate` and `gitleaks` on PATH → 59 passed.

### Merge order

Merge **#13 before #12**. PR #12's head predates `develop-checks.yml`, so for its `pull_request` event GitHub finds no such workflow and it is not gated by it — its current green checks are stale results from when it targeted `master`. After #13 lands on `develop`, rebase #12 onto `develop` so the new workflow actually runs on it.

## Next step

Branch 2 — `fix/lint-correctness` off `develop`: T2.1 through T2.7. T2.8 (container rebuild) needs approval because it restarts a service.
