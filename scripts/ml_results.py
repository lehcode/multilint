"""Read lint.sh JSON results: failing and skipped checks, findings, violated rules, config warnings.

Imported by lint_changed.py, which is the hook entry point.
"""

from __future__ import annotations


# Nothing is filtered out of the skipped set any more. gitleaks used to be listed here because
# lint.sh gated it on "$TARGET_DIR/.git" and a single-file target can never satisfy that, so it was
# reported skipped after every edit. lint.sh now selects `--no-git` when there is no repository, so
# the check produces a real verdict either way and a skip means something again.
STRUCTURALLY_SKIPPED: frozenset[str] = frozenset()


# The message lint.sh gives a formatter finding; the check's "fix" line already says it, so it is not repeated.
FORMAT_ONLY_MESSAGE = "formatting required"


def is_failing(check) -> bool:
    """A check entry that failed or counted failures; anything that is not an entry is not failing."""
    return isinstance(check, dict) and (check.get("status") == "failed" or (check.get("failed") or 0) > 0)


def failing_checks(document: dict) -> list[str]:
    """Names of checks that found something, as "name: N failure(s)"."""
    reported = []
    for name, check in sorted((document.get("checks") or {}).items()):
        if is_failing(check):
            count = check.get("failed") or 0
            reported.append(f"{name}: {count} failure(s)")
    return reported


def skipped_checks(document: dict) -> list[str]:
    """Checks that did not run, excluding the ones that structurally never can.

    A skipped check is not a pass, and before this the two were indistinguishable: a missing tool
    reported zero failures exactly like a clean run. Surfacing it matters even when return_code is
    0, which is why the caller cannot simply short-circuit on a zero exit.
    """
    names = document.get("summary", {}).get("checks_skipped")
    if not isinstance(names, list):
        return []
    return [n for n in names if isinstance(n, str) and n not in STRUCTURALLY_SKIPPED]


def skip_reasons(results: list) -> dict[str, str]:
    """Why each skipped check did not run, from lint.sh's per-check "reason" (absent in older lint.sh)."""
    reasons: dict[str, str] = {}
    for _name, document, _detail in results:
        checks = document.get("checks")
        if not isinstance(checks, dict):
            continue
        for check, entry in checks.items():
            if isinstance(entry, dict) and entry.get("status") == "skipped" and isinstance(entry.get("reason"), str):
                reasons.setdefault(check, entry["reason"])
    return reasons


def failing_lines(stdout: str) -> list[str]:
    """Only the lines that report a problem, so the context stays small."""
    return [line.rstrip() for line in stdout.splitlines() if "✗" in line or "⚠" in line]


def finding_location(item: dict) -> str:
    """The "file:line" of a finding, or just "file" when the tool gave no line."""
    file = item.get("file") or ""
    return f"{file}:{item['line']}" if item.get("line") is not None and file else file


def finding_line(item: dict) -> str | None:
    """One notice line for a finding; None for a formatter's bare "formatting required"."""
    if not isinstance(item, dict) or item.get("message") == FORMAT_ONLY_MESSAGE:
        return None
    parts = [item.get("rule"), item.get("symbol"), finding_location(item)]
    return "  " + " ".join(str(part) for part in parts if part) + f"  {item.get('message') or ''}".rstrip()


def check_block(name: str, check: dict) -> str:
    """The notice block of one failed check: its fix line, then one line per finding."""
    fix = check.get("fix")
    lines = [f"{name} — {fix}" if isinstance(fix, str) and fix else name]
    lines.extend(line for line in map(finding_line, check["findings"]) if line)
    if check.get("findings_truncated"):
        lines.append("  … more findings omitted")
    return "\n".join(lines)


def finding_blocks(document: dict) -> tuple[list[str], list[str]]:
    """(failed check names, notice blocks) built from the "findings"/"fix" fields.

    Both are empty for a document from an older lint.sh, whose checks carry counts only, so the caller can
    fall back to the marker lines.
    """
    names: list[str] = []
    blocks: list[str] = []
    for name, check in (document.get("checks") or {}).items():
        if isinstance(check, dict) and check.get("status") == "failed" and isinstance(check.get("findings"), list):
            names.append(name)
            blocks.append(check_block(name, check))
    return names, blocks


def failed_names(results: list) -> list[str]:
    """Failed check names across every result, in lint.sh's check order, first appearance wins."""
    return list(dict.fromkeys(n for _file, document, _detail in results for n in finding_blocks(document)[0]))


def violated_rules(results: list) -> list[str]:
    """Sorted unique rule IDs lint.sh reported across every result."""
    rules: set[str] = set()
    for _name, document, _detail in results:
        listed = (document.get("summary") or {}).get("rules_violated")
        if isinstance(listed, list):
            rules.update(r for r in listed if isinstance(r, str))
    return sorted(rules)


def config_warnings(document: dict) -> list[str]:
    """The lint.sh "warnings" field, or [] when the field is missing or malformed.

    Independent of return_code: a malformed .multilint.json is a configuration mistake the user
    needs to hear about even on a run that otherwise passed every check.
    """
    warnings = document.get("warnings")
    if not isinstance(warnings, list):
        return []
    return [w for w in warnings if isinstance(w, str)]
