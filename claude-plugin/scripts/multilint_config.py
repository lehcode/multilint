#!/usr/bin/env python3
"""Read, validate and edit <project-root>/.multilint.json; delegate plugin settings.

Usage: multilint_config.py [--root DIR] VERB [OPERAND...]   (see --help)

Docker-free and stdlib-only. The validation constants below are verbatim copies of the ML_CONFIG_PY
heredoc in lint.sh, which stays authoritative at lint time; tests/unit/test_multilint_config.py
fails when the two diverge. This tool only prevents writing input that lint.sh would warn about or
ignore. The `image` and `search_ceiling` settings are not configuration keys: they are delegated to
lint_changed.py (--set/--get/--unset), the only writer of the settings database. Environment
variables are never read or set as a configuration channel.

Exit status: 0 on success (a no-op edit and `show` included), 1 on every rejection, refusal and
usage error; a settings verb returns lint_changed.py's status unchanged.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from pathlib import Path

ALL_CHECKS = (
    "bash_syntax",
    "shellcheck",
    "bashate",
    "shfmt",
    "flake8",
    "black",
    "pylint",
    "mypy",
    "bandit",
    "markdownlint",
    "yaml_prettier",
    "json_prettier",
    "toml_sort",
    "security_secrets",
    "security_dangerous_patterns",
    "gitleaks",
)
NO_ARGS_CHECKS = {"bash_syntax", "security_secrets", "security_dangerous_patterns"}
CHECK_SETTING_KEYS = {"enabled", "threshold", "args"}
OPTION_GROUPS = {
    "gitleaks": {"depth", "config"},
    "bandit": {"severity"},
    "mypy": {"cache_dir"},
}
OPTION_CHOICES = {
    ("gitleaks", "depth"): ("all", "1"),
    ("bandit", "severity"): ("-l", "-ll", "-lll"),
}
SETTING_NAMES = ("image", "search_ceiling")
CONFIG_NAME = ".multilint.json"
MAX_BYTES = 1024 * 1024
PROG = "multilint_config.py"
LINE_WIDTH = 80

USAGE = """\
usage: multilint_config.py [--root DIR] VERB [OPERAND...]

  show                           print file, config and settings; write nothing
  enable CHECK | disable CHECK   checks.CHECK.enabled = true | false
  threshold CHECK N              checks.CHECK.threshold = N (non-negative integer)
  args CHECK [ARG...]            checks.CHECK.args = [ARG...] (no ARG: [], harness flags only)
  clear-args CHECK               remove checks.CHECK.args
  option GROUP KEY VALUE         GROUP.KEY = VALUE (gitleaks depth|config, bandit severity, mypy cache_dir)
  image VALUE | unset-image      container image setting (lint_changed.py, not .multilint.json)
  search-ceiling PATH | unset-search-ceiling
                                 search ceiling setting (lint_changed.py, not .multilint.json)

--root DIR must be the first two arguments; otherwise the root is the nearest ancestor of the
working directory holding .git, else the working directory. Everything after the verb is taken
verbatim, so values may start with '-'. "gitleaks" is both a check (enable/disable) and an option
group (option gitleaks depth|config ...).
"""


class Rejected(Exception):
    """Refused input or state; the message goes to stderr and the exit status is 1."""


def jd(value) -> str:
    """Render an untrusted value safely: quoted, control characters escaped."""
    return json.dumps(value)


def has_control_char(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def is_nonneg_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def find_root(explicit: str | None) -> Path:
    """--root, else the nearest ancestor of the working directory with .git (dir or file), else cwd."""
    if explicit is not None:
        root = Path(explicit).resolve()
        if not root.is_dir():
            raise Rejected(f"--root {jd(explicit)} is not an existing directory")
        return root
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return candidate
    return cwd


def load(path: Path) -> dict:
    """Parsed document; a missing file is {}. Anything unusable is refused, never repaired."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise Rejected(f"{path} is a symlink or not a regular file; refusing to edit it")
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except OSError as exc:
        raise Rejected(f"cannot read {path}: {exc}") from exc
    if len(raw) > MAX_BYTES:
        raise Rejected(f"{path} is larger than 1 MiB; refusing to edit it")
    try:
        document = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise Rejected(f"{path} cannot be parsed: not valid UTF-8 ({exc})") from exc
    except ValueError as exc:
        raise Rejected(f"{path} cannot be parsed: {exc}") from exc
    if not isinstance(document, dict):
        raise Rejected(f"{path} cannot be edited: the top level must be an object")
    return document


