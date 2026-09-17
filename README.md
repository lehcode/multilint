# multilint

A single-purpose container that lints shell, Python, Markdown, YAML, JSON, and TOML files in a target directory. Exposes functionality via HTTP API and MCP server for AI agent delegation.

## Build

```bash
cd ~/docker-compose.d/multilint
docker compose build
```

## Service

Managed by Docker Compose with `restart: unless-stopped`:

```bash
cd ~/docker-compose.d/multilint
docker compose up -d
docker compose restart
```

Check status:

```bash
docker ps --filter name=multilint
docker compose ps
```

## Operational Modes

**Continuous mode** (default, `CONTINUOUS_LINT=1`): HTTP API server on port 8591 + MCP server on port 8592 run as persistent background processes. The container stays running and serves requests on demand.

**One-shot mode** (`CONTINUOUS_LINT` unset or `0`): Runs `lint.sh` directly and exits after completion. Use for ad-hoc linting without keeping the service running:

```bash
docker exec multilint bash /usr/local/bin/lint.sh [dir] [--format json]
```

## MCP Server

Exposes three MCP tools over Streamable HTTP on port 8592 (`/mcp` path):

| Tool | Purpose |
|------|---------|
| `lint_files(path, cwd)` | Run full linting pipeline |
| `health_check()` | Verify service is running |
| `get_help()` | Get usage information |

Connect from any MCP-compatible client:

```json
{
  "mcpServers": {
    "multilint": {
      "url": "http://localhost:8592",
      "transport": "streamable-http"
    }
  }
}
```

## HTTP API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `POST /lint` | POST | Run linting pipeline |
| `GET /health` | GET | Health check |
| `GET /` | GET | Service info |

**POST /lint payload:**

```json
{"path": "./my-dir/", "cwd": "/workspace/"}
```

Both `path` and `cwd` are optional. `path` defaults to `.` (workspace root), `cwd` defaults to the server's working directory.

## JSON Output

Add `--format json` to any `docker exec` call for structured output on stdout (terminal output goes to stderr):

```bash
docker exec multilint bash /usr/local/bin/lint.sh /workspace/ --format json
```

Returns a JSON object with `summary`, `checks`, `return_code`, and `files` fields.

## Configuration

### Thresholds (`.multilint.json`)

Per-check configurable thresholds via `.multilint.json` in the target directory. Any key omitted defaults to `0` (zero tolerance):

```json
{
  "bash_syntax": 0, "shellcheck": 0, "bashate": 0, "shfmt": 0,
  "flake8": 0, "black": 0, "pylint": 0, "markdownlint": 0,
  "yaml_prettier": 0, "json_prettier": 0, "toml_sort": 0,
  "security_secrets": 0, "security_dangerous_patterns": 0,
  "gitleaks": 0
}
```

A check passes if its failures are ≤ threshold. Failures exceeding the threshold are marked ⚠, otherwise ✓.

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

Control gitleaks scan scope:

| Variable | Effect |
|----------|--------|
| `MULTILINT_GITLEAKS_DEPTH=1` | Last commit only (default) |
| `MULTILINT_GITLEAKS_DEPTH=all` | Full git history |

## What Gets Checked

**Shell** — each `*.sh`/`*.bash` file gets:

- **Bash syntax check** (`bash -n`) — unclosed quotes, bad syntax
- **ShellCheck** — zero tolerance, excludes SC1091/SC2155/SC2086, style disabled
- **Bashate** — 4-space indentation, E006 (line length) excluded
- **shfmt** — format check only

**Python** — each `*.py` file runs a **3/3 code quality benchmark**:

- **flake8** (style, 120-col, ignores E203,E111,E121,E124,BLK100)
- **black** (formatting, 120-col)
- **pylint** (errors/warnings, disables C,R,E0401,E1123,W1510)

Any tool failure increments the fail counter; benchmark fails if ≥1/3 tools fail.

**Markdown** — each `*.md` file gets **markdownlint** with minimal rules (MD003, MD013 disabled).

**YAML / JSON** — each file gets **prettier --check** (error log level).

**TOML** — each file gets **toml-sort --check --sort-keys**.

**Security** (grep-based):

- **Hardcoded secrets** — password/api_key/token assignment with non-empty values in shell and Python files (excludes server/mcp/test files and comments in lint.sh)
- **Dangerous patterns** — `chmod 777`, `curl | bash`, `eval` with variable expansion in shell scripts

**Gitleaks** — git history secret detection via `gitleaks detect` (skipped when `.git` not found).

## Policies

- **Zero tolerance** by default (configurable thresholds)
- **Fail fast**: bash syntax failure skips remaining checks for that file
- **4-space indentation**: bashate enforces this for shell scripts
- **Bashate E006**: excluded (line length)

## Which Files Are Skipped

Discovery uses `find` and prunes: dot-paths (`.git/`, `.claude/`), `venv/`, `node_modules/`, `test/`, `__pycache__/`, `cache/`, `output/`, `fixtures/`.

## Reading the Output

Each file is printed with a per-checker `✓` or `✗`, followed by a summary banner. The container exits `0` only when every check on every file passed.

A `✗` is accompanied by the tool's own diagnostics. A `✗` on `shfmt` or `black` carries no detail — the fix is mechanical.

Check summaries show failures vs. threshold: `✓ check: 0 failures (threshold: 0)` or `⚠ check: 3 failures (threshold: 0)`.

## Design Note: Multi-stage Dockerfile

markdownlint (Node.js) is built in a separate `node:20` stage, then copied into the final `python:3.12-slim` image. This avoids installing Node runtime in the Python container. Prettier is installed via `corepack` in the Python stage (avoids heavy apt `nodejs`). Gitleaks v8.30.1 is downloaded and extracted directly.

## Troubleshooting

**`bash: /usr/local/bin/lint.sh: No such file or directory`** — rebuild the image.

**Every file reports "(none found)"** — the mount is missing or the target path is wrong. Confirm with `docker exec multilint ls /workspace`.

**`pylint` passes a file you expect to fail** — only error-class messages are enabled. Run it directly through `docker exec`, without `--disable`, to see the full report.

**Security checks fail on legitimate files** — hardcoded secrets detection excludes `server.py`, `mcp_server.py`, `test_*.py`, and `__init__.py`. Add exclusions in your `.multilint.json` threshold or disable with `MULTILINT_SECURITY_CHECK=off`.
