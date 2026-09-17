# Changelog

All notable changes to MultiLint. This file follows [Keep a Changelog](https://keepachangelog.com/) and [SemVer](https://semver.org/).

## [Unreleased]

### Changed

- **Deployment model** — switched from systemd-managed to Docker Compose `restart: unless-stopped`; systemd service (`docker-compose@multilint`) deprecated and removed
- **Port configuration** — HTTP API on port 8591, MCP server on port 8592 (configurable via env vars)

### Fixed

- Container now survives machine reboots and opencode restarts via Docker restart policy

## [0.1.0] — 2026-08-29

### Added

- AI CLI client harness positioning (opencode-first, client-adaptable MCP)
- **FastMCP migration** — replaced custom MCPServer with FastMCP (streamable-http on port 8592)
- **New checks:** YAML/JSON formatting (prettier), TOML sorting (toml-sort), security scanning (grep-based secrets + dangerous patterns), gitleaks (git history secret detection)
- **Configurable thresholds** — per-check thresholds via `.multilint.json` (default 0 = zero tolerance)
- **Feature toggles** — `MULTILINT_*_CHECK` env vars to disable any check without deleting code
- **JSON output** — `--format json` flag for structured results on stdout (terminal output to stderr)
- **Claude Code integration** — HTTP proxy on port 8592 with streamable-http transport
- `.gitleaks.toml` — gitleaks configuration file
- `.multilint.json` — default threshold configuration
- `opencode.json` — opencode permission rules and MCP memory config
- `ROADMAP.md` — improvement roadmap with 8 phased items
- `doc/promotion.md` — promotion plan with positioning, release strategy, adoption guide
- Pre-commit hooks for multilint self-linting (black, pylint, flake8, shellcheck, pytest)
- `start.sh` / `stop.sh` — service management scripts (venv + systemd mode)
- PID tracking via `pids/` directory
- `test_lint_sh_extended.py` — extended lint test suite

### Changed

- Replaced hardcoded lint script paths with `MULTILINT_SCRIPT` env var (portable Docker container)
- Shell script discovery now excludes `test/` and `fixtures/` directories
- Python discovery now excludes `fixtures/` directory
- flake8 now also ignores `BLK100` (black-compatible)
- pylint now also ignores `E0401`, `E1123`, `W1510`
- `conftest.py` — simplified fixtures, added `sample_project_with_threshold_config`, migrated to pytest-mock
- `test_server.py` — removed mock_subprocess fixtures, tests use real subprocess execution

### Fixed

- Dockerfile: use `node:20` (not slim) for npm, `nodejs` apt package for runtime, `gitleaks v8.30.1`
- Pre-commit pytest hook runs from venv with proper path resolution
- HTTP test client path resolution (`tests/unit/.../server.py` → `tests/.../server.py` → `tests/../server.py`)

### Removed

- Zero-tolerance hard requirement — replaced with configurable thresholds (fail-fast only at 0)
- `continue` statements in bash syntax check (now increments failure counter)

### Notes

- `gitleaks` skips when `.git` not found (expected behavior)
- All 11 checks now report per-check failure summaries with threshold comparison (✓ or ⚠)
