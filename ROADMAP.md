# MultiLint Improvement Roadmap

## Overview

MultiLint is a Docker-hosted linting service that validates shell, Python, Markdown, YAML, JSON, and TOML files against configurable thresholds. It exposes functionality via HTTP API and MCP server. This roadmap tracks 8 improvement areas — completed items marked with implementation references, pending items ranked by priority, effort, and impact.

---

## ✅ Phase 1: Foundation (Completed 2026-08-29)

### 1. Fix Hardcoded Paths [P0] ✅ Complete

**Implementation:** Dockerfile `COPY lint.sh /usr/local/bin/lint.sh`, `MULTILINT_SCRIPT` env var used in `server.py:63` and `mcp_server.py:44`. Default `/usr/local/bin/lint.sh` maintained for backward compat.

### 2. MCP Registry Listing + GEO [P0] ✅ Complete

**Implementation:** MCP tool descriptions rewritten in `mcp_server.py` with GEO guidelines: use-case phrases ("code quality", "linting", "security scanning"), tool categories, and comprehensive instructions.

---

## ✅ Phase 2: Core Capabilities (Completed 2026-08-29)

### 3. Structured JSON Output [P1] ✅ Complete

**Implementation:** `--format {text,json}` flag added to `lint.sh:42-65`. JSON output renders to stdout with `summary`, `checks`, `return_code`, and `files` fields (lines 650-703). Terminal text output redirected to stderr. MCP tool supports optional `format` parameter.

### 4. Docker Registry Publishing [P2] ✅ Complete

**Implementation:** `.github/workflows/docker-publish.yml`, triggered on `v*` tags. Multi-arch `docker buildx` push to Docker Hub `lehcode/multilint` and `ghcr.io/lehcode/multilint`, both `:<tag>` and `:latest`.

**Verified published:** Docker Hub carries `latest`, `v0.2.1` and `v0.1.6` (2026-09-29). Anonymous pulls are served, so no credentials are needed to consume it.

> **Reclassified from ❌ Deferred.** The deferral said *"not yet a priority given the Docker-in-Docker usage pattern (services already have Docker; they just build and run locally)"*. That reasoning no longer holds, and the entry was also simply out of date — the workflow had already shipped and published successfully.
>
> It is now **load-bearing rather than convenient.** Both plugins run `docker run <image>` per invocation, so without a published image every user must clone this repository and build ~506 MB locally before the plugin does anything. The registry is the distribution mechanism, not a nicety.

**Remaining follow-up:** add `docker pull` instructions to README. The README is stale in other respects too and is tracked separately.

### 5. Expanded File Type Coverage [P2] ✅ Complete

**Implementation:** YAML/JSON via `prettier --check` (lint.sh:385-450), TOML via `toml-sort --check --sort-keys` (lint.sh:452-503). Registered in `check_failures` dict, threshold config keys added, summary lines displayed.

### 6. Security Scanning [P2] ✅ Complete

**Implementation:** grep-based hardcoded secrets detection for shell and Python files (lint.sh:508-554), dangerous shell patterns detection (lint.sh:556-574), gitleaks git history secret scanning (lint.sh:597-630). Feature toggle `MULTILINT_SECURITY_CHECK` and `MULTILINT_GITLEAKS_CHECK` control activation.

---

## ⏳ Phase 3: Actionability (Pending)

### 7. Auto-Fix Support [P3]

**Problem:** Detection-only. Manual fixes required.

**Changes:**

- Add `--fix` flag to `lint.sh`
- When active, runs fix variants instead of check:

| Check | Fix command |
|-------|-------------|
| shfmt | `shfmt -w` (write in place) |
| black | `black` (write in place) |
| markdownlint | `markdownlint --fix` |

- Reports: `fixed: N files, N issues`
- Don't modify timestamps or create uncommitted diff surprises — summarize at end

**Impact:** Actionable. Users fix issues in one command instead of two.

**Risk:** Low. Fix commands are well-established.

---

## ⏳ Phase 4: Observability (Pending)

### 8. Opt-In Telemetry [P4]

