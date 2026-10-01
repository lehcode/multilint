# multilint

An opencode MCP server that delegates code quality checks to AI clients. One server, three tools, eleven checks across five file types — shell, Python, Markdown, YAML/JSON, and TOML. Agents never invoke shellcheck, black, pylint, or gitleaks directly; they call `lint_files()` and receive structured results.

Compatible with any MCP-compatible client (Claude Code, Cursor, custom agents).

## Prerequisites

- **Docker** (with compose plugin)
- **opencode** (for MCP client installation)
- **Git** (for gitleaks scanning)

## Installation

### opencode (preferred)

```sh
opencode mcp add multilint --url http://localhost:8592/mcp
```

### Claude Code

Claude Code uses `claude mcp add` to connect. Use `--transport http` with the `/mcp` endpoint:

```sh
claude mcp add --transport http multilint http://localhost:8592/mcp
```

Add `--scope user` for global scope, `--scope project` to write `.mcp.json` (shared with team).

**JSON config** (`.mcp.json` in project root, or `~/.claude.json` globally):

```json
{
  "mcpServers": {
    "multilint": {
      "type": "http",
      "url": "http://localhost:8592/mcp"
    }
  }
}
```

> **Note**: Claude Code's CLI `--transport` flag only accepts `stdio`, `sse`, `http` (not `streamable-http`). Use `type: "http"` in JSON — `streamable-http` works as an alias there.

**Verify:**

```sh
claude mcp list
claude mcp get multilint
```

Or inside a Claude Code session: `/mcp`

### Other MCP clients

Same connection string — any MCP-compatible client connects identically:

```json
{
  "mcpServers": {
    "multilint": {
      "type": "http",
      "url": "http://localhost:8592/mcp"
    }
  }
}
```

## Tools

| Tool | Parameters | Purpose |
|------|-----------|---------|
| `lint_files` | `path` (string), `cwd` (string) | Run full linting pipeline against target directory |
| `health_check` | none | Verify service is running |
| `get_help` | none | Usage information |

All parameters default to workspace root (`.`) and server working directory respectively.

## HTTP API

For CI/CD pipelines and non-MCP clients — port **8591**:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `POST /lint` | POST | Run linting pipeline with JSON payload |
| `GET /health` | GET | Health check |
| `GET /` | GET | Service info |

**POST /lint payload:**

```json
{"path": "./my-dir/", "cwd": "/workspace/"}
```

## Checks

**Shell** (`*.sh`/`*.bash`) — 4 checks per file:

- **Bash syntax** (`bash -n`) — unclosed quotes, bad syntax
- **ShellCheck** — zero tolerance, excludes SC1091/SC2155/SC2086, style disabled
- **Bashate** — 4-space indentation, E006 (line length) excluded
- **shfmt** — format check only

**Python** (`*.py`) — 3/3 code quality benchmark:

- **flake8** (style, 120-col, ignores E203/E111/E121/E124/BLK100)
- **black** (formatting, 120-col)
- **pylint** (errors/warnings, disables C,R,E0401,E1123,W1510)

Any tool failure increments the fail counter; benchmark fails if ≥1/3 tools fail.

**Markdown** (`*.md`) — **markdownlint** with minimal rules (MD003, MD013, MD024, MD041 disabled).

**YAML / JSON** — **prettier --check** (error log level).

**TOML** — **toml-sort --check --sort-keys**.

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
  "mypy": { "cache_dir": "/tmp/.mypy_cache_project" }
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
|---|---|---|
| `bash_syntax` | `-n` | n/a — `args` unsupported |
| `shellcheck` | `-f gcc` (after the policy flags, so findings parse) | `-e SC1091 -e SC2155 -e SC2086 -S style` |
| `bashate` | none | `-i E006` |
| `shfmt` | `-d` | `-i 4` |
| `flake8` | none | `--max-line-length=120 --extend-ignore=E203,E111,E121,E124,BLK100` |
| `black` | `--check` | `--line-length=120` |
| `pylint` | `--output-format=text` | `--disable=C,R,E0401,E1123,W1510` |
| `mypy` | `--cache-dir=<mypy.cache_dir>` `--no-error-summary` | `--ignore-missing-imports --follow-imports=silent` |
| `bandit` | `-q <bandit.severity> -f custom --msg-template ...` | empty — severity is the `bandit.severity` option, not `args` |
| `markdownlint` | none | `-c .markdownlint.json` when that file exists in `$PWD`, else empty |
| `yaml_prettier` / `json_prettier` | `--check --log-level error` | empty |
| `toml_sort` | `--check` | `--sort-keys` |
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

