"""Integration tests for the HTTP API (full server lifecycle)."""

# pylint: disable=redefined-outer-name
import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

SERVER_PY = Path(__file__).parent.parent.parent / "server.py"
LINT_SH = Path(__file__).parent.parent / "lint.sh"


def _start_test_server(tmp_dir: str):
    """Start a test HTTP server in a subprocess and return (host, port, proc)."""
    # Copy server.py into tmp dir with fixed lint.sh path
    server_code = SERVER_PY.read_text()
    server_code = server_code.replace(
        "/home/takeshi/docker-compose.d/multilint/lint.sh",
        str(LINT_SH),
    )
    (Path(tmp_dir) / "server.py").write_text(server_code)

    port = 0
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    proc = subprocess.Popen(
        ["python3", str(Path(tmp_dir) / "server.py")],
        cwd=tmp_dir,
        env={
            **os.environ,
            "LINT_SERVER_PORT": str(port),
            "LINT_SERVER_HOST": "127.0.0.1",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for server readiness
    for _ in range(30):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(("127.0.0.1", port))
                break
        except ConnectionRefusedError:
            time.sleep(0.2)
    else:
        proc.terminate()
        raise RuntimeError("Server failed to start")

    return "127.0.0.1", port, proc


class _TestClient:
    """Simple HTTP client for integration tests."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port

    def _request(self, method: str, path: str, body: bytes = b"", timeout: float = 10) -> dict:
        import http.client

        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        headers = {"Content-Type": "application/json"} if body else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode()
        conn.close()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = raw
        return {"status": resp.status, "headers": dict(resp.getheaders()), "body": data}

    def get(self, path: str, timeout: float = 10) -> dict:
        return self._request("GET", path, timeout=timeout)

    def post(self, path: str, data: dict, timeout: float = 10) -> dict:
        body = json.dumps(data).encode()
        return self._request("POST", path, body=body, timeout=timeout)


@pytest.fixture
def test_server(tmp_dir: str):
    """Start a test HTTP server and yield a client."""
    host, port, proc = _start_test_server(tmp_dir)
    client = _TestClient(host, port)
    yield client
    proc.terminate()
    proc.wait(timeout=5)


class TestHTTPApi:
    """Integration tests for the HTTP API."""

    def test_health_check(self, test_server):
        """GET /health returns 200."""
        resp = test_server.get("/health")
        assert resp["status"] == 200
        assert resp["body"] == {"status": "ok"}

    def test_root_endpoint(self, test_server):
        """GET / returns help text."""
        resp = test_server.get("/")
        assert resp["status"] == 200
        assert "MultiLint Service" in str(resp["body"])

    def test_lint_with_real_files(self, test_server, sample_project):
        """POST /lint with actual sample files returns result."""
        resp = test_server.post(
            "/lint",
            {
                "path": f"{sample_project}/shell",
                "cwd": sample_project,
            },
        )
        assert resp["status"] == 200
        body = resp["body"]
        assert body["target"] == f"{sample_project}/shell"
        assert body["cwd"] == sample_project
        assert "return_code" in body
        assert "stdout" in body

    def test_lint_with_invalid_cwd(self, test_server):
        """POST /lint with nonexistent cwd returns 400."""
        resp = test_server.post("/lint", {"path": ".", "cwd": "/nonexistent/dir"})
        assert resp["status"] == 400
        assert "working directory not found" in resp["body"]["error"]

    def test_lint_bad_json(self, test_server):
        """POST /lint with malformed JSON returns 400."""
        import http.client

        conn = http.client.HTTPConnection(test_server.host, test_server.port)
        conn.request(
            "POST",
            "/lint",
            body=b"not json",
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status == 400
