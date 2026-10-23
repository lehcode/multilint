# shellcheck shell=bash
# ml_config.sh - sourced by lint.sh at top level, never run on its own.
# Configuration: built-in defaults, then .multilint.json via ml_config_schema.py.
# Sets the <check>_threshold/_enabled/_args variables, the ML_CFG_* options and ml_warnings.
# Reads and writes lint.sh globals (TARGET_DIR, OUTPUT_FORMAT, ALL_CHECKS, check_failures,
# check_totals, check_status, check_reason, ...), so it is not wrapped in a function:
# a declare -A inside a function would be local.
# shellcheck disable=SC2034,SC2154

# ---------------------------------------------------------------------------
# Configuration: built-in defaults -> .multilint.json -> the linter's own config
#
# Three ordered layers, each owning exactly one precedence level. No
# environment variable participates in this resolution (user decision,
# 2026-09-30: environment variables are not a multilint configuration
# surface):
#   1. Built-in defaults (today's behavior: zero tolerance, every check on).
#   2. .multilint.json, read once by a single python3 parse (see
#      ml_find_config below for lookup order) whose output is filtered
#      against a fixed vocabulary before eval, so nothing beyond the
#      variables this script itself defines can ever be assigned.
#   3. A linter's own config in the scope root (user decision, 2026-10-23:
#      existing project configuration always wins). The check keeps only its
#      harness flags; both layers above lose their policy flags for it.
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
ML_CFG_MARKDOWNLINT_RULES=""

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
# The built-in markdownlint rule set (MD013 off). A project's own markdownlint config is
# found by markdownlint itself in $PWD, and layer 3 drops this flag when one exists.
markdownlint_args=(-c "$ML_DIR/ml_markdownlint.json")
yaml_prettier_args=()
json_prettier_args=()
toml_sort_args=(--sort-table-keys)
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
        || "$line" == ML_CFG_BANDIT_SEVERITY=* || "$line" == ML_CFG_MYPY_CACHE_DIR=* \
        || "$line" == ML_CFG_MARKDOWNLINT_RULES=* ]]; then
        return 0
    fi
    if [[ "$line" == "ml_warnings+=("*")" ]]; then
        return 0
    fi
    return 1
}

if [ -n "$ml_config_file" ]; then
    if command -v python3 >/dev/null 2>&1; then
        if ml_config_out="$(python3 "$ML_DIR/ml_config_schema.py" "$ml_config_file")"; then
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
                    ml_json_applied="$ml_config_out"
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

