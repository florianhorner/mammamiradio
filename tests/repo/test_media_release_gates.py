"""Fail closed if a merge, image publish, or channel promotion loses media proof."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _job(text: str, name: str) -> str:
    jobs = text.split("\njobs:", 1)[1]
    match = re.search(rf"\n  {re.escape(name)}:\n((?:    .+\n|\n)*)", jobs)
    assert match, f"missing {name} job"
    return match.group(1)


def test_release_path_keeps_strict_media_gate() -> None:
    """scripts/pre-release-check.sh section 10 is the hard media gate on the
    release path. Every per-PR/build/local lane asserted report-only below
    depends on this gate staying hard; a media-proof failure here fails the
    script, never a notice."""

    pre_release = _read("scripts/pre-release-check.sh")

    assert '"$MEDIA_PYTHON" scripts/media-proof.py --quick' in pre_release
    assert 'fail "strict media proof failed' in pre_release
    assert "NOTICE: media-proof reported missing content" not in pre_release


def test_per_pr_invariants_keep_non_media_sections_strict() -> None:
    """Only the media section of the per-PR invariants script is report-only;
    the recovery-audio, spoken-assets, release-beat, and fallback-wait
    invariants keep failing hard."""

    makefile = _read("Makefile")
    invariants = _read("scripts/check-release-invariants.sh")

    assert re.search(r"(?m)^check:\s+media-check\b", makefile)
    assert '"$MEDIA_PYTHON" "$SCRIPT_DIR/validate-spoken-assets.py"' in invariants
    assert '"$MEDIA_PYTHON" "$SCRIPT_DIR/validate-release-beat.py"' in invariants
    assert '"$MEDIA_PYTHON" - "$QUEUE_FALLBACK_WAIT"' in invariants
    assert 'fail "packaged spoken asset manifest validation failed"' in invariants
    assert 'fail "release beat manifest validation failed"' in invariants


def test_pr_lanes_and_local_check_run_media_proof_report_only() -> None:
    """While the starter tracks are absent by design, the PR quality lane, the
    add-on validate job, the add-on build full media-proof job, the per-PR
    invariants media section, and local make media-check run the proof
    report-only: the proof and its report remain, but the missing content no
    longer fails the lane (the full job's hard failure was skipping push and
    starving the edge channel of images). The release path asserted strict
    above keeps release blocked."""

    build = _read(".github/workflows/addon-build.yml")
    quality = _job(_read(".github/workflows/quality.yml"), "media-report")
    validate = _job(build, "validate")
    build_proof = _job(build, "media-proof")
    invariants = _read("scripts/check-release-invariants.sh")
    makefile = _read("Makefile")
    notice = "NOTICE: media-proof reported missing content"

    assert "if python scripts/media-proof.py --quick --output media-proof.json; then" in quality
    assert notice in quality
    assert "name: media-proof-quality-${{ github.sha }}" in quality
    assert "if python scripts/media-proof.py --quick --output media-proof.json; then" in validate
    assert notice in validate
    assert "name: media-proof-quick-${{ github.sha }}" in validate
    assert "if python scripts/media-proof.py \\" in build_proof
    assert "--output media-proof.json; then" in build_proof
    assert notice in build_proof
    assert "name: media-proof-full-${{ github.sha }}" in build_proof
    assert 'if "$MEDIA_PYTHON" scripts/media-proof.py --quick; then' in invariants
    assert notice in invariants
    assert 'fail "strict media proof failed' not in invariants
    assert "if $(PYTHON) scripts/media-proof.py --quick; then" in makefile
    assert notice in makefile


def test_addon_publish_and_stable_promotion_require_both_image_proof() -> None:
    """Publish still waits for the full both-image proof to run before any
    docker push (the job is report-only on missing starter content — see the
    lane split above), and the stable promotion proof in addon-release.yml
    stays strict: a media-proof failure there fails the job, never a notice."""

    build = _read(".github/workflows/addon-build.yml")
    build_image = _job(build, "build")
    build_proof = _job(build, "media-proof")
    publish = _job(build, "push")
    release = _read(".github/workflows/addon-release.yml")
    release_proof = _job(release, "media-proof")
    promote = _job(release, "promote")

    assert "push: false" in build_image
    assert "docker save --output" in build_image
    assert "needs: [validate, build]" in build_proof
    assert "--amd64-image" in build_proof and "--aarch64-image" in build_proof
    assert "needs: [validate, media-proof]" in publish
    assert "docker push" not in build_image
    assert "docker push" in publish

    assert 'docker pull --platform linux/amd64 "$AMD64_IMAGE"' in release_proof
    assert 'docker pull --platform linux/arm64 "$AARCH64_IMAGE"' in release_proof
    assert "--amd64-image" in release_proof and "--aarch64-image" in release_proof
    assert "NOTICE: media-proof reported missing content" not in release_proof
    assert "needs: [pre-flight, media-proof, smoke-prebuilt]" in promote


def test_standalone_publish_path_keeps_strict_gate() -> None:
    standalone = _read(".github/workflows/docker.yml")

    gate = "python scripts/media-proof.py --quick --output media-proof.json"
    assert gate in standalone
    assert standalone.index(gate) < standalone.index("push: true")
    assert "name: media-proof-standalone-${{ github.sha }}" in standalone
    assert "NOTICE: media-proof reported missing content" not in standalone


def test_edge_cut_runs_media_proof_report_only() -> None:
    edge = _read("scripts/cut-edge-release.sh")

    edge_gate = 'if "$MEDIA_PYTHON" scripts/media-proof.py --quick; then'
    assert edge_gate in edge
    assert "NOTICE: media-proof reported missing content" in edge
    assert edge.index(edge_gate) < edge.index('git commit -q -m "chore(edge):')
