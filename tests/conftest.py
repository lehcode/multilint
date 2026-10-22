# pylint: disable=redefined-outer-name
"""Shared pytest fixtures for multilint tests."""

import json
import sys
import tempfile
from pathlib import Path
from typing import Generator

import pytest
import pytest_mock

# core/ holds scripts that import their siblings (multilint_config -> ml_config_schema); tests load them by path.
sys.path.insert(0, str(Path(__file__).parent.parent / "core"))


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
