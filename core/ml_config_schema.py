"""Parse a .multilint.json and print shell assignments for lint.sh to validate and eval.

lint.sh runs this as `python3 ml_config_schema.py <config-file>`; multilint_config.py imports
the validation constants below so the two never drift apart. Stdlib-only.
"""

import json
import shlex
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
    "markdownlint": {"rules"},
}
OPTION_VARS = {
    ("gitleaks", "depth"): "ML_CFG_GITLEAKS_DEPTH",
    ("gitleaks", "config"): "ML_CFG_GITLEAKS_CONFIG",
    ("bandit", "severity"): "ML_CFG_BANDIT_SEVERITY",
    ("mypy", "cache_dir"): "ML_CFG_MYPY_CACHE_DIR",
    ("markdownlint", "rules"): "ML_CFG_MARKDOWNLINT_RULES",
}
# The built-in markdownlint rule set (MD013 off); markdownlint.rules is overlaid on it.
MARKDOWNLINT_DEFAULTS = Path(__file__).with_name("ml_markdownlint.json")

warnings = []
assignments = []


def jd(value):
    """Render an untrusted string safely: quoted, control characters escaped."""
    return json.dumps(value)


def q(value):
    """Shell-quote a value for eval-safe assignment."""
    return shlex.quote(str(value))


def has_control_char(value):
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def is_nonneg_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def clean_string(value):
    return isinstance(value, str) and value != "" and not has_control_char(value)


def finish():
    for line in assignments:
        print(line)
    for message in warnings:
        print("ml_warnings+=( %s )" % q(message))
    sys.exit(0)


