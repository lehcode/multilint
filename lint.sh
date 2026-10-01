#!/usr/bin/env bash
#
# lint.sh — lint shell, Python, and Markdown files in the given directory.
#
# Policies:
#   - Configurable thresholds per check (zero tolerance = default)
#   - Config file: .multilint.json, resolved $PWD-first (see ml_find_config)
#   - 4-space indentation required for shell scripts
#   - Bashate E006 (line length) is excluded
#
# Config format (.multilint.json), parsed once per run:
#   {
#     "bash_syntax": 0, "shellcheck": 0, "bashate": 0, "shfmt": 0,
#     "flake8": 0, "black": 0, "pylint": 0, "mypy": 0, "bandit": 0,
#     "markdownlint": 0,
#     "yaml_prettier": 0, "json_prettier": 0, "toml_sort": 0,
#     "security_secrets": 0, "security_dangerous_patterns": 0,
#     "checks": {
#       "flake8": { "enabled": true, "threshold": 2, "args": ["--max-line-length=100"] }
#     },
#     "gitleaks": { "depth": "all", "config": ".gitleaks.toml" },
#     "bandit": { "severity": "-lll" },
#     "mypy": { "cache_dir": "/tmp/.mypy_cache" }
#   }
#   Any key omitted defaults to 0/on. The flat top-level form above remains a
#   valid threshold for every check; "checks.<name>.threshold" wins over it
#   when both are present (a warning names the redundant flat key). Precedence
#   for every setting is: .multilint.json value > built-in default. There is
#   no environment-variable layer (user decision, 2026-09-30: environment
#   variables are not a multilint configuration surface). A malformed file,
#   an unknown key, or a wrongly typed value produces a warning (stderr in
#   text mode, JSON "warnings" field) and that single setting falls back
#   rather than silently reading as 0.
#
# Thresholds gate the run verdict: the process exit code and JSON
# "return_code" are 1 if and only if at least one enabled check has failures
# strictly greater than its effective threshold; a check within its threshold
# no longer fails the run by itself.
#
# JSON output (--format json), per entry under "checks":
#   failures            files that failed this check (gitleaks: findings)
#   passed              total - failures
#   total               units actually submitted to the tool; 0 when skipped
#   threshold           configured tolerance
#   threshold_exceeded  failures > threshold
#   status              "ok" | "failed" | "skipped"
#
#   status is "skipped" when the tool is not installed or the check is switched
#   off, so a consumer can tell "nothing was wrong" from "nothing was checked".
#   summary.checks_skipped lists those names, and summary.checks_run is derived
#   from the check list rather than hardcoded.
#
#   A failed check also carries (additive, absent otherwise):
#   findings            [{file, line, rule, message}] parsed from the tool's
#                       output, at most 50 (findings_truncated: true when cut);
#                       line/rule are null when the tool gives none
#   fix                 one-line hint: the exact auto-fix command for formatters,
#                       otherwise "fix the code" (+ a docs link per rule)
#   summary.rules_violated  sorted unique rule IDs across all failed checks

# Configurable thresholds per check (zero tolerance = default)
#
# Usage:
#   bash lint.sh [dir]          # defaults to current directory
#   bash lint.sh [dir] --format json  # JSON output
#   bash lint.sh [dir] --format text  # terminal output (default)

# File-wide: every "<check>_threshold" and "<check>_enabled" variable is
# assigned dynamically via `printf -v` (built-in defaults) and `eval` (the
# .multilint.json loader's filtered output), so shellcheck cannot trace the
# assignment back to the read site. A per-line disable at every one of the
# sixteen checks' guard sites would be noise; the dynamic-assignment pattern
# itself is what SC2154 exists to flag, and it is intentional here.
# shellcheck disable=SC2154

set -euo pipefail

# shellcheck disable=SC2034
TARGET_DIR="${1:-.}"
EXIT_CODE=0
OUTPUT_FORMAT="text"
FILES_CHECKED=()

# Parse flags from remaining args
OUTPUT_FORMAT="text"
fmt_pending=false
for arg in "$@"; do
    case "$arg" in
        --format=*) OUTPUT_FORMAT="${arg#--format=}" ;;
        --format) fmt_pending=true ;;
        *)
            if [ "$fmt_pending" = true ]; then
                OUTPUT_FORMAT="${arg:-text}"
                fmt_pending=false
            fi
            ;;
    esac
done

# Save original stdout; redirect check output to stderr in JSON mode so JSON goes clean to stdout
exec 3>&1
if [ "$OUTPUT_FORMAT" = "json" ]; then
    exec 1>&2
fi

# Canonical check list. Single source of truth for the JSON emitter and for
# summary.checks_run, which used to be a hardcoded 11 that silently disagreed
# with the counters above. It also fixes the order of the "checks" object, which
# would otherwise follow environment-variable order and vary between runs.
ALL_CHECKS=(
    bash_syntax shellcheck bashate shfmt
    flake8 black pylint mypy bandit
    markdownlint
    yaml_prettier json_prettier toml_sort
    security_secrets security_dangerous_patterns
    gitleaks
)

# Fail counters per check (across ALL files)
declare -A check_failures
check_failures[bash_syntax]=0
check_failures[shellcheck]=0
check_failures[bashate]=0
check_failures[shfmt]=0
check_failures[flake8]=0
check_failures[black]=0
check_failures[pylint]=0
check_failures[mypy]=0
check_failures[bandit]=0
check_failures[markdownlint]=0
check_failures[yaml_prettier]=0
check_failures[json_prettier]=0
check_failures[toml_sort]=0
check_failures[security_secrets]=0
check_failures[security_dangerous_patterns]=0
check_failures[gitleaks]=0

# Per-check units actually submitted to the tool, so "passed" has a denominator.
# Without it, failures=0 is indistinguishable from "the check never ran" — the
# false-green this pair of arrays exists to close.
declare -A check_totals
# Per-check outcome: "ok" until something says otherwise, "skipped" when the
# tool is absent or the check is switched off. "failed" is derived in the
# emitter from the failure counter, so it is never stored here.
declare -A check_status
for _ml_check in "${ALL_CHECKS[@]}"; do
    check_totals[$_ml_check]=0
    check_status[$_ml_check]="ok"
done
unset _ml_check

# ---------------------------------------------------------------------------
# Configuration: built-in defaults -> .multilint.json
#
# Two ordered layers, each owning exactly one precedence level. No
# environment variable participates in this resolution (user decision,
# 2026-09-30: environment variables are not a multilint configuration
# surface):
#   1. Built-in defaults (today's behavior: zero tolerance, every check on).
#   2. .multilint.json, read once by a single python3 parse (see
#      ml_find_config below for lookup order) whose output is filtered
#      against a fixed vocabulary before eval, so nothing beyond the
#      variables this script itself defines can ever be assigned.
#   Warnings collected along the way are printed once to stderr and exposed
#   to the JSON emitter as a top-level "warnings" list.
# ---------------------------------------------------------------------------

# ml_find_config: $PWD/.multilint.json first (both plugins set --workdir to
# the scope root, so this is where the file lives on the hook path); fall
# back to $TARGET_DIR/.multilint.json only when TARGET_DIR is a directory,
# preserving direct `bash lint.sh <dir>` usage. TARGET_DIR is a single FILE
# on the plugin path, so it is never treated as a directory to look under --
# that mismatch was defect 1: every threshold silently read as 0.
ml_find_config() {
    if [ -f "$PWD/.multilint.json" ]; then
        echo "$PWD/.multilint.json"
    elif [ -d "$TARGET_DIR" ] && [ -f "$TARGET_DIR/.multilint.json" ]; then
        echo "$TARGET_DIR/.multilint.json"
    fi
}
ml_config_file="$(ml_find_config)"
ml_warnings=()

# 1. Built-in defaults.
for _ml_check in "${ALL_CHECKS[@]}"; do
    printf -v "${_ml_check}_threshold" '%s' 0
    printf -v "${_ml_check}_enabled" '%s' on
