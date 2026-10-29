# multilint

[![Develop Checks](https://github.com/lehcode/multilint/actions/workflows/develop-checks.yml/badge.svg)](https://github.com/lehcode/multilint/actions/workflows/develop-checks.yml)
[![CodeQL](https://github.com/lehcode/multilint/actions/workflows/github-code-scanning/codeql/badge.svg)](https://github.com/lehcode/multilint/actions/workflows/github-code-scanning/codeql)
[![Gitleaks](https://github.com/lehcode/multilint/actions/workflows/gitleaks.yml/badge.svg)](https://github.com/lehcode/multilint/actions/workflows/gitleaks.yml)
[![Docker publish](https://github.com/lehcode/multilint/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/lehcode/multilint/actions/workflows/docker-publish.yml)
[![npm publish](https://github.com/lehcode/multilint/actions/workflows/npm-publish.yml/badge.svg)](https://github.com/lehcode/multilint/actions/workflows/npm-publish.yml)
[![GitHub release](https://img.shields.io/github/v/release/lehcode/multilint)](https://github.com/lehcode/multilint/releases)
[![npm](https://img.shields.io/npm/v/@lehcode/multilint)](https://www.npmjs.com/package/@lehcode/multilint)
[![Docker image](https://img.shields.io/docker/v/lehcode/multilint?sort=semver&label=docker)](https://hub.docker.com/r/lehcode/multilint)
[![Image size](https://img.shields.io/docker/image-size/lehcode/multilint?sort=semver)](https://hub.docker.com/r/lehcode/multilint)
[![License: MIT](https://img.shields.io/github/license/lehcode/multilint)](LICENSE)
[![Conventional Commits](https://img.shields.io/badge/Conventional%20Commits-1.0.0-yellow.svg)](https://www.conventionalcommits.org)

Lints every file an AI coding agent edits, and hands the findings straight back to the agent. multilint ships as a **Claude Code plugin** and an **OpenCode plugin**; both run the same engine — 16 checks across shell, Python, Markdown, YAML/JSON and TOML, plus secret scanning — in a short-lived Docker container, one per lint.

## Prerequisites

- **Docker** — every lint runs in a throwaway `lehcode/multilint` container (about 500 MB, pulled once). Without Docker the hook reports the file as unverified.
- **python3** on the host — runs the hook. Without it the hook prints a notice and exits.
- **Git** (optional) — `gitleaks` scans repository history.

## Installation

### Claude Code

```text
/plugin marketplace add lehcode/multilint
/plugin install multilint@lehcode
```

The plugin adds a `PostToolUse` hook on `Write|Edit` and a `multilint` subagent for explicit lint and configuration requests.

### OpenCode

Add the npm package to `opencode.json` (or `opencode.jsonc`); OpenCode installs it on first load:

```json
{
  "plugin": ["@lehcode/multilint"]
}
```

Pin a version with `@lehcode/multilint@<version>`. The plugin supports OpenCode V1 (`tool.execute.after`) and V2 (`execute.after`).

## How it works

1. The agent writes or edits a file with a lintable extension: `.sh`, `.bash`, `.py`, `.md`, `.yaml`, `.yml`, `.json`, `.toml`.
2. The plugin passes the edit to `scripts/multilint.sh`, which runs `scripts/lint_changed.py`. It finds the project root (the git root, searched no higher than the `search_ceiling` setting) and skips the file when its content has not changed since the last lint, re-reporting the last verdict instead.
3. `lint_changed.py` starts one ephemeral container:

   | Property | Reason |
   | --- | --- |
   | `docker run --rm`, one per lint | Nothing persists between runs; Docker is the only dependency. |
   | The plugin's own `scripts/` mounted read-only at `/opt/multilint` | The container runs the plugin's `lint.sh`; the image supplies only the linters. |
   | The project root mounted read-only at its own path, and used as the working directory | Findings name host paths, nothing can be auto-fixed, and `.multilint.json` resolves from the root. |
   | `--network none`, `--user <uid>:<gid>`, `--memory 2g --cpus 2` | No network, nothing runs as root, and a run cannot starve the machine. |

4. Findings, fix hints and violated rules go back to the agent (Claude Code: `additionalContext`, plus a one-line `systemMessage` for the user; OpenCode: appended to the tool result). A check that could not run is reported as skipped, never as a pass.

Only the edited file is linted; changes made outside a write tool (`sed -i` through Bash, a generator) are not picked up. Use the subagent, or `lint_changed.py --run <root> <target>`, to lint a directory.

## Checks

**Shell** (`*.sh`/`*.bash`) — 4 checks per file:

- **Bash syntax** (`bash -n`) — unclosed quotes, bad syntax
- **ShellCheck** — zero tolerance, excludes SC1091/SC2155/SC2086, style disabled
- **Bashate** — 4-space indentation, E006 (line length) excluded
- **shfmt** — format check only

**Python** (`*.py`):

- **flake8** (style, 120-col, ignores E203/E111/E121/E124/BLK100)
- **black** (formatting, 120-col)
- **pylint** (errors/warnings, disables C,R,E0401,E1123,W1510)
- **mypy** (`--ignore-missing-imports --follow-imports=silent`)
- **bandit** (security linter; severity set by `bandit.severity`)

**Markdown** (`*.md`) — **markdownlint** with every rule except MD013 (line length), unless the project has its own markdownlint config (see [A linter's own config wins](#a-linters-own-config-wins)).

**YAML / JSON** — **prettier --check** (error log level).

**TOML** — **toml-sort --check --sort-table-keys**.

**Security** (grep-based):

- **Hardcoded secrets** — password/api_key/token assignment with non-empty values in shell and Python files
- **Dangerous patterns** — `chmod 777`, `curl | bash`, `eval` with variable expansion

**Gitleaks** — git history secret detection via `gitleaks detect` (skipped when `.git` not found).

## Configuration

`.multilint.json` is the single configuration surface for `lint.sh`. There is
no environment-variable layer for any threshold, enablement flag, or tool
option (user decision, 2026-09-30: environment variables are not a multilint
configuration surface) — a setting is either a value in this file or the
built-in default; nothing else participates.

Ask the `multilint` subagent to change it ("disable gitleaks", "allow 2 flake8 failures"), or run the config script that ships with the plugin: `python3 <plugin>/scripts/multilint_config.py <verb>` with `show`, `enable`, `disable`, `threshold`, `args`, `clear-args` or `option`, and `--root <dir>` first to target another project.

### Lookup order

`lint.sh` resolves the config file as `$PWD/.multilint.json` first, falling
back to `<target-dir>/.multilint.json` only when the target argument is a
directory. A single-file target (the plugin invocation shape) never falls
back to a directory-relative path — this is why both plugins set their
working directory to the project's scope root before invoking `lint.sh`.

### Schema

Two forms per check, both accepted in the same file:

```json
{
  "shellcheck": 0,
  "checks": {
    "flake8": { "enabled": true, "threshold": 2, "args": ["--max-line-length=100"] },
    "pylint": { "enabled": false }
  },
  "gitleaks": { "depth": "all", "config": ".gitleaks.toml" },
  "bandit": { "severity": "-lll" },
  "mypy": { "cache_dir": "/tmp/.mypy_cache_project" },
  "markdownlint": { "rules": { "MD041": false } }
}
```

- **Flat form**: `"<check>": <non-negative integer>` sets that check's
  threshold. Valid for all 16 checks (`bash_syntax`, `shellcheck`, `bashate`,
  `shfmt`, `flake8`, `black`, `pylint`, `mypy`, `bandit`, `markdownlint`,
  `yaml_prettier`, `json_prettier`, `toml_sort`, `security_secrets`,
  `security_dangerous_patterns`, `gitleaks`).
- **Object form**: `"checks.<name>"` accepts `enabled` (boolean), `threshold`
  (non-negative integer), and `args` (array of strings). If a check has both
  a flat and an object threshold, `checks.<name>.threshold` wins and a
  warning names the redundant flat key.
- `gitleaks`, `bandit`, and `mypy` double as option-group keys, disambiguated
  by JSON type: an integer is a flat threshold, an object carries
  `gitleaks.depth`/`gitleaks.config`, `bandit.severity`, or `mypy.cache_dir`.
- `markdownlint.rules` is an object of markdownlint rule settings, merged over the built-in set (every rule, MD013 off). It is ignored, with a warning, when the project has its own markdownlint config or sets `checks.markdownlint.args`.

Any key omitted resolves to its built-in default (threshold `0`, enablement
`on`). A malformed file, an unrecognized key, or a wrongly typed value
produces a warning — stderr in text mode, a top-level `"warnings"` array in
JSON mode — and that single setting falls back to its default rather than
silently reading as `0`.

A check passes if its failures are at or below its threshold; the process
exit code and the JSON `return_code` are `1` if and only if at least one
enabled check's failures exceed its threshold. Failures exceeding threshold
are marked ⚠ in text output; otherwise ✓.

### Every check can be disabled

`"checks": { "<name>": { "enabled": false } }` skips that check for any of
the 16 checks — including `bash_syntax`, `shellcheck`, `flake8`, `pylint`,
and `markdownlint`, which had no toggle before this schema. A disabled check
reports `status: "skipped"`, distinct from a check that ran and found
nothing.

### `args` replaces built-in policy flags; harness flags are always kept

Each tool-backed check's flags split into **harness flags** (what makes the
invocation a non-writing, countable check at all — never affected by `args`)
and **policy flags** (the tool's opinionated defaults, replaced wholesale by
`checks.<name>.args` when present):

| Check | Harness flags (always passed) | Default policy flags (`args` replaces these) |
| --- | --- | --- |
| `bash_syntax` | `-n` | n/a — `args` unsupported |
| `shellcheck` | `-f gcc` (after the policy flags, so findings parse) | `-e SC1091 -e SC2155 -e SC2086 -S style` |
| `bashate` | none | `-i E006` |
| `shfmt` | `-d` | `-i 4` |
| `flake8` | none | `--max-line-length=120 --extend-ignore=E203,E111,E121,E124,BLK100` |
| `black` | `--check` | `--line-length=120` |
| `pylint` | `--output-format=text` | `--disable=C,R,E0401,E1123,W1510` |
| `mypy` | `--cache-dir=<mypy.cache_dir>` `--no-error-summary` | `--ignore-missing-imports --follow-imports=silent` |
| `bandit` | `-q <bandit.severity> -f custom --msg-template ...` | empty — severity is the `bandit.severity` option, not `args` |
| `markdownlint` | none | `-c <scripts>/ml_markdownlint.json` (every rule, MD013 off); with `markdownlint.rules`, `-c` a temp file holding those rules merged over that set |
| `yaml_prettier` / `json_prettier` | `--check --log-level error` | empty |
| `toml_sort` | `--check` | `--sort-table-keys` |
| `security_secrets` / `security_dangerous_patterns` | grep pattern | n/a — `args` unsupported |
| `gitleaks` | `detect`, `--source`/`--no-git --source`, config/depth flags, `--verbose --no-color --no-banner` | empty |

`"args": []` runs the check with harness flags only and no policy flags,
letting the tool's own configuration discovery from `$PWD` take over. `args`
never removes a harness flag: `"checks": {"black": {"args": []}}` still runs
`black --check`, so the check never rewrites the target file — it only drops
`--line-length=120`. Some default policy flags are container-coupled rather
than merely stylistic (`shfmt`'s `-i 4`, which is what keeps it from
contradicting `bashate`; `mypy`'s import flags, because the image installs no
project dependencies), so replacing them produces no runtime warning — that
is a deliberate, self-announcing choice, not an oversight.

### A linter's own config wins

When the project already has a linter's own config, multilint passes that linter harness flags only: its default policy flags and `checks.<name>.args` are both dropped, and the tool reads its config itself. Dropping `args` produces a warning (`checks.<name>.args ignored: <file> takes precedence`); dropping the defaults is silent. `bash_syntax`, `bashate`, `bandit` and the grep-based security checks are unaffected.

| Check | Its own config, looked up in `$PWD` (the scope root) only |
| --- | --- |
| `shellcheck` | `.shellcheckrc` |
| `shfmt` | `.editorconfig` |
| `flake8` | `.flake8`; `[flake8]` in `setup.cfg` or `tox.ini` |
| `black` | `[tool.black]` in `pyproject.toml` |
| `pylint` | `.pylintrc`, `pylintrc`; `[tool.pylint…]` in `pyproject.toml`; `[pylint…]` in `setup.cfg` or `tox.ini` |
| `mypy` | `mypy.ini`, `.mypy.ini`; `[tool.mypy]` in `pyproject.toml`; `[mypy]` in `setup.cfg` |
| `markdownlint` | `.markdownlint.json`, `.markdownlint.jsonc`, `.markdownlint.yaml`, `.markdownlint.yml`, `.markdownlintrc` |
| `yaml_prettier` / `json_prettier` | `.prettierrc`, `.prettierrc.{json,json5,yaml,yml,toml,js,cjs,mjs}`, `prettier.config.{js,cjs,mjs}`, a `"prettier"` key in `package.json` |
| `toml_sort` | `[tool.tomlsort]` in `pyproject.toml` |
| `gitleaks` | `.gitleaks.toml`, passed as `--config`; wins over `gitleaks.config` and the image's config |

### Host-plugin settings (`image`, `search_ceiling`)

The Docker image both plugins run and the highest directory the Python hook may search for a project root are **not** `.multilint.json` keys — they configure the plugin process itself, not a check. They live in a SQLite `settings` table in `$XDG_STATE_HOME/multilint/changes.db` (default `~/.local/state/multilint/`), written only by `lint_changed.py`:

```bash
python3 <plugin>/scripts/multilint_config.py image local/multilint:dev
python3 <plugin>/scripts/multilint_config.py unset-image
python3 <plugin>/scripts/multilint_config.py search-ceiling /home/user/projects
python3 <plugin>/scripts/multilint_config.py unset-search-ceiling
```

The config script delegates to `lint_changed.py --set/--get/--unset`, which can also be called directly. Only `image` and `search_ceiling` are accepted keys; any other key exits non-zero and writes nothing. A key with no stored setting falls back to its built-in default: the release tag pinned in `scripts/ml_settings.py` (`lehcode/multilint:v<version>`) for `image`, the user's home directory for `search_ceiling`. The OpenCode plugin reads the same table read-only and never writes to it.

## Policies

- **Zero tolerance** by default (configurable thresholds)
- **Fail fast**: bash syntax failure skips remaining checks for that file
- **4-space indentation**: enforced by both `bashate` and `shfmt -i 4`. The `-i 4` is required — shfmt's default is tab indents, which `bashate` rejects as E002, so without it the two checks demand opposite things and no shell file can pass both.
- **Bashate E006**: excluded (line length)

## Output format

`lint.sh` supports a `--format` flag:

- `text` (default) — human-readable report on stdout, config warnings on stderr
- `json` — the JSON document on stdout (`summary`, `checks`, `warnings`, `return_code`, `files`), the human-readable report on stderr

Both plugins run `lint.sh --format json`.

**Findings.** In `json` mode every failed check also carries the reason, so a client never has to re-run a linter. All three fields are additive; no existing field changes name, type or position:

- `checks.<name>.findings` — `[{"file", "line", "rule", "message"}]`, one per reported problem, parsed from the tool's own output. `line` and `rule` are `null` when the tool gives none; a line that does not parse is kept with both `null` rather than dropped. pylint and markdownlint findings add a `symbol` (`bad-indentation`, `no-trailing-spaces`). Capped at 50 per check, with `findings_truncated: true` when cut.
- `checks.<name>.fix` — one-line hint. Formatters (`black`, `shfmt`, `yaml_prettier`, `json_prettier`, `toml_sort`) give the exact auto-fix command with the effective policy flags and one `formatting required` finding per file; `shellcheck` and `markdownlint` append a documentation link per rule; the rest say to fix the code and name the `checks.<name>.args` override.
- `summary.rules_violated` — sorted unique rule IDs across all failed checks.

Both plugins build their notice from these fields and fall back to the old `✗`/`⚠` marker lines when a document has no `findings` (an older image).

## Developer guide

### Layout

| Path | Content |
| --- | --- |
| `core/` | The engine: `lint.sh` with `ml_*.sh` / `ml_*.py` modules, the hook (`lint_changed.py`, `multilint.sh`) and the config script. Edit here only. |
| `scripts/`, `claude-plugin/scripts/`, `opencode-plugin/scripts/` | Copies of `core/`, written by `scripts/sync-core.sh`; `scripts/sync-core.sh --check` fails on drift (pre-commit hook). |
| `.claude-plugin/`, `hooks/`, `agents/` | Claude Code marketplace, plugin manifest, hook and subagent. |
| `src/`, `dist/`, `index.js` | OpenCode plugin: TypeScript source, esbuild bundle (`npm run build`), V2 directory entry. |
| `Dockerfile` | Linter toolchain image; it contains no multilint code. |

### Docker image

Three stages: `node-tools` installs `markdownlint-cli` and `prettier`; `binaries` downloads `shellcheck`, `shfmt` and `gitleaks` (8.30.1); the final `python:3.12-slim` stage pip-installs `flake8`, `black`, `pylint`, `mypy`, `bandit`, `bashate` and `toml-sort`, adds `git`, copies the node tools, binaries and `.gitleaks.toml`, verifies every tool, and runs as `nobody`. There is no entrypoint: the plugin mounts its own `lint.sh` at `/opt/multilint` and runs it.

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/unit -q
python -m pytest tests/integration -q   # needs Docker and the image; skipped otherwise
```

CI (`Develop Checks`) runs the unit tests, flake8, black, shellcheck and the npm build on pull requests into `develop` and `master`.

### Releases

Conventional commits on `master` drive [release-please](https://github.com/googleapis/release-please): it keeps a release PR open and queues it for auto-merge, so the PR merges once the required checks pass. The merge tags `v<version>` and publishes the GitHub release; the tag builds and pushes the image to Docker Hub and ghcr.io, and the release publishes the npm package through npm trusted publishing (no token in CI). Only `feat`, `fix` and breaking changes produce a release.

## Troubleshooting

**`docker not found on PATH` or the daemon is unreachable** — the file is reported as unverified, not clean. Start Docker; the first lint pulls the image.

**A check reports `skipped`** — it did not run (disabled, tool missing, or nothing applicable). `skipped` is never a pass; the notice names the reason.

**`gitleaks` never runs on an edited file** — it scans history and only runs when the target contains `.git`. Lint the repository root through the subagent to exercise it.

**`pylint` passes a file you expect to fail** — only error-class messages are enabled. Run `pylint` directly for the full report.

**Security checks fail on legitimate files** — hardcoded-secret detection skips `test_*.py`, `__init__.py`, and paths under `fixtures/` or `test_project/`. Adjust thresholds in `.multilint.json`, or disable a check with `"checks": {"security_secrets": {"enabled": false}}` (or `security_dangerous_patterns`).

**A repository above your home directory is not found** — raise the `search_ceiling` setting.

## License

[MIT](LICENSE)