def check_name(name: str) -> str:
    if name in ALL_CHECKS:
        return name
    hint = ""
    if name in SETTING_NAMES:
        hint = f" ({name} is a setting, not a check: use the {name.replace('_', '-')} verb)"
    raise Rejected(f"unknown check {jd(name)}{hint}")


def parse_threshold(text: str) -> int:
    if not re.fullmatch(r"[0-9]+", text):
        raise Rejected(f"threshold {jd(text)} must be a non-negative integer")
    return int(text)


def check_args(check: str, values: list[str]) -> list[str]:
    if check in NO_ARGS_CHECKS:
        raise Rejected(f"args have no effect on {check}; refusing to set them")
    for value in values:
        if has_control_char(value):
            raise Rejected(f"args element {jd(value)} contains a control character")
    return list(values)


def check_option(group: str, key: str, value: str) -> str:
    if group not in OPTION_GROUPS:
        hint = f" ({group} is a setting, not an option group)" if group in SETTING_NAMES else ""
        raise Rejected(f"unknown option group {jd(group)}{hint}")
    if key not in OPTION_GROUPS[group]:
        raise Rejected(f"unknown option {jd(key)} for group {jd(group)}")
    choices = OPTION_CHOICES.get((group, key))
    if choices is not None and value not in choices:
        raise Rejected(f"{group}.{key} must be one of {', '.join(choices)}, not {jd(value)}")
    if value == "" or has_control_char(value):
        raise Rejected(f"{group}.{key} must be a non-empty string without control characters")
    return value


def existing_entry(doc: dict, check: str) -> dict | None:
    """checks.<check> when present; Rejected when it or "checks" is not an object."""
    checks = doc.get("checks")
    if checks is None:
        return None
    if not isinstance(checks, dict):
        raise Rejected(f'"checks" holds {jd(checks)}, not an object; fix it by hand before editing checks')
    entry = checks.get(check)
    if entry is not None and not isinstance(entry, dict):
        raise Rejected(f'"checks.{check}" holds {jd(entry)}, not an object; fix it by hand before editing it')
    return entry


def check_entry(doc: dict, check: str) -> dict:
    """checks.<check> as an object, created on demand; Rejected when the structure conflicts."""
    entry = existing_entry(doc, check)
    if entry is None:
        entry = doc.setdefault("checks", {}).setdefault(check, {})
    return entry


def option_section(doc: dict, group: str) -> dict:
    """Top-level option group as an object, created on demand; a flat value is a conflict."""
    section = doc.setdefault(group, {})
    if isinstance(section, dict):
        return section
    fix = f'move the flat threshold to "checks.{group}.threshold" and remove the "{group}" entry'
    raise Rejected(f'"{group}" holds {jd(section)}, not an object, so no option can be set on it; {fix}')


def problem(func, *args) -> str | None:
    """The Rejected message `func(*args)` raises, or None. Lets audit reuse the edit validators."""
    try:
        func(*args)
    except Rejected as exc:
        return str(exc)
    return None


def audit_enabled(name: str, value) -> str | None:
    return None if isinstance(value, bool) else f'"checks.{name}.enabled" must be a boolean'


def audit_threshold(name: str, value) -> str | None:
    return None if is_nonneg_int(value) else f'"checks.{name}.threshold" must be a non-negative integer'


def audit_args(name: str, value) -> str | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return f'"checks.{name}.args" must be a list of strings'
    return problem(check_args, name, value)


SETTING_AUDITORS = {"enabled": audit_enabled, "threshold": audit_threshold, "args": audit_args}


