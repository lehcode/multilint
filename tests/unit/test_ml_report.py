"""Unit tests for core/ml_report.py — findings notice, skip reasons, build_notices."""

# pylint: disable=redefined-outer-name,protected-access,unused-argument
import ml_report
import ml_results


class TestFindingsNotice:
    def test_notice_is_built_from_a_findings_document(self, hook):
        document = {
            "return_code": 1,
            "summary": {"rules_violated": ["F401"]},
            "checks": {
                "flake8": {
                    "status": "failed",
                    "fix": "fix the code (policy: checks.flake8.args)",
                    "findings": [{"file": "a.py", "line": 1, "rule": "F401", "message": "'os' imported but unused"}],
                },
            },
        }
        results = [("a.py", document, "")]
        findings, skipped, warnings, failed = ml_report.summarize_results(results)
        names = ml_results.finding_blocks(document)[0]
        (notice,) = ml_report.build_notices(
            "a.py", findings, skipped, warnings, failed, names, ml_results.violated_rules(results)
        )
        assert notice == (
            "multilint: a.py failed flake8\n\n"
            "flake8 — fix the code (policy: checks.flake8.args)\n"
            "  F401 a.py:1  'os' imported but unused\n\n"
            "Rules: F401"
        )

    def test_skipped_checks_carry_their_reason(self, hook):
        document = {
            "return_code": 0,
            "summary": {"checks_skipped": ["gitleaks", "hadolint"]},
            "checks": {
                "gitleaks": {"status": "skipped", "reason": "not installed on PATH"},
                "hadolint": {"status": "skipped"},
            },
        }
        results = [("a.py", document, "")]
        findings, skipped, warnings, failed = ml_report.summarize_results(results)
        reasons = ml_results.skip_reasons(results)
        (notice,) = ml_report.build_notices("a.py", findings, skipped, warnings, failed, reasons=reasons)
        assert notice == (
            "multilint could not run these checks, so a.py is unverified for them:\n"
            "  gitleaks: not installed on PATH\n"
            "  hadolint"
        )
