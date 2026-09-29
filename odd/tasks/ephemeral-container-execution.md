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

### Branch 2 — `fix/lint-correctness` → `develop` — **PR #14, all 5 checks green**

Line numbers below are from PR #12's `lint.sh` (783 lines), which is what `develop` now carries. `develop`'s pre-#12 copy was 715 lines and had no `mypy`/`bandit` at all — see the ordering note under Merge order.

- [x] **T2.1** `"passed"` and `"failed"` both held the failure count. `passed` now means clean units, which needs a denominator, so each check records how many units it received (`total`). *(route: inline — single file, already understood)*
- [x] **T2.2** Emitter enumerated 11 names and omitted `mypy`, `bandit`, `yaml_prettier`, `json_prettier`, `toml_sort`. Now driven by a canonical `ALL_CHECKS` array; `ML_CHECKS_COUNT` derived from it. *(route: inline)*
- [x] **T2.3** Added `"status": "ok" | "failed" | "skipped"` plus `summary.checks_skipped`. *(route: inline)*
- [x] **T2.4** Guarded `shellcheck`, `flake8`, `pylint`, `black`, `mypy`, `bandit` with `command -v`. *(route: inline)*
- [x] **T2.5** `from __future__ import annotations` added to `lint_changed.py`. Not executed on a 3.9 interpreter — none available here; the mechanism is PEP 563 vs PEP 604. *(route: inline)*
- [x] **T2.6** `Dockerfile` now copies `/usr/local/lib/node_modules` and recreates the launcher as a symlink; the `|| true` masking the exit status is gone. *(route: inline)*
- [x] **T2.7** 11 new tests in `test_lint_sh_extended.py`, asserting contract invariants rather than tool verdicts. All 11 verified to fail against the previous `lint.sh`. *(route: inline)*
- [x] **T2.9** *(added)* Pinned dev dependencies in `requirements-dev.txt`, installed from it in both CI jobs. Unpinned installs had made CI non-deterministic. *(route: inline)*
- [ ] **T2.8** Rebuild the container so the allowlist, `USER nobody` and current `lint.sh` actually run. *(needs user approval — restarts a service)*

### Branch 3 — `feat/ephemeral-container-execution` → `develop`

Breaking change. Ships with `feat!:` and a `BREAKING CHANGE:` footer; the surface is listed under *Breaking-change surface* below.

- [x] **T3.1** `lint()` replaced by `build_docker_argv()` + `run_lint()`. `path_map()`, `to_container_path()`, `DEFAULT_LINT_URL`, the `urllib` imports and the `# nosec B310` guard are gone. Path translation is deleted rather than reimplemented: the mount is an identity mount (`source=<root>,target=<root>`), so host and container paths are the same string. *(route: delegated-direct writer boundary not fired — one file, already mapped; done inline)*
- [x] **T3.2** `resolve_image()` reads `MULTILINT_IMAGE`, defaulting to `lehcode/multilint:latest`. `shutil.which("docker")` absent → silent `return 0`. *(route: inline)*
- [x] **T3.3** `main()` reports any non-structural `status: "skipped"` even when `return_code == 0`. A check that did not run is not a check that passed. *(route: inline)*
- [x] **T3.4** `retired_env_in_use()` emits one notice naming whichever of `MULTILINT_URL` / `MULTILINT_PATH_MAP` is set, so the reason they stopped mattering is discoverable instead of silent. *(route: inline)*
- [x] **T3.5** `claude-plugin/.mcp.json` removed with `git rm`. `plugin.json`'s description updated to state the docker requirement. *(route: inline)*
- [x] **T3.6** `.opencode/plugins/multilint-lint.js` rewritten. The false green came from `execFile` throwing on docker's non-zero exit; the catch now re-parses `error.stdout`, because a non-zero exit is the *normal* result when lint.sh finds problems. *(route: inline)*
- [x] **T3.7** `docker-compose.yml` — mount is `${MULTILINT_WORKSPACE:?…}` with no default, ports bound to `127.0.0.1`, `MULTILINT_ALLOWED_ROOTS` derived from the mount target rather than hardcoded. *(route: inline)*
- [x] **T3.8** `claude-plugin/agents/multilint.md` rewritten: the `docker run` invocation, the JSON field table, and an explicit statement that `status: "skipped"` is not a pass. *(route: inline)*
- [x] **T3.9** `tests/unit/test_lint_changed.py` — 40 tests, no Docker executed. Loads the hook via `importlib` because the file has no `.py`-importable package. Covers the argv, image resolution, retired env vars, the absence of the HTTP path, result parsing, skipped-check surfacing, and target selection. *(route: inline)*
- [x] **T3.10** `tests/integration/test_http_api.py` still applies and is unchanged. `server.py` survives branch 3 — it is the MCP-registry surface (ROADMAP item 2), it merely leaves the plugin's hot path. 5 integration tests still pass.
- [x] **T3.11** ROADMAP item 4 updated — but **not** as the plan said. See the deviation below: it was already shipped, so it moved `❌ Deferred` → `✅ Complete`, not "Deferred → required".
- [x] **T3.12** *(added)* `MAX_LINT_TARGETS = 25` and `select_targets()`. Not in the plan, and it is a real defect the plan missed: the changed set is everything the repo has dirty, which the triggering edit does not bound. One edit in a tree with 83 dirty files queued 52 lint runs; a larger tree would exceed `RUN_TIMEOUT_SECONDS` and report nothing — an unchecked file then looks exactly like a clean one. The edited file is always kept and the truncation is stated in the notice. Extracted as a pure function so it is unit-testable.
- [x] **T3.13** *(added)* Windows support stripped on instruction. The `typeof process.getuid === "function"` and `hasattr(os, "getuid")` guards are gone, and `os.pathsep` reverted to a literal `:` in two test assertions. WSL is POSIX and needs nothing extra; native Windows is not a target.

