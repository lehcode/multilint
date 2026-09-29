# pylint: disable=redefined-outer-name
"""Shared pytest fixtures for multilint tests."""

import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Generator

import pytest
import pytest_mock

SERVER_PY = Path(__file__).parent.parent / "server.py"
LINT_SH = Path(__file__).parent.parent / "lint.sh"


@pytest.fixture
def tmp_dir() -> Generator[tempfile.TemporaryDirectory, None, None]:
    """Provide a temporary directory for test fixtures."""
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp


@pytest.fixture
def sample_project(tmp_dir: str) -> str:
    """Create a sample project with valid and broken files."""
    proj = Path(tmp_dir)

    # Shell files
    (proj / "shell").mkdir(parents=True, exist_ok=True)
    (proj / "shell" / "valid.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\necho hello\n")
    (proj / "shell" / "broken.sh").write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\nif [ "$FOO";\n  echo "broken"\nfi\n'
    )

    # Python files
    (proj / "python").mkdir(parents=True, exist_ok=True)
    (proj / "python" / "valid.py").write_text("def greet(name: str) -> str:\n    return f'Hello, {name}!'\n")
    (proj / "python" / "broken.py").write_text("import os,sys\n\ndef bad_format( x,y ):\n    return x+y\n")

    # Markdown files
    (proj / "markdown").mkdir(parents=True, exist_ok=True)
    (proj / "markdown" / "valid.md").write_text("# Valid Markdown\n\nThis is valid.\n\n## Section\n\nContent.\n")
    (proj / "markdown" / "broken.md").write_text("# Invalid Markdown\n\nThis is invalid.\n")

    return tmp_dir


@pytest.fixture
def sample_project_empty(tmp_dir: str) -> str:
    """Create an empty project with no matching files."""
    return tmp_dir


@pytest.fixture
def sample_project_with_hidden(tmp_dir: str) -> str:
    """Create project with hidden files that should be excluded."""
    proj = Path(tmp_dir)
    (proj / "visible.sh").write_text("#!/usr/bin/env bash\necho hi\n")
    (proj / ".hidden.sh").write_text("#!/usr/bin/env bash\necho hi\n")
    (proj / "venv").mkdir()
    (proj / "venv" / "script.sh").write_text("#!/usr/bin/env bash\necho venv\n")
    (proj / ".git").mkdir()
    (proj / ".git" / "hook.sh").write_text("#!/usr/bin/env bash\necho git\n")
    return tmp_dir


@pytest.fixture
def sample_project_with_threshold_config(tmp_dir: str) -> str:
    """Create project with .multilint.json threshold config."""
    proj = Path(tmp_dir)

    # Create .multilint.json with shellcheck threshold=2
    (proj / ".multilint.json").write_text(
        json.dumps(
            {
                "bash_syntax": 0,
                "shellcheck": 2,
                "bashate": 0,
                "shfmt": 0,
                "flake8": 0,
                "black": 0,
                "pylint": 0,
                "markdownlint": 0,
            }
        )
    )

    # Shell files: 2 valid, 1 broken (will have 1 shellcheck failure)
    (proj / "shell").mkdir(parents=True, exist_ok=True)
    (proj / "shell" / "valid1.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\necho hi\n")
    (proj / "shell" / "valid2.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\necho hi\n")
    # Broken file with intentional shellcheck issues
    (proj / "shell" / "broken.sh").write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\nif [ "$FOO";\n  echo "broken"\nfi\n'
    )

    return tmp_dir


@pytest.fixture
def http_client(tmp_dir: str) -> Generator[object, None, None]:
    """Start a test HTTP server and provide a client."""
    # Copy server.py into tmp dir so subprocess calls don't use host path
    server_code = SERVER_PY.read_text()
    # Replace the hardcoded lint.sh path with one that exists
    server_code = server_code.replace(
        "/home/takeshi/docker-compose.d/multilint/lint.sh",
        str(LINT_SH),
    )
    (Path(tmp_dir) / "server.py").write_text(server_code)

    # Start server in thread
    port = _find_free_port()
    proc = subprocess.Popen(
        ["python3", str(Path(tmp_dir) / "server.py")],
        cwd=tmp_dir,
        env={
            **os.environ,
            "LINT_SERVER_PORT": str(port),
            "LINT_SERVER_HOST": "127.0.0.1",
            # Allow the test's own tmp dir as the only permitted run root
            # (server default is /workspace:/multilint, which don't exist here).
            "MULTILINT_ALLOWED_ROOTS": tmp_dir,
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for server to be ready
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

    client = _TestClient(("127.0.0.1", port))

    yield client

    proc.terminate()
    proc.wait(timeout=5)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _TestClient:
    """Simple HTTP client for testing."""

    def __init__(self, addr: tuple) -> None:
        self.addr = addr

    def _request(self, method: str, path: str, body: bytes = b"", timeout: float = 5) -> dict:
        import http.client

        conn = http.client.HTTPConnection(self.addr[0], self.addr[1], timeout=timeout)
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

    def get(self, path: str, timeout: float = 5) -> dict:
        return self._request("GET", path, timeout=timeout)

    def post(self, path: str, data: dict, timeout: float = 5) -> dict:
        body = json.dumps(data).encode()
        return self._request("POST", path, body=body, timeout=timeout)


@pytest.fixture
def mock_subprocess_run(
    mocker,
):
    """Return the mocker for patching subprocess.run."""
    return mocker


@pytest.fixture
def mock_subprocess(
    mock_subprocess_run: pytest_mock.MockerFixture,
) -> pytest_mock.MockerFixture:
    """Mock subprocess.run and return the mock object."""
    return mock_subprocess_run.patch("subprocess.run")
