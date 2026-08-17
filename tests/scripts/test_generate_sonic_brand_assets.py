from __future__ import annotations

import pytest

from scripts import generate_sonic_brand_assets as generator


def test_validate_only_checks_runtime_manifest_independently_of_listening_receipt(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_validate(argv: list[str]) -> int:
        calls.append(argv)
        return 17

    monkeypatch.setattr(generator.validate_audio_asset_pack, "main", fake_validate)

    assert generator.main(["--validate-only"]) == 17
    assert calls == [[]]


def test_generation_and_rejected_source_rebuild_options_are_unavailable() -> None:
    with pytest.raises(SystemExit, match="2"):
        generator.main([])
    with pytest.raises(SystemExit, match="2"):
        generator.main(["--source-dir", "/tmp/rejected"])

    assert not hasattr(generator, "build_public_imaging_pack")
