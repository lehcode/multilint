"""Unit tests for core/ml_settings.py — image settings, DEFAULT_IMAGE, SETTINGS_KEYS."""

# pylint: disable=redefined-outer-name,protected-access,unused-argument
import re
from pathlib import Path

import ml_settings
import ml_state
import pytest


class TestImageResolution:
    """DEFAULT_IMAGE and resolve_image()."""

    def test_default_image_is_a_pinned_release_tag(self, hook):
        """A release tag, never :latest; release-please rewrites the version, so match the shape."""
        assert re.fullmatch(r"lehcode/multilint:v\d+\.\d+\.\d+", ml_settings.DEFAULT_IMAGE)

    @pytest.mark.parametrize("value", ["--privileged", "-v", "bad image"])
    def test_image_setting_rejects_docker_options_and_spaces(self, hook, monkeypatch, tmp_path, value):
        monkeypatch.setattr(ml_state, "state_dir", lambda: tmp_path / "state")
        with pytest.raises(ValueError, match="cannot start with '-'"):
            ml_settings.write_setting("image", value)

    def test_default_image_has_version_marker(self):
        source = Path(ml_settings.__file__).read_text(encoding="utf-8")
        assert "x-release-please-version" in source

    def test_resolve_image_defaults(self, hook, monkeypatch):
        monkeypatch.setattr(ml_settings, "read_setting", lambda key: None)
        assert ml_settings.resolve_image() == ml_settings.DEFAULT_IMAGE

    def test_resolve_image_uses_setting(self, hook, monkeypatch):
        monkeypatch.setattr(ml_settings, "read_setting", lambda key: "local/build:dev")
        assert ml_settings.resolve_image() == "local/build:dev"

    def test_image_in_settings_keys(self, hook):
        assert "image" in ml_settings.SETTINGS_KEYS

    def test_settings_keys_order(self, hook):
        assert ml_settings.SETTINGS_KEYS == ("image", "search_ceiling")
