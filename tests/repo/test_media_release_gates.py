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
    quality = _job(_read(".github/workflows/quality.yml"), "quality")

    assert re.search(r"(?m)^check:\s+media-check\b", makefile)
    assert '"$MEDIA_PYTHON" scripts/media-proof.py --quick' in invariants
    assert '"$MEDIA_PYTHON" "$SCRIPT_DIR/validate-spoken-assets.py"' in invariants
    assert '"$MEDIA_PYTHON" "$SCRIPT_DIR/validate-release-beat.py"' in invariants
    assert '"$MEDIA_PYTHON" - "$QUEUE_FALLBACK_WAIT"' in invariants
    assert '"$MEDIA_PYTHON" scripts/media-proof.py --quick' in pre_release
    assert "python scripts/media-proof.py --quick --output media-proof.json" in quality
    assert "name: media-proof-quality-${{ github.sha }}" in quality


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


def test_standalone_and_edge_publish_paths_keep_strict_gate() -> None:
    standalone = _read(".github/workflows/docker.yml")
    edge = _read("scripts/cut-edge-release.sh")

    gate = "python scripts/media-proof.py --quick --output media-proof.json"
    assert gate in standalone
    assert standalone.index(gate) < standalone.index("push: true")
    assert "name: media-proof-standalone-${{ github.sha }}" in standalone

    edge_gate = '"$MEDIA_PYTHON" scripts/media-proof.py --quick'
    assert edge_gate in edge
    assert edge.index(edge_gate) < edge.index('git commit -q -m "chore(edge):')
