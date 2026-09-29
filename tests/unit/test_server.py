"""Unit tests for server.py HTTP API."""

# pylint: disable=redefined-outer-name
import os
import socket

import pytest

SERVER_PY = str(__import__("pathlib").Path(__file__).parent.parent / "server.py")


class TestLintHandler:
    """Tests for the LintHandler HTTP request handler."""

    def test_get_health(self, http_client):
        """GET /health returns 200 with status ok."""
        resp = http_client.get("/health")
        assert resp["status"] == 200
        assert resp["body"] == {"status": "ok"}

    def test_get_root(self, http_client):
        """GET / returns 200 with text/plain help text."""
        resp = http_client.get("/")
        assert resp["status"] == 200
        assert resp["headers"].get("Content-Type") == "text/plain"
        assert "MultiLint Service" in str(resp["body"])
        assert "/lint" in str(resp["body"])

    def test_get_unknown(self, http_client):
        """GET /unknown returns 404."""
        resp = http_client.get("/foo")
        assert resp["status"] == 404
        assert resp["body"] == {"error": "not found"}

    def test_post_lint_valid(self, http_client, sample_project):
        """POST /lint with valid body returns 200 with result dict."""
        resp = http_client.post("/lint", {"path": "./shell/", "cwd": sample_project})
        assert resp["status"] == 200
        body = resp["body"]
        assert body["target"] == "./shell/"
        assert body["cwd"] == sample_project

    def test_post_lint_bad_json(self, http_client):
        """POST /lint with invalid JSON returns 400."""
        resp = http_client._request("POST", "/lint", body=b"not valid json\n")  # pylint: disable=protected-access
        assert resp["status"] == 400
        assert "bad request" in resp["body"]["error"]

    def test_post_lint_default_path(self, http_client, tmp_dir):
        """POST /lint with no path defaults to '.', resolved to the server's cwd."""
        resp = http_client.post("/lint", {})
        assert resp["status"] == 200
        assert resp["body"]["cwd"] == os.path.realpath(tmp_dir)

    def test_post_lint_invalid_cwd(self, http_client, tmp_dir):
        """POST /lint with nonexistent directory (inside the allowed root) returns 400."""
        missing = os.path.join(tmp_dir, "nonexistent")
        resp = http_client.post("/lint", {"path": ".", "cwd": missing})
        assert resp["status"] == 400
        assert "working directory not found" in resp["body"]["error"]

    def test_post_lint_path_traversal_rejected(self, http_client, tmp_dir):
        """POST /lint with a cwd that traverses out of the allowed root returns 400."""
        traversal = os.path.join(tmp_dir, "..", "..", "etc")
        resp = http_client.post("/lint", {"path": ".", "cwd": traversal})
        assert resp["status"] == 400
        assert "not permitted" in resp["body"]["error"]

    def test_post_lint_absolute_outside_allowlist_rejected(self, http_client):
        """POST /lint with an absolute cwd outside any allowed root returns 400."""
        resp = http_client.post("/lint", {"path": ".", "cwd": "/etc"})
        assert resp["status"] == 400
        assert "not permitted" in resp["body"]["error"]

    def test_post_lint_prefix_confusion_rejected(self, http_client, tmp_dir):
        """A cwd that merely shares a string prefix with the allowed root is rejected."""
        lookalike = tmp_dir + "-evil"
        resp = http_client.post("/lint", {"path": ".", "cwd": lookalike})
        assert resp["status"] == 400
        assert "not permitted" in resp["body"]["error"]

    def test_post_lint_error_body_does_not_leak_paths(self, http_client, tmp_dir):
        """The rejection response must not echo the resolved path or allowlist contents."""
        resp = http_client.post("/lint", {"path": ".", "cwd": "/etc"})
        assert resp["status"] == 400
        error_text = resp["body"]["error"]
        assert tmp_dir not in error_text
        assert "/etc" not in error_text
        assert "/workspace" not in error_text
        assert "/multilint" not in error_text

    def test_ansi_code_cleans_output(self, http_client):
        """ANSI escape codes are stripped from stdout/stderr."""
        resp = http_client.post("/lint", {"path": "."})
        assert resp["status"] == 200
        # The actual lint output may contain ANSI codes, so verify they are cleaned
        assert "\x1b" not in resp["body"]["stdout"]
        assert "\x1b" not in resp["body"]["stderr"]


class TestMain:
    """Tests for the main() entry point."""

    def test_main_port_in_use(self):
        """main() raises SystemExit(1) when port is already bound."""
        from server import main

        # Bind a port so it's in use; keep socket open during test
        # Do NOT set SO_REUSEADDR — we need the port to be truly exclusive
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

        try:
            with pytest.raises(SystemExit, match="1"):
                main("127.0.0.1", port)
        finally:
            s.close()
