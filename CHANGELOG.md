# Changelog

All notable changes to MultiLint. This file follows [Keep a Changelog](https://keepachangelog.com/) and [SemVer](https://semver.org/).

## [0.3.0](https://github.com/lehcode/multilint/compare/v0.2.1...v0.3.0) (2026-10-29)


### ⚠ BREAKING CHANGES

* **image:** server.py, mcp_server.py, docker-compose.yml, start.sh, stop.sh and the image entrypoint are removed. The image no longer serves HTTP (8591) or MCP and no longer contains lint.sh; use the Claude Code or OpenCode plugin, which mounts its own lint.sh into the container.
* **plugin:** Docker is required again. Host-installed linters are no longer used.
* **opencode:** the plugin no longer calls the HTTP service on port 8591. The root package (src/dual.ts, index.js, package.json) is gone.
* **plugin:** Docker and the lehcode/multilint image are no longer used. Install the linters on the host. The `image`/`unset-image` config verbs and the `image` setting are removed.
* **lint:** the settings CLI path is now claude-plugin/scripts/multilint.py (was lint_changed.py); the OpenCode plugin path in opencode.json is now ./.opencode/plugins/multilint.js.
* **plugin:** MULTILINT_IMAGE, MULTILINT_SEARCH_CEILING and MULTILINT_STATE_DIR no longer have any effect. Use `python3 claude-plugin/scripts/lint_changed.py --set image <value>` and `--set search_ceiling <value>` instead; MULTILINT_STATE_DIR has no replacement (XDG_STATE_HOME is still honored). The retired-variable notice for MULTILINT_URL/MULTILINT_PATH_MAP is also removed from both plugins.
* **plugin:** the hook no longer lints files other than the one just edited, so changes made outside a Write tool -- sed -i through Bash, or a generator -- are no longer picked up incidentally. MULTILINT_SEARCH_CEILING is new: a repository above $HOME is no longer found unless it is set.
* **compose:** 8591 and 8592 are no longer published to all interfaces, so callers reaching the HTTP API from another host lose it. MULTILINT_WORKSPACE is now required to start the stack.
* **opencode:** .opencode linting starts actually reporting, so failures that were previously hidden will surface.
* **plugin:** claude-plugin/.mcp.json is removed, so the plugin no longer provides an MCP server. MULTILINT_URL and MULTILINT_PATH_MAP are no longer read; a one-line notice fires when either is set. `docker` on PATH replaces "a running multilint service" as the hook's requirement. agents/multilint.md drops the lint_files(path, cwd) MCP contract.

### Features

* **agent:** restore the multilint subagent ([1f28639](https://github.com/lehcode/multilint/commit/1f28639e40ee889e70afc248ec7ea8e01d7068c5))
* **ci:** add Code Quality Benchmark hook for pre-commit and pre-push ([#22](https://github.com/lehcode/multilint/issues/22)) ([ea0fe4c](https://github.com/lehcode/multilint/commit/ea0fe4c2ccd549f8bc7b0a71687ece4411cd3047))
* **claude:** add Claude Code plugin with change-scoped linting ([c939ce8](https://github.com/lehcode/multilint/commit/c939ce8223e09d1750d10e8dbb2e27d2e2f33d3a))
* **image:** ship a toolchain-only image and remove service mode ([58b9125](https://github.com/lehcode/multilint/commit/58b912591c98f84b861695ed1cd3f7dfdc90539f))
* include actual findings in Claude hook systemMessage ([35f8136](https://github.com/lehcode/multilint/commit/35f8136eb6b19bb3c2dc3f39b2ecf72c096bd3ba))
* **lint:** let a linter's own config take precedence over multilint defaults ([f73082b](https://github.com/lehcode/multilint/commit/f73082b6358d9e5bd9884d4127ed41140341e9a9))
* **lint:** per-check enable/disable and args replacement via .multilint.json ([#21](https://github.com/lehcode/multilint/issues/21)) ([720e084](https://github.com/lehcode/multilint/commit/720e0847f1eb55dee0edeb61e897c67050d03c68))
* **lint:** report findings, violated rules and fix hints ([#24](https://github.com/lehcode/multilint/issues/24)) ([359750f](https://github.com/lehcode/multilint/commit/359750f56addab244c1c72fc98f05c8c72cdd614))
* **lint:** run mypy and bandit on Python files ([1c5e14b](https://github.com/lehcode/multilint/commit/1c5e14b49f9e65755c4cea509db3266d144a2f75))
* **opencode:** ship a self-contained OpenCode plugin ([e1dbdf1](https://github.com/lehcode/multilint/commit/e1dbdf107c21ec6b67a1e85ad71b303c25e4200b))
* **plugin:** add a marketplace manifest for the Claude Code plugin ([053ec92](https://github.com/lehcode/multilint/commit/053ec92539190437b8d3a5e8fa760d89beb4e4d8))
* **plugin:** add multilint config script ([b1d4594](https://github.com/lehcode/multilint/commit/b1d4594d644ff43c5e025b1237b0b441f12f6a11))
* **plugin:** emit one-line TUI notices for failed and recovered files ([55437ae](https://github.com/lehcode/multilint/commit/55437ae76ec0bd4f16764ba6e5d7f44a946562d6))
* **plugin:** lint in a throwaway container per invocation ([3a42547](https://github.com/lehcode/multilint/commit/3a42547d35573ad6fe06b832bcdbda6a6220745f))
* **plugin:** lint in an ephemeral container with the plugin's own lint.sh ([fdad967](https://github.com/lehcode/multilint/commit/fdad96784fb88dd7915c0e6b08b584142f09835e))
* **plugin:** lint on the host instead of in a container ([2673842](https://github.com/lehcode/multilint/commit/2673842ca9e86a67f674d64e3409967ccd61b82e))
* **plugin:** move image and search-ceiling settings to a SQLite store ([#20](https://github.com/lehcode/multilint/issues/20)) ([f1add8b](https://github.com/lehcode/multilint/commit/f1add8b442539952fa1a1304db66633c6269a803))
* **plugin:** route configuration requests to multilint subagent ([4afe634](https://github.com/lehcode/multilint/commit/4afe634b1f5e12e9d204e431efb582f23eba74d9))
* re-report last verdict on unchanged-content re-writes (R2) ([32c5c4e](https://github.com/lehcode/multilint/commit/32c5c4e0a332681e4e9cb31505e324e8edfab3df))
* report why skipped checks did not run ([d19431e](https://github.com/lehcode/multilint/commit/d19431e394f72f2a4e8810f898fc9d203e73665a))


### Bug Fixes

* **compose:** default the workspace mount to the caller's directory ([#18](https://github.com/lehcode/multilint/issues/18)) ([faa6437](https://github.com/lehcode/multilint/commit/faa6437842d7b1727ca3d1f0d4f07640ea349c19))
* **docker:** ship markdownlint's dependency tree so the check can run ([963efce](https://github.com/lehcode/multilint/commit/963efcecef7d555ed4c17a73b2214e3581af6bfc))
* harden settings lookup and validate gitleaks.config ([#23](https://github.com/lehcode/multilint/issues/23)) ([1d2eab3](https://github.com/lehcode/multilint/commit/1d2eab394f89fde9890048ede683d6b06a78de7f))
* **hook:** harden the ephemeral container run ([7c4171e](https://github.com/lehcode/multilint/commit/7c4171e8edbc81b61b0f54528dc08c2137e9896d))
* **hook:** never go silent without python3 or after a failed lint ([30b6217](https://github.com/lehcode/multilint/commit/30b6217af4d70fec6cd240e90ba4ec57eb723024))
* **hook:** report a file that could not be linted ([cfcbf68](https://github.com/lehcode/multilint/commit/cfcbf681347782b6adb36a770c170bc0c57c754f))
* **image:** install git so gitleaks can scan repository history ([db87437](https://github.com/lehcode/multilint/commit/db874371652a76215ad4602a6a279fe65bf14922))
* **lint:** detect a prettier config only from a top-level package.json key ([4c55641](https://github.com/lehcode/multilint/commit/4c556413a8c29f0348e879b2ad3c7cfeebfdc0f9))
* **lint:** lint a named file even inside a walk-excluded directory ([8f95556](https://github.com/lehcode/multilint/commit/8f95556acb74af4c6df63033a5c781a1df557f28))
* **lint:** make gitleaks actually produce a verdict ([386b83e](https://github.com/lehcode/multilint/commit/386b83e4775c1b205c7cfbf3bbcbfe26ec320d95))
* **lint:** make silent checks fail and rename plugin entry points to multilint ([#25](https://github.com/lehcode/multilint/issues/25)) ([d814fe1](https://github.com/lehcode/multilint/commit/d814fe1605fd2f031fa4b23fe686d187297467d5))
* **lint:** print the missing-scratch-directory warning in text mode too ([c54af07](https://github.com/lehcode/multilint/commit/c54af072ff72afcc0daa92f9ce4ef272feb16734))
* **lint:** report skipped checks and stop faking a pass ([4e2137c](https://github.com/lehcode/multilint/commit/4e2137ce959e8167bfb8280e41a77214b75621f3))
* **lint:** resolve .multilint.json relative to cwd and gate verdict on thresholds ([#19](https://github.com/lehcode/multilint/issues/19)) ([6e87e47](https://github.com/lehcode/multilint/commit/6e87e47263c55530207a106c3efdd7c5f246e788))
* **lint:** scan server.py and mcp_server.py for hardcoded secrets ([262308b](https://github.com/lehcode/multilint/commit/262308b23902e376312edd5ca12a2ca69bc09c65))
* **lint:** warn when markdownlint.rules has no scratch directory ([6d6c9c3](https://github.com/lehcode/multilint/commit/6d6c9c3f6de68867b9788bfaf655f891eac75e05))
* **opencode:** report a crashed lint run and tighten path checks ([dc18011](https://github.com/lehcode/multilint/commit/dc180113341bcb08af6b6fd04a7099cf44306697))
* **opencode:** report lint failures instead of a false green ([39cfd47](https://github.com/lehcode/multilint/commit/39cfd4703924b27b046c89718c1e68e3b6b887ac))
* **plugin:** accept both path and filePath from tool input ([d1d39c7](https://github.com/lehcode/multilint/commit/d1d39c7601eeea761ab6165722a524fea618dbfc))
* **plugin:** add root index.js so OpenCode V2 directory entries load ([6625fa5](https://github.com/lehcode/multilint/commit/6625fa56ea6ad200938fe2567c6a29b3c2c71aa9))
* **plugin:** defer annotations so the hook imports on Python 3.9 ([042383d](https://github.com/lehcode/multilint/commit/042383d8273500b5b68cadbba327b06f2fe05ee8))
* **plugin:** lint the edited file, not the whole dirty tree ([1e11d2b](https://github.com/lehcode/multilint/commit/1e11d2bfe1cb4f4b7ba88782c8895f32041da66d))
* **plugin:** move marketplace manifest to .claude-plugin/ ([0b263d3](https://github.com/lehcode/multilint/commit/0b263d32efe97d5dbe3c6db37d01c7d2106771ac))
* **release:** bump versions at flattened package paths ([68fb165](https://github.com/lehcode/multilint/commit/68fb1651a043a4b17c76afe0b8601aae88c3365e))
* **test:** make pytest read pytest.ini ([1341f46](https://github.com/lehcode/multilint/commit/1341f46d316695b12d680b1cea54a112a568f1a2))


### Code Refactoring

* **compose:** env-driven mounts and loopback-only ports ([e05d1d8](https://github.com/lehcode/multilint/commit/e05d1d8567d786a31da832cde1baebd5deb2a0e2))

## [0.2.1](https://github.com/lehcode/multilint/compare/v0.2.0...v0.2.1) (2026-09-29)

### Bug Fixes

* **ci:** upgrade npm before staging the release ([#8](https://github.com/lehcode/multilint/issues/8)) ([3f5b884](https://github.com/lehcode/multilint/commit/3f5b884e1123154c3be9f3ceb4c2b8aa86e88e79))
* **docker:** move USER nobody below the build steps ([#9](https://github.com/lehcode/multilint/issues/9)) ([a295e1b](https://github.com/lehcode/multilint/commit/a295e1bc082f236f747d7f1aaac3a8f72fb505ad))

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

* **OpenCode V2 plugin** — auto-lint on file save via `execute.after` hook; supports `.sh`, `.bash`, `.py`, `.md`, `.yaml`, `.yml`, `.json`, `.toml`; errors appended to tool result for agent visibility

### Changed

* **Deployment model** — switched from systemd-managed to Docker Compose `restart: unless-stopped`; systemd service (`docker-compose@multilint`) deprecated and removed
* **Port configuration** — HTTP API on port 8591, MCP server on port 8592 (configurable via env vars)

### Fixed

* Container now survives machine reboots and opencode restarts via Docker restart policy

## [0.1.0] — 2026-08-29

### Added

* AI CLI client harness positioning (opencode-first, client-adaptable MCP)
* **FastMCP migration** — replaced custom MCPServer with FastMCP (streamable-http on port 8592)
* **New checks:** YAML/JSON formatting (prettier), TOML sorting (toml-sort), security scanning (grep-based secrets + dangerous patterns), gitleaks (git history secret detection)
* **Configurable thresholds** — per-check thresholds via `.multilint.json` (default 0 = zero tolerance)
* **Feature toggles** — `MULTILINT_*_CHECK` env vars to disable any check without deleting code
* **JSON output** — `--format json` flag for structured results on stdout (terminal output to stderr)
* **Claude Code integration** — HTTP proxy on port 8592 with streamable-http transport
* `.gitleaks.toml` — gitleaks configuration file
* `.multilint.json` — default threshold configuration
* `opencode.json` — agent prompt reference, MCP server URL, plugin path
* `ROADMAP.md` — improvement roadmap with 8 phased items
* `doc/promotion.md` — promotion plan with positioning, release strategy, adoption guide
* Pre-commit hooks for multilint self-linting (black, pylint, flake8, shellcheck, pytest)
* `start.sh` / `stop.sh` — service management scripts (venv + systemd mode)
* PID tracking via `pids/` directory
* `test_lint_sh_extended.py` — extended lint test suite

### Changed

* Replaced hardcoded lint script paths with `MULTILINT_SCRIPT` env var (portable Docker container)
* Shell script discovery now excludes `test/` and `fixtures/` directories
* Python discovery now excludes `fixtures/` directory
* flake8 now also ignores `BLK100` (black-compatible)
* pylint now also ignores `E0401`, `E1123`, `W1510`
* `conftest.py` — simplified fixtures, added `sample_project_with_threshold_config`, migrated to pytest-mock
* `test_server.py` — removed mock_subprocess fixtures, tests use real subprocess execution

### Fixed

* Dockerfile: use `node:20` (not slim) for npm, `nodejs` apt package for runtime, `gitleaks v8.30.1`
* Pre-commit pytest hook runs from venv with proper path resolution
* HTTP test client path resolution (`tests/unit/.../server.py` → `tests/.../server.py` → `tests/../server.py`)

### Removed

* Zero-tolerance hard requirement — replaced with configurable thresholds (fail-fast only at 0)
* `continue` statements in bash syntax check (now increments failure counter)

### Notes

* `gitleaks` skips when `.git` not found (expected behavior)
* All 11 checks now report per-check failure summaries with threshold comparison (✓ or ⚠)
