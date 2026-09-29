#!/usr/bin/env python3
"""MultiLint HTTP API server — runs inside the linting container."""

import json
import os
import re
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Default read-only mounts inside the container (see docker-compose.yml).
DEFAULT_ALLOWED_ROOTS = "/workspace:/multilint"


def _allowed_roots() -> list[str]:
    """Return the realpath-resolved allowlist of permitted run directories.

    Roots come from MULTILINT_ALLOWED_ROOTS (colon-separated); the default is
    the two read-only container mounts. Resolving the roots here is safe to
    keep out of line: this function never touches the request-supplied path,
    so it carries no taint of its own.
    """
    allowed_env = os.environ.get("MULTILINT_ALLOWED_ROOTS", DEFAULT_ALLOWED_ROOTS)
    return [os.path.realpath(root) for root in allowed_env.split(":") if root]


class LintHandler(BaseHTTPRequestHandler):
    """Handle HTTP requests for linting and health checks."""

    def _respond(self, code: int, body: dict) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body, ensure_ascii=False).encode())

    def do_GET(self) -> None:
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
        elif self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b'MultiLint Service\nEndpoints:\n  POST /lint {"path": "./gw/"}\n  GET  /health\n')
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/lint":
            content_length = int(self.headers["Content-Length"])
            body = self.rfile.read(content_length)
            try:
                payload = json.loads(body)
                target = payload.get("path", ".")
                cwd = payload.get("cwd", None)
            except (json.JSONDecodeError, KeyError):
                self._respond(400, {"error": "bad request"})
                return

            # Determine working directory: explicit cwd takes precedence,
            # otherwise default to "." (server's own directory).
            # run_dir = cwd if cwd else "."  # replaced: validated inline below
            #
            # The realpath() normalisation and the containment guard are kept in
            # this function, next to the isdir()/subprocess sinks they protect.
            # An extracted helper is equivalent at runtime, but CodeQL's
            # py/path-injection barrier detection does not follow a guard across
            # a function-return boundary and keeps reporting the sink.
            run_dir = os.path.realpath(cwd if cwd else ".")

            permitted = False
            for allowed_root in _allowed_roots():
                # Exact match, or strictly inside the root. The os.sep boundary
                # stops "/workspace-evil" passing as a child of "/workspace".
                if run_dir == allowed_root or run_dir.startswith(allowed_root + os.sep):
                    permitted = True
                    break

            if not permitted:
                # Do not echo the resolved absolute path or allowlist contents
                # to an unauthenticated caller — generic message only.
                self._respond(
                    400,
                    {
                        "error": "working directory not permitted",
                        "return_code": 1,
                        "stdout": "",
                        "stderr": "",
                        "cwd": cwd if cwd else ".",
                        "target": target,
                    },
                )
                return

            if not os.path.isdir(run_dir):
                self._respond(
                    400,
                    {
                        "error": f"working directory not found: {run_dir}",
                        "return_code": 1,
                        "stdout": "",
                        "stderr": "",
                        "cwd": run_dir,
                        "target": target,
                    },
                )
                return

            lint_script = os.environ.get("MULTILINT_SCRIPT", "/usr/local/bin/lint.sh")
            proc = subprocess.run(
                ["bash", lint_script, target],
                capture_output=True,
                text=True,
                check=False,
                cwd=run_dir,
            )

            stdout_clean = ANSI_RE.sub("", proc.stdout).strip()
            stderr_clean = ANSI_RE.sub("", proc.stderr).strip()

            result = {
                "target": target,
                "cwd": run_dir,
                "stdout": stdout_clean,
                "stderr": stderr_clean,
                "return_code": proc.returncode,
            }
            self._respond(200, result)
        else:
            self._respond(404, {"error": "not found"})

    def log_request(self, *args) -> None:
        pass  # suppress default logging


def main(host: str, port: int) -> None:
    try:
        server = HTTPServer((host, port), LintHandler)
        print(f"MultiLint server listening on {host}:{port}")
        server.serve_forever()
    except OSError as e:
        print(f"Failed to start server on {host}:{port}: {e}")
        if e.errno == 98:  # EADDRINUSE
            print(
                f"Port {port} is already in use. "
                "Another MultiLint process may be running. "
                "Stop it first or use a different port."
            )
        raise SystemExit(1) from e


if __name__ == "__main__":
    srv_host = os.environ.get("LINT_SERVER_HOST", "0.0.0.0")
    srv_port = int(os.environ.get("LINT_SERVER_PORT", "8591"))
    main(srv_host, srv_port)
