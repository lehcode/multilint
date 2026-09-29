# Changelog

All notable changes to MultiLint. This file follows [Keep a Changelog](https://keepachangelog.com/) and [SemVer](https://semver.org/).

## [0.2.0](https://github.com/lehcode/multilint/compare/v0.1.6...v0.2.0) (2026-09-29)


### Features

* add @lehcode/multilint dual-mode npm plugin package ([6e111af](https://github.com/lehcode/multilint/commit/6e111af34f061e47d82a159bea53e6a8e80815a9))
* **ci:** automate version and tag bumping with release-please ([a39d869](https://github.com/lehcode/multilint/commit/a39d8694b1a1be8d7ec33308fd8139d7db407524))
* **opencode:** migrate plugin to V2 format with execute.after hook ([a6c70cc](https://github.com/lehcode/multilint/commit/a6c70cc1a74d7823e97bc855454e83bdca1a3b98))


### Bug Fixes

* **ci:** pin gitleaks-action to v3 and pass GITHUB_TOKEN ([c3c86c8](https://github.com/lehcode/multilint/commit/c3c86c8a98366c8c791985ea0356f8ad962d3fa1))
* **mcp:** fix OCI labels, multi-arch, and remove debug step ([73e4c73](https://github.com/lehcode/multilint/commit/73e4c736ae8177a612bbbb7cfac89be7d52958f0))
* **npm:** make package publishable and add staged npm release ([a813991](https://github.com/lehcode/multilint/commit/a813991e2171b760fa971b177f8a0b39d2679260))
* **pre-commit:** exclude test files and fix YAML syntax ([145d31d](https://github.com/lehcode/multilint/commit/145d31d5ff9f269cba8694459295bffc12136300))
* **pre-commit:** exclude tests/test_files from markdownlint; fix README code block language ([d37825b](https://github.com/lehcode/multilint/commit/d37825ba768d724f7015e582109131a9f131d97b))
* **pre-commit:** exclude tests/test_files from shellcheck ([2391f0b](https://github.com/lehcode/multilint/commit/2391f0bb8c4a9b3e180aeac79fe9fddb4cd7b438))
* **server:** validate lint working directory against allowlist ([27eeaa3](https://github.com/lehcode/multilint/commit/27eeaa33ae5a1d749aa06ccd6358719eeb4f7d0e))

## [Unreleased]

### Added

- **OpenCode V2 plugin** — auto-lint on file save via `execute.after` hook; supports `.sh`, `.bash`, `.py`, `.md`, `.yaml`, `.yml`, `.json`, `.toml`; errors appended to tool result for agent visibility

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
- `opencode.json` — agent prompt reference, MCP server URL, plugin path
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
