from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "scripts" / "validate-release-beat.py"
MANIFEST = Path("mammamiradio/assets/release/release_beat.toml")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _write_pyproject(tmp_path: Path, *, package_data: str = '"assets/**/*"') -> None:
    _write(
        tmp_path / "pyproject.toml",
        "\n".join(
            [
                "[project]",
                'name = "mammamiradio"',
                'version = "2.15.0"',
                "",
                "[tool.setuptools.package-data]",
                f"mammamiradio = [{package_data}]",
                "",
            ]
        ),
    )


def _edge_manifest(
    *,
    beat_id: str = "edge-4a15270-hans-guenther",
    build_sha: str = "4a1527080692eed5541e72a5a2b0f2c344e3ca9a",
    extra: str = "",
    facts: str = '"Hans Guenther can now wait in the studio hallway as a guest-host prop."',
    props: str = '"a human-sized crate labeled HANS GUENTHER"',
) -> str:
    return f"""
[release_beat]
id = "{beat_id}"
channel = "edge"
build_sha = "{build_sha}"
priority = "normal"
facts = [{facts}]
props = [{props}]
avoid = ["claiming the listener updated successfully before boot"]
copy = ["There is a crate in Studio B, and everyone is pretending that is normal."]
{extra}
"""


def _stable_manifest(*, semver: str = "2.15.0") -> str:
    return f"""
[release_beat]
id = "stable-{semver}-studio-crate"
channel = "stable"
semver = "{semver}"
facts = ["The studio now has a cleaner restart handoff for ordinary listening."]
props = ["a freshly labeled tape box near the console"]
"""


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def test_missing_manifest_is_noop(tmp_path: Path) -> None:
    result = _run(tmp_path)

    assert result.returncode == 0
    assert "no manifest" in result.stdout


def test_disabled_manifest_is_noop_even_without_pyproject(tmp_path: Path) -> None:
    _write(tmp_path / MANIFEST, "[release_beat]\nenabled = false\n")

    result = _run(tmp_path, "--channel", "edge", "--target-sha", "4a15270")

    assert result.returncode == 0
    assert "disabled" in result.stdout


def test_edge_target_accepts_full_manifest_sha_with_short_selected_target(tmp_path: Path) -> None:
    _write_pyproject(tmp_path)
    _write(tmp_path / MANIFEST, _edge_manifest())

    result = _run(tmp_path, "--channel", "edge", "--target-sha", "4a15270")

    assert result.returncode == 0, result.stderr
    assert "edge manifest OK" in result.stdout


def test_edge_target_mismatch_fails(tmp_path: Path) -> None:
    _write_pyproject(tmp_path)
    _write(tmp_path / MANIFEST, _edge_manifest())

    result = _run(tmp_path, "--channel", "edge", "--target-sha", "1111111")

    assert result.returncode == 1
    assert "does not match selected edge target" in result.stderr


def test_stable_target_semver_mismatch_fails(tmp_path: Path) -> None:
    _write_pyproject(tmp_path)
    _write(tmp_path / MANIFEST, _stable_manifest(semver="2.15.0"))

    result = _run(tmp_path, "--channel", "stable", "--semver", "2.16.0")

    assert result.returncode == 1
    assert "does not match stable release" in result.stderr


def test_enabled_manifest_requires_package_data_coverage(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, package_data='"web/templates/*.html"')
    _write(tmp_path / MANIFEST, _edge_manifest())

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "package-data" in result.stderr
    assert "assets/release/release_beat.toml" in result.stderr


def test_listener_unsafe_release_terms_fail_by_default(tmp_path: Path) -> None:
    _write_pyproject(tmp_path)
    _write(
        tmp_path / MANIFEST,
        _edge_manifest(facts='"This GitHub PR ships a new dependency version for the station."'),
    )

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "listener-unsafe term" in result.stderr
    assert "github" in result.stderr.lower()
    assert "dependency" in result.stderr.lower()


def test_listener_safe_terms_must_be_explicit(tmp_path: Path) -> None:
    _write_pyproject(tmp_path)
    _write(
        tmp_path / MANIFEST,
        _edge_manifest(
            facts='"The version has a new studio crate for the hosts."',
            extra='listener_safe_terms = ["version"]',
        ),
    )

    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr


def test_facts_and_props_must_be_lists(tmp_path: Path) -> None:
    _write_pyproject(tmp_path)
    _write(
        tmp_path / MANIFEST,
        """
[release_beat]
id = "edge-4a15270-hans-guenther"
channel = "edge"
build_sha = "4a15270"
facts = "Hans Guenther can now wait in the studio hallway."
props = ["a human-sized crate labeled HANS GUENTHER"]
""",
    )

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "release_beat.facts must be a list of strings" in result.stderr


def test_release_id_must_not_be_only_the_target_token(tmp_path: Path) -> None:
    _write_pyproject(tmp_path)
    _write(tmp_path / MANIFEST, _edge_manifest(beat_id="4a15270", build_sha="4a15270"))

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "must be globally unique" in result.stderr


def test_release_scripts_call_target_aware_validator() -> None:
    check_invariants = (ROOT / "scripts/check-release-invariants.sh").read_text()
    cut_edge = (ROOT / "scripts/cut-edge-release.sh").read_text()
    pre_release = (ROOT / "scripts/pre-release-check.sh").read_text()

    assert 'python3 "$SCRIPT_DIR/validate-release-beat.py"' in check_invariants
    assert 'python3 scripts/validate-release-beat.py --channel edge --target-sha "$SHA"' in cut_edge
    assert 'python3 "$SCRIPT_DIR/validate-release-beat.py" --channel stable --semver "$ADDON_VER"' in pre_release
