"""Unit tests for scripts/sync-core.sh drift detection and repair."""

# pylint: disable=relative-beyond-top-level
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync-core.sh"


def _run_sync(*args, cwd=None):
    """Run sync-core.sh with the given args and return the process result."""
    cmd = ["bash", str(SYNC_SCRIPT)] + list(args)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd or REPO_ROOT,
        env={**os.environ, "GIT_DIR": str(REPO_ROOT / ".git")},
    )


class TestSyncCheck:
    """--check mode on the committed repo state should report no drift."""

    def test_no_drift_on_commit(self):
        """sync-core.sh --check exits 0 when core/ and targets are in sync."""
        result = _run_sync("--check")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stderr.strip() == ""


TARGET_PLUGINS = ("claude-plugin", "opencode-plugin")


class TestSyncDriftDetection:
    """Drift detection in an isolated tmp repo."""

    @pytest.fixture
    def tmp_repo(self, tmp_path):
        """A temporary git repo with core/, scripts/sync-core.sh, and both in-sync target dirs.

        The sync script resolves repo root via git rev-parse --show-toplevel; its targets are
        claude-plugin/scripts and opencode-plugin/scripts relative to that root.
        """
        core_dir = tmp_path / "core"
        core_dir.mkdir()

        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()

        targets = [tmp_path / plugin / "scripts" for plugin in TARGET_PLUGINS]
        for target_dir in targets:
            target_dir.mkdir(parents=True)

        # Copy core files into tmp repo
        for f in ("lint.sh", "lint_changed.py", "multilint.sh", "multilint_config.py"):
            (core_dir / f).write_text(f"content-{f}\n", encoding="utf-8")
            for target_dir in targets:
                (target_dir / f).write_text(f"content-{f}\n", encoding="utf-8")

        # Copy sync script into scripts/
        sync_dest = scripts_dir / "sync-core.sh"
        sync_dest.write_text(SYNC_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
        sync_dest.chmod(0o755)

        # Initialize git repo
        subprocess.run(
            ["git", "init", "-q"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )

        return tmp_path

    def test_fixture_starts_in_sync(self, tmp_repo):
        """Guards the tests below: any drift they see is the drift they introduced."""
        result = subprocess.run(
            ["bash", str(tmp_repo / "scripts" / "sync-core.sh"), "--check"],
            capture_output=True,
            text=True,
            cwd=tmp_repo,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

    @pytest.mark.parametrize("plugin", TARGET_PLUGINS)
    def test_modified_plugin_copy_detected(self, tmp_repo, plugin):
        """Modifying a target copy triggers --check exit 1 with the file named."""
        target_dir = tmp_repo / plugin / "scripts"
        (target_dir / "lint.sh").write_text("MODIFIED\n", encoding="utf-8")

        result = subprocess.run(
            ["bash", str(tmp_repo / "scripts" / "sync-core.sh"), "--check"],
            capture_output=True,
            text=True,
            cwd=tmp_repo,
        )
        assert result.returncode == 1
        assert f"{plugin}/scripts/lint.sh" in result.stderr

    def test_extra_file_detected(self, tmp_repo):
        """An extra file in target is reported as drift."""
        target_dir = tmp_repo / "claude-plugin" / "scripts"
        (target_dir / "extra.sh").write_text("# extra\n", encoding="utf-8")

        result = subprocess.run(
            ["bash", str(tmp_repo / "scripts" / "sync-core.sh"), "--check"],
            capture_output=True,
            text=True,
            cwd=tmp_repo,
        )
        assert result.returncode == 1
        assert "extra.sh" in result.stderr

    def test_sync_repairs_drift(self, tmp_repo):
        """Running sync (no --check) repairs drift; --check then exits 0."""
        target_dir = tmp_repo / "claude-plugin" / "scripts"
        # Introduce drift
        (target_dir / "lint.sh").write_text("MODIFIED\n", encoding="utf-8")
        (target_dir / "extra.sh").write_text("# extra\n", encoding="utf-8")

        # Run sync (default mode)
        subprocess.run(
            ["bash", str(tmp_repo / "scripts" / "sync-core.sh")],
            capture_output=True,
            text=True,
            cwd=tmp_repo,
            check=True,
        )

        # Verify repaired
        result = subprocess.run(
            ["bash", str(tmp_repo / "scripts" / "sync-core.sh"), "--check"],
            capture_output=True,
            text=True,
            cwd=tmp_repo,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert not (target_dir / "extra.sh").exists()

    def test_missing_target_detected(self, tmp_repo):
        """A target directory that does not exist is drift: the plugin would ship without its engine."""
        target_dir = tmp_repo / "claude-plugin" / "scripts"
        for child in target_dir.iterdir():
            child.unlink()
        target_dir.rmdir()

        result = subprocess.run(
            ["bash", str(tmp_repo / "scripts" / "sync-core.sh"), "--check"],
            capture_output=True,
            text=True,
            cwd=tmp_repo,
        )
        assert result.returncode == 1
        assert "missing" in result.stderr
