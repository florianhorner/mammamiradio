"""Unit tests for the shared packaged-asset guard.

Producer and streamer both delegate here with an explicit assets_dir, so the
default-argument contract would otherwise only ever run in production.
"""

from mammamiradio.core.packaged_assets import DEMO_ASSETS_DIR, is_packaged_asset


def test_default_assets_dir_protects_the_packaged_tree():
    assert is_packaged_asset(DEMO_ASSETS_DIR / "recovery" / "continuity_1.mp3") is True


def test_default_assets_dir_rejects_outside_paths(tmp_path):
    assert is_packaged_asset(tmp_path / "not-packaged.mp3") is False


def test_explicit_assets_dir_overrides_default(tmp_path):
    assert is_packaged_asset(tmp_path / "assets" / "clip.mp3", tmp_path) is True
    assert is_packaged_asset(DEMO_ASSETS_DIR / "recovery" / "continuity_1.mp3", tmp_path) is False


def test_traversal_out_of_assets_dir_is_rejected(tmp_path):
    assert is_packaged_asset(tmp_path / "assets" / ".." / ".." / "escape.mp3", tmp_path / "assets") is False


def test_non_path_input_returns_false():
    assert is_packaged_asset(None) is False  # type: ignore[arg-type]
    assert is_packaged_asset("a-string") is False  # type: ignore[arg-type]
