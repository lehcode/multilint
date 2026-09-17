#!/usr/bin/env python3
"""MultiLint MCP server — exposes linting as MCP tools."""

import os
import subprocess

from fastmcp import FastMCP


def create_server() -> FastMCP:
    """Create the MCP server with linting tools and resources."""
    server = FastMCP(
        "multilint",
        version="1.0.0",
        instructions=(
            "Code quality linting, static analysis, and security scanning for shell scripts, Python files, "
            "Markdown, YAML, JSON, and TOML. Validates bash syntax, shellcheck warnings, bashate indentation, "
            "shfmt formatting, flake8 style, black formatting, pylint errors, markdownlint rules, "
            "YAML/JSON formatting via prettier, TOML sorting via toml-sort, hardcoded secrets via grep, "
            "dangerous shell patterns, and git history secrets via gitleaks. "
            "Configurable per-check thresholds via .multilint.json. Returns structured results with "
            "pass/fail counts per check. Use when a developer asks about code quality, lint errors, "
            "formatting check, code review, security scanning, secret detection, or CI/CD pipeline validation."
        ),
    )

    @server.tool(
        name="lint_files",
        description=(
            "Run the full linting pipeline on a target directory to check code quality, formatting, "
            "and security. Validates shell scripts for syntax errors, common mistakes, and indentation. "
            "Checks Python for style issues, formatting, and code quality. "
            "Validates Markdown for formatting, YAML/JSON via prettier, TOML via toml-sort, "
            "hardcoded secrets via grep, dangerous shell patterns, and git history secrets via gitleaks. "
            "Supports configurable thresholds per check. "
            "Returns stdout, stderr, cwd, and return code. "
            "Use when a developer asks about code quality, lint errors, formatting issues, "
            "security scanning, or secret detection."
        ),
    )
    def lint_files(path: str = ".", cwd: str = None) -> dict:
        target = path if path else "."
        run_dir = cwd if cwd else "."
        lint_script = os.environ.get("MULTILINT_SCRIPT", "/usr/local/bin/lint.sh")
        proc = subprocess.run(
            ["bash", lint_script, target],
            capture_output=True,
            text=True,
            check=False,
            cwd=run_dir,
        )
        return {
            "target": target,
            "cwd": run_dir,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "return_code": proc.returncode,
        }

    @server.tool(
        name="health_check",
        description=(
            "Check if the MultiLint service is running and responsive. "
            "Returns {'status': 'ok'} when healthy. "
            "Use when debugging connectivity or verifying the service state."
        ),
    )
    def health_check() -> dict:
        return {"status": "ok"}

    @server.tool(
        name="get_help",
        description=(
            "Display help text with available tools, parameters, and example usage. "
            "Use when a developer needs to learn how to use the MultiLint MCP server."
        ),
    )
    def get_help() -> dict:
        return {
            "text": (
                "MultiLint MCP Server\n\n"
                "Tools:\n"
                "  lint_files(path, cwd) — Run linting pipeline.\n"
                "  health_check() — Check service health.\n"
                "  get_help() — Display this help.\n"
                "\n"
                "Example:\n"
                "  lint_files(path='./gw/', cwd='/workspace/')\n"
                "\n"
                "Endpoints:\n"
                "  MCP: streamable-http on port 8592\n"
                '  HTTP: POST /lint {"path": "./dir"}\n'
                "  Health: GET /health\n"
            ),
        }

    @server.resource("resource://help")
    def help_text() -> str:
        return (
            "MultiLint MCP Server\n\n"
            "Tools:\n"
            "  lint_files(path, cwd) — Run linting pipeline on a directory.\n"
            "  health_check() — Check service health.\n"
            "  get_help() — Display this help text.\n"
            "\n"
            "Example:\n"
            "  lint_files(path='./gw/', cwd='/home/takeshi/lan-hosts/')\n"
            "\n"
            "Endpoints:\n"
            "  MCP transport: streamable-http on port 8592\n"
            '  HTTP API: POST http://localhost:8591/lint {"path": "./gw/", "cwd": "/dir"}\n'
            "  Health: GET http://localhost:8591/health\n"
        )

    return server


def main():
    """Entry point — run MCP server over streamable-http."""
    server = create_server()
    port = int(os.environ.get("MCP_SERVER_PORT", "8592"))
    server.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=port,
        path="/mcp",
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
