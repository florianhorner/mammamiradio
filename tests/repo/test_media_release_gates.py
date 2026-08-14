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


def test_local_merge_and_pre_release_paths_are_strict() -> None:
    makefile = _read("Makefile")
    invariants = _read("scripts/check-release-invariants.sh")
    pre_release = _read("scripts/pre-release-check.sh")

    assert re.search(r"(?m)^check:\s+media-check\b", makefile)
    assert '"$MEDIA_PYTHON" scripts/media-proof.py --quick' in invariants
    assert '"$MEDIA_PYTHON" "$SCRIPT_DIR/validate-spoken-assets.py"' in invariants
    assert '"$MEDIA_PYTHON" "$SCRIPT_DIR/validate-release-beat.py"' in invariants
    assert '"$MEDIA_PYTHON" - "$QUEUE_FALLBACK_WAIT"' in invariants
    assert '"$MEDIA_PYTHON" scripts/media-proof.py --quick' in pre_release
    # Both stay hard: a media-proof failure fails the script, never a notice.
    assert 'fail "strict media proof failed' in invariants
    assert 'fail "strict media proof failed' in pre_release
    assert "NOTICE: media-proof reported missing content" not in invariants
    assert "NOTICE: media-proof reported missing content" not in pre_release


def test_pr_lanes_run_media_proof_report_only() -> None:
    """While the starter tracks are absent by design, the PR quality lane and
    the add-on validate job run the proof report-only: the proof and its
    uploaded report remain, but the missing content no longer fails the lane.
    The release paths asserted strict above and below keep release blocked."""

    quality = _job(_read(".github/workflows/quality.yml"), "quality")
    validate = _job(_read(".github/workflows/addon-build.yml"), "validate")
    notice = "NOTICE: media-proof reported missing content"

    assert "if python scripts/media-proof.py --quick --output media-proof.json; then" in quality
    assert notice in quality
    assert "name: media-proof-quality-${{ github.sha }}" in quality
    assert "if python scripts/media-proof.py --quick --output media-proof.json; then" in validate
    assert notice in validate
    assert "name: media-proof-quick-${{ github.sha }}" in validate


def test_addon_publish_and_stable_promotion_require_both_image_proof() -> None:
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
