# Lint the edited file, cache the git root, bound the search at $HOME

## Objective

Make the hook lint the file Claude's hook actually reported, resolve the enclosing git repository through a bounded and cached lookup, and record enough in the state database to answer "was this file in a repository, and when did we last see it".

## Problem

Four defects, found by the user reviewing the ephemeral-container work (#16) rather than by the tests.

1. **The hook lints the whole dirty tree, not the edited file.** `GIT_CHANGE_COMMANDS` is `git diff --name-only HEAD`, `git diff --name-only --cached` and `git ls-files --others --exclude-standard`. `main()` calls `changed_under_git(git_root)` and only then `changed.add(edited)`, so the edited file is merely guaranteed to be *in* the set rather than being the subject. One `Write` in a dirty repository lints up to `MAX_LINT_TARGETS` unrelated files.

   The premise behind scanning was wrong, not just the size of the result. The hook fires once per `Write` with one `file_path`; a 2000-file changed set never represents 2000 simultaneous saves, it represents a dirty repository. `MAX_LINT_TARGETS` was treating the symptom. It stays as a guard, but it stops being the routine path.

2. **The git-root search is unbounded upward.** `find_git_root()` walks `(start, *start.parents)`, which reaches `/`. Combined with #16 bind-mounting the scope root, an edit to a file sitting directly in a system directory mounts that directory. The state database holds a complete recursive walk of `/etc` — 37 lintable files across `init.d`, `profile.d`, `rc*.d`, `wpa_supplicant`, `docker`, `containerd` — which only happens if the scope root resolved to `/etc`.

3. **The git root is recomputed on every edit.** One `.git` stat per ancestor, per hook invocation, for a value that changes almost never.

4. **`gitleaks` never runs on the hook path.** `lint.sh` gates it on `$TARGET_DIR/.git` and the target is a single file. gitleaks has supported `--no-git` for exactly this case ("treat git repo as a regular directory and scan those files"), verified in the installed binary's help, so the check can produce a real verdict instead of a structural skip.

## Why this approach

Bounding the ancestor walk at `$HOME` is what removes defect 2, and it removes it at the source rather than by blocklisting directory names. A path outside `$HOME` has no ancestors worth searching, so no repository is claimed for it.

Caching keyed on the file's base folder is the right granularity: every file in a directory shares its repository, so one cached answer serves all of them, and the comparison on a later edit is a string comparison against an absolute path rather than a filesystem walk.

**Caching a git root is a staleness hazard, so the cache is validated rather than trusted.** `git init`, `rm -rf .git`, a clone, or a moved directory all change the answer. Every cache hit re-stats the recorded root's `.git` before being used, and re-resolves on a miss. One stat is cheaper than a full ancestor walk and cannot serve a wrong answer.

## Constraints

- The state database already exists in the field with schema `seen(path, size, mtime_ns, digest)`. New columns must be added by migration, not by assuming a fresh database.
- `MULTILINT_STATE_DIR` stays outside `~/.claude/` (issue #41156) and outside OpenCode's config directories.
- Change detection for the non-git path is not deleted. It stops selecting lint targets, but the digest bookkeeping is what makes an unchanged second edit a no-op.
- POSIX only, as established in #16.

## Tasks

- [x] **T4.1** `find_git_root()` now resolves `search_ceiling()` (`$HOME`, or `MULTILINT_SEARCH_CEILING`) and returns `None` without a single stat when the start path is not under it; the ancestor loop breaks at the ceiling. *(route: inline — one function)*
- [x] **T4.2** `open_state()` creates the table in its original shape and then adds `in_git`, `git_root`, `first_seen_at`, `last_seen_at` and `last_linted_at` by `ALTER TABLE`, guarded by `PRAGMA table_info` so a concurrent session's migration is a success rather than a duplicate-column error. *(route: inline)*
- [x] **T4.3** `folders(folder, git_root, resolved_at, last_used_at)` plus `cached_git_root()`. A hit re-stats `<root>/.git` before it is trusted, and a cached `NULL` is re-checked too, because a repository can appear where there was none. *(route: inline)*
- [x] **T4.4** `main()` lints `[relative]` — the edited file, nothing else. `record_and_check()` keeps the digest bookkeeping, so an identical re-save returns before the container starts. *(route: inline)*
- [x] **T4.5** `lint.sh` resolves the probe directory from the target (`dirname` when it is a file), walks up for `.git`, then runs git mode or `--no-git`. `STRUCTURALLY_SKIPPED` is now empty in both plugins. *(route: inline)*
- [x] **T4.6** `now_stamp()` — ISO-8601 UTC to the second. Written on row creation, every sighting, each lint, and both git-root cache events. `first_seen_at` is preserved by `COALESCE` rather than overwritten. *(route: inline)*
- [x] **T4.7** 68 tests in `test_lint_changed.py` (up from 34) and 8 gitleaks tests in `test_lint_sh_extended.py`. 142 unit tests total.

### Two further defects found while implementing T4.5, and fixed because they defeat it

Both were discovered by a test that *should* have passed and did not: gitleaks reported `0 findings` on a file containing a valid GitHub token.

1. **`--config /usr/local/bin/.gitleaks.toml` is a container-only path.** Run on a host or in CI, gitleaks died with `unable to load gitleaks config` and never scanned anything. Now resolved in order: `MULTILINT_GITLEAKS_CONFIG`, the image path, `.gitleaks.toml` beside `lint.sh`, then omitted so gitleaks uses its built-in rules instead of refusing to start. Container behaviour is unchanged because the image path is still tried first.

2. **A crashed gitleaks was reported as a pass.** The condition was `[ rc -eq 0 ] || [ findings -eq 0 ]`. Measured: gitleaks exits **1 both for "leaks found" and for a config error**, so the exit status cannot distinguish them and the `findings -eq 0` clause resolved the ambiguity in favour of ✓. Non-zero with no findings is now `skipped` with the exit code named. Without this fix T4.5 would have turned a permanent "skipped" into a permanent "passed", which is worse.

## Acceptance criteria

| Case | Expected |
| --- | --- |
| Edit in a dirty repository | Exactly one file linted — the edited one |
| Edit an unchanged file twice | Second edit is a no-op |
| Edit a file under `/etc` | No repository claimed, nothing above the file's directory searched |
| `.git` removed after being cached | Cache detected stale, root re-resolved, no wrong answer served |
| Pre-existing database from an older version | Migrated in place, no data loss, no crash |
| File in a repository | `in_git` true, `git_root` recorded, `gitleaks` runs in git mode |
| File outside any repository | `in_git` false, `gitleaks` runs with `--no-git` |

## Verification evidence

Measured 2026-09-30 by feeding real hook payloads to the script with `MULTILINT_STATE_DIR` pointed at a throwaway directory, then reading the database back.

| Case | Before | After |
| --- | --- | --- |
| A file in this repository, 4 files dirty | 52 files linted | **1** — one `seen` row, `in_git=1`, `git_root=/home/takeshi/docker-compose.d/multilint` |
| The same file again, unchanged | re-linted | **no-op**, nothing written, container never started |
| `/etc/docker/daemon.json` | full recursive walk of `/etc`, 37 rows | **1** row, `in_git=0`, `git_root=NULL`; the only `folders` entry is `/etc/docker`, so nothing above it was searched |

Unit suite: **142 passed**. `black --check --line-length=120` clean on 13 files; flake8 with the repo's `--extend-ignore` clean; `pylint --disable=C,R,E0401,E1123,W1510` → 10.00/10; `shellcheck` with the repo's flags clean; `bash -n lint.sh` clean; `node --check` clean.

Independently verified against the installed `gitleaks` before writing any code:

- `--no-git` exists and is documented as "treat git repo as a regular directory and scan those files".
- `gitleaks detect --no-git --source <a single file>` works, and still prints `Finding:`, which is the string `lint.sh` already counts — so the parsing needed no change.
- Exit codes measured directly: `0` clean, `1` leak found, `1` config error. That is what proves defect 2 above rather than assuming it.

## Findings recorded, not acted on

- **`lint.sh:58` sets `TARGET_DIR="${1:-.}"` and `:89` reads `"$TARGET_DIR/.multilint.json"`.** The hook passes a *file* as `$1`, so that path resolves under a file name and can never exist. **Per-project thresholds are therefore silently ignored on the entire hook path** and every threshold falls back to `0`. Invisible in this repository because its own `.multilint.json` sets every value to `0`, which is exactly why it survived: the fallback is indistinguishable from the configuration. Any project setting a non-zero threshold is affected. Not fixed — no instruction, and it changes threshold semantics for existing users.

- **A scope root outside `$HOME` is still mounted.** T4.1 stops the *search* above `$HOME`, so no repository is claimed, but an edit to `/etc/foo.json` still makes `/etc` the scope root and therefore the mount. Read-only, `--network none`, non-root uid, and after T4.4 nothing walks or hashes it — so the residual is "an offline throwaway container can read that one directory". Refusing a scope root outside `$HOME` outright is a one-line guard, deliberately left for the user to approve rather than added quietly.

## Next step

T4.1 through T4.7 are done and verified. Open for the user:

1. **The residual mount outside `$HOME`**, above. A one-line guard would refuse it; it is deliberately not added unilaterally.
2. **The `$TARGET_DIR/.multilint.json` defect**, above. Fixing it changes threshold semantics for anyone relying on the current silent `0`, so it needs a decision rather than a patch.
3. **T2.8 from the previous feature** — rebuilding the deployed container. Restarts a service, so it needs explicit approval. The container is now further behind: it carries neither the gitleaks fixes nor this scoping work.
