"""Run lint.sh for the edited file in an ephemeral, sandboxed container and parse its JSON results.

Imported by lint_changed.py, which is the hook entry point.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import ml_settings


# Wall-clock ceiling for the whole container, not per file. Generous because a first pull has to
# fetch the image, and a timeout here means the edit is reported as unchecked rather than clean.
RUN_TIMEOUT_SECONDS = 300


# Resource caps, so the hook cannot starve the machine it runs on.
MEMORY_LIMIT = "2g"


CPU_LIMIT = "2"


# Separator printed to stdout before each file's JSON document, so one run producing N
# documents can be split back apart. Chosen to be something no linter emits.
FILE_MARKER = "===MULTILINT-FILE==="


# Where the plugin's scripts/ directory (lint.sh included) is mounted read-only in the container.
CONTAINER_SCRIPTS = "/opt/multilint"


def container_program() -> str:
    """The bash program run inside the container.

    One lint.sh run per file, in one container. lint.sh takes a single target and has no
    multi-target mode.

    Each document is preceded by a marker line so the combined stdout can be split back into one
    result per file. In JSON mode lint.sh sends human-readable output to stderr and only JSON to
    stdout, so stdout stays parsable.

    `|| rc=1` rather than `set -e`: a failing file must not stop the remaining files from being
    checked, but the overall exit status should still reflect that something failed.
    """
    return (
        "rc=0\n"
        'for f in "$@"; do\n'
        f'    printf "%s%s\\n" "{FILE_MARKER}" "$f"\n'
        # Also on stderr, because that is where lint.sh sends the human-readable findings in JSON
        # mode. Without a marker there, the detail for N files arrives as one undivided blob and
        # cannot be attributed to the file it belongs to.
        f'    printf "%s%s\\n" "{FILE_MARKER}" "$f" >&2\n'
        f'    bash {CONTAINER_SCRIPTS}/lint.sh "$f" --format json || rc=1\n'
        "done\n"
        "exit $rc\n"
    )


def scripts_dir() -> Path:
    """The plugin's scripts directory: this module's own directory, which also holds lint.sh."""
    return Path(__file__).resolve().parent


def bind_mount(source: str, target: str) -> str:
    """A read-only `--mount` spec. Docker parses it as CSV, so a path holding ',' or '"' is quoted."""

    def field(key: str, value: str) -> str:
        text = f"{key}={value}"
        return '"' + text.replace('"', '""') + '"' if ("," in text or '"' in text) else text

    return ",".join(["type=bind", field("source", source), field("target", target), "readonly"])


def container_name() -> str:
    """A unique name, so a timed-out container can be found and removed."""
    return f"multilint-{os.getpid()}-{secrets.token_hex(4)}"


def build_docker_argv(
    scope_root: Path, relative_paths: list[str], image: str | None = None, name: str | None = None
) -> list[str]:
    """Assemble the full `docker run` argv.

    Runs nothing, so tests can assert on the exact arguments without Docker present.

    Two mounts are used:

    1. **Identity mount** — source and target are the same absolute path. The container sees the file
       at the path the host calls it, so no translation table exists to be wrong or out of date.
    2. **Scripts mount** — the plugin's scripts directory at ``/opt/multilint``, so the container
       runs this plugin's lint.sh rather than any copy baked into the image.

    ``--mount`` rather than ``-v``: the ``-v src:dst:opts`` form is colon-delimited, and in zsh
    ``"$PWD:$PWD:ro"`` silently becomes ``.../multilint:.../multilinto``, because ``:r`` is a
    parameter modifier that applies even inside double quotes. That produced a container mounted
    read-write at the wrong target, an empty working directory, and a lint run that reported success
    having examined nothing. ``--mount`` takes named keys and cannot be misread that way.
    """
    root = str(scope_root)
    argv = [
        "docker",
        "run",
        "--rm",
        "--name",
        name or container_name(),
        # No linter needs the network. gitleaks reads local history; every other tool reads files.
        "--network",
        "none",
        "--memory",
        MEMORY_LIMIT,
        "--cpus",
        CPU_LIMIT,
        # Identity mount: source root at the same path inside the container.
        "--mount",
        bind_mount(root, root),
        "--mount",
        bind_mount(str(scripts_dir()), CONTAINER_SCRIPTS),
        "--workdir",
        root,
        # lint.sh resolves .multilint.json and .markdownlint.json relative to the working directory,
        # so the scope root has to be the working directory for per-project config to apply.
        "--entrypoint",
        "bash",
        # Run as the invoking user so nothing in the container acts as root, whatever USER the image
        # declares. POSIX only, which includes WSL; native Windows is not a target.
        "--user",
        f"{os.getuid()}:{os.getgid()}",
    ]
    argv += [image or ml_settings.resolve_image(), "-c", container_program(), "_"]
    argv += relative_paths
    return argv


def split_stream(text: str) -> dict[str, str]:
    """Split a marker-delimited stream into {file: body}."""
    sections: dict[str, str] = {}
    for block in text.split(FILE_MARKER):
        if not block.strip():
            continue
        name, _, body = block.partition("\n")
        sections[name.strip()] = body
    return sections


class LintUnavailable(Exception):
    """lint.sh could not produce a result; the message says why."""


def parse_results(proc: subprocess.CompletedProcess) -> list[tuple[str, dict, str]]:
    """Split the container's marker-delimited output into (file, JSON document, detail) per file.

    Raises LintUnavailable when no document parses, naming docker's own error when there is one.
    """
    details = split_stream(proc.stderr)
    results: list[tuple[str, dict, str]] = []
    for name, body in split_stream(proc.stdout).items():
        try:
            document = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(document, dict):
            results.append((name, document, details.get(name, "")))
    if not results:
        # Docker reports daemon, pull and mount errors on stderr before the first marker; lint.sh's own
        # output comes after it. The last of those lines is the one that says what went wrong.
        docker_lines = [line.strip() for line in proc.stderr.split(FILE_MARKER, 1)[0].splitlines() if line.strip()]
        reason = f"exit status {proc.returncode}" + (f": {docker_lines[-1]}" if docker_lines else "")
        raise LintUnavailable(f"the ephemeral container produced no readable result ({reason})")
    return results


def run_lint(scope_root: Path, relative_paths: list[str]) -> list[tuple[str, dict, str]]:
    """Run one container over every changed file.

    Returns (file, JSON document, human-readable detail) per file. A document that will not parse is
    dropped rather than guessed at, so a malformed result is never reported as a finding.

    Raises LintUnavailable when nothing could be determined — no docker, a daemon or pull failure, a
    timeout, no readable document — so the caller can say the file is unverified instead of staying
    silent.
    """
    if shutil.which("docker") is None:
        raise LintUnavailable("docker not found on PATH")
    lint_sh = scripts_dir() / "lint.sh"
    if not lint_sh.exists():
        raise LintUnavailable(f"lint.sh not found at {lint_sh}")
    name = container_name()
    try:
        proc = subprocess.run(
            build_docker_argv(scope_root, relative_paths, name=name),
            capture_output=True,
            text=True,
            check=False,
            timeout=RUN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        # Killing the docker client leaves the container running; remove it by name. Best effort: a
        # failed removal must not replace the notice with a crash.
        try:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
        raise LintUnavailable(f"the ephemeral container did not finish within {RUN_TIMEOUT_SECONDS} s") from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise LintUnavailable(f"docker could not be started: {exc}") from None

    return parse_results(proc)