### Host-plugin settings (`image`, `search_ceiling`)

The Docker image both plugins run and the highest directory the Python hook
may search for a project root are **not** `.multilint.json` keys — they
configure the plugin process itself, not a check. They live in a small
SQLite `settings` table instead, managed through a CLI mode in
`claude-plugin/scripts/lint_changed.py`:

```bash
python3 claude-plugin/scripts/lint_changed.py --set image local/multilint:dev
python3 claude-plugin/scripts/lint_changed.py --get image
python3 claude-plugin/scripts/lint_changed.py --unset image
python3 claude-plugin/scripts/lint_changed.py --set search_ceiling /home/user/projects
```

Only `image` and `search_ceiling` are accepted keys; any other key exits
non-zero and writes nothing. A key with no stored setting falls back to its
built-in default (`lehcode/multilint:latest` for `image`, the user's home
directory for `search_ceiling`). The OpenCode plugin reads the same table
read-only and never writes to it.

## Policies

- **Zero tolerance** by default (configurable thresholds)
- **Fail fast**: bash syntax failure skips remaining checks for that file
- **4-space indentation**: enforced by both `bashate` and `shfmt -i 4`. The `-i 4` is required — shfmt's default is tab indents, which `bashate` rejects as E002, so without it the two checks demand opposite things and no shell file can pass both.
- **Bashate E006**: excluded (line length)

## Deployment

MultiLint runs as a Docker container with `restart: unless-stopped`. The container is the deployment mechanism, not the product — the MCP server and HTTP API are what clients consume.

```bash
cd <project-root>
docker compose build
docker compose up -d
```

The compose file mounts `MULTILINT_WORKSPACE` read-only at `/workspace` inside the container, defaulting to the directory you run compose from. This is what AI agents and CI pipelines lint. Set it explicitly to lint somewhere else:

```bash
MULTILINT_WORKSPACE=/path/to/project docker compose up -d
```

## Developer Guide

### Dockerfile Stages

**Stage 1 — `node-tools`**: `node:20` installs `markdownlint-cli` and `prettier` globally. Only used to copy binaries into the final image.

**Stage 2 — `python:3.12-slim`**: Runtime base. Three categories:

| Category | Packages | Install method |
|----------|----------|----------------|
| System | `shellcheck`, `shfmt`, `bash`, `curl`, `nodejs` | apt |
| Node.js | `prettier` (markdownlint from stage 1) | corepack |
| Python | `flake8`, `pylint`, `black`, `mypy`, `bashate`, `bandit`, `fastmcp`, `mcp`, `toml-sort` | pip |

Gitleaks (v8.30.1) downloaded directly from GitHub releases.

### File Placement

All source files copied to `/usr/local/bin/`: `lint.sh`, `.gitleaks.toml`, `server.py`, `mcp_server.py`, `entrypoint.sh`.

### ENTRYPOINT

`/usr/local/bin/entrypoint.sh` — dispatches based on `CONTINUOUS_LINT`:

- `1` → runs HTTP + MCP servers as persistent background processes
- `0`/unset → runs `lint.sh` and exits

### Invocation: `path` vs `cwd`

The `lint_files` tool takes two string parameters with different roles:

| Parameter | Role | What it controls |
|-----------|------|-----------------|
| `path` | **What** to lint | Directory where `find` searches for files (passed to `lint.sh` as `$1`) |
| `cwd` | **Context** for linting | Working directory for `subprocess.run()` — controls import resolution, relative includes, git history scanning |

**Example:** `lint_files(path="./src/", cwd="/home/takeshi/my-project/")`

- `path="./src/"` → `lint.sh` runs `find ./src/` → only lints files under `./src/`
- `cwd` → Python imports resolve from there, `gitleaks --source` sees the `.git`, shell `source ../config.sh` resolves relative to the project root

In the typical case both are the same — the repo root. `cwd` matters when the repo root differs from the subdirectory you want to lint.

### Output Format

`lint.sh` supports a `--format` flag (applies to direct invocation and the HTTP API):

- `text` (default) — terminal output to stderr, JSON summary to stdout
- `json` — structured output on stdout with `summary`, `checks`, `return_code`, and `files` fields

The MCP tools always return structured results regardless of this flag.

**Findings.** In `json` mode every failed check also carries the reason, so a client never has to re-run a linter. All three fields are additive; no existing field changes name, type or position:

- `checks.<name>.findings` — `[{"file", "line", "rule", "message"}]`, one per reported problem, parsed from the tool's own output. `line` and `rule` are `null` when the tool gives none; a line that does not parse is kept with both `null` rather than dropped. pylint and markdownlint findings add a `symbol` (`bad-indentation`, `no-trailing-spaces`). Capped at 50 per check, with `findings_truncated: true` when cut.
- `checks.<name>.fix` — one-line hint. Formatters (`black`, `shfmt`, `yaml_prettier`, `json_prettier`, `toml_sort`) give the exact auto-fix command with the effective policy flags and one `formatting required` finding per file; `shellcheck` and `markdownlint` append a documentation link per rule; the rest say to fix the code and name the `checks.<name>.args` override.
- `summary.rules_violated` — sorted unique rule IDs across all failed checks.

Both plugins build their notice from these fields and fall back to the old `✗`/`⚠` marker lines when a document has no `findings` (an older image).

### Optimization

- `pip install --no-cache-dir` — no pip cache persisted
- `rm -rf /var/lib/apt/lists/*` — apt cache cleaned
- `mypy` installed for dev testing only (not used in linting)
- `bandit` installed but lint.sh uses grep-based security scanning

## OpenCode Integration

MultiLint integrates with OpenCode through three mechanisms, configured in `opencode.json`:

### 1. Plugin — Automatic linting on file save

```json
"plugin": ["./.opencode/plugins/multilint-lint.js"]
```

The plugin runs linting automatically after every file save — no agent involvement needed.

**How it works:**

1. OpenCode loads `multilint-lint.js` as a V2 plugin (`export default { id, setup }`)
2. The plugin hooks into `ctx.tool.hook("execute.after", ...)` — fires after every tool execution
3. Extracts the file path from tool input, checks extension against whitelist
4. Calls `POST http://localhost:8591/lint` with the file's parent directory
5. Errors are **appended to the tool result** so the agent sees them in context
6. Silent if server is unreachable or no errors found

**Flow:**

```text
Agent writes sample.sh → tool executes → execute.after fires
  → Plugin checks ext=".sh" → matches whitelist
  → Plugin POSTs to /lint with path="./test-lint/"
  → Plugin appends errors to tool result
  → Agent sees: "multilint reported errors in sample.sh. Fix them before continuing."
```

**Lintable extensions:** `.sh`, `.bash`, `.py`, `.md`, `.yaml`, `.yml`, `.json`, `.toml`

### 2. MCP Server — Manual linting via `lint_files()`

```json
"mcp": {
  "servers": {
    "multilint": {
      "type": "remote",
      "url": "http://localhost:8592/mcp"
    }
  }
}
```

Connects the OpenCode agent to the multilint MCP server on port 8592 (streamable-http transport). The agent calls `multilint.lint_files(path, cwd)` to run the full pipeline on any directory and receives structured results (`stdout`, `stderr`, `return_code`).

Use this for **explicit/manual linting** or when the agent needs to lint an arbitrary directory not being edited.

### 3. Agent — Dedicated system prompt

```json
"agent": {
  "multilint": {
    "description": "Run code quality linting via the multilint service.",
    "prompt": "agents/multilint.md"
  }
}
```

When the multilint agent is selected, OpenCode loads `agents/multilint.md` as the system prompt. This gives the agent instructions on when and how to use the multilint MCP tools.

### Configuration

The OpenCode plugin reads no environment variables. It runs the image named by the `image` setting (see "Host-plugin settings" above), else `lehcode/multilint:latest`, and lints with the project's `.multilint.json`.

### Limitations

- Plugin only hooks into tool executions; not triggered by file watchers or external edits
- Plugin is non-blocking — failures do not prevent the save from completing
- TOML check has a known CLI bug (`--check --sort-keys` flags conflict in `lint.sh` line 471)

## Troubleshooting

**AI agent can't connect** — verify the container is running (`docker ps --filter name=multilint`) and port 8592 is reachable.

**Lint results are empty or "(none found)"** — the target path or working directory is incorrect. Verify with the MCP `get_help()` tool or `GET /`.

**`pylint` passes a file you expect to fail** — only error-class messages are enabled. Run `pylint` directly for the full report.

**Security checks fail on legitimate files** — hardcoded secrets detection excludes `server.py`, `mcp_server.py`, `test_*.py`, and `__init__.py`. Adjust thresholds in `.multilint.json`, or disable a check with `"checks": {"security_secrets": {"enabled": false}}` (or `security_dangerous_patterns`).