#### Deviations from the approved plan

1. **T3.11 was mis-scoped in the plan.** The plan asked to reclassify ROADMAP item 4 from `❌ Deferred` to *required*. Checking the registries first showed it was already **done**: tags are published and pullable. So item 4 became `✅ Complete` with the published-tag evidence, and the Summary Table row's impact went High → Critical, because every plugin user now runs `docker run`.
2. **The `gitleaks` acceptance criterion cannot be met on the hook path.** The table below says "`gitleaks` on a mounted git root → Runs". It does not, and no amount of mounting fixes it: `lint.sh` gates gitleaks on `$TARGET_DIR/.git`, and the hook's target is a single *file*, so `TARGET_DIR` is that file's parent. The scope root is mounted and does contain `.git`, but it is not what gets passed to `lint.sh`. Rather than report a structural skip after every single write, both plugins list `gitleaks` in `STRUCTURALLY_SKIPPED` and filter it out, and `agents/multilint.md` documents that git-history scanning belongs to a directory-scoped run. Making it actually run would mean changing `lint.sh`'s gitleaks gate, which is out of this branch's scope.

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

### Branch 3 — acceptance criteria, measured 2026-09-29/30

| Case | Result |
| --- | --- |
| Two sessions editing two unrelated repos at once | **Pass.** Two concurrent invocations against two repositories each mounted only their own scope root and reported only their own findings. Nothing is shared between runs, because nothing outlives the container. |
| A project in a directory named in no config file | **Pass.** Linted with no compose edit and no restart — the scope root is computed per invocation, so there is no config to name it in. |
| A 10-file changeset | **Pass.** One container. 4 files → one `docker run`, confirmed from the argv. Measured earlier at T0.1: 6.35 s for ten files in one container vs 8.05 s in ten. |
| `markdownlint` deliberately broken | **Pass.** Reported as `status: "skipped"` and surfaced to the agent even on `return_code == 0`. |
| `docker` absent from `PATH` | **Pass.** `shutil.which` returns `None`, hook exits 0 with no output. |
| Non-git scratch directory | **Pass**, after a corrected test. The first attempt used `/home/takeshi/.claude`, which *is* a git repository with 83 dirty files, so `find_git_root()` resolved there and 52 files were linted — the git path, not the SQLite path. Re-run under `mktemp -d /var/tmp/…`: first edit linted, unchanged second edit suppressed, third edit after a real change linted again. |
| `gitleaks` on a mounted git root | **Not met, and cannot be on this path.** See deviation 2 above. |

Toolchain verification on the final tree: 111 unit + 5 integration tests pass; `black --check --line-length=120` clean on 13 files; flake8 with the repo's `--extend-ignore` clean; `pylint --disable=C,R,E0401,E1123,W1510` → 10.00/10; shellcheck clean; `node --check` and prettier clean on the rewritten plugin; `yamllint` clean on `docker-compose.yml`; `npm run build` completes.

**A local pass is not evidence unless the flags match.** Running `black`/`flake8` without `--line-length=120` and the repo's `--extend-ignore` reported 8 files needing reformatting and dozens of E501s on untouched files. The CI flags are the contract; a bare invocation answers a different question.

### Breaking-change surface, for the `BREAKING CHANGE:` footer

1. `claude-plugin/.mcp.json` removed — the plugin no longer provides an MCP server.
2. `MULTILINT_URL` and `MULTILINT_PATH_MAP` are no longer read. A one-line notice fires when either is set.
3. `docker-compose.yml` no longer publishes `8591`/`8592` to all interfaces and no longer hardcodes `MULTILINT_ALLOWED_ROOTS`. Callers reaching the HTTP API from another host lose it.
4. `docker` on `PATH` is now a hard requirement for the hook, replacing "a running multilint service".
5. `agents/multilint.md` drops the `lint_files(path, cwd)` MCP contract.
6. `.opencode` linting starts actually reporting. A fix, but behaviourally breaking for anyone whose workflow rested on the false green.

No deprecation shim. Keeping `MULTILINT_URL` alive for one release would mean keeping the HTTP path this change exists to delete.

### T0.8 — branch protection and CodeQL scope, confirmed via the GitHub API