def load_document(config_path):
    """The parsed top-level object, or finish() with a single warning when it cannot be used."""
    try:
        with open(config_path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        warnings.append("cannot read config file: %s" % exc)
        finish()

    if len(raw) > 1024 * 1024:
        warnings.append("config file larger than 1 MiB, ignored")
        finish()

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        warnings.append("config is not valid utf-8: %s" % exc)
        finish()

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        warnings.append("invalid JSON at line %d column %d: %s" % (exc.lineno, exc.colno, exc.msg))
        finish()

    if not isinstance(document, dict):
        warnings.append("config top level must be an object")
        finish()
    return document


def new_parsed():
    """Accumulators filled while walking the document, in document order."""
    return {
        "flat_thresholds": {},
        "checks_thresholds": {},
        "checks_enabled": {},
        "checks_args": {},
        "options": {},
    }


def parse_check_args(check_name, setting_value, parsed):
    if check_name in NO_ARGS_CHECKS:
        warnings.append('"checks.%s.args" has no effect and is ignored' % check_name)
        return
    if not isinstance(setting_value, list) or not all(isinstance(item, str) for item in setting_value):
        warnings.append('"checks.%s.args" must be a list of strings' % check_name)
        return
    if any(has_control_char(item) for item in setting_value):
        warnings.append('"checks.%s.args" contains a control character, ignored' % check_name)
        return
    parsed["checks_args"][check_name] = setting_value


def parse_check_setting(check_name, setting_key, setting_value, parsed):
    if setting_key not in CHECK_SETTING_KEYS:
        warnings.append('unknown key %s under "checks.%s" ignored' % (jd(setting_key), check_name))
        return
    if setting_key == "enabled":
        if not isinstance(setting_value, bool):
            warnings.append('"checks.%s.enabled" must be a boolean' % check_name)
            return
        parsed["checks_enabled"][check_name] = setting_value
    elif setting_key == "threshold":
        if not is_nonneg_int(setting_value):
            warnings.append('"checks.%s.threshold" must be a non-negative integer' % check_name)
            return
        parsed["checks_thresholds"][check_name] = setting_value
    elif setting_key == "args":
        parse_check_args(check_name, setting_value, parsed)


def parse_checks(value, parsed):
    if not isinstance(value, dict):
        warnings.append('"checks" must be an object')
        return
    for check_name, check_value in value.items():
        if check_name not in ALL_CHECKS:
            warnings.append('unknown check %s under "checks" ignored' % jd(check_name))
            continue
        if not isinstance(check_value, dict):
            warnings.append('"checks.%s" must be an object' % check_name)
            continue
        for setting_key, setting_value in check_value.items():
            parse_check_setting(check_name, setting_key, setting_value, parsed)


def parse_option(key, option_key, option_value, options):
    if option_key not in OPTION_GROUPS[key]:
        warnings.append('unknown option %s under "%s" ignored' % (jd(option_key), key))
        return
    if key == "gitleaks" and option_key == "depth":
        if option_value == "all":
            options[(key, option_key)] = "all"
        elif option_value in ("1", 1):
            options[(key, option_key)] = "1"
        else:
            warnings.append('"gitleaks.depth" must be "all" or "1"')
        return
    if key == "bandit" and option_key == "severity":
        if option_value in ("-l", "-ll", "-lll"):
            options[(key, option_key)] = option_value
        else:
            warnings.append('"bandit.severity" must be one of "-l", "-ll", "-lll"')
        return
    if not clean_string(option_value):
        warnings.append('"%s.%s" must be a non-empty string' % (key, option_key))
        return
    options[(key, option_key)] = option_value


def markdownlint_defaults():
    """The built-in rule set beside this script; {} with a warning when it cannot be read."""
    try:
        defaults = json.loads(MARKDOWNLINT_DEFAULTS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        defaults = None
    if not isinstance(defaults, dict):
        warnings.append("%s is unreadable; markdownlint.rules applied without defaults" % MARKDOWNLINT_DEFAULTS.name)
        return {}
    return defaults


def parse_markdownlint_rules(value, options):
    """markdownlint.rules: an object of markdownlint rule settings, stored merged over the defaults as JSON."""
    if not isinstance(value, dict):
        warnings.append('"markdownlint.rules" must be an object')
        return
    options[("markdownlint", "rules")] = json.dumps({**markdownlint_defaults(), **value})


def parse_option_group(key, value, parsed):
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 0:
            warnings.append('"%s" threshold must be a non-negative integer' % key)
        else:
            parsed["flat_thresholds"][key] = value
    elif key == "markdownlint" and isinstance(value, dict):
        for option_key, option_value in value.items():
            if option_key == "rules":
                parse_markdownlint_rules(option_value, parsed["options"])
            else:
                warnings.append('unknown option %s under "%s" ignored' % (jd(option_key), key))
    elif isinstance(value, dict):
        for option_key, option_value in value.items():
            parse_option(key, option_key, option_value, parsed["options"])
    else:
        warnings.append('"%s" must be an object or a non-negative integer' % key)


def parse_top_level(key, value, parsed):
    if key == "checks":
        parse_checks(value, parsed)
    elif key in OPTION_GROUPS:
        parse_option_group(key, value, parsed)
    elif key in ALL_CHECKS:
        if not is_nonneg_int(value):
            warnings.append('"%s" threshold must be a non-negative integer' % key)
        else:
            parsed["flat_thresholds"][key] = value
    else:
        warnings.append("unknown top-level key %s ignored" % jd(key))


def emit_thresholds(flat_thresholds, checks_thresholds):
    for name in ALL_CHECKS:
        has_flat = name in flat_thresholds
        has_checks = name in checks_thresholds
        if has_flat and has_checks:
            assignments.append("%s_threshold=%s" % (name, q(checks_thresholds[name])))
            warnings.append(
                '"%s" has both a flat threshold and "checks.%s.threshold"; using checks.%s.threshold'
                % (name, name, name)
            )
        elif has_checks:
            assignments.append("%s_threshold=%s" % (name, q(checks_thresholds[name])))
        elif has_flat:
            assignments.append("%s_threshold=%s" % (name, q(flat_thresholds[name])))


def emit_assignments(parsed):
    emit_thresholds(parsed["flat_thresholds"], parsed["checks_thresholds"])

    for name, enabled_value in parsed["checks_enabled"].items():
        assignments.append("%s_enabled=%s" % (name, q("on" if enabled_value else "off")))

    for name, arg_list in parsed["checks_args"].items():
        quoted_items = " ".join(q(item) for item in arg_list)
        assignments.append("%s_args=( %s )" % (name, quoted_items))

    for (group_key, option_key), option_value in parsed["options"].items():
        assignments.append("%s=%s" % (OPTION_VARS[(group_key, option_key)], q(option_value)))


def main():
    document = load_document(sys.argv[1])
    parsed = new_parsed()
    for key, value in document.items():
        parse_top_level(key, value, parsed)
    emit_assignments(parsed)
    finish()


if __name__ == "__main__":
    main()
