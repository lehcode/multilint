"""Unit tests for the configuration tool, claude-plugin/scripts/multilint_config.py.

Nothing here starts a container. Settings verbs run the real lint_changed.py as a child process
against a throwaway XDG_STATE_HOME, exactly as the tool does in use.
"""

# pylint: disable=redefined-outer-name,protected-access
import ast
import importlib.util
import json
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent.parent
SCRIPT_PATH = REPO / "claude-plugin" / "scripts" / "multilint_config.py"
LINT_SH = REPO / "lint.sh"
CONSTANTS = ("ALL_CHECKS", "NO_ARGS_CHECKS", "CHECK_SETTING_KEYS", "OPTION_GROUPS")


def _load_module():
    """Import the tool by path; it is a script shipped in the plugin, not an installed package."""
    spec = importlib.util.spec_from_file_location("multilint_config_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _load_module()


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep the settings database (and the retired variables) away from the real environment."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("MULTILINT_IMAGE", raising=False)


@pytest.fixture
def root(tmp_path):
    directory = tmp_path / "project"
    directory.mkdir()
    return directory


def run(tool, root, *argv):
    return tool.main(["--root", str(root), *argv])


def on_disk(root):
    return json.loads((root / ".multilint.json").read_text(encoding="utf-8"))


class TestEdits:
    def test_disable_gitleaks_creates_file(self, tool, root, capsys):
        assert run(tool, root, "disable", "gitleaks") == 0
        assert on_disk(root) == {"checks": {"gitleaks": {"enabled": False}}}
        assert (root / ".multilint.json").read_text(encoding="utf-8") == capsys.readouterr().out

    def test_merge_preserves_and_reports_unknown_key(self, tool, root, capsys):
        before = {
            "checks": {"gitleaks": {"threshold": 3}, "mypy": {"enabled": False}},
            "bandit": {"severity": "-ll"},
            "custom": 1,
            "bogus": True,
        }
        (root / ".multilint.json").write_text(json.dumps(before), encoding="utf-8")
        assert run(tool, root, "disable", "gitleaks") == 0
        before["checks"]["gitleaks"]["enabled"] = False
        assert on_disk(root) == before
        assert 'warning: unknown top-level key "bogus"' in capsys.readouterr().err

    def test_threshold_keeps_flat_entry_and_warns(self, tool, root, capsys):
        (root / ".multilint.json").write_text('{"flake8": 0}', encoding="utf-8")
        assert run(tool, root, "threshold", "flake8", "5") == 0
        assert on_disk(root) == {"flake8": 0, "checks": {"flake8": {"threshold": 5}}}
        assert "flat threshold" in capsys.readouterr().err

    def test_args_set_empty_and_clear(self, tool, root):
        assert run(tool, root, "args", "flake8", "--max-line-length=100") == 0
        assert on_disk(root)["checks"]["flake8"]["args"] == ["--max-line-length=100"]
        assert run(tool, root, "args", "flake8") == 0
        assert on_disk(root)["checks"]["flake8"]["args"] == []
        assert run(tool, root, "clear-args", "flake8") == 0
        assert "args" not in on_disk(root)["checks"]["flake8"]

    def test_option_writes_group_only(self, tool, root):
        assert run(tool, root, "option", "gitleaks", "depth", "1") == 0
        assert on_disk(root) == {"gitleaks": {"depth": "1"}}

    def test_clear_args_on_no_args_check_removes_stale_key(self, tool, root):
        (root / ".multilint.json").write_text('{"checks":{"bash_syntax":{"args":["-x"]}}}', encoding="utf-8")
        assert run(tool, root, "clear-args", "bash_syntax") == 0
        assert "args" not in on_disk(root)["checks"]["bash_syntax"]

    def test_noop_edit_writes_nothing(self, tool, root):
        assert run(tool, root, "clear-args", "flake8") == 0
        assert not (root / ".multilint.json").exists()


REJECTIONS = [
    (("disable", "nonexistent"), "nonexistent"),
    (("threshold", "flake8", "-1"), "-1"),
    (("threshold", "flake8", "1.5"), "1.5"),
    (("threshold", "flake8", "true"), "true"),
    (("args", "bash_syntax", "-x"), "bash_syntax"),
    (("args", "security_secrets", "-x"), "security_secrets"),
    (("args", "security_dangerous_patterns", "-x"), "security_dangerous_patterns"),
    (("args", "flake8", "a\nb"), "control character"),
    (("option", "gitleaks", "verbose", "1"), "verbose"),
    (("option", "flake8", "x", "1"), "flake8"),
    (("option", "bandit", "severity", "-lllll"), "-lllll"),
    (("disable", "image"), "image"),
]


class TestRejections:
    @pytest.mark.parametrize("argv, needle", REJECTIONS)
    def test_rejected_without_writing(self, tool, root, capsys, argv, needle):
        assert run(tool, root, *argv) == 1
        assert needle in capsys.readouterr().err
        assert not (root / ".multilint.json").exists()

    def test_structure_conflict_is_not_coerced(self, tool, root, capsys):
        (root / ".multilint.json").write_text('{"gitleaks": 3}', encoding="utf-8")
        assert run(tool, root, "option", "gitleaks", "depth", "1") == 1
        err = capsys.readouterr().err
        assert "gitleaks" in err and "checks.gitleaks.threshold" in err
        assert (root / ".multilint.json").read_text(encoding="utf-8") == '{"gitleaks": 3}'

    @pytest.mark.parametrize(
        "content",
        [b'{"checks":', b"[1,2]", b'{"k": "' + b"x" * (1024 * 1024) + b'"}'],
        ids=["truncated", "non-object", "oversized"],
    )
    def test_unusable_file_is_refused_untouched(self, tool, root, content):
        (root / ".multilint.json").write_bytes(content)
        assert run(tool, root, "disable", "gitleaks") == 1
        assert (root / ".multilint.json").read_bytes() == content

    def test_failed_replace_keeps_original_and_leaves_no_temp(self, tool, root, monkeypatch):
        original = '{"custom": 1}\n'
        (root / ".multilint.json").write_text(original, encoding="utf-8")

        def fail(*_args, **_kwargs):
            raise OSError("boom")

        monkeypatch.setattr(os, "replace", fail)
        assert run(tool, root, "disable", "gitleaks") == 1
        assert (root / ".multilint.json").read_text(encoding="utf-8") == original
        assert not list(root.glob("*.tmp"))


class TestRoot:
    def test_defaults_to_git_root_from_subdirectory(self, tool, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "sub").mkdir()
        monkeypatch.chdir(repo / "sub")
        assert tool.main(["disable", "gitleaks"]) == 0
        assert on_disk(repo) == {"checks": {"gitleaks": {"enabled": False}}}
        assert not (repo / "sub" / ".multilint.json").exists()


class TestSettings:
    def test_show_on_absent_file_writes_nothing(self, tool, root, capsys):
        assert run(tool, root, "show") == 0
        status = json.loads(capsys.readouterr().out)
        assert status["config"] == {}
        assert status["exists"] is False
        assert status["settings"] == {"image": None, "search_ceiling": None}
        assert not (root / ".multilint.json").exists()

    def test_image_is_a_setting_and_ignores_retired_variable(self, tool, root, capsys, monkeypatch):
        monkeypatch.setenv("MULTILINT_IMAGE", "foo")
        assert run(tool, root, "image", "local/multilint:dev") == 0
        capsys.readouterr()
        assert run(tool, root, "show") == 0
        assert json.loads(capsys.readouterr().out)["settings"]["image"] == "local/multilint:dev"
        assert not (root / ".multilint.json").exists()

    def test_downstream_rejection_is_surfaced(self, tool, root, capsys):
        assert run(tool, root, "search-ceiling", "relative/path") != 0
        assert "lint_changed.py:" in capsys.readouterr().err


def _heredoc_constants():
    match = re.search(r"<<'ML_CONFIG_PY'\n(.*?)\nML_CONFIG_PY\n", LINT_SH.read_text(encoding="utf-8"), re.S)
    assert match, "ML_CONFIG_PY heredoc not found in lint.sh; the drift guard cannot compare constants"
    values = {}
    for node in ast.parse(match.group(1)).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in CONSTANTS:
                values[node.targets[0].id] = ast.literal_eval(node.value)
    return values


class TestDriftGuard:
    @pytest.mark.parametrize("name", CONSTANTS)
    def test_constant_matches_lint_sh(self, tool, name):
        expected = _heredoc_constants()
        assert name in expected, f"{name} not found in the lint.sh heredoc"
        assert getattr(tool, name) == expected[name], f"{name} differs from lint.sh"