def audit_check(name: str, entry) -> list[str]:
    issues = []
    unknown = problem(check_name, name)
    if unknown:
        return [unknown + ' under "checks" is ignored by lint.sh']
    if not isinstance(entry, dict):
        return [f'"checks.{name}" must be an object']
    for key, value in entry.items():
        if key not in CHECK_SETTING_KEYS:
            issues.append(f'unknown key {jd(key)} under "checks.{name}" is ignored by lint.sh')
            continue
        issue = SETTING_AUDITORS[key](name, value)
        if issue:
            issues.append(issue)
    return issues


def audit_checks(value) -> list[str]:
    if not isinstance(value, dict):
        return ['"checks" must be an object']
    issues = []
    for name, entry in value.items():
        issues.extend(audit_check(name, entry))
    return issues


def audit_option(group: str, key: str, value) -> str | None:
    if group == "gitleaks" and key == "depth" and value == 1 and not isinstance(value, bool):
        value = "1"
    if not isinstance(value, str):
        return f'"{group}.{key}" must be a string'
    return problem(check_option, group, key, value)


def audit_group(group: str, value) -> list[str]:
    if is_nonneg_int(value):
        return []
    if not isinstance(value, dict):
        return [f'"{group}" must be an object or a non-negative integer']
    issues = []
    for key, option_value in value.items():
        if key in OPTION_GROUPS[group]:
            issue = audit_option(group, key, option_value)
        else:
            issue = f'unknown option {jd(key)} under "{group}" is ignored by lint.sh'
        if issue:
            issues.append(issue)
    return issues


def audit_overrides(doc: dict) -> list[str]:
    """Flat "<name>": N next to checks.<name>.threshold: lint.sh uses the object value and warns."""
    checks = doc.get("checks")
    if not isinstance(checks, dict):
        return []
    issues = []
    for name in ALL_CHECKS:
        entry = checks.get(name)
        if isinstance(entry, dict) and is_nonneg_int(doc.get(name)) and is_nonneg_int(entry.get("threshold")):
            issues.append(f'"{name}" has both a flat threshold and "checks.{name}.threshold"; lint.sh uses the latter')
    return issues


def audit(doc: dict) -> list[str]:
    """Read-only pass over a document: everything lint.sh would warn about or ignore."""
    issues = []
    for key, value in doc.items():
        if key == "checks":
            issues.extend(audit_checks(value))
        elif key in OPTION_GROUPS:
            issues.extend(audit_group(key, value))
        elif key in ALL_CHECKS:
            if not is_nonneg_int(value):
                issues.append(f'"{key}" threshold must be a non-negative integer')
        else:
            issues.append(f"unknown top-level key {jd(key)} is ignored by lint.sh")
    return issues + audit_overrides(doc)


def render(value, indent: int = 0, prefix: int = 0) -> str:
    """JSON text: key order kept, 2-space indent, scalar lists inline when the line fits 80 columns."""
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = []
        for key, item in value.items():
            head = f"{pad}  {json.dumps(key, ensure_ascii=False)}: "
            lines.append(head + render(item, indent + 1, len(head) - len(pad) - 2))
        return "{\n" + ",\n".join(lines) + f"\n{pad}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        inline = json.dumps(value, ensure_ascii=False)
        scalars = not any(isinstance(item, (dict, list)) for item in value)
        if scalars and len(pad) + prefix + len(inline) <= LINE_WIDTH:
            return inline
        lines = [f"{pad}  {render(item, indent + 1)}" for item in value]
        return "[\n" + ",\n".join(lines) + f"\n{pad}]"
    return json.dumps(value, ensure_ascii=False)


