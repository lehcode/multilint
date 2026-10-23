"""A linter's own config file takes precedence over multilint's policy flags (core/ml_config.sh, layer 3).

Every linter is replaced by a PATH shim that records the argv it was given, so the tests run on any host
and observe exactly what lint.sh passes: built-in defaults when the project has no config of its own,
harness flags only when it does.
"""

# pylint: disable=redefined-outer-name,subprocess-run-check
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

CORE = Path(__file__).parent.parent.parent / "core"
LINT_SH = CORE / "lint.sh"
SHIMMED = (
    "shellcheck",
    "bashate",
    "shfmt",
    "flake8",
    "black",
    "pylint",
    "mypy",
    "bandit",
    "markdownlint",
    "prettier",
    "toml-sort",
    "gitleaks",
)
# One argument per line, then a terminator. A `-c <file>` config is copied out, because lint.sh
# deletes its scratch directory on exit.
SHIM = """#!/bin/sh
{ for arg in "$@"; do printf '%s\\n' "$arg"; done; echo "--END--"; } >>"$ML_ARGV_DIR/NAME"
if [ "$1" = "-c" ] && [ -f "$2" ]; then cp "$2" "$ML_ARGV_DIR/NAME.config"; fi
exit 0
"""
SAMPLES = {
    "script.sh": "#!/bin/bash\necho hi\n",
    "mod.py": "X = 1\n",
    "doc.md": "# Title\n\nBody.\n",
    "data.yaml": "a: 1\n",
    "data.toml": 'a = "b"\n',
}


@pytest.fixture
def proj(tmp_path):
    directory = tmp_path / "proj"
    directory.mkdir()
    for name, text in SAMPLES.items():
        (directory / name).write_text(text, encoding="utf-8")
    return directory


@pytest.fixture
def argv_dir(tmp_path):
    bin_dir = tmp_path / "shim-bin"
    bin_dir.mkdir()
    for name in SHIMMED:
        shim = bin_dir / name
        shim.write_text(SHIM.replace("NAME", name), encoding="utf-8")
        shim.chmod(0o755)
    record = tmp_path / "argv"
    record.mkdir()
    return record


def lint(proj, argv_dir, config=None):
    """Run lint.sh from the project root, as both plugins do, and return the JSON document."""
    if config is not None:
        (proj / ".multilint.json").write_text(json.dumps(config), encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{argv_dir.parent / 'shim-bin'}:{os.environ['PATH']}",
        "ML_ARGV_DIR": str(argv_dir),
    }
    result = subprocess.run(
        ["bash", str(LINT_SH), str(proj), "--format", "json"], capture_output=True, text=True, env=env, cwd=proj
    )
    assert result.stdout, f"no JSON on stdout; stderr tail: {result.stderr[-500:]}"
    return json.loads(result.stdout)


def invocations(argv_dir, tool):
    """Each recorded call of tool as a list of arguments."""
    path = argv_dir / tool
    assert path.exists(), f"{tool} was never run"
    calls = path.read_text(encoding="utf-8").split("--END--\n")
    return [call.splitlines() for call in calls if call]


def only_call(argv_dir, tool):
    calls = invocations(argv_dir, tool)
    assert len(calls) == 1, calls
    return calls[0]


# (shim, native file, its content, a flag only the built-in defaults pass, harness flags that always stay)
NATIVE_ROWS = [
    ("shellcheck", ".shellcheckrc", "disable=SC2034\n", "-S", ["-f", "gcc"]),
    ("shfmt", ".editorconfig", "root = true\n", "-i", ["-d"]),
    ("flake8", ".flake8", "[flake8]\nmax-line-length = 88\n", "--max-line-length=120", []),
    ("flake8", "setup.cfg", "[metadata]\nname = x\n[flake8]\nmax-line-length = 88\n", "--max-line-length=120", []),
    ("flake8", "tox.ini", "[flake8]\nmax-line-length = 88\n", "--max-line-length=120", []),
    ("black", "pyproject.toml", "[tool.black]\nline-length = 88\n", "--line-length=120", ["--check"]),
    ("pylint", ".pylintrc", "[MAIN]\n", "--disable=C,R,E0401,E1123,W1510", ["--output-format=text"]),
    ("pylint", "pylintrc", "[MAIN]\n", "--disable=C,R,E0401,E1123,W1510", ["--output-format=text"]),
    ("pylint", "pyproject.toml", "[tool.pylint.main]\n", "--disable=C,R,E0401,E1123,W1510", ["--output-format=text"]),
    ("pylint", "setup.cfg", "[pylint.main]\n", "--disable=C,R,E0401,E1123,W1510", ["--output-format=text"]),
    ("mypy", "mypy.ini", "[mypy]\n", "--ignore-missing-imports", ["--no-error-summary"]),
    ("mypy", ".mypy.ini", "[mypy]\n", "--ignore-missing-imports", ["--no-error-summary"]),
    ("mypy", "pyproject.toml", "[tool.mypy]\n", "--ignore-missing-imports", ["--no-error-summary"]),
    ("mypy", "setup.cfg", "[mypy]\n", "--ignore-missing-imports", ["--no-error-summary"]),
    ("markdownlint", ".markdownlint.json", "{}\n", "-c", []),
    ("markdownlint", ".markdownlint.jsonc", "{}\n", "-c", []),
    ("markdownlint", ".markdownlint.yaml", "default: true\n", "-c", []),
    ("markdownlint", ".markdownlint.yml", "default: true\n", "-c", []),
    ("markdownlint", ".markdownlintrc", "{}\n", "-c", []),
    ("toml-sort", "pyproject.toml", "[tool.tomlsort]\nall = true\n", "--sort-table-keys", ["--check"]),
]