# 3. The linter's own config. Looked up in $PWD only, which both plugins set to the scope root,
# and where each tool finds its config by itself once multilint passes no policy flags.
# ml_first_file <name>...: print the first name that exists in $PWD.
ml_first_file() {
    local name
    for name in "$@"; do
        if [ -f "$PWD/$name" ]; then
            echo "$name"
            return 0
        fi
    done
    return 1
}
# ml_first_section <line-pattern> <name>...: print the first file in $PWD with a matching line.
ml_first_section() {
    local pattern="$1" name
    shift
    for name in "$@"; do
        if [ -f "$PWD/$name" ] && grep -q -e "^$pattern" "$PWD/$name"; then
            echo "$name"
            return 0
        fi
    done
    return 1
}
# ml_package_json_key <key>: print package.json when $PWD/package.json has <key> at its top level.
# Parsed rather than grepped: a "prettier" dependency entry is nested, and is not a config. -I keeps
# python3 from importing modules out of $PWD, which is the project being linted.
ml_package_json_key() {
    [ -f "$PWD/package.json" ] && command -v python3 >/dev/null 2>&1 || return 1
    python3 -I -c 'import json, sys
try:
    doc = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    sys.exit(1)
sys.exit(0 if isinstance(doc, dict) and sys.argv[2] in doc else 1)' "$PWD/package.json" "$1" || return 1
    echo package.json
}
# ml_native_config <check>: print the file holding the check's own config; fail when there is none.
# bashate and bandit are absent: the way lint.sh runs them, neither reads a project config file.
ml_native_config() {
    case "$1" in
    shellcheck) ml_first_file .shellcheckrc ;;
    shfmt) ml_first_file .editorconfig ;;
    flake8) ml_first_file .flake8 || ml_first_section '\[flake8\]' setup.cfg tox.ini ;;
    black) ml_first_section '\[tool\.black\]' pyproject.toml ;;
    pylint)
        ml_first_file .pylintrc pylintrc || ml_first_section '\[tool\.pylint' pyproject.toml ||
            ml_first_section '\[pylint' setup.cfg tox.ini
        ;;
    mypy)
        ml_first_file mypy.ini .mypy.ini || ml_first_section '\[tool\.mypy\]' pyproject.toml ||
            ml_first_section '\[mypy\]' setup.cfg
        ;;
    markdownlint)
        ml_first_file .markdownlint.json .markdownlint.jsonc .markdownlint.yaml .markdownlint.yml .markdownlintrc
        ;;
    yaml_prettier | json_prettier)
        ml_first_file .prettierrc .prettierrc.json .prettierrc.json5 .prettierrc.yaml .prettierrc.yml \
            .prettierrc.toml .prettierrc.js .prettierrc.cjs .prettierrc.mjs \
            prettier.config.js prettier.config.cjs prettier.config.mjs ||
            ml_package_json_key prettier
        ;;
    toml_sort) ml_first_section '\[tool\.tomlsort\]' pyproject.toml ;;
    *) return 1 ;;
    esac
}
ml_json_applied="${ml_json_applied:-}"
for _ml_check in "${ALL_CHECKS[@]}"; do
    _ml_native="$(ml_native_config "$_ml_check")" || continue
    # Only args the project set are worth a warning; dropping multilint's own defaults is silent.
    if grep -q "^${_ml_check}_args=" <<<"$ml_json_applied"; then
        ml_warnings+=("checks.${_ml_check}.args ignored: $_ml_native takes precedence")
    fi
    # Names come from ALL_CHECKS, never from input.
    eval "${_ml_check}_args=()"
done
unset _ml_check _ml_native

# markdownlint.rules, already merged over ml_markdownlint.json by the loader. It applies only
# when neither the project's own markdownlint config nor checks.markdownlint.args is set;
# ml_check_docs.sh writes it into lint.sh's scratch directory, which is removed on exit.
ml_markdownlint_rules=""
if [ -n "$ML_CFG_MARKDOWNLINT_RULES" ]; then
    if _ml_native="$(ml_native_config markdownlint)"; then
        ml_warnings+=("markdownlint.rules ignored: $_ml_native takes precedence")
    elif grep -q '^markdownlint_args=' <<<"$ml_json_applied"; then
        ml_warnings+=("markdownlint.rules ignored: checks.markdownlint.args takes precedence")
    else
        ml_markdownlint_rules="$ML_CFG_MARKDOWNLINT_RULES"
    fi
    unset _ml_native
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
# The project's own .gitleaks.toml (layer 3) wins over gitleaks.config and over the image's copy.
# It is passed explicitly with --config rather than left for gitleaks to find, because gitleaks
# looks beside the scanned source, which is not always the scope root.
GITLEAKS_NATIVE_CONFIG=""
if [ -f "$PWD/.gitleaks.toml" ]; then
    GITLEAKS_NATIVE_CONFIG="$PWD/.gitleaks.toml"
    if [ -n "$GITLEAKS_CONFIG_OVERRIDE" ]; then
        ml_warnings+=("gitleaks.config ignored: .gitleaks.toml takes precedence")
    fi
fi

# 3. Warnings -- printed once, regardless of output format, since this
# explicit stderr redirect (>&2) bypasses the JSON-mode stdout swap above.
for ml_warning in "${ml_warnings[@]+"${ml_warnings[@]}"}"; do
    printf 'multilint: config warning: %s\n' "$ml_warning" >&2
done
