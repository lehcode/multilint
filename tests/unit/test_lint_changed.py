"""Unit tests for core/lint_changed.py — CLI entry point and hook end-to-end flow.

Nothing here starts a container. The argv builder is a pure function precisely so that the
invocation can be asserted on a machine with no Docker and no image, which is what CI is.
"""

# pylint: disable=redefined-outer-name,protected-access,unused-argument
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import ml_container
import ml_repo
import ml_state
import pytest

from .hook_helpers import (
    HOOK_PATH,
    _drive_hook,
    _git_repo,
)

REPO_ROOT = Path(__file__).parent.parent.parent


class TestRunCli:
    """`--run ROOT TARGET` is the agent's entry: the same container, the document on stdout."""

    def test_prints_the_document_and_the_detail(self, hook, monkeypatch, capsys, tmp_path):
        calls = []

        def fake_run_lint(scope_root, relative_paths):
            calls.append((scope_root, relative_paths))
            return [("a.md", {"checks": {"markdownlint": {"status": "failed"}}}, "MD009 a.md:1\n")]

        monkeypatch.setattr(ml_container, "run_lint", fake_run_lint)
        assert hook.run_cli(str(tmp_path), "a.md") == 0
        out, err = capsys.readouterr()
        assert json.loads(out) == {"checks": {"markdownlint": {"status": "failed"}}}
        assert err == "MD009 a.md:1\n"
        assert calls == [(tmp_path.resolve(), ["a.md"])]

    def test_run_without_both_operands_is_a_usage_error_not_a_hook_run(self, tmp_path):
        """A short --run must not fall through to the hook, which would block reading stdin."""
        proc = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--run", str(tmp_path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "XDG_STATE_HOME": str(tmp_path / "state")},
        )
        assert proc.returncode == 1
        assert proc.stderr == "lint_changed.py: usage: --run ROOT TARGET\n"

    def test_unavailable_exits_1_with_the_reason(self, hook, monkeypatch, capsys, tmp_path):
        def unavailable(_root, _paths):
            raise ml_container.LintUnavailable("docker not found on PATH")

        monkeypatch.setattr(ml_container, "run_lint", unavailable)
        assert hook.run_cli(str(tmp_path), "a.md") == 1
        out, err = capsys.readouterr()
        assert out == ""
        assert err == "lint_changed.py: could not lint a.md: docker not found on PATH\n"


class TestHTTPPathIsGone:
    """Guards against the HTTP client creeping back in.

    It is not dead code to leave lying around: the path-translation layer it needed is what produced
    a passing report for a directory the container could not see.
    """

    def test_no_urllib_import(self):
        assert "import urllib" not in HOOK_PATH.read_text(encoding="utf-8")

    def test_path_translation_helpers_removed(self, hook):
        assert not hasattr(hook, "path_map")
        assert not hasattr(hook, "to_container_path")
        assert not hasattr(hook, "DEFAULT_LINT_URL")

    def test_nosec_suppression_removed(self):
        """The `# nosec B310` existed only to silence bandit about urlopen."""
        assert "nosec" not in HOOK_PATH.read_text(encoding="utf-8")


class TestTreeWalkIsGone:
    """The dirty-tree scan and its guards must not come back.

    The hook receives one file_path per invocation. Collecting every dirty file in the repository
    meant one Write reported on files the user had not touched, and the 2000-file scan cap existed
    only to make that survivable. A cap on a list that can hold one element is not a guard.
    """

    @pytest.mark.parametrize("name", ["SKIP_DIRS", "MAX_SCAN_FILES", "MAX_LINT_TARGETS"])
    def test_scan_constants_are_gone(self, hook, name):
        assert not hasattr(hook, name)

    @pytest.mark.parametrize(
        "name",
        ["candidates_under", "changed_under_git", "changed_via_sqlite", "select_targets"],
    )
    def test_scan_functions_are_gone(self, hook, name):
        assert not hasattr(hook, name)

    def test_source_no_longer_enumerates_the_dirty_tree(self):
        """git diff / ls-files must not appear: they are what produced the unrelated targets."""
        source = HOOK_PATH.read_text(encoding="utf-8")
        assert "ls-files" not in source
        assert "--name-only" not in source