def write_atomic(path: Path, text: str) -> None:
    """Temp file in the same directory, fsync, mode copied, os.replace; nothing left behind on failure."""
    tmp = path.with_name(f"{CONFIG_NAME}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise Rejected(f"cannot write {path}: {exc}") from exc


def call_setting(argv: list[str]) -> subprocess.CompletedProcess:
    """lint_changed.py --set/--get/--unset from this script's directory; no shell, environment untouched."""
    script = Path(__file__).resolve().parent / "lint_changed.py"
    return subprocess.run([sys.executable, str(script), *argv], capture_output=True, text=True, check=False)


def run_setting(argv: list[str]) -> int:
    """Relay lint_changed.py's stdout, stderr and exit status verbatim."""
    proc = call_setting(argv)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


def do_enable(doc: dict, ops: list[str]) -> None:
    check_entry(doc, check_name(ops[0]))["enabled"] = True


def do_disable(doc: dict, ops: list[str]) -> None:
    check_entry(doc, check_name(ops[0]))["enabled"] = False


def do_threshold(doc: dict, ops: list[str]) -> None:
    name = check_name(ops[0])
    value = parse_threshold(ops[1])
    check_entry(doc, name)["threshold"] = value


def do_args(doc: dict, ops: list[str]) -> None:
    name = check_name(ops[0])
    values = check_args(name, ops[1:])
    check_entry(doc, name)["args"] = values


def do_clear_args(doc: dict, ops: list[str]) -> None:
    entry = existing_entry(doc, check_name(ops[0]))
    if entry is not None:
        entry.pop("args", None)


def do_option(doc: dict, ops: list[str]) -> None:
    value = check_option(ops[0], ops[1], ops[2])
    option_section(doc, ops[0])[ops[1]] = value


# verb -> (operand usage, minimum operands, maximum operands or None, handler)
EDITS = {
    "enable": ("CHECK", 1, 1, do_enable),
    "disable": ("CHECK", 1, 1, do_disable),
    "threshold": ("CHECK N", 2, 2, do_threshold),
    "args": ("CHECK [ARG...]", 1, None, do_args),
    "clear-args": ("CHECK", 1, 1, do_clear_args),
    "option": ("GROUP KEY VALUE", 3, 3, do_option),
}

# verb -> (operand usage, operand count, lint_changed.py argv prefix)
SETTINGS = {
    "image": ("VALUE", 1, ["--set", "image"]),
    "unset-image": ("", 0, ["--unset", "image"]),
    "search-ceiling": ("PATH", 1, ["--set", "search_ceiling"]),
    "unset-search-ceiling": ("", 0, ["--unset", "search_ceiling"]),
}


def warn(issues: list[str]) -> None:
    for issue in issues:
        print(f"{PROG}: warning: {issue}", file=sys.stderr)


def run_edit(root: Path, handler, ops: list[str]) -> int:
    path = root / CONFIG_NAME
    current = load(path)
    updated = copy.deepcopy(current)
    handler(updated, ops)
    warn(audit(updated))
    text = render(updated) + "\n"
    if updated != current:
        write_atomic(path, text)
    sys.stdout.write(text)
    return 0


def run_show(root: Path) -> int:
    path = root / CONFIG_NAME
    document = load(path)
    warn(audit(document))
    settings = {}
    for key in SETTING_NAMES:
        proc = call_setting(["--get", key])
        sys.stderr.write(proc.stderr)
        if proc.returncode != 0:
            return proc.returncode
        settings[key] = proc.stdout.rstrip("\n") or None
    status = {"file": str(path), "exists": path.exists(), "config": document, "settings": settings}
    sys.stdout.write(render(status) + "\n")
    return 0


def need_operands(verb: str, usage: str, count: int, low: int, high: int | None) -> None:
    if count < low or (high is not None and count > high):
        raise Rejected(f"usage: {verb} {usage}".rstrip())


def dispatch(argv: list[str]) -> int:
    args = list(argv)
    root_arg = None
    if len(args) >= 2 and args[0] == "--root":
        root_arg, args = args[1], args[2:]
    if not args:
        print(USAGE, file=sys.stderr, end="")
        return 1
    verb, ops = args[0], args[1:]
    if verb in ("-h", "--help"):
        print(USAGE, end="")
        return 0
    if verb in SETTINGS:
        usage, count, prefix = SETTINGS[verb]
        need_operands(verb, usage, len(ops), count, count)
        return run_setting(prefix + ops)
    if verb == "show":
        need_operands(verb, "", len(ops), 0, 0)
        return run_show(find_root(root_arg))
    if verb in EDITS:
        usage, low, high, handler = EDITS[verb]
        need_operands(verb, usage, len(ops), low, high)
        return run_edit(find_root(root_arg), handler, ops)
    raise Rejected(f"unknown verb {jd(verb)}; run with --help for the list")


def main(argv: list[str]) -> int:
    try:
        return dispatch(argv)
    except Rejected as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