**Problem:** No visibility into lint coverage, pass rates, or file counts.

**Changes:**

- Add `telemetry` section to `.multilint.json`:

  ```json
  {
    "telemetry": {
      "enabled": false,
      "endpoint": "https://telemetry.example.com/collect"
    }
  }
  ```

- On each run (if enabled), POST anonymous data:
  - `check_duration_ms`, `files_count`, `checks_count`, `pass_rate`, `tool_versions`
- No paths, no file contents, no user identifiers

**Impact:** Proves ROI. Helps prioritize which checks to improve.

**Risk:** Low. Disabled by default. Optional dependency.

---

## ⏳ Phase 5: Reach (Postponed 2026-10-17)

### 9. Standalone stdio MCP Server [P3]

**Problem:** The HTTP MCP server (port 8592, persistent container) was removed with service mode. MCP clients that cannot load the Claude Code or OpenCode plugin (Cursor, Codex CLI, Gemini CLI, Zed, Claude Desktop) have no way to use multilint, and there is nothing to list in MCP registries.

**Design (agreed):**

- `core/mcp_server.py`, stdlib only: newline-delimited JSON-RPC 2.0 over stdin/stdout (MCP stdio transport). The client starts it per session; no port, no HTTP, no daemon.
- One tool, `lint_files(path)`, calling `lint_changed.run_lint()`, so it uses the same ephemeral container and sandbox flags as the hook.
- The scope root is bind-mounted, so paths outside the search ceiling (`$HOME` unless the `search_ceiling` setting overrides it) are refused, the bound the hook already uses. Unlintable types and paths with control characters are refused with a tool error; a lint that cannot run returns the "unverified" notice as `isError`.
- Installable separately from the plugins: an `mcp-server/` package as a third `scripts/sync-core.sh` target.

**Open decision — distribution channel:**

| Channel | Launch | Host needs | Release cost |
|---|---|---|---|
| npm `@lehcode/multilint-mcp` | `npx -y @lehcode/multilint-mcp` | Node, python3, Docker | Low: one more job in the existing npm publish |
| PyPI `multilint-mcp` | `uvx multilint-mcp` | uv, Docker | Medium: `pyproject.toml`, PyPI publish workflow, release-please entry |

Rejected: the image as the MCP server. It needs a per-project absolute mount in each client config, `lint.sh` back in the image, and `docker.sock` to start its own containers.

**Impact:** Multilint for every MCP client; MCP registry listing (`doc/promotion.md`).

**Risk:** Low to medium. A second user-facing surface that duplicates the plugins' hook and subagent; the path bound is the security boundary and needs its own tests.

---

## Summary Table

| Priority | Item | Status | Effort | Impact |
|----------|------|--------|--------|--------|
| P0 | Fix hardcoded paths | ✅ Complete | Low | Critical |
| P0 | MCP Registry + GEO | ✅ Complete | Low | Critical |
| P1 | Structured JSON output | ✅ Complete | Medium | High |
| P2 | Docker publishing | ✅ Complete | Medium | Critical |
| P2 | Expanded file types | ✅ Complete | Medium | Medium |
| P2 | Security scanning | ✅ Complete | Medium-High | High |
| P3 | Auto-fix | ⏳ Pending | Medium-High | Medium |
| P3 | Standalone stdio MCP server | ⏳ Postponed | Medium | Medium |
| P4 | Telemetry | ⏳ Pending | Low | Low |

---

## Active Work

The remaining items (auto-fix, telemetry) form the next development cycle. Priority order: auto-fix (actionability) → telemetry (observability).

Docker publishing is no longer deferred — it shipped, and it is now the distribution mechanism both plugins depend on, so its impact is upgraded from High to Critical. A registry outage or an unpublished tag stops linting for every consumer.

Auto-fix needs re-scoping before it is picked up. Its premise is a writable mount, and the execution model mounts the project read-only on purpose. Delivering it means either a second writable invocation or returning patches for the agent to apply; the latter fits the current design better and keeps the "nothing is auto-fixed" guarantee that the read-only mount enforces.
