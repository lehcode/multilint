"""Turn lint results into the notices and the one-line summary the hook reports.

Imported by lint_changed.py, which is the hook entry point.
"""

from __future__ import annotations


import ml_results

# Cap on the text handed back to Claude. The full lint output of a failing directory runs to several
# kilobytes; only the failing lines are worth the context.
MAX_CONTEXT_CHARS = 4000


def summarize_results(results: list) -> tuple[list[str], set[str], list[str], int]:
    """Fold per-file lint results into (findings, skipped checks, config warnings, failed count).

    A document that carries structured findings contributes its rendered check blocks; one from an older
    lint.sh falls back to the marker lines of the human report.
    """
    findings: list[str] = []
    skipped: set[str] = set()
    warnings: list[str] = []
    failed = 0
    for name, document, detail in results:
        skipped.update(ml_results.skipped_checks(document))
        warnings.extend(ml_results.config_warnings(document))
        if document.get("return_code") == 0:
            continue
        failed += 1
        _names, blocks = ml_results.finding_blocks(document)
        if blocks:
            findings.append("\n".join(blocks))
            continue
        lines = ml_results.failing_lines(detail)
        if not lines:
            # Fall back to the structured counts when the human output carried no marked lines.
            lines = ml_results.failing_checks(document)
        if lines:
            findings.append(f"{name}:\n" + "\n".join(lines))
    return findings, skipped, warnings, failed


def structured_notice(relative: str, findings: list[str], names: list[str], rules: list[str]) -> str:
    """Header, findings, Rules footer; only the findings are cut to fit MAX_CONTEXT_CHARS."""
    header = f"multilint: {relative} failed " + ", ".join(names)
    footer = ("Rules: " + " ".join(rules)) if rules else ""
    body = "\n".join(findings)
    budget = MAX_CONTEXT_CHARS - len(header) - len(footer) - 4
    if len(body) > budget:
        body = body[: max(budget - 2, 0)].rsplit("\n", 1)[0] + "\n…"
    return "\n\n".join(part for part in (header, body, footer) if part)


def skipped_notice(relative: str, skipped: set[str], reasons: dict[str, str]) -> str:
    """One line per check that did not run; the reason is what tells the user what to fix."""
    listed = "\n".join(f"  {name}: {reasons[name]}" if reasons.get(name) else f"  {name}" for name in sorted(skipped))
    return f"multilint could not run these checks, so {relative} is unverified for them:\n{listed}"


def failure_notice(relative: str, findings: list[str], names: list[str] | None, rules: list[str] | None) -> str:
    """The failure notice: structured when the failing check names are known, plain findings otherwise."""
    if names:
        return structured_notice(relative, findings, names, rules or [])
    context = "\n\n".join(findings)[:MAX_CONTEXT_CHARS]
    if not context:
        context = "multilint reported failing checks but produced no parsable output."
    return f"multilint found failing checks in {relative}.\n\n{context}"


def build_notices(
    relative: str,
    findings: list[str],
    skipped: set[str],
    warnings: list[str],
    failed: int,
    names: list[str] | None = None,
    rules: list[str] | None = None,
    reasons: dict[str, str] | None = None,
) -> list[str]:
    """The user-facing notices, one per kind of problem; empty when there is nothing to report.

    With failing check names the failure notice is the structured one: a header, the findings, then a
    Rules footer. Only the findings are cut to fit MAX_CONTEXT_CHARS, never the header or the footer.
    """
    notices: list[str] = []
    if failed:
        notices.append(failure_notice(relative, findings, names, rules))
    if skipped:
        # Reported even when everything passed. A check that did not run is not a check that passed,
        # and the old contract could not tell the two apart.
        notices.append(skipped_notice(relative, skipped, reasons or {}))
    if warnings:
        # Independent of return_code: a malformed .multilint.json is worth surfacing even when the
        # run otherwise passed, the same way defect 1 (a silently-ignored threshold) went unnoticed.
        notices.append("multilint: config warning: " + "; ".join(warnings))
    return notices


def report_summary(failed: int, skipped: set[str]) -> str:
    """The one-line count shown before the notices."""
    summary = []
    if failed:
        summary.append(f"{failed} file(s) with failing checks")
    if skipped:
        summary.append(f"{len(skipped)} check(s) skipped")
    return ", ".join(summary) if summary else "configuration notice"
