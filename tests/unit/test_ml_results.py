"""Unit tests for core/ml_results.py — skipped checks, failing checks, failing lines."""

# pylint: disable=redefined-outer-name,protected-access,unused-argument
import ml_results


class TestSkippedChecksAreSurfaced:
    """A skipped check is not a passing check, and must reach the agent even on a zero exit."""

    @staticmethod
    def _document(skipped):
        return {"summary": {"checks_skipped": skipped}, "checks": {}, "return_code": 0}

    def test_reports_skipped_checks(self, hook):
        assert ml_results.skipped_checks(self._document(["markdownlint", "prettier"])) == [
            "markdownlint",
            "prettier",
        ]

    def test_gitleaks_is_no_longer_filtered(self, hook):
        """It used to be suppressed, because lint.sh gated it on <target>/.git and a single-file
        target can never satisfy that, so the skip fired after every edit and carried no
        information. lint.sh now falls back to --no-git, so a reported gitleaks skip means the
        check genuinely did not run and the user needs to hear it."""
        assert ml_results.skipped_checks(self._document(["gitleaks"])) == ["gitleaks"]
        assert ml_results.skipped_checks(self._document(["gitleaks", "mypy"])) == ["gitleaks", "mypy"]

    def test_nothing_is_structurally_suppressed(self, hook):
        assert ml_results.STRUCTURALLY_SKIPPED == frozenset()

    def test_tolerates_a_missing_summary(self, hook):
        assert ml_results.skipped_checks({"checks": {}}) == []

    def test_tolerates_a_malformed_summary(self, hook):
        assert ml_results.skipped_checks({"summary": {"checks_skipped": "mypy"}}) == []


class TestFailingChecks:
    def test_lists_failed_checks_with_counts(self, hook):
        document = {
            "checks": {
                "flake8": {"status": "failed", "failed": 2},
                "black": {"status": "ok", "failed": 0},
                "mypy": {"status": "skipped", "failed": 0},
            }
        }
        assert ml_results.failing_checks(document) == ["flake8: 2 failure(s)"]

    def test_counts_failures_even_without_a_status(self, hook):
        """Tolerates an older document shape rather than reporting a failure as clean."""
        assert ml_results.failing_checks({"checks": {"pylint": {"failed": 1}}}) == ["pylint: 1 failure(s)"]

    def test_empty_document_reports_nothing(self, hook):
        assert ml_results.failing_checks({}) == []


class TestFailingLines:
    def test_keeps_only_marked_lines(self, hook):
        output = "  ✓ flake8\n  ✗ black\n  ⚠ pylint: 3 failures\n  📄 file.sh\n"
        assert ml_results.failing_lines(output) == ["  ✗ black", "  ⚠ pylint: 3 failures"]
