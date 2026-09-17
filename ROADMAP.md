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

### 4. Docker Registry Publishing [P2] ⏳ Pending

**Problem:** Users must build locally. No Docker Hub or ghcr.io presence.

**Changes:**

- Create `.github/workflows/docker-publish.yml`:
  - Trigger: push to `main`, tags matching `v*`
  - `docker buildx` for multi-arch (linux/amd64, linux/arm64)
  - Push to `ghcr.io/<org>/multilint:<tag>`
  - Update `latest` tag on `main` pushes
- Add `docker pull` instructions to README

**Impact:** Zero-friction adoption. `docker run ghcr.io/org/multilint:latest` works out of the box.

**Risk:** Low. Standard GitHub Actions pattern.

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

## Summary Table

| Priority | Item | Status | Effort | Impact |
|----------|------|--------|--------|--------|
| P0 | Fix hardcoded paths | ✅ Complete | Low | Critical |
| P0 | MCP Registry + GEO | ✅ Complete | Low | Critical |
| P1 | Structured JSON output | ✅ Complete | Medium | High |
| P2 | Docker publishing | ⏳ Pending | Medium | High |
| P2 | Expanded file types | ✅ Complete | Medium | Medium |
| P2 | Security scanning | ✅ Complete | Medium-High | High |
| P3 | Auto-fix | ⏳ Pending | Medium-High | Medium |
| P4 | Telemetry | ⏳ Pending | Low | Low |

---

## Active Work

The remaining items (Docker publishing, auto-fix, telemetry) form the next development cycle. Priority order: Docker publishing (adoption) → auto-fix (actionability) → telemetry (observability).