class TestEveryLintableTypeReachesLint:
    """A Write or Edit of every supported type inside a repository reaches the linter.

    The user could confirm only Markdown as auto-linted. These cases pin the host side of the
    contract: every suffix in LINTABLE_SUFFIXES, from either tool, tracked or not, produces exactly
    one run_lint call scoped to the repository root with the repo-relative path.
    """

    @pytest.mark.parametrize("tracked", (False, True), ids=("untracked", "tracked"))
    @pytest.mark.parametrize("tool_name", ("Write", "Edit"))
    @pytest.mark.parametrize("suffix", sorted(ml_repo.LINTABLE_SUFFIXES))
    def test_reaches_run_lint(self, hook, tmp_path, monkeypatch, suffix, tool_name, tracked):
        repo = _git_repo(tmp_path)
        relative = f"src/sample{suffix}"
        (repo / "src").mkdir()
        (repo / relative).write_text("content\n", encoding="utf-8")
        if tracked:
            subprocess.run(["git", "add", relative], cwd=repo, check=True)
        payload = {"tool_name": tool_name, "cwd": str(repo), "tool_input": {"file_path": str(repo / relative)}}

        code, calls, _out = _drive_hook(hook, monkeypatch, tmp_path, payload)

        assert code == 0
        assert calls == [(repo.resolve(), [relative])]

    def test_relative_file_path_resolves_against_cwd(self, hook, tmp_path, monkeypatch):
        repo = _git_repo(tmp_path)
        (repo / "src").mkdir()
        (repo / "src" / "sample.py").write_text("content\n", encoding="utf-8")
        payload = {"tool_name": "Write", "cwd": str(repo), "tool_input": {"file_path": "src/sample.py"}}

        _code, calls, _out = _drive_hook(hook, monkeypatch, tmp_path, payload)

        assert calls == [(repo.resolve(), ["src/sample.py"])]

    @pytest.mark.parametrize("name", ("src/sample.ts", "Dockerfile", "src/sample.txt"))
    def test_unsupported_type_is_not_linted(self, hook, tmp_path, monkeypatch, name):
        """Documents the current contract: anything outside LINTABLE_SUFFIXES is ignored silently."""
        repo = _git_repo(tmp_path)
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("content\n", encoding="utf-8")
        payload = {"tool_name": "Write", "cwd": str(repo), "tool_input": {"file_path": str(repo / name)}}

        code, calls, out = _drive_hook(hook, monkeypatch, tmp_path, payload)

        assert (code, calls, out) == (0, [], "")

    def test_failing_result_reaches_claude(self, hook, tmp_path, monkeypatch):
        """A failed check comes back as additionalContext naming the check and the rule."""
        repo = _git_repo(tmp_path)
        (repo / "sample.md").write_text("# T  \n", encoding="utf-8")
        document = {
            "return_code": 1,
            "checks": {
                "markdownlint": {
                    "status": "failed",
                    "failed": 1,
                    "findings": [{"rule": "MD009", "file": "sample.md", "line": 1, "message": "Trailing spaces"}],
                }
            },
            "summary": {"rules_violated": ["MD009"]},
        }
        payload = {"tool_name": "Edit", "cwd": str(repo), "tool_input": {"file_path": str(repo / "sample.md")}}

        _code, _calls, out = _drive_hook(hook, monkeypatch, tmp_path, payload, [("sample.md", document, "")])

        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "markdownlint" in context
        assert "MD009" in context

    def test_unavailable_lint_is_reported_not_silent(self, hook, tmp_path, monkeypatch):
        """When docker cannot run, Claude is told the file is unverified; the edit still succeeds."""
        repo = _git_repo(tmp_path)
        (repo / "sample.py").write_text("x = 1\n", encoding="utf-8")
        payload = {"tool_name": "Write", "cwd": str(repo), "tool_input": {"file_path": str(repo / "sample.py")}}

        code, _calls, out = _drive_hook(
            hook, monkeypatch, tmp_path, payload, ml_container.LintUnavailable("docker not found on PATH")
        )

        output = json.loads(out)
        assert code == 0
        assert output["hookSpecificOutput"]["additionalContext"] == (
            "multilint could not lint sample.py: docker not found on PATH. Treat the file as unverified."
        )
        assert output["systemMessage"] == (
            "multilint: could not lint\n\n"
            "multilint could not lint sample.py: docker not found on PATH. Treat the file as unverified."
        )

    def test_identical_save_after_unavailable_lint_is_linted_again(self, hook, tmp_path, monkeypatch):
        """The digest is recorded before linting; a failed run must not make the next save "unchanged"."""
        repo = _git_repo(tmp_path)
        (repo / "sample.py").write_text("x = 1\n", encoding="utf-8")
        payload = {"tool_name": "Write", "cwd": str(repo), "tool_input": {"file_path": str(repo / "sample.py")}}

        _drive_hook(hook, monkeypatch, tmp_path, payload, ml_container.LintUnavailable("docker not found on PATH"))
        _code, calls, _out = _drive_hook(hook, monkeypatch, tmp_path, payload)
        assert len(calls) == 1, "an identical save after a failed lint was treated as unchanged"
        _code, calls, _out = _drive_hook(hook, monkeypatch, tmp_path, payload)
        assert calls == [], "after a successful lint, an identical save is still skipped"

    def test_multilint_sh_without_python3_reports_a_notice(self, tmp_path):
        """No python3 on PATH: the entry script exits 0 with a notice, never silently."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        bash = subprocess.run(["bash", "-c", "command -v bash"], capture_output=True, text=True).stdout.strip()
        proc = subprocess.run(
            [bash, str(HOOK_PATH.parent / "multilint.sh")],
            input="{}",
            capture_output=True,
            text=True,
            env={"PATH": str(bin_dir)},
        )
        assert proc.returncode == 0
        output = json.loads(proc.stdout)
        assert "python3 not found on PATH" in output["hookSpecificOutput"]["additionalContext"]
        assert output["systemMessage"] == "multilint: could not lint: python3 not found on PATH"


class TestRepeatedVerdict:
    """An identical save repeats a failing verdict instead of going silent; a clean one stays silent."""

    FAILING = {
        "return_code": 1,
        "checks": {
            "flake8": {
                "status": "failed",
                "findings": [{"rule": "F401", "file": "sample.py", "line": 1, "message": "'os' imported but unused"}],
            }
        },
        "summary": {"rules_violated": ["F401"]},
    }
    CLEAN = {"return_code": 0, "checks": {"flake8": {"status": "passed"}}, "summary": {}}

    @pytest.fixture
    def payload(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "sample.py").write_text("import os\n", encoding="utf-8")
        return {"tool_name": "Write", "cwd": str(repo), "tool_input": {"file_path": str(repo / "sample.py")}}

    def test_identical_save_of_a_failing_file_repeats_the_findings(self, hook, tmp_path, monkeypatch, payload):
        _code, _calls, first = _drive_hook(hook, monkeypatch, tmp_path, payload, [("sample.py", self.FAILING, "")])
        _code, calls, second = _drive_hook(hook, monkeypatch, tmp_path, payload)

        assert calls == [], "identical content must not be re-linted"
        first, second = json.loads(first), json.loads(second)
        assert second["hookSpecificOutput"] == first["hookSpecificOutput"]
        assert second["systemMessage"].startswith(
            "multilint: 1 file(s) with failing checks (unchanged since last lint)"
        )
        assert "F401" in second["systemMessage"]

    def test_identical_save_of_a_clean_file_stays_silent(self, hook, tmp_path, monkeypatch, payload):
        _drive_hook(hook, monkeypatch, tmp_path, payload, [("sample.py", self.CLEAN, "")])
        _code, calls, out = _drive_hook(hook, monkeypatch, tmp_path, payload)

        assert (calls, out) == ([], "")

    def test_a_clean_lint_clears_the_stored_verdict(self, hook, tmp_path, monkeypatch, payload):
        target = Path(payload["tool_input"]["file_path"])
        _drive_hook(hook, monkeypatch, tmp_path, payload, [("sample.py", self.FAILING, "")])
        target.write_text("x = 1\n", encoding="utf-8")
        _drive_hook(hook, monkeypatch, tmp_path, payload, [("sample.py", self.CLEAN, "")])
        _code, calls, out = _drive_hook(hook, monkeypatch, tmp_path, payload)

        assert (calls, out) == ([], "")

    def test_changed_content_drops_the_old_verdict(self, hook, tmp_path, monkeypatch, payload):
        """The stored report described bytes that are gone; it must not survive a content change."""
        target = Path(payload["tool_input"]["file_path"])
        _drive_hook(hook, monkeypatch, tmp_path, payload, [("sample.py", self.FAILING, "")])
        target.write_text("x = 1\n", encoding="utf-8")
        monkeypatch.setattr(ml_state, "state_dir", lambda: tmp_path / "state")
        state = ml_state.open_state()
        try:
            assert ml_state.record_and_check(state, target.resolve(), None) is True
        finally:
            state.close()

        assert ml_state.stored_report(target.resolve()) is None

    def test_user_message_carries_every_notice(self, hook, tmp_path, monkeypatch, payload):
        document = dict(self.FAILING, summary={"rules_violated": ["F401"], "checks_skipped": ["gitleaks"]})
        document["checks"] = dict(self.FAILING["checks"], gitleaks={"status": "skipped", "reason": "not installed"})

        _code, _calls, out = _drive_hook(hook, monkeypatch, tmp_path, payload, [("sample.py", document, "")])

        message = json.loads(out)["systemMessage"]
        assert "F401" in message
        assert "gitleaks: not installed" in message


class TestLintableTypesMatchLintSh:
    """The hook's suffix list and lint.sh's find patterns must agree.

    A type lint.sh learns to lint but the hook filters out is never linted on edit; a type the hook
    forwards but lint.sh never finds is reported as clean. Either drift is silent without this guard.
    """

    def test_suffixes_match_lint_sh_find_patterns(self, hook):
        core = REPO_ROOT / "core"
        engine = [core / "lint.sh", *sorted(core.glob("ml_*.sh"))]
        text = "\n".join(path.read_text(encoding="utf-8") for path in engine)
        calls = re.findall(r"< <\(ml_files ((?:'[^']+' )+)--", text)
        patterns = {suffix for call in calls for suffix in re.findall(r"'\*(\.\w+)'", call)}
        assert calls, "no ml_files calls found in lint.sh; update this guard with the file selection"
        assert patterns == ml_repo.LINTABLE_SUFFIXES


class TestHookWiring:
    """hooks.json -> multilint.sh -> lint_changed.py: a break anywhere means no edit is linted."""

    def test_post_tool_use_matches_write_and_edit(self):
        hooks = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        entry = hooks["hooks"]["PostToolUse"][0]
        assert {"Write", "Edit"} <= set(entry["matcher"].split("|"))
        assert entry["hooks"][0]["command"].endswith("scripts/multilint.sh")

    def test_entrypoint_execs_hook_body(self):
        script = (REPO_ROOT / "core" / "multilint.sh").read_text(encoding="utf-8")
        assert 'exec python3 "$(dirname -- "$0")/lint_changed.py"' in script