class TestNativeConfigDropsPolicyFlags:
    @pytest.mark.parametrize("tool, native, text, default_flag, harness", NATIVE_ROWS)
    def test_defaults_without_native_config(
        self, proj, argv_dir, tool, native, text, default_flag, harness
    ):  # pylint: disable=unused-argument
        lint(proj, argv_dir)
        for call in invocations(argv_dir, tool):
            assert default_flag in call, call
            assert all(flag in call for flag in harness), call

    @pytest.mark.parametrize("tool, native, text, default_flag, harness", NATIVE_ROWS)
    def test_native_config_wins(self, proj, argv_dir, tool, native, text, default_flag, harness):
        (proj / native).write_text(text, encoding="utf-8")
        data = lint(proj, argv_dir)
        for call in invocations(argv_dir, tool):
            assert default_flag not in call, call
            assert all(flag in call for flag in harness), call
        # multilint's own defaults are dropped silently; there is nothing for the project to fix.
        assert not any("takes precedence" in w for w in data["warnings"]), data["warnings"]

    def test_section_must_be_present_not_just_the_file(self, proj, argv_dir):
        (proj / "pyproject.toml").write_text("[tool.other]\nx = 1\n", encoding="utf-8")
        lint(proj, argv_dir)
        assert "--line-length=120" in only_call(argv_dir, "black")
        assert "--ignore-missing-imports" in only_call(argv_dir, "mypy")

    def test_only_the_scope_root_is_searched(self, proj, argv_dir):
        """A config in a subdirectory is not the project's config for the run."""
        (proj / "sub").mkdir()
        (proj / "sub" / ".flake8").write_text("[flake8]\n", encoding="utf-8")
        lint(proj, argv_dir)
        assert all("--max-line-length=120" in call for call in invocations(argv_dir, "flake8"))


class TestProjectArgsYieldToNativeConfig:
    def test_args_are_dropped_with_a_warning(self, proj, argv_dir):
        (proj / ".flake8").write_text("[flake8]\n", encoding="utf-8")
        data = lint(proj, argv_dir, {"checks": {"flake8": {"args": ["--select=E9"]}}})
        assert "--select=E9" not in only_call(argv_dir, "flake8")
        assert "checks.flake8.args ignored: .flake8 takes precedence" in data["warnings"]

    def test_args_apply_without_native_config(self, proj, argv_dir):
        data = lint(proj, argv_dir, {"checks": {"flake8": {"args": ["--select=E9"]}}})
        assert only_call(argv_dir, "flake8")[0] == "--select=E9"
        assert not any("takes precedence" in w for w in data["warnings"]), data["warnings"]

    @pytest.mark.parametrize(
        "native, text",
        [
            (".prettierrc", "{}\n"),
            ("prettier.config.js", "module.exports = {};\n"),
            ("package.json", '{\n  "prettier": {}\n}\n'),
        ],
    )
    def test_prettier_config(self, proj, argv_dir, native, text):
        (proj / native).write_text(text, encoding="utf-8")
        data = lint(proj, argv_dir, {"checks": {"yaml_prettier": {"args": ["--tab-width=4"]}}})
        yaml_calls = [call for call in invocations(argv_dir, "prettier") if call[-1].endswith("data.yaml")]
        assert yaml_calls and all("--tab-width=4" not in call for call in yaml_calls), yaml_calls
        assert f"checks.yaml_prettier.args ignored: {native} takes precedence" in data["warnings"]

    def test_prettier_args_without_native_config(self, proj, argv_dir):
        lint(proj, argv_dir, {"checks": {"yaml_prettier": {"args": ["--tab-width=4"]}}})
        yaml_calls = [call for call in invocations(argv_dir, "prettier") if call[-1].endswith("data.yaml")]
        assert yaml_calls and all("--tab-width=4" in call for call in yaml_calls), yaml_calls


