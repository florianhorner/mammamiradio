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
    release path. Every per-PR/local lane asserted report-only below
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


def test_report_only_lanes_stay_visible_but_addon_build_runs_the_release_gate_blocking() -> None:
    """Fast PR/local reports remain advisory, while every main image build must
    pass the same full proof that gates stable promotion."""

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
    assert "python scripts/media-proof.py \\" in build_proof
    assert '--image-arch "$IMAGE_ARCH"' in build_proof
    assert '"$IMAGE_OPTION" "$IMAGE_REF"' in build_proof
    assert "if python scripts/media-proof.py" not in build_proof
    assert notice not in build_proof
    assert "name: media-proof-full-${{ matrix.arch }}-${{ github.sha }}" in build_proof
    assert 'if "$MEDIA_PYTHON" scripts/media-proof.py --quick; then' in invariants
    assert notice in invariants
    assert 'fail "strict media proof failed' not in invariants
    assert "if $(PYTHON) scripts/media-proof.py --quick; then" in makefile
    assert notice in makefile


def test_addon_publish_and_stable_promotion_require_native_per_arch_image_proof() -> None:
    """Both publish paths wait for blocking proofs on native architecture runners."""

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
    assert "addon-image-${{ matrix.arch }}-${{ github.sha }}" in build_proof
    assert "needs: [validate, media-proof]" in publish
    assert "packages: write" in publish
    assert 'docker push "$SHA_REF"' in publish
    assert 'docker push "$SHORT_REF"' in publish
    assert "docker push" not in build_image
    assert "push: true" not in build_image
    for proof in (build_proof, release_proof):
        assert "runs-on: ${{ matrix.runner }}" in proof
        assert "timeout-minutes: 30" in proof
        assert "fail-fast: false" in proof
        assert "- arch: amd64\n            runner: ubuntu-latest\n            image_option: --amd64-image" in proof
        assert (
            "- arch: aarch64\n            runner: ubuntu-24.04-arm\n            image_option: --aarch64-image"
        ) in proof
        assert "docker/setup-qemu-action@" not in proof
        assert '--image-arch "$IMAGE_ARCH"' in proof
        assert '"$IMAGE_OPTION" "$IMAGE_REF"' in proof
    assert 'run: docker pull "$IMAGE_REF"' in release_proof
    assert "name: media-proof-stable-${{ matrix.arch }}-${{ github.sha }}" in release_proof
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