done
unset _ml_check
ML_CFG_GITLEAKS_DEPTH=1
ML_CFG_GITLEAKS_CONFIG=""
ML_CFG_BANDIT_SEVERITY="-ll"
ML_CFG_MYPY_CACHE_DIR="/tmp/.mypy_cache"

# Built-in default policy-flag arrays, one per tool-backed check. Each holds
# today's opinionated defaults; "checks.<name>.args" in .multilint.json
# REPLACES the array wholesale (never partially), while the harness flags
# hardcoded at each call site are always kept regardless of args. bash_syntax,
# security_secrets, and security_dangerous_patterns have no configurable
# tool flags and therefore no "_args" array (see NO_ARGS_CHECKS in the loader
# below, which rejects "args" for those three with a warning).
shellcheck_args=(-e SC1091 -e SC2155 -e SC2086 -S style)
bashate_args=(-i E006)
shfmt_args=(-i 4)
# shellcheck disable=SC2054
flake8_args=(--max-line-length=120 --extend-ignore=E203,E111,E121,E124,BLK100)
black_args=(--line-length=120)
# shellcheck disable=SC2054
pylint_args=(--disable=C,R,E0401,E1123,W1510)
mypy_args=(--ignore-missing-imports --follow-imports=silent)
bandit_args=()
# markdownlint's config flag depends on $PWD, so it is computed once here,
# before the loader runs, rather than re-checked per file as before.
if [ -f "$PWD/.markdownlint.json" ]; then
    markdownlint_args=(-c .markdownlint.json)
else
    markdownlint_args=()
fi
yaml_prettier_args=()
json_prettier_args=()
toml_sort_args=(--sort-keys)
gitleaks_args=()

# 2. JSON layer. ml_line_allowed is the line filter: every line the loader
# prints must match one of these patterns or the whole output is discarded.
# Names never derive from input (they come from ALL_CHECKS and the fixed
# ML_CFG_*/ml_warnings names below), and every value the loader prints is
# already shlex.quote()d, so the filter is defence in depth on top of the
# loader's own contract, not the only thing standing between a hostile file
# and eval.
ml_check_alt="$(
    IFS='|'
    echo "${ALL_CHECKS[*]}"
)"
ml_line_allowed() {
    local line="$1"
    if [[ "$line" =~ ^($ml_check_alt)_(threshold|enabled)=.*$ ]]; then
        return 0
    fi
    if [[ "$line" =~ ^($ml_check_alt)_args=\(.*\)$ ]]; then
        return 0
    fi
    if [[ "$line" == ML_CFG_GITLEAKS_DEPTH=* || "$line" == ML_CFG_GITLEAKS_CONFIG=* \
        || "$line" == ML_CFG_BANDIT_SEVERITY=* || "$line" == ML_CFG_MYPY_CACHE_DIR=* ]]; then
        return 0
    fi
    if [[ "$line" == "ml_warnings+=("*")" ]]; then
        return 0
    fi
    return 1
}

if [ -n "$ml_config_file" ]; then
    if command -v python3 >/dev/null 2>&1; then
        if ml_config_out="$(python3 - "$ml_config_file" <<'ML_CONFIG_PY'
import json
import shlex
import sys

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
OPTION_VARS = {
    ("gitleaks", "depth"): "ML_CFG_GITLEAKS_DEPTH",
    ("gitleaks", "config"): "ML_CFG_GITLEAKS_CONFIG",
    ("bandit", "severity"): "ML_CFG_BANDIT_SEVERITY",
    ("mypy", "cache_dir"): "ML_CFG_MYPY_CACHE_DIR",
}

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


config_path = sys.argv[1]

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

flat_thresholds = {}
checks_thresholds = {}
checks_enabled = {}
checks_args = {}
options = {}

for key, value in document.items():
    if key == "checks":
        if not isinstance(value, dict):
            warnings.append('"checks" must be an object')
            continue
        for check_name, check_value in value.items():
            if check_name not in ALL_CHECKS:
                warnings.append('unknown check %s under "checks" ignored' % jd(check_name))
                continue
            if not isinstance(check_value, dict):
                warnings.append('"checks.%s" must be an object' % check_name)
                continue
            for setting_key, setting_value in check_value.items():
                if setting_key not in CHECK_SETTING_KEYS:
                    warnings.append(
                        'unknown key %s under "checks.%s" ignored' % (jd(setting_key), check_name)
                    )
                    continue
                if setting_key == "enabled":
                    if not isinstance(setting_value, bool):
                        warnings.append('"checks.%s.enabled" must be a boolean' % check_name)
                        continue
                    checks_enabled[check_name] = setting_value
                elif setting_key == "threshold":
                    if not is_nonneg_int(setting_value):
                        warnings.append('"checks.%s.threshold" must be a non-negative integer' % check_name)
                        continue
                    checks_thresholds[check_name] = setting_value
                elif setting_key == "args":
                    if check_name in NO_ARGS_CHECKS:
                        warnings.append('"checks.%s.args" has no effect and is ignored' % check_name)
                        continue
                    if not isinstance(setting_value, list) or not all(
                        isinstance(item, str) for item in setting_value
                    ):
                        warnings.append('"checks.%s.args" must be a list of strings' % check_name)
                        continue
                    if any(has_control_char(item) for item in setting_value):
                        warnings.append('"checks.%s.args" contains a control character, ignored' % check_name)
                        continue
                    checks_args[check_name] = setting_value
    elif key in OPTION_GROUPS:
        if isinstance(value, int) and not isinstance(value, bool):
            if value < 0:
                warnings.append('"%s" threshold must be a non-negative integer' % key)
            else:
                flat_thresholds[key] = value
        elif isinstance(value, dict):
            for option_key, option_value in value.items():
                if option_key not in OPTION_GROUPS[key]:
                    warnings.append('unknown option %s under "%s" ignored' % (jd(option_key), key))
                    continue
                if key == "gitleaks" and option_key == "depth":
                    if option_value == "all":
                        options[(key, option_key)] = "all"
                    elif option_value in ("1", 1):
                        options[(key, option_key)] = "1"
                    else:
                        warnings.append('"gitleaks.depth" must be "all" or "1"')
                    continue
                if key == "bandit" and option_key == "severity":
                    if option_value in ("-l", "-ll", "-lll"):
                        options[(key, option_key)] = option_value
                    else:
                        warnings.append('"bandit.severity" must be one of "-l", "-ll", "-lll"')
                    continue
                if not clean_string(option_value):
                    warnings.append('"%s.%s" must be a non-empty string' % (key, option_key))
                    continue
                options[(key, option_key)] = option_value
        else:
            warnings.append('"%s" must be an object or a non-negative integer' % key)
    elif key in ALL_CHECKS:
        if not is_nonneg_int(value):
            warnings.append('"%s" threshold must be a non-negative integer' % key)
        else:
            flat_thresholds[key] = value
    else:
        warnings.append('unknown top-level key %s ignored' % jd(key))

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

for name, enabled_value in checks_enabled.items():
    assignments.append("%s_enabled=%s" % (name, q("on" if enabled_value else "off")))

for name, arg_list in checks_args.items():
    quoted_items = " ".join(q(item) for item in arg_list)
    assignments.append("%s_args=( %s )" % (name, quoted_items))

for (group_key, option_key), option_value in options.items():
    assignments.append("%s=%s" % (OPTION_VARS[(group_key, option_key)], q(option_value)))

finish()
ML_CONFIG_PY
        )"; then
            ml_config_ok=true
            while IFS= read -r ml_line; do
                [ -z "$ml_line" ] && continue
                if ! ml_line_allowed "$ml_line"; then
                    ml_config_ok=false
                    break
                fi
            done <<<"$ml_config_out"
            if [ "$ml_config_ok" = true ]; then
                if [ -n "$ml_config_out" ]; then
                    eval "$ml_config_out"
                fi
            else
                ml_warnings+=("configuration loader produced unexpected output; ignoring the file")
            fi
        else
            ml_warnings+=("configuration loader failed to run; using defaults")
        fi
    else
        ml_warnings+=("configuration file found but python3 is not available; using defaults")
    fi