class TestMarkdownlintRules:
    def test_built_in_rule_set_turns_md013_off(self, proj, argv_dir):
        lint(proj, argv_dir)
        call = only_call(argv_dir, "markdownlint")
        assert call[0] == "-c" and call[1].endswith("ml_markdownlint.json"), call
        assert json.loads((argv_dir / "markdownlint.config").read_text(encoding="utf-8")) == {
            "default": True,
            "MD013": False,
        }

    def test_rules_merge_over_the_defaults(self, proj, argv_dir):
        data = lint(proj, argv_dir, {"markdownlint": {"rules": {"MD041": False}}})
        assert only_call(argv_dir, "markdownlint")[0] == "-c"
        merged = json.loads((argv_dir / "markdownlint.config").read_text(encoding="utf-8"))
        assert merged == {"default": True, "MD013": False, "MD041": False}
        assert data["warnings"] == []

    def test_rules_can_turn_md013_back_on(self, proj, argv_dir):
        lint(proj, argv_dir, {"markdownlint": {"rules": {"MD013": {"line_length": 100}}}})
        merged = json.loads((argv_dir / "markdownlint.config").read_text(encoding="utf-8"))
        assert merged["MD013"] == {"line_length": 100}

    def test_native_config_beats_rules(self, proj, argv_dir):
        (proj / ".markdownlint.json").write_text("{}\n", encoding="utf-8")
        data = lint(proj, argv_dir, {"markdownlint": {"rules": {"MD041": False}}})
        assert "-c" not in only_call(argv_dir, "markdownlint")
        assert "markdownlint.rules ignored: .markdownlint.json takes precedence" in data["warnings"]

    def test_args_beat_rules(self, proj, argv_dir):
        data = lint(
            proj, argv_dir, {"markdownlint": {"rules": {"MD041": False}}, "checks": {"markdownlint": {"args": []}}}
        )
        assert only_call(argv_dir, "markdownlint") == [str(proj / "doc.md")]
        assert "markdownlint.rules ignored: checks.markdownlint.args takes precedence" in data["warnings"]

    @pytest.mark.parametrize("rules", [[], "MD013", None, 1])
    def test_rules_must_be_an_object(self, proj, argv_dir, rules):
        data = lint(proj, argv_dir, {"markdownlint": {"rules": rules}})
        assert '"markdownlint.rules" must be an object' in data["warnings"]
        assert only_call(argv_dir, "markdownlint")[1].endswith("ml_markdownlint.json")

    def test_unknown_markdownlint_option_is_reported(self, proj, argv_dir):
        data = lint(proj, argv_dir, {"markdownlint": {"config": "x.json"}})
        assert 'unknown option "config" under "markdownlint" ignored' in data["warnings"]


class TestGitleaksConfig:
    @staticmethod
    def config_of(argv_dir):
        call = only_call(argv_dir, "gitleaks")
        return call[call.index("--config") + 1] if "--config" in call else None

    def test_project_config_is_passed_explicitly(self, proj, argv_dir):
        (proj / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n", encoding="utf-8")
        data = lint(proj, argv_dir)
        assert self.config_of(argv_dir) == str(proj / ".gitleaks.toml")
        assert not any("gitleaks.config" in w for w in data["warnings"]), data["warnings"]

    def test_project_config_beats_the_option(self, proj, argv_dir):
        (proj / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n", encoding="utf-8")
        other = proj / "other.toml"
        other.write_text("[extend]\nuseDefault = true\n", encoding="utf-8")
        data = lint(proj, argv_dir, {"gitleaks": {"config": str(other)}})
        assert self.config_of(argv_dir) == str(proj / ".gitleaks.toml")
        assert "gitleaks.config ignored: .gitleaks.toml takes precedence" in data["warnings"]

    def test_option_applies_without_project_config(self, proj, argv_dir):
        other = proj / "other.toml"
        other.write_text("[extend]\nuseDefault = true\n", encoding="utf-8")
        lint(proj, argv_dir, {"gitleaks": {"config": str(other)}})
        assert self.config_of(argv_dir) == str(other)


def _config_tool():
    spec = importlib.util.spec_from_file_location("multilint_config_native_test", CORE / "multilint_config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestConfigTool:
    def test_rules_cannot_be_set_from_the_command_line(self, tmp_path, capsys):
        tool = _config_tool()
        assert tool.main(["--root", str(tmp_path), "option", "markdownlint", "rules", "x"]) == 1
        assert "markdownlint.rules is an object" in capsys.readouterr().err
        assert not (tmp_path / ".multilint.json").exists()

    def test_audit_accepts_an_object(self):
        assert _config_tool().audit({"markdownlint": {"rules": {"MD013": True}}}) == []

    def test_audit_flags_a_non_object(self):
        assert '"markdownlint.rules" must be an object' in _config_tool().audit({"markdownlint": {"rules": []}})
