"""Fixtures shared by tests/unit/ test files.

Provides the ``hook`` fixture; imports shared helpers from hook_helpers.
Do NOT put helper functions here — they belong in hook_helpers.py.
"""

import importlib.util

import pytest

from .hook_helpers import HOOK_PATH


def _load_module():
    """Import the hook by path — it is a script next to the plugin, not an installed package."""
    spec = importlib.util.spec_from_file_location("lint_changed_under_test", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def hook():
    return _load_module()
