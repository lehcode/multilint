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

### Thresholds (`.multilint.json`)

Per-check configurable thresholds in the target directory. Any key omitted defaults to `0` (zero tolerance):

```json
{
  "bash_syntax": 0, "shellcheck": 0, "bashate": 0, "shfmt": 0,
  "flake8": 0, "black": 0, "pylint": 0, "markdownlint": 0,
  "yaml_prettier": 0, "json_prettier": 0, "toml_sort": 0,
  "security_secrets": 0, "security_dangerous_patterns": 0,
  "gitleaks": 0
}
```

A check passes if its failures ≤ threshold. Failures exceeding threshold are marked ⚠; otherwise ✓.

### Feature Toggles

Disable individual checks via environment variables:

| Variable | Effect |
|----------|--------|
| `MULTILINT_BLACK_CHECK=off` | Skip black formatting check |
| `MULTILINT_SHFMT_CHECK=off` | Skip shfmt formatting check |
| `MULTILINT_BASHATE_CHECK=off` | Skip bashate indentation check |
| `MULTILINT_SECURITY_CHECK=off` | Skip security scanning |
| `MULTILINT_GITLEAKS_CHECK=off` | Skip gitleaks scanning |
| `MULTILINT_TOML_CHECK=off` | Skip TOML linting |
| `MULTILINT_YAML_JSON_CHECK=off` | Skip YAML/JSON linting |

All enabled by default. Set to `"off"` to disable.

### Gitleaks Depth

| Variable | Effect |
|----------|--------|
| `MULTILINT_GITLEAKS_DEPTH=1` | Last commit only (default) |
| `MULTILINT_GITLEAKS_DEPTH=all` | Full git history |

## Policies

- **Zero tolerance** by default (configurable thresholds)
- **Fail fast**: bash syntax failure skips remaining checks for that file
- **4-space indentation**: bashate enforces this for shell scripts
- **Bashate E006**: excluded (line length)

## Deployment

MultiLint runs as a Docker container with `restart: unless-stopped`. The container is the deployment mechanism, not the product — the MCP server and HTTP API are what clients consume.

```bash
cd <project-root>
docker compose build
docker compose up -d
```

By default the compose file mounts the host's workspace as read-only at `/workspace` inside the container. This is what AI agents and CI pipelines lint.

## Release Process

Releases are automated with [release-please](https://github.com/googleapis/release-please). Conventional Commits merged to `master` open or update a **Release PR** tracking the next version and changelog. Merging that PR:

- Tags the release (`vX.Y.Z`)
- Creates a GitHub release
- Bumps `version` in `package.json`
- Updates `CHANGELOG.md`

The new tag then triggers `.github/workflows/docker-publish.yml`, which builds and pushes Docker images to Docker Hub and ghcr.io.

The project is pre-1.0, so `bump-minor-pre-major` is enabled in `release-please-config.json`: a `feat:` commit bumps the minor version (e.g. `0.1.6` → `0.2.0`) instead of the patch.

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

| Env var | Default | Description |
|---------|---------|-------------|
| `MULTILINT_HOST` | `http://localhost:8591` | Multilint HTTP API base URL (plugin only) |

### Limitations

- Plugin only hooks into tool executions; not triggered by file watchers or external edits
- Plugin is non-blocking — failures do not prevent the save from completing
- TOML check has a known CLI bug (`--check --sort-keys` flags conflict in `lint.sh` line 471)

## Troubleshooting

**AI agent can't connect** — verify the container is running (`docker ps --filter name=multilint`) and port 8592 is reachable.

**Lint results are empty or "(none found)"** — the target path or working directory is incorrect. Verify with the MCP `get_help()` tool or `GET /`.

**`pylint` passes a file you expect to fail** — only error-class messages are enabled. Run `pylint` directly for the full report.

**Security checks fail on legitimate files** — hardcoded secrets detection excludes `server.py`, `mcp_server.py`, `test_*.py`, and `__init__.py`. Adjust thresholds in `.multilint.json` or disable with `MULTILINT_SECURITY_CHECK=off`.