fi

# mypy writes an incremental cache beside the sources it checks. The workspace is
# mounted read-only, where that makes mypy abort with "INTERNAL ERROR", so the
# cache is redirected to a writable path. Resolved from .multilint.json's
# mypy.cache_dir, falling back to the same built-in default the JSON layer
# already applies when nothing is configured.
MYPY_CACHE_DIR="${ML_CFG_MYPY_CACHE_DIR:-/tmp/.mypy_cache}"

# bandit reports low-severity findings (assert usage, subprocess imports) that are
# noise in this codebase; -ll limits output to medium and high severity. Resolved
# from .multilint.json's bandit.severity, falling back to the built-in default.
BANDIT_SEVERITY="${ML_CFG_BANDIT_SEVERITY:--ll}"

# bandit's default report appends a metrics block that says nothing actionable. The
# custom format collapses each finding to a single grep-friendly line.
BANDIT_TEMPLATE="{relpath}:{line}: [{test_id}] {severity}: {msg}"

# Gitleaks depth control: "1" = last commit, "all" = full history. Resolved
# from .multilint.json's gitleaks.depth, falling back to the built-in default.
GITLEAKS_DEPTH="${ML_CFG_GITLEAKS_DEPTH:-1}"

# Gitleaks config path override: .multilint.json's gitleaks.config, falling
# back to the image/beside-script candidates tried below.
GITLEAKS_CONFIG_OVERRIDE="${ML_CFG_GITLEAKS_CONFIG:-}"
# A relative gitleaks.config is relative to the .multilint.json that names it,
# not to whatever $PWD lint.sh happens to run in. A configured file that does
# not exist is reported: falling through to the built-in rules unannounced
# would drop the project's allowlist and custom rules without a trace.
if [ -n "$GITLEAKS_CONFIG_OVERRIDE" ]; then
    if [[ "$GITLEAKS_CONFIG_OVERRIDE" != /* ]] && [ -n "$ml_config_file" ]; then
        GITLEAKS_CONFIG_OVERRIDE="$(dirname "$ml_config_file")/$GITLEAKS_CONFIG_OVERRIDE"
    fi
    if [ ! -f "$GITLEAKS_CONFIG_OVERRIDE" ]; then
        ml_warnings+=("gitleaks.config: $GITLEAKS_CONFIG_OVERRIDE does not exist; using the built-in gitleaks rules")
    fi
fi

# 3. Warnings -- printed once, regardless of output format, since this
# explicit stderr redirect (>&2) bypasses the JSON-mode stdout swap above.
for ml_warning in "${ml_warnings[@]+"${ml_warnings[@]}"}"; do
    printf 'multilint: config warning: %s\n' "$ml_warning" >&2
done

info()  { echo -e "\n\033[1m→ $*\033[0m"; }
pass()  { echo "  ✓ $*"; }
fail()  { echo "  ✗ $*"; }
warn()  { echo "  ~ $*"; }

# Record that $1 actually ran against one more unit of work.
ran()  { check_totals[$1]=$(( check_totals[$1] + 1 )); }
# Record that $1 did not run at all. Idempotent: the guards live inside the
# per-file loops, so this fires once per file when a tool is missing.
skipped() { check_status[$1]="skipped"; }

# Raw tool output per failing unit, kept for the JSON emitter, which parses it
# into "findings". One <seq>.<check>.file/.out pair per call so the emitter can
# replay them in order and no tool output can collide with a delimiter. The
# human report above is unchanged: this only keeps a copy.
ML_MSG_DIR="$(mktemp -d 2>/dev/null)" || ML_MSG_DIR=""
if [ -n "$ML_MSG_DIR" ]; then
    trap 'rm -rf "$ML_MSG_DIR"' EXIT
fi
ml_msg_seq=0
# record_output <check> <file> <raw output>; <file> is "" for repo-wide tools.
record_output() {
    [ -n "$ML_MSG_DIR" ] || return 0
    ml_msg_seq=$(( ml_msg_seq + 1 ))
    printf '%s' "$2" >"$ML_MSG_DIR/$(printf '%04d' "$ml_msg_seq").$1.file"
    printf '%s\n' "$3" >"$ML_MSG_DIR/$(printf '%04d' "$ml_msg_seq").$1.out"
}

# True unless "<check>_enabled" has been set to "off" (built-in default, then
# .multilint.json). One helper shared by every one of the sixteen checks so
# there is a single oracle for "is this check switched on".
check_enabled() {
    local v="${1}_enabled"
    [ "${!v}" != "off" ]
}

# ---------------------------------------------------------------------------
# Shell scripts
# ---------------------------------------------------------------------------
info "Linting shell scripts..."

mapfile -t shell_files < <(find "$TARGET_DIR" -type f \( -name '*.sh' -o -name '*.bash' \) ! -path '*/\.*' ! -path '*/tests/*' ! -path '*/test/*' ! -path '*/venv/*' ! -path '*/node_modules/*' 2>/dev/null)

if [ ${#shell_files[@]} -eq 0 ]; then
    echo "  (none found)"
else
    for f in "${shell_files[@]}"; do
        FILES_CHECKED+=("$f")
        echo ""
        echo "  📄 $f"

        # Bash syntax check — bash itself is always present; only enablement
        # (checks.bash_syntax.enabled) can skip it. args is unsupported here
        # (bash -n takes no policy flags).
        if ! check_enabled bash_syntax; then
            skipped bash_syntax
            warn "bash_syntax (disabled)"
        else
            ran bash_syntax
            if bash -n "$f" >/dev/null 2>&1; then
                pass "bash syntax"
            else
                bs_output="$(bash -n "$f" 2>&1 || true)"
                echo "    $bs_output"
                record_output bash_syntax "$f" "$bs_output"
                check_failures[bash_syntax]=$(( check_failures[bash_syntax] + 1 ))
                fail "bash syntax"
            fi
        fi

        # ShellCheck — policy flags come from shellcheck_args (default:
        # excludes SC1091/SC2155/SC2086, reports down to style severity).
        # -f gcc is a harness flag, always kept after the policy flags: one
        # file:line:col line per finding, which the JSON emitter can parse.
        if ! check_enabled shellcheck; then
            skipped shellcheck
            warn "shellcheck (disabled)"
        elif command -v shellcheck >/dev/null 2>&1; then
            ran shellcheck
            set +e
            sc_output="$(shellcheck "${shellcheck_args[@]+"${shellcheck_args[@]}"}" -f gcc "$f" 2>&1)"
            sc_code=$?
            set -e
            if [ "$sc_code" -eq 0 ]; then
                pass "shellcheck"
            else
                echo "    $sc_output"
                record_output shellcheck "$f" "$sc_output"
                check_failures[shellcheck]=$(( check_failures[shellcheck] + 1 ))
                fail "shellcheck"
            fi
        else
            skipped shellcheck
            warn "shellcheck (not installed, skipping)"
        fi

        # Bashate — 4-space indentation check; policy flags come from
        # bashate_args (default: excludes E006, line length).
        if ! check_enabled bashate; then
            skipped bashate
            warn "bashate (disabled)"
        elif command -v bashate >/dev/null 2>&1; then
            ran bashate
            set +e
            bashate_output="$(bashate "${bashate_args[@]+"${bashate_args[@]}"}" "$f" 2>&1)"
            bashate_rc=$?
            set -e
            if [ "$bashate_rc" -eq 0 ]; then
                pass "bashate"
            else
                echo "    $bashate_output"
                record_output bashate "$f" "$bashate_output"
                check_failures[bashate]=$(( check_failures[bashate] + 1 ))
                fail "bashate (indentation required)"
            fi
        else
            skipped bashate
            warn "bashate (not installed, skipping)"
        fi

        # shfmt (format check only)
        #
        # -i 4 is load-bearing, not cosmetic. shfmt's default is 0, meaning TAB indents, while
        # bashate above emits E002 "Tab indents" and E003 "Indent not multiple of 4" — so with the
        # default the two checks demanded opposite things and NO shell file could pass both. Four
        # spaces is the documented project policy (see Policies in README.md) and what three of the
        # four shell files here already use, and bashate's rule cannot be configured to accept tabs,
        # so shfmt is the side that gets configured.
        #
        # The cost of not doing this was real: claude-plugin/scripts/lint-changed.sh was written with
        # no indented lines at all, purely so it could satisfy both checks.
        #
        # Passing a printer flag also makes shfmt ignore any .editorconfig it finds, which keeps this
        # verdict identical inside the container and on a contributor's machine.
        if ! check_enabled shfmt; then
            skipped shfmt
            warn "shfmt (disabled)"
        elif command -v shfmt >/dev/null 2>&1; then
            ran shfmt
            if shfmt "${shfmt_args[@]+"${shfmt_args[@]}"}" -d "$f" | grep -q .; then
                record_output shfmt "$f" ""
                check_failures[shfmt]=$(( check_failures[shfmt] + 1 ))
                fail "shfmt (formatting required)"
            else
                pass "shfmt"
            fi
        else
            skipped shfmt
            warn "shfmt (not installed, skipping)"
        fi
    done
fi

echo ""
# Threshold summary for shell checks
for check in bash_syntax shellcheck bashate shfmt; do
    failures=${check_failures[$check]}
    threshold_var="${check}_threshold"
    threshold=${!threshold_var}
    if [ "$failures" -gt 0 ]; then
        if [ "$failures" -gt "$threshold" ]; then
            echo "  ⚠ $check: $failures failures (threshold: $threshold)"
        else
            echo "  ✓ $check: $failures failures (threshold: $threshold)"
        fi
    else
        echo "  ✓ $check: 0 failures (threshold: $threshold)"
    fi
done

# ---------------------------------------------------------------------------
# Python scripts
# ---------------------------------------------------------------------------
info "Linting Python scripts..."

mapfile -t py_files < <(find "$TARGET_DIR" -type f -name '*.py' ! -path '*/\.*' ! -path '*/test/*' ! -path '*/__pycache__/*' ! -path '*/venv/*' ! -path '*/node_modules/*' ! -path '*/fixtures/*' 2>/dev/null)

if [ ${#py_files[@]} -eq 0 ]; then
    echo "  (none found)"
else
    for f in "${py_files[@]}"; do
        FILES_CHECKED+=("$f")
        echo ""
        echo "  🐍 $f"

        # flake8 (style); policy flags come from flake8_args.
        if ! check_enabled flake8; then
            skipped flake8
            warn "flake8 (disabled)"
        elif command -v flake8 >/dev/null 2>&1; then
            ran flake8
            set +e
            pb_flake8="$(flake8 "${flake8_args[@]+"${flake8_args[@]}"}" "$f" 2>&1)"
            pb_flake8_rc=$?
            set -e
            if [ "$pb_flake8_rc" -eq 0 ]; then
                pass "flake8"
            else
                echo "    $pb_flake8"
                record_output flake8 "$f" "$pb_flake8"
                check_failures[flake8]=$(( check_failures[flake8] + 1 ))
                fail "flake8"
            fi
        else
            skipped flake8
            warn "flake8 (not installed, skipping)"
        fi

        # black (formatting); harness flag --check is always kept so a run
        # never rewrites the target file; policy flags come from black_args.
        if ! check_enabled black; then
            skipped black
            warn "black (disabled)"
        elif command -v black >/dev/null 2>&1; then
            ran black
            set +e
            pb_black="$(black --check "${black_args[@]+"${black_args[@]}"}" "$f" 2>&1)"
            pb_black_rc=$?
            set -e
            if [ "$pb_black_rc" -eq 0 ]; then
                pass "black"
            else
                echo "    $pb_black"
                record_output black "$f" "$pb_black"
                check_failures[black]=$(( check_failures[black] + 1 ))
                fail "black"
            fi
        else
            skipped black
            warn "black (not installed, skipping)"
        fi

        # pylint (errors/warnings); harness flag --output-format=text is
        # always kept; policy flags come from pylint_args.
        if ! check_enabled pylint; then
            skipped pylint
            warn "pylint (disabled)"
        elif command -v pylint >/dev/null 2>&1; then
            ran pylint
            set +e
            pb_pylint="$(pylint --output-format=text "${pylint_args[@]+"${pylint_args[@]}"}" "$f" 2>&1)"
            pb_pylint_rc=$?
            set -e
            if [ "$pb_pylint_rc" -eq 0 ]; then
                pass "pylint"
            else
                echo "    $pb_pylint"
                record_output pylint "$f" "$pb_pylint"
                check_failures[pylint]=$(( check_failures[pylint] + 1 ))
                fail "pylint"
            fi
        else
            skipped pylint
            warn "pylint (not installed, skipping)"
        fi

        # mypy (static types)
        #
        # --ignore-missing-imports and --follow-imports=silent are the default
        # mypy_args (a checks.mypy.args override replaces them): the image
        # installs no project dependencies, so without them every third-party
        # import reports import-not-found and drowns out real findings. This
        # mirrors pylint running with E0401 disabled. --cache-dir and
        # --no-error-summary are harness flags and are always kept.
        if ! check_enabled mypy; then
            skipped mypy
            warn "mypy (disabled)"
        elif command -v mypy >/dev/null 2>&1; then
            ran mypy
            set +e
            pb_mypy="$(mypy --cache-dir="$MYPY_CACHE_DIR" --no-error-summary \
                "${mypy_args[@]+"${mypy_args[@]}"}" "$f" 2>&1)"
            pb_mypy_rc=$?
            set -e
            if [ "$pb_mypy_rc" -eq 0 ]; then
                pass "mypy"
            else
                echo "    $pb_mypy"
                record_output mypy "$f" "$pb_mypy"
                check_failures[mypy]=$(( check_failures[mypy] + 1 ))
                fail "mypy"
            fi
        else
            skipped mypy
            warn "mypy (not installed, skipping)"
        fi

        # bandit (security); harness flags -q "$BANDIT_SEVERITY" -f custom
        # --msg-template are always kept (severity is the bandit.severity
        # option, not args); bandit has no default policy flags of its own.
        if ! check_enabled bandit; then
            skipped bandit
            warn "bandit (disabled)"
        elif command -v bandit >/dev/null 2>&1; then
            ran bandit
            set +e
            pb_bandit="$(bandit -q "$BANDIT_SEVERITY" -f custom \
                --msg-template "$BANDIT_TEMPLATE" "${bandit_args[@]+"${bandit_args[@]}"}" "$f" 2>&1)"
            pb_bandit_rc=$?
            set -e
            if [ "$pb_bandit_rc" -eq 0 ]; then
                pass "bandit"
            else
                echo "    $pb_bandit"
                record_output bandit "$f" "$pb_bandit"
                check_failures[bandit]=$(( check_failures[bandit] + 1 ))
                fail "bandit"
            fi
        else
            skipped bandit
            warn "bandit (not installed, skipping)"
        fi
    done
fi

echo ""
# Threshold summary for Python checks
for check in flake8 black pylint mypy bandit; do
    failures=${check_failures[$check]}
    threshold_var="${check}_threshold"
    threshold=${!threshold_var}
    if [ "$failures" -gt 0 ]; then
        if [ "$failures" -gt "$threshold" ]; then
            echo "  ⚠ $check: $failures failures (threshold: $threshold)"
        else
            echo "  ✓ $check: $failures failures (threshold: $threshold)"
        fi
    else
        echo "  ✓ $check: 0 failures (threshold: $threshold)"
    fi
done

# ---------------------------------------------------------------------------
# Markdown files
# ---------------------------------------------------------------------------
info "Linting Markdown files..."

mapfile -t md_files < <(find "$TARGET_DIR" -type f -name '*.md' ! -path '*/\.*' ! -path '*/node_modules/*' ! -path '*/cache/*' ! -path '*/output/*' 2>/dev/null)

if [ ${#md_files[@]} -eq 0 ]; then
    echo "  (none found)"
else
    for f in "${md_files[@]}"; do
        FILES_CHECKED+=("$f")
        echo ""
        echo "  📝 $f"

        # markdownlint; policy flags come from markdownlint_args (default:
        # -c .markdownlint.json when that file exists in $PWD, computed once
        # in the built-in defaults above, before the loader runs). The array
        # expansion also replaces the former unquoted $MD_CONFIG word-split.
        if ! check_enabled markdownlint; then
            skipped markdownlint
            warn "markdownlint (disabled)"
        elif command -v markdownlint >/dev/null 2>&1; then
            ran markdownlint
            # The former `|| true` on the next line was both redundant and
            # actively harmful: `set +e` already stops a non-zero exit from
            # aborting the script, while `|| true` made the command list itself
            # succeed, so md_rc read `true`'s 0 and never markdownlint's status.
            # Every Markdown file therefore reported ✓ — including while the
            # binary was crashing outright with "Cannot find package 'commander'".
            set +e
            md_output="$(markdownlint "${markdownlint_args[@]+"${markdownlint_args[@]}"}" "$f" 2>&1)"
            md_rc=$?
            set -e
            if [ "$md_rc" -eq 0 ]; then
                pass "markdownlint"
            else
                echo "    $md_output"
                record_output markdownlint "$f" "$md_output"
                check_failures[markdownlint]=$(( check_failures[markdownlint] + 1 ))
                fail "markdownlint"
            fi
        else
            skipped markdownlint
            warn "markdownlint (not installed, skipping)"
        fi
    done
fi

echo ""
# Threshold summary for Markdown checks
# shellcheck disable=SC2043
for check in markdownlint; do
    failures=${check_failures[$check]}
    threshold_var="${check}_threshold"
    threshold=${!threshold_var}
    if [ "$failures" -gt 0 ]; then
        if [ "$failures" -gt "$threshold" ]; then
            echo "  ⚠ $check: $failures failures (threshold: $threshold)"
        else
            echo "  ✓ $check: $failures failures (threshold: $threshold)"
        fi
    else
        echo "  ✓ $check: 0 failures (threshold: $threshold)"
    fi
done

# ---------------------------------------------------------------------------
# YAML and JSON files
# ---------------------------------------------------------------------------
info "Linting YAML and JSON files..."

mapfile -t yaml_files < <(find "$TARGET_DIR" -type f \( -name '*.yaml' -o -name '*.yml' \) ! -path '*/\.*' ! -path '*/venv/*' ! -path '*/node_modules/*' ! -path '*/__pycache__/*' 2>/dev/null)
mapfile -t json_files < <(find "$TARGET_DIR" -type f -name '*.json' ! -path '*/\.*' ! -path '*/node_modules/*' ! -path '*/venv/*' ! -path '*/__pycache__/*' 2>/dev/null)

if [ ${#yaml_files[@]} -eq 0 ] && [ ${#json_files[@]} -eq 0 ]; then
    echo "  (none found)"
elif ! check_enabled yaml_prettier && ! check_enabled json_prettier; then
    skipped yaml_prettier
    skipped json_prettier
    warn "YAML/JSON checks (disabled)"
elif command -v prettier >/dev/null 2>&1; then
    for f in "${yaml_files[@]}" "${json_files[@]}"; do
        FILES_CHECKED+=("$f")
        echo ""
        echo "  📋 $f"

        # yaml_prettier and json_prettier resolve independently -- each has
        # its own "checks.<name>.enabled" and "checks.<name>.args" in
        # .multilint.json. Harness flags --check --log-level error are
        # always kept.
        if [[ "$f" == *.yaml || "$f" == *.yml ]]; then
            if ! check_enabled yaml_prettier; then
                skipped yaml_prettier
                warn "yaml prettier (disabled)"
            else
                ran yaml_prettier
                set +e
                prettier_output="$(prettier --check --log-level error \
                    "${yaml_prettier_args[@]+"${yaml_prettier_args[@]}"}" "$f" 2>&1)"
                prettier_rc=$?
                set -e
                if [ "$prettier_rc" -eq 0 ]; then
                    pass "yaml prettier"
                else
                    echo "    $prettier_output"
                    record_output yaml_prettier "$f" "$prettier_output"
                    check_failures[yaml_prettier]=$(( check_failures[yaml_prettier] + 1 ))
                    fail "yaml prettier"
                fi
            fi
        elif [[ "$f" == *.json ]]; then
            if ! check_enabled json_prettier; then
                skipped json_prettier
                warn "json prettier (disabled)"
            else
                ran json_prettier
                set +e
                prettier_output="$(prettier --check --log-level error \
                    "${json_prettier_args[@]+"${json_prettier_args[@]}"}" "$f" 2>&1)"
                prettier_rc=$?
                set -e
                if [ "$prettier_rc" -eq 0 ]; then
                    pass "json prettier"
                else
                    echo "    $prettier_output"
                    record_output json_prettier "$f" "$prettier_output"
                    check_failures[json_prettier]=$(( check_failures[json_prettier] + 1 ))
                    fail "json prettier"
                fi
            fi
        fi
    done
else
    skipped yaml_prettier
    skipped json_prettier
    warn "prettier (not installed, skipping YAML/JSON checks)"
fi

echo ""
# Threshold summary for YAML/JSON checks
for check in yaml_prettier json_prettier; do
    failures=${check_failures[$check]}
    threshold_var="${check}_threshold"
    threshold=${!threshold_var}
    if [ "$failures" -gt 0 ]; then
        if [ "$failures" -gt "$threshold" ]; then
            echo "  ⚠ $check: $failures failures (threshold: $threshold)"
        else
            echo "  ✓ $check: $failures failures (threshold: $threshold)"
        fi
    else
        echo "  ✓ $check: 0 failures (threshold: $threshold)"
    fi
done

# ---------------------------------------------------------------------------
# TOML files
# ---------------------------------------------------------------------------
info "Linting TOML files..."

mapfile -t toml_files < <(find "$TARGET_DIR" -type f -name '*.toml' ! -path '*/\.*' ! -path '*/venv/*' ! -path '*/node_modules/*' ! -path '*/__pycache__/*' 2>/dev/null)

if [ ${#toml_files[@]} -eq 0 ]; then
    echo "  (none found)"
else
    if ! check_enabled toml_sort; then
        skipped toml_sort
        warn "TOML checks (disabled)"
    elif command -v toml-sort >/dev/null 2>&1; then
        for f in "${toml_files[@]}"; do
            FILES_CHECKED+=("$f")
            echo ""
            echo "  📝 $f"

            # Harness flag --check is always kept; policy flags come from
            # toml_sort_args (default: --sort-keys).
            ran toml_sort
            set +e
            toml_output="$(toml-sort --check "${toml_sort_args[@]+"${toml_sort_args[@]}"}" "$f" 2>&1)"
            toml_rc=$?
            set -e
            if [ "$toml_rc" -eq 0 ]; then
                pass "toml-sort"
            else
                echo "    $toml_output"
                record_output toml_sort "$f" "$toml_output"
                check_failures[toml_sort]=$(( check_failures[toml_sort] + 1 ))
                fail "toml-sort"
            fi
        done
    else
        skipped toml_sort
        warn "toml-sort (not installed, skipping TOML checks)"
    fi
fi

echo ""
# Threshold summary for TOML checks
# shellcheck disable=SC2043
for check in toml_sort; do
    failures=${check_failures[$check]}
    threshold_var="${check}_threshold"
    threshold=${!threshold_var}
    if [ "$failures" -gt 0 ]; then
        if [ "$failures" -gt "$threshold" ]; then
            echo "  ⚠ $check: $failures failures (threshold: $threshold)"
        else
            echo "  ✓ $check: $failures failures (threshold: $threshold)"
        fi
    else
        echo "  ✓ $check: 0 failures (threshold: $threshold)"
    fi
done

# ---------------------------------------------------------------------------
# Security scanning
# ---------------------------------------------------------------------------
info "Running security scans..."

# security_secrets and security_dangerous_patterns resolve independently --
# each has its own "checks.<name>.enabled" in .multilint.json.
if ! check_enabled security_secrets; then
    skipped security_secrets
    warn "security_secrets (disabled)"
else
    # --- Hardcoded secrets in shell scripts ---
    info "Checking for hardcoded secrets in shell scripts..."
    # shellcheck disable=SC2043
    for f in "${shell_files[@]}"; do
        # Skip the lint script itself (contains patterns in comments)
        [ "$(basename "$f")" = "lint.sh" ] && continue
        ran security_secrets
        set +e
        secret_output=$(grep -Eni \
            "(password|passwd|secret|api_key|apikey|token|auth_token)[[:space:]]*=[[:space:]]*[\"'][^\"']+[\"']" \
            "$f" 2>&1) || secret_rc=$?
        set -e
        if [ "${secret_rc:-0}" -eq 0 ]; then
            check_failures[security_secrets]=$(( check_failures[security_secrets] + 1 ))
            echo "  ✗ hardcoded secrets: $f"
            echo "    $secret_output"
            record_output security_secrets "$f" "$secret_output"
            fail "security (hardcoded secrets)"
        fi
    done

    # --- Hardcoded secrets in Python files ---
    info "Checking for hardcoded secrets in Python files..."
    # shellcheck disable=SC2043
    for f in "${py_files[@]}"; do
        # Skip server/mcp/test/init files (docstrings/strings contain pattern keywords)
        case "$(basename "$f")" in
            server.py|mcp_server.py|test_*.py|__init__.py) continue ;;
        esac
        # Skip test fixture directories
        case "$f" in
            */fixtures/*|*/test_project/*) continue ;;
        esac
        ran security_secrets
        set +e
        py_secret_output=$(grep -Eni \
            "(password|passwd|secret|api_key|apikey|token|auth_token)[[:space:]]*=[[:space:]]*[\"'][^\"']+[\"']" \
            "$f" 2>&1) || py_secret_rc=$?
        set -e
        if [ "${py_secret_rc:-0}" -eq 0 ]; then
            check_failures[security_secrets]=$(( check_failures[security_secrets] + 1 ))
            echo "  ✗ hardcoded secrets: $f"
            echo "    $py_secret_output"
            record_output security_secrets "$f" "$py_secret_output"
            fail "security (hardcoded secrets)"
        fi
    done
fi

if ! check_enabled security_dangerous_patterns; then
    skipped security_dangerous_patterns
    warn "security_dangerous_patterns (disabled)"
else
    # --- Dangerous shell patterns ---
    info "Checking for dangerous shell patterns..."
    # shellcheck disable=SC2043
    for f in "${shell_files[@]}"; do
        # Skip the lint script itself (contains patterns in comments)
        [ "$(basename "$f")" = "lint.sh" ] && continue
        ran security_dangerous_patterns
        set +e
        dangerous_output=$(grep -Eni \
            'chmod[[:space:]]+777|curl[[:space:]].*[[:space:]]*\|[[:space:]]*.*bash|eval[[:space:]]+.*\$' \
            "$f" 2>&1) || dangerous_rc=$?
        set -e
        if [ "${dangerous_rc:-0}" -eq 0 ]; then
            check_failures[security_dangerous_patterns]=$(( check_failures[security_dangerous_patterns] + 1 ))
            echo "  ✗ dangerous patterns: $f"
            echo "    $dangerous_output"
            record_output security_dangerous_patterns "$f" "$dangerous_output"
            fail "security (dangerous patterns)"
        fi
    done
fi

echo ""
# Threshold summary for security checks
for check in security_secrets security_dangerous_patterns; do
    failures=${check_failures[$check]}
    threshold_var="${check}_threshold"
    threshold=${!threshold_var}
    if [ "$failures" -gt 0 ]; then
        if [ "$failures" -gt "$threshold" ]; then
            echo "  ⚠ $check: $failures failures (threshold: $threshold)"
        else
            echo "  ✓ $check: $failures failures (threshold: $threshold)"
        fi
    else
        echo "  ✓ $check: 0 failures (threshold: $threshold)"
    fi
done

# ---------------------------------------------------------------------------
# Gitleaks — git history secret detection
# ---------------------------------------------------------------------------
info "Running gitleaks..."

if ! check_enabled gitleaks; then
    skipped gitleaks
    warn "gitleaks (disabled)"
elif command -v gitleaks >/dev/null 2>&1; then
    # Which directory to search for a repository. TARGET_DIR is "$1", and both plugins pass a single
    # FILE, so the previous test -- [ ! -d "$TARGET_DIR/.git" ] -- asked whether a path *underneath a
    # file* was a directory. It never was, so gitleaks reported "skipped" after every edit and in
    # practice never ran on the plugin path at all.
    if [ -d "$TARGET_DIR" ]; then
        gitleaks_probe="$TARGET_DIR"
    else
        gitleaks_probe="$(dirname "$TARGET_DIR")"
    fi

    # Nearest ancestor holding .git, so a file deep inside a repository still gets history scanned.
    # No ceiling is needed here: this runs inside the container, where the mount is the only thing
    # visible, and the loop stops at "/" regardless.
    gitleaks_repo=""
    gitleaks_probe="$(cd "$gitleaks_probe" 2>/dev/null && pwd)" || gitleaks_probe=""
    while [ -n "$gitleaks_probe" ]; do
        if [ -d "$gitleaks_probe/.git" ]; then
            gitleaks_repo="$gitleaks_probe"
            break
        fi
        [ "$gitleaks_probe" = "/" ] && break
        gitleaks_probe="$(dirname "$gitleaks_probe")"
    done

    # The config lived at a hardcoded /usr/local/bin/.gitleaks.toml, which only exists inside the
    # image. Run on a host or in CI, gitleaks died with "unable to load gitleaks config" and, because
    # of the pass condition below, that counted as a pass. Resolved against the image path first so
    # container behaviour is unchanged, then against the copy beside this script, then dropped so
    # gitleaks falls back to its built-in rules rather than refusing to start.
    gitleaks_config=""
    for candidate in \
        "$GITLEAKS_CONFIG_OVERRIDE" \
        /usr/local/bin/.gitleaks.toml \
        "$(dirname "${BASH_SOURCE[0]}")/.gitleaks.toml"; do
        if [ -n "$candidate" ] && [ -f "$candidate" ]; then
            gitleaks_config="$candidate"
            break
        fi
    done
    gitleaks_config_args=()
    if [ -n "$gitleaks_config" ]; then
        gitleaks_config_args=(--config "$gitleaks_config")
    fi

    ran gitleaks
    set +e
    # Project args (checks.gitleaks.args, default empty) are inserted before
    # --verbose; the harness flags around them (detect, --source/--no-git,
    # config args, --log-opts, --verbose --no-color --no-banner) are always
    # kept regardless of args.
    if [ -n "$gitleaks_repo" ]; then
        # History mode. GITLEAKS_DEPTH=all walks every commit; the default limits it to the most
        # recent one, which is what makes this affordable to run after a single edit.
        if [ "$GITLEAKS_DEPTH" = "all" ]; then
            gitleaks_output="$(gitleaks detect --source "$gitleaks_repo" \
                "${gitleaks_config_args[@]}" \
                "${gitleaks_args[@]+"${gitleaks_args[@]}"}" \
                --verbose --no-color --no-banner 2>&1)"
        else
            gitleaks_output="$(gitleaks detect --source "$gitleaks_repo" \
                "${gitleaks_config_args[@]}" \
                --log-opts="-1" \
                "${gitleaks_args[@]+"${gitleaks_args[@]}"}" \
                --verbose --no-color --no-banner 2>&1)"
        fi
    else
        # No repository, so there is no history -- but the working copy can still be scanned.
        # --no-git treats the source as an ordinary path, which may be a single file, and --log-opts
        # is documented as having no effect in this mode, so it is not passed. This is the branch that
        # turns a permanent "skipped" into a real verdict.
        gitleaks_output="$(gitleaks detect --no-git --source "$TARGET_DIR" \
            "${gitleaks_config_args[@]}" \
            "${gitleaks_args[@]+"${gitleaks_args[@]}"}" \
            --verbose --no-color --no-banner 2>&1)"
    fi
    gitleaks_rc=$?
    set -e
    gitleaks_findings=$(echo "$gitleaks_output" | grep -c "Finding:" 2>/dev/null || true)
    gitleaks_findings=${gitleaks_findings:-0}
    # gitleaks exits 1 both for "leaks found" and for its own failures -- a missing config file exits
    # 1 with no findings -- so the exit status alone cannot tell them apart. The previous condition
    # was `rc -eq 0 || findings -eq 0`, which resolved that ambiguity by calling both a pass: a
    # gitleaks that never started reported ✓. Non-zero with no findings is now reported as skipped,
    # which is what it is.
    if [ "$gitleaks_rc" -eq 0 ]; then
        pass "gitleaks"
    elif [ "$gitleaks_findings" -gt 0 ]; then
        echo "$gitleaks_output" | head -50
        record_output gitleaks "" "$gitleaks_output"
        check_failures[gitleaks]=$gitleaks_findings
        fail "gitleaks"
    else
        echo "$gitleaks_output" | head -10
        skipped gitleaks
        warn "gitleaks (did not complete, exit $gitleaks_rc)"
    fi
else
    skipped gitleaks
    warn "gitleaks (not installed, skipping)"
fi

echo ""
# Threshold summary for gitleaks
# shellcheck disable=SC2043
for check in gitleaks; do
    failures=${check_failures[$check]}
    threshold_var="${check}_threshold"
    threshold=${!threshold_var}
    if [ "$failures" -gt 0 ]; then
        if [ "$failures" -gt "$threshold" ]; then
            echo "  ⚠ $check: $failures findings (threshold: $threshold)"
        else
            echo "  ✓ $check: $failures findings (threshold: $threshold)"
        fi
    else
        echo "  ✓ $check: 0 findings (threshold: $threshold)"
    fi
done

# ---------------------------------------------------------------------------
# Threshold-gated verdict — computed once, unconditionally, from every
# check's failures vs. its effective threshold. This is the single source
# both the JSON emitter and the text-mode summary line read from; fail()
# above no longer accumulates EXIT_CODE as a side effect of printing an
# individual finding, so there is nothing left to disagree with this.
# ---------------------------------------------------------------------------
declare -A check_threshold_exceeded
EXIT_CODE=0
for check in "${ALL_CHECKS[@]}"; do
    failures=${check_failures[$check]}
    threshold_var="${check}_threshold"
    threshold=${!threshold_var}
    if [ "$failures" -gt "$threshold" ]; then
        check_threshold_exceeded[$check]="true"
        EXIT_CODE=1
    else
        check_threshold_exceeded[$check]="false"
    fi
done

# ---------------------------------------------------------------------------
# Summary and JSON output
# ---------------------------------------------------------------------------
echo ""

if [ "$OUTPUT_FORMAT" = "json" ]; then
    # Restore stdout for JSON output
    exec 1>&3
    # Export data for JSON generation.
    #
    # Driven by ALL_CHECKS rather than a hand-maintained list. The old literal
    # enumerated 11 names and omitted mypy, bandit, yaml_prettier, json_prettier
    # and toml_sort, so five checks could fail and never appear in the JSON that
    # the plugin consumes.
    #
    # Record layout is failures:threshold:exceeded:status:total. status and total
    # are appended after the original three fields, so the positional indices
    # anything already parsing this format relies on do not move.
    for check in "${ALL_CHECKS[@]}"; do
        failures=${check_failures[$check]}
        total=${check_totals[$check]}
        status=${check_status[$check]}
        threshold_var="${check}_threshold"
        threshold=${!threshold_var}
        threshold_exceeded="${check_threshold_exceeded[$check]}"
        # A check with failures reports "failed" regardless of threshold: the
        # threshold governs whether the run fails, not whether the check found
        # anything. "skipped" wins, since a check that never ran cannot fail.
        if [ "$status" != "skipped" ] && [ "$failures" -gt 0 ]; then
            status="failed"
        fi
        export "ML_CHECK_${check}=${failures}:${threshold}:${threshold_exceeded}:${status}:${total}"
    done
    # Build file list as comma-separated
    ML_FILES=""
    FIRST=1
    for f in "${FILES_CHECKED[@]}"; do
        [ "$FIRST" -eq 0 ] && ML_FILES="${ML_FILES},"
        ML_FILES="${ML_FILES}${f}"
        FIRST=0
    done
    export ML_FILES
    # Per-check message directory and the effective policy flags of the
    # formatters, which the emitter needs to spell out exact auto-fix commands.
    # printf %q keeps each flag paste-safe in the hint.
    export ML_MSG_DIR
    for check in black shfmt yaml_prettier json_prettier toml_sort; do
        declare -n ml_args_ref="${check}_args"
        if [ "${#ml_args_ref[@]}" -gt 0 ]; then
            export "ML_ARGS_${check}=$(printf '%q ' "${ml_args_ref[@]}")"
        fi
        unset -n ml_args_ref
    done
    # Emission order for the "checks" object. Reading os.environ instead would
    # make key order depend on the environment, so it varied run to run.
    ML_CHECK_ORDER="$(IFS=,; echo "${ALL_CHECKS[*]}")"
    export ML_CHECK_ORDER
    # Derived, not hardcoded. This was literally 11 while check_failures held 16
    # entries, so summary.checks_run disagreed with the checks it summarised.
    export ML_CHECKS_COUNT=${#ALL_CHECKS[@]}
    export ML_TOTAL_FILES=${#FILES_CHECKED[@]}
    export ML_EXIT_CODE=$EXIT_CODE
    # Newline-joined configuration warnings, safe to split on "\n": the
    # loader rejects any string containing a control character, so no
    # warning message can contain an embedded newline of its own.
    ML_WARNINGS="$(printf '%s\n' "${ml_warnings[@]+"${ml_warnings[@]}"}")"
    export ML_WARNINGS
    # shellcheck disable=SC2155
    _ml_json="$(
        python3 <<'ML_PYTHON'
import glob, json, os, re, shlex

# Record layout exported by the shell above:
#   failures : threshold : threshold_exceeded : status : total
FAILURES, THRESHOLD, EXCEEDED, STATUS, TOTAL = range(5)

checks = {}
for name in os.environ.get("ML_CHECK_ORDER", "").split(","):
    name = name.strip()
    raw = os.environ.get("ML_CHECK_" + name) if name else None
    if raw:
        parts = raw.split(":")
        failed = int(parts[FAILURES])
        total = int(parts[TOTAL])
        checks[name] = {
            # "passed" previously held int(parts[0]) — the failure count, the
            # same value as "failed". It now means what its name says: units
            # that went through this check and came back clean. max() guards
            # against gitleaks, whose counter holds findings rather than files
            # and can therefore exceed its own total of 1.
            "passed": max(total - failed, 0),
            "failed": failed,
            "total": total,
            "threshold": int(parts[THRESHOLD]),
            "threshold_exceeded": parts[EXCEEDED] == "true",
            # "ok" | "failed" | "skipped". Without this a missing tool and a
            # clean pass are both failures=0, so a check that never ran is
            # indistinguishable from one that ran and found nothing.
            "status": parts[STATUS],
        }


# Findings: each tool's native line format parsed into {file, line, rule,
# message}. A line that does not parse is kept with line/rule null, never
# dropped, except the decorative lines listed in SKIP_LINES. "symbol" is an
# additive extra for tools that name a rule twice (pylint, markdownlint).
MAX_FINDINGS = 50
FORMAT_MESSAGE = "formatting required"
PARSERS = {
    "bash_syntax": r"^(?P<file>.+?): line (?P<line>\d+): (?P<message>.*)$",
    "shellcheck": r"^(?P<file>.+?):(?P<line>\d+):\d+: \w+: (?P<message>.*) \[(?P<rule>SC\d+)\]$",
    "bashate": r"^(?P<file>.+?):(?P<line>\d+):\d+: (?P<rule>E\d+) (?P<message>.*)$",
    "flake8": r"^(?P<file>.+?):(?P<line>\d+):\d+: (?P<rule>[A-Z]+\d+) (?P<message>.*)$",
    "pylint": r"^(?P<file>.+?):(?P<line>\d+):\d+: (?P<rule>[A-Z]\d+): (?P<message>.*) \((?P<symbol>[\w-]+)\)$",
    "mypy": r"^(?P<file>.+?):(?P<line>\d+):(?:\d+:)? (?:error|note|warning): (?P<message>.*?)(?:  \[(?P<rule>[\w-]+)\])?$",
    "bandit": r"^(?P<file>.+?):(?P<line>\d+): \[(?P<rule>B\d+)\] (?P<message>.*)$",
    "markdownlint": r"^(?P<file>.+?):(?P<line>\d+)(?::\d+)? (?:error|warning) (?P<rule>MD\d+)/(?P<symbol>\S+) (?P<message>.*)$",
    # grep -n on a single file: "<line>:<matched text>". The matched text is
    # not echoed for secrets, so a finding never carries the secret itself.
    "security_secrets": r"^(?P<line>\d+):.*$",
    "security_dangerous_patterns": r"^(?P<line>\d+):(?P<message>.*)$",
}
FIXED = {
    "security_secrets": {"rule": "hardcoded-secret", "message": "possible hardcoded secret"},
    "security_dangerous_patterns": {"rule": "dangerous-pattern"},
}
SKIP_LINES = {
    "pylint": r"^(\*+ Module .*|-{5,}|Your code has been rated .*)$",
    "bashate": r"^\d+ bashate error\(s\) found$",
}
# Formatters: one finding per file, no rule. Output lines that mention an error
# (a parse failure, a bad flag) are reported as findings of their own instead.
FORMATTERS = {
    "black": "black {args} {files}",
    "shfmt": "shfmt {args} -w {files}",
    "yaml_prettier": "prettier --write {args} {files}",
    "json_prettier": "prettier --write {args} {files}",
    "toml_sort": "toml-sort {args} -i {files}",
}
NO_ARGS_CHECKS = ("bash_syntax", "security_secrets", "security_dangerous_patterns")
DOC_LINKS = {
    "shellcheck": "https://www.shellcheck.net/wiki/{rule}",
    "markdownlint": "https://github.com/DavidAnson/markdownlint/blob/main/doc/{rule_lower}.md",
}
MAX_DOC_LINKS = 10
MAX_FIX_FILES = 20


def make_finding(file, line, rule, message, symbol=None):
    item = {"file": file, "line": line, "rule": rule, "message": message}
    if symbol:
        item["symbol"] = symbol
    return item


def parse_gitleaks(text):
    """gitleaks --verbose prints one "Key: value" block per leak; the secret itself is never copied."""
    found, current = [], None
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key == "Finding":
            current = {}
            found.append(current)
        elif current is not None and key in ("RuleID", "File", "Line", "Description"):
            current[key] = value
    return [
        make_finding(
            item.get("File", ""),
            int(item["Line"]) if item.get("Line", "").isdigit() else None,
            item.get("RuleID"),
            item.get("Description") or "secret detected",
        )
        for item in found
    ]


def parse_output(check, file, text):
    if check == "gitleaks":
        parsed = parse_gitleaks(text)
        if parsed:
            return parsed
    if check in FORMATTERS:
        errors = [
            re.sub(r"^(\[error\]|error:)\s*", "", line.strip(), flags=re.I)
            for line in text.splitlines()
            if re.search(r"\berror\b", line, re.I)
        ]
        return [make_finding(file, None, None, e) for e in errors] or [
            make_finding(file, None, None, FORMAT_MESSAGE)
        ]
    pattern = re.compile(PARSERS[check]) if check in PARSERS else None
    skip = re.compile(SKIP_LINES[check]) if check in SKIP_LINES else None
    results = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or (skip and skip.match(line.strip())):
            continue
        match = pattern.match(line) if pattern else None
        if match is None:
            results.append(make_finding(file, None, None, line.strip()))
            continue
        fields = match.groupdict()
        fields.update(FIXED.get(check, {}))
        results.append(
            make_finding(
                fields.get("file") or file,
                int(fields["line"]),
                fields.get("rule"),
                fields.get("message") or "",
                fields.get("symbol"),
            )
        )
    return results


def collect_findings(check):
    msg_dir = os.environ.get("ML_MSG_DIR", "")
    results = []
    for out_path in sorted(glob.glob(os.path.join(glob.escape(msg_dir), "*.%s.out" % check))) if msg_dir else []:
        try:
            with open(out_path[:-4] + ".file", encoding="utf-8", errors="replace") as handle:
                file = handle.read()
            with open(out_path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        results.extend(parse_output(check, file, text))
    # Parsed findings first, so tool noise never crowds them out of the cap.
    results.sort(key=lambda item: item["rule"] is None and item["line"] is None)
    return results


def fix_hint(check, findings):
    if check in FORMATTERS:
        files = []
        for item in findings:
            if item["message"] == FORMAT_MESSAGE and item["file"] not in files:
                files.append(item["file"])
        if files:
            command = FORMATTERS[check].format(
                args=os.environ.get("ML_ARGS_" + check, "").strip(),
                files=" ".join(shlex.quote(f) for f in files[:MAX_FIX_FILES]),
            )
            return "formatting only; run: " + " ".join(command.split())
    if check == "gitleaks":
        hint = "remove the secret from the code and rotate it (policy: checks.gitleaks.args)"
    elif check in NO_ARGS_CHECKS:
        hint = "fix the code"
    else:
        hint = "fix the code (policy: checks.%s.args)" % check
    rules = sorted({item["rule"] for item in findings if item["rule"]})
    if check in DOC_LINKS and rules:
        links = [DOC_LINKS[check].format(rule=r, rule_lower=r.lower()) for r in rules[:MAX_DOC_LINKS]]
        hint += "; docs: " + " ".join(links)
    return hint


rules_violated = set()
for name, entry in checks.items():
    if entry["status"] != "failed":
        continue
    findings = collect_findings(name)
    rules_violated.update(item["rule"] for item in findings if item["rule"])
    if len(findings) > MAX_FINDINGS:
        entry["findings"] = findings[:MAX_FINDINGS]
        entry["findings_truncated"] = True
    else:
        entry["findings"] = findings
    entry["fix"] = fix_hint(name, findings)
files = [f.strip() for f in os.environ.get("ML_FILES", "").split(",") if f.strip()]
skipped = sorted(n for n, c in checks.items() if c["status"] == "skipped")
warnings = [w for w in os.environ.get("ML_WARNINGS", "").split("\n") if w]
result = {
    "summary": {
        "files_checked": len(files),
        "checks_run": int(os.environ.get("ML_CHECKS_COUNT", 8)),
        # Promoted into the summary so a consumer can react to skipped checks
        # without walking every entry in "checks".
        "checks_skipped": skipped,
        # Sorted unique rule IDs across every failed check's findings (before
        # the per-check cap, so a truncated list never hides a rule). Additive.
        "rules_violated": sorted(rules_violated),
    },
    "checks": checks,
    "return_code": int(os.environ.get("ML_EXIT_CODE", "0")),
    "files": files,
    # Configuration problems (malformed JSON, unknown keys, wrongly typed
    # values, …) — additive; every existing field keeps its name, type and
    # position. Always present, [] when there is nothing to report.
    "warnings": warnings,
}
print(json.dumps(result, indent=2))
ML_PYTHON
    )"
    printf '%s\n' "$_ml_json"
else
    echo "================================"
    if [ "$EXIT_CODE" -eq 0 ]; then
        echo "  All checks passed ✓"
    else
        echo "  Some checks failed ✗"
    fi
    echo "================================"
fi

exit "$EXIT_CODE"