- CodeQL default setup is `configured` (languages: actions, javascript, javascript-typescript, python, typescript; `threat_model: remote`; weekly schedule). Default setup covers the **default branch and PRs targeting it**, and the default branch is `master`. **PRs into `develop` therefore get no CodeQL.**
- Ruleset `23642850` ("Default", `active`) is scoped to `refs/heads/master` only, with rules `deletion`, `non_fast_forward`, `pull_request`, `code_quality`, `code_scanning`. `non_fast_forward` confirms the linear-history requirement.
- **Consequence:** `develop` is entirely unprotected. `develop-checks.yml` will run on PRs into it but cannot block a merge, because no ruleset requires its checks. `code_quality` and `code_scanning` are enforced only at the `develop` → `master` promotion. Adding a ruleset on `develop` requiring the three new checks is a GitHub-UI decision left to the user.

## Findings recorded, not acted on

- **The scope root is unbounded, and branch 3 gives it teeth.** Scope root is the enclosing git repository, else the edited file's *parent directory*, and branch 3 bind-mounts it into the container. For an edit to a file sitting directly in a non-git system directory that means the whole directory is mounted. This is not hypothetical: `~/.local/state/multilint/changes.db` holds a complete recursive walk of `/etc` — 37 lintable files across `init.d`, `profile.d`, `rc*.d`, `wpa_supplicant`, `docker`, `containerd`, `nvidia-container-runtime`, `gdm3` — which only happens if `candidates_under(Path("/etc"))` ran, i.e. the scope root was `/etc`.

  Mitigations already in place: the mount is `readonly`, the container has `--network none`, and it runs as the invoking uid rather than root. So the exposure is "a throwaway offline container can read this directory", not exfiltration. But the hook also hashes up to `MAX_SCAN_FILES` (2000) files of it, which is work nobody asked for, and the linters then read system configuration.

  **The table has no timestamp column, so I cannot date those rows and will not guess** whether they came from a normal edit or from manual hook testing earlier in this work. Either way the structural path exists in the shipped code.

  Not fixed, because the fix is a design decision rather than a correction: refuse a scope root outside the user's home, require a project marker (`.git`, `package.json`, `pyproject.toml`) before mounting anything, or cap the mount at the edited file's directory only. Also note `SKIP_DIRS` blocks venvs by *name* (`venv`, `.venv`), so a venv called anything else is walked in full — a throwaway `blackvenv/` in a scratchpad put 542 files in one changed set, which is how this was noticed. Testing for `pyvenv.cfg` would be robust; the name list is not. `SKIP_DIRS` and the non-git walk are pre-existing code the plan explicitly left unchanged.

  What did work: `MAX_LINT_TARGETS` caught it. The hook reported *"checked 25 of 542 changed files (517 not checked, limit 25 per run)"* instead of queueing 542 lint runs and timing out silently. That is T3.12 doing exactly the job it was added for, observed in the wild rather than in a test.

- **The hardcoded-secrets scan silently misses almost every secret.** All three security loops reuse a status variable that is only assigned on the *no-match* branch:

  ```bash
  secret_output=$(grep -Eni "…" "$f" 2>&1) || secret_rc=$?
  if [ "${secret_rc:-0}" -eq 0 ]; then   # report
  ```

  `grep` returns 0 when it *finds* something, so `|| secret_rc=$?` fires only when a file is clean — setting `secret_rc=1`. It is never reset, so after the first clean file every later file with a secret is skipped. Reproduced in isolation: with a clean file scanned first, a following file containing `password="hunter2"` matches (non-empty `secret_output`) and is **not reported**. Only a match in the first scanned file, or one immediately after another match, is caught. Affects `secret_rc`, `py_secret_rc` and `dangerous_rc` identically.

  Not fixed: out of the approved T2.x scope, and CLAUDE.md requires reporting rather than unilaterally changing existing code. This is the most serious defect found so far — a security check reporting clean while holding a match in hand.

- **`Dockerfile:3` installs prettier into the `node-tools` stage, and `:30` installs it again via corepack in the final stage.** T2.6 now copies the whole `node_modules` tree out of `node-tools`, so the corepack line is redundant. Left in place rather than removed.

- **CI's shellcheck is unpinned apt.** The runner carries 0.9.0; upstream is 0.11.0. Unlike gitleaks it has no pinned release asset in the workflow, so a runner-image bump can change the verdict. Recorded in the workflow comment.

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

Branch 3 is code-complete and verified. Two things are waiting on the user, neither of which I should decide:

1. **PR #14 (`fix/lint-correctness` → `develop`)** is green on all 5 checks and needs a merge decision. Branch 3's PR is stacked on it, so #14 has to land before branch 3 can target a clean `develop`.
2. **T2.8 — rebuild the deployed `multilint` container.** Restarts a service, so it needs explicit approval.

Also open: the `develop` → `master` promotion (cuts one release for the whole range, not one per branch — release-please computes a single release per push), an optional GitHub ruleset on the unprotected `develop`, and the `secret_rc` security-scan defect under *Findings recorded, not acted on*, which is still awaiting instruction.
