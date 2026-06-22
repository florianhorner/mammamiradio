from __future__ import annotations

import json

from scripts import fictional_ad_lab as lab


def test_default_output_dir_never_uses_context_runtime_state() -> None:
    assert ".context" not in lab.DEFAULT_OUTPUT_DIR.parts


def test_review_rejects_existing_brand_and_real_brand_marker() -> None:
    duplicate = lab._candidate(
        "Prezzoforte",
        "Conveniente. Sempre. Forse.",
        "services",
        False,
        "Duplicate candidate.",
        "Should be rejected.",
        ("test",),
    )
    lookalike = lab._candidate(
        "Barilla Express",
        "Pasta veloce per tutti.",
        "food",
        False,
        "Too close to a real brand.",
        "Should be rejected.",
        ("test",),
    )

    duplicate_review = lab.review_candidate(duplicate, {"prezzoforte"})
    lookalike_review = lab.review_candidate(lookalike)

    assert duplicate_review.status == "reject"
    assert any("duplicate" in flag for flag in duplicate_review.flags)
    assert lookalike_review.status == "reject"
    assert any("real-brand marker" in flag for flag in lookalike_review.flags)


def test_select_candidates_skips_existing_radio_brands() -> None:
    selected = lab.select_candidates(25, {"ombrello preventivo"})
    names = {candidate.name for candidate, _review in selected}

    assert "Ombrello Preventivo" not in names
    assert len(names) == len(selected)
    assert selected
    assert all(review.status != "reject" for _candidate, review in selected)


def test_campaign_contract_uses_known_formats_and_speakers() -> None:
    for candidate in lab.CANDIDATE_POOL:
        if not candidate.campaign:
            continue
        assert set(candidate.campaign.format_pool) <= lab.VALID_FORMATS
        assert candidate.campaign.spokesperson in lab.VALID_SPEAKERS


def test_write_artifacts_outputs_reviewable_lab_pack(tmp_path) -> None:
    candidates = lab.select_candidates(4, set())
    finalists = lab.finalist_candidates(candidates, 2)

    paths = lab.write_artifacts(
        candidates,
        finalists,
        tmp_path,
        config_path=tmp_path / "radio.toml",
        timestamp="20260622T180000Z",
    )

    assert set(paths) == {
        "manifest",
        "candidates",
        "collision_checks",
        "toml_candidates",
        "trigger_ideas",
        "recommendation",
    }
    manifest = json.loads(paths["manifest"].read_text())
    assert manifest["generated_at"] == "20260622T180000Z"
    assert manifest["finalists"] == [candidate.name for candidate, _review in finalists]
    assert "producer_headroom.headroom_ok" in manifest["bff_load_perspective"]

    toml_candidates = paths["toml_candidates"].read_text()
    assert "[[ads.brands]]" in toml_candidates
    assert "[ads.brands.campaign]" in toml_candidates
    assert "Paste only approved, web-checked finalists" in toml_candidates

    collision_checks = paths["collision_checks"].read_text()
    for candidate, _review in finalists:
        assert f'"{candidate.name}"' in collision_checks

    trigger_ideas = paths["trigger_ideas"].read_text()
    assert "Do not fire during queue rescue" in trigger_ideas
    assert "BFF/load view" in trigger_ideas


def test_dry_run_writes_no_artifacts(tmp_path) -> None:
    candidates = lab.select_candidates(2, set())

    paths = lab.write_artifacts(
        candidates,
        lab.finalist_candidates(candidates, 1),
        tmp_path,
        config_path=tmp_path / "radio.toml",
        timestamp="20260622T180000Z",
        dry_run=True,
    )

    assert paths == {}
    assert list(tmp_path.iterdir()) == []
