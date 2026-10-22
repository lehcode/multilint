"""Render lint.sh's JSON report from the ML_* environment the shell exports before running this.

lint.sh runs this as `python3 ml_report_json.py` in --format json mode and prints its stdout.
Inputs: ML_CHECK_ORDER, ML_CHECK_<name>, ML_REASON_<name>, ML_ARGS_<name>, ML_MSG_DIR, ML_FILES,
ML_WARNINGS, ML_CHECKS_COUNT, ML_EXIT_CODE. Stdlib-only.
"""

import glob
import json
import os
import re
import shlex

# Record layout exported by the shell above:
#   failures : threshold : threshold_exceeded : status : total
FAILURES, THRESHOLD, EXCEEDED, STATUS, TOTAL = range(5)

# Findings: each tool's native line format parsed into {file, line, rule,
# message}. A line that does not parse is kept with line/rule null, never
# dropped, except the decorative lines listed in SKIP_LINES. "symbol" is an
# additive extra for tools that name a rule twice (pylint, markdownlint).
MAX_FINDINGS = 50
ANSI = re.compile(r"\x1b\[[0-9;]*m")
FORMAT_MESSAGE = "formatting required"
PARSERS = {
    "bash_syntax": r"^(?P<file>.+?): line (?P<line>\d+): (?P<message>.*)$",
    "shellcheck": r"^(?P<file>.+?):(?P<line>\d+):\d+: \w+: (?P<message>.*) \[(?P<rule>SC\d+)\]$",
    "bashate": r"^(?P<file>.+?):(?P<line>\d+):\d+: (?P<rule>E\d+) (?P<message>.*)$",
    "flake8": r"^(?P<file>.+?):(?P<line>\d+):\d+: (?P<rule>[A-Z]+\d+) (?P<message>.*)$",
    "pylint": r"^(?P<file>.+?):(?P<line>\d+):\d+: (?P<rule>[A-Z]\d+): (?P<message>.*) \((?P<symbol>[\w-]+)\)$",
    "mypy": (
        r"^(?P<file>.+?):(?P<line>\d+):(?:\d+:)? (?:error|note|warning): "
        r"(?P<message>.*?)(?:  \[(?P<rule>[\w-]+)\])?$"
    ),
    "bandit": r"^(?P<file>.+?):(?P<line>\d+): \[(?P<rule>B\d+)\] (?P<message>.*)$",
    "markdownlint": (
        r"^(?P<file>.+?):(?P<line>\d+)(?::\d+)? (?:error|warning) (?P<rule>MD\d+)/(?P<symbol>\S+) (?P<message>.*)$"
    ),
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
    # Raw-output fallback only: never echo the secret value or the matched line into a finding.
    "gitleaks": r"^(Secret|Match|Finding):",
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


def check_entry(name, raw):
    parts = raw.split(":")
    failed = int(parts[FAILURES])
    total = int(parts[TOTAL])
    entry = {
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
    # Only skipped checks carry one: why the check did not run.
    reason = os.environ.get("ML_REASON_" + name, "")
    if parts[STATUS] == "skipped" and reason:
        entry["reason"] = reason
    return entry


def read_checks():
    checks = {}
    for name in os.environ.get("ML_CHECK_ORDER", "").split(","):
        name = name.strip()
        raw = os.environ.get("ML_CHECK_" + name) if name else None
        if raw:
            checks[name] = check_entry(name, raw)
    return checks


def make_finding(file, line, rule, message, symbol=None):
    item = {"file": file, "line": line, "rule": rule, "message": message}
    if symbol:
        item["symbol"] = symbol
    return item


def parse_gitleaks(text):
    """gitleaks --verbose prints one "Key: value" block per leak; the secret itself is never copied."""
    found, current = [], None
    for line in ANSI.sub("", text).splitlines():
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


def formatter_findings(file, text):
    errors = [
        re.sub(r"^(\[error\]|error:)\s*", "", line.strip(), flags=re.I)
        for line in text.splitlines()
        if re.search(r"\berror\b", line, re.I)
    ]
    return [make_finding(file, None, None, e) for e in errors] or [make_finding(file, None, None, FORMAT_MESSAGE)]


def line_finding(check, file, line, pattern):
    match = pattern.match(line) if pattern else None
    if match is None:
        return make_finding(file, None, None, line.strip())
    fields = match.groupdict()
    fields.update(FIXED.get(check, {}))
    return make_finding(
        fields.get("file") or file,
        int(fields["line"]),
        fields.get("rule"),
        fields.get("message") or "",
        fields.get("symbol"),
    )


def parse_lines(check, file, text):
    pattern = re.compile(PARSERS[check]) if check in PARSERS else None
    skip = re.compile(SKIP_LINES[check]) if check in SKIP_LINES else None
    results = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or (skip and skip.match(line.strip())):
            continue
        results.append(line_finding(check, file, line, pattern))
    return results


def parse_output(check, file, text):
    if check == "gitleaks":
        parsed = parse_gitleaks(text)
        if parsed:
            return parsed
    if check in FORMATTERS:
        return formatter_findings(file, text)
    return parse_lines(check, file, text)


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


def formatter_fix(check, findings):
    """The formatter command for the files that only need formatting, or None when there are none."""
    files = []
    for item in findings:
        if item["message"] == FORMAT_MESSAGE and item["file"] not in files:
            files.append(item["file"])
    if not files:
        return None
    command = FORMATTERS[check].format(
        args=os.environ.get("ML_ARGS_" + check, "").strip(),
        files=" ".join(shlex.quote(f) for f in files[:MAX_FIX_FILES]),
    )
    return "formatting only; run: " + " ".join(command.split())


def base_hint(check):
    if check == "gitleaks":
        return "remove the secret from the code and rotate it (policy: checks.gitleaks.args)"
    if check in NO_ARGS_CHECKS:
        return "fix the code"
    return "fix the code (policy: checks.%s.args)" % check


def fix_hint(check, findings):
    if check in FORMATTERS:
        command = formatter_fix(check, findings)
        if command:
            return command
    hint = base_hint(check)
    rules = sorted({item["rule"] for item in findings if item["rule"]})
    if check in DOC_LINKS and rules:
        links = [DOC_LINKS[check].format(rule=r, rule_lower=r.lower()) for r in rules[:MAX_DOC_LINKS]]
        hint += "; docs: " + " ".join(links)
    return hint


def attach_findings(checks):
    """Add findings and a fix hint to every failed check; return the rule IDs seen before the cap."""
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
    return rules_violated


def build_result(checks, rules_violated):
    files = [f.strip() for f in os.environ.get("ML_FILES", "").split(",") if f.strip()]
    skipped = sorted(n for n, c in checks.items() if c["status"] == "skipped")
    warnings = [w for w in os.environ.get("ML_WARNINGS", "").split("\n") if w]
    return {
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


def main():
    checks = read_checks()
    rules_violated = attach_findings(checks)
    print(json.dumps(build_result(checks, rules_violated), indent=2))


if __name__ == "__main__":
    main()
