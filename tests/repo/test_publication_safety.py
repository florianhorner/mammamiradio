"""Public examples and publication text must not copy installation identity."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _docs_check(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(ROOT / "scripts/check-docs-safety.sh"), str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "body",
    [
        "Preship V2 comes before HA runs.",
        "Finalize version, changelogs, and the V2 preship receipt\nbefore recording.",
        "Refresh the V2 review receipt before landing.",
        "Commit the V2 review receipt before landing.",
        "Run scripts/emit-review-evidence.sh before recording.",
        "Run `scripts/emit-review-evidence.sh` before recording.",
        "1. Run `scripts/emit-review-evidence.sh` before recording.",
        "The V2 review-receipt is required before release.",
        "The V2 pre-ship review receipt is required before release.",
        "A V2 preship receipt is required before release.",
        "V2 review receipts are mandatory before release.",
        "V2 review receipts are needed before release.",
        "V2 review receipts are necessary before release.",
        "V2 review receipts are obligatory before release.",
        "V2 review receipts are compulsory before release.",
        "V2 review receipts are essential before release.",
        "V2 admission is retired; however, restore the V2 receipt before release.",
        "V2 review receipts aren't required; however, refresh the V2 review receipt before release.",
        "You must no longer refresh V2 review receipts, create a V2 review receipt instead.",
        "V2 review receipts must no longer be refreshed, but a V2 review receipt is required before release.",
        "You must no longer refresh V2 review receipts and must create a V2 review receipt before release.",
        "You must no longer refresh V2 review receipts and a new V2 review receipt is required before release.",
        "You must no longer refresh V2 review receipts and a new V2 review receipt is still required before release.",
        "You must no longer refresh V2 review receipts and should create a V2 review receipt before release.",
        "| Gate | Requirement |\n|---|---|\n| V2 preship receipt | Required before HA runs |",
        "```bash\nscripts/emit-review-evidence.sh --reattest\n```",
        "```bash\nbash -e scripts/emit-review-evidence.sh --reattest\n```",
        "```bash\n/usr/bin/env bash -e ./scripts/emit-review-evidence.sh --reattest\n```",
        "The old release required V2 preship receipts, so finalize a new V2 preship receipt before recording.",
        "The old release required V2 preship receipts, so generate them before recording.",
        "The old release required V2 preship receipts, so commit them before recording.",
    ],
)
def test_release_docs_reject_retired_receipt_requirements(tmp_path: Path, body: str) -> None:
    guide = tmp_path / "release.md"
    guide.write_text(body)
    result = _docs_check(guide)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "retired review-receipt requirement" in result.stdout


@pytest.mark.parametrize(
    "body",
    [
        "Historical V2 preship receipts remain readable with the standalone verifier.",
        "Historical V2 preship receipts name the reviewed commit.",
        "V2 preship receipts were required before recording.",
        "V2 preship receipts are no longer required for release.",
        "V2 review receipts must no longer be refreshed before release.",
        "You must no longer refresh V2 review receipts before release.",
        "V2 review receipts must no longer be required before release.",
        "V2 review receipts should no longer be required before release.",
        "V2 review receipts mustn't be refreshed before release.",
        "You must no longer refresh V2 review receipts and must not create a V2 review receipt.",
        "Do not create V2 review receipts or treat them as required for release.",
        "Do not create V2 review receipts or claim they are required for release.",
        "Do not create V2 review receipts or claim they must be created before release.",
        "You must no longer refresh V2 review receipts and should not create a V2 review receipt.",
        "A V2 review receipt isn't required before release.",
        "A V2 review receipt isn’t required before release.",
        "V2 review receipts aren't required before release.",
        "V2 review receipts aren’t required before release.",
        "V2 review receipts are no longer mandatory before release.",
        "V2 review receipts aren't needed before release.",
        "V2 review receipts were mandatory before release.",
        "V2 review receipts are no longer necessary before release.",
        "V2 review receipts were obligatory before release.",
        "V2 review receipts aren't compulsory before release.",
        "V2 review receipts were essential before release.",
        "No V2 preship receipt is required before recording.",
        "Do not emit or refresh V2 receipts before HA runs.",
        "Do not run `scripts/emit-review-evidence.sh` before HA runs.",
        "The old release required V2 preship receipts. That admission is retired.",
        "The old release required V2 preship receipts, and those receipts remain readable.",
        "The old release required V2 preship receipts, so run the current release check instead.",
        "HA Green receipts are required when the physical evidence gate is armed.",
        "Save the First Listen privacy-review receipt before completing setup.",
        "Run scripts/check-preship-evidence.sh to read historical receipts.",
    ],
)
def test_release_docs_preserve_history_and_other_receipts(tmp_path: Path, body: str) -> None:
    guide = tmp_path / "release.md"
    guide.write_text(body)
    result = _docs_check(guide)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("domain", ["person", "device_tracker", "lock"])
def test_public_examples_require_synthetic_identity(tmp_path: Path, domain: str) -> None:
    guide = tmp_path / "example.md"
    canary = f"{domain}.recorded_household_value"
    guide.write_text(f"Use `{canary}` in this example.")
    result = _docs_check(guide)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "non-synthetic household identifier" in result.stdout
    assert canary not in result.stdout + result.stderr

    guide.write_text(f"Use `{domain}.example_resident` in this example.")
    result = _docs_check(guide)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("surface", ["pr", "issue", "changelog"])
@pytest.mark.parametrize(
    "body",
    [
        "<!-- conductor-workspace-link -->",
        "<!-- CONDUCTOR-WORKSPACE-LINK -->",
        "See [workspace](https://app.conductor.build/workspace/private-canary-123).",
        "See [workspace](https://APP.CONDUCTOR.BUILD/workspace/private-canary-123).",
        "Release notes.\n\n[proof]: https://app.conductor.build/workspace/private-canary-123",
        "https://app.conductor.build/workspace?name=private-canary-123",
        "conductor://prompt=private-canary-123&path=/Users/example/private-canary-123",
        "See [workspace](conductor://prompt=private-canary-123&path=%2FUsers%2Fexample).",
        "Release notes.\n\n[proof]: conductor://prompt=private-canary-123&path=%2FUsers%2Fexample",
        "ConDuCtOr://prompt=private-canary-123&path=%2FUsers%2Fexample",
    ],
)
def test_publication_checks_reject_workspace_metadata_without_echoing_it(
    tmp_path: Path, surface: str, body: str
) -> None:
    body_path = tmp_path / "body.md"
    body_path.write_text(body)
    if surface == "changelog":
        (tmp_path / "CHANGELOG.md").write_text(body)
        addon = tmp_path / "ha-addon/mammamiradio"
        addon.mkdir(parents=True)
        (addon / "CHANGELOG.md").write_text("# Release notes\n")
        command = ["bash", str(ROOT / "scripts/check-changelog-lint.sh")]
    else:
        command = ["bash", str(ROOT / f"scripts/check-{surface}-body-lint.sh"), str(body_path)]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "private-canary-123" not in result.stdout + result.stderr


@pytest.mark.parametrize("surface", ["pr", "issue"])
def test_publication_checks_preserve_public_links_and_attribution(tmp_path: Path, surface: str) -> None:
    body = tmp_path / "body.md"
    body.write_text(
        "See https://conductor.build/docs/reference/settings.\n"
        "API guide: https://app.conductor.build/workspace-api.\n"
        "Music credit: Example Artist.\n"
        "Co-authored-by: Example Contributor <contributor@example.com>\n"
    )
    result = subprocess.run(
        ["bash", str(ROOT / f"scripts/check-{surface}-body-lint.sh"), str(body)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("path", "poison"),
    [
        ("docs/runbooks/ha-addon.md", "Refresh the V2 preship receipt before recording."),
        ("docs/release-process.md", "Preship V2 comes before HA runs."),
        ("docs/music-sources.md", "Finalize the V2 preship receipt before recording."),
        ("scripts/showreel/README.md", "Use lock.recorded_household_value."),
        ("scripts/showreel_out/door-bentornato-fable-v2-notes.md", "Use person.recorded_household_value."),
        ("scripts/showreel_out/ma-pr-3836-notes.md", "Use device_tracker.recorded_household_value."),
    ],
)
def test_default_docs_check_covers_each_publication_surface(tmp_path: Path, path: str, poison: str) -> None:
    files = [
        "CLAUDE.md",
        "README.md",
        "CONTRIBUTING.md",
        "ha-addon/README.md",
        "ha-addon/mammamiradio/README.md",
        "ha-addon/mammamiradio-edge/README.md",
        "ha-addon/mammamiradio/DOCS.md",
        "docs/REPO_MAP.md",
        "docs/agents.md",
        "docs/architecture.md",
        "docs/conductor.md",
        "docs/festival-mode.md",
        "docs/listener-qs-train.md",
        "docs/troubleshooting.md",
        "docs/operations.md",
        "docs/runbooks/parallel-workspaces.md",
        "docs/runbooks/ha-addon.md",
        "docs/release-process.md",
        "docs/music-sources.md",
        "scripts/showreel/README.md",
        "scripts/showreel_out/door-bentornato-fable-v2-notes.md",
        "scripts/showreel_out/ma-pr-3836-notes.md",
    ]
    for name in files:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Safe\n")
    for script in ("check-docs-safety.sh", "lint-patterns.sh", "docs_safety.py", "public_tree_safety.py"):
        shutil.copy2(ROOT / "scripts" / script, tmp_path / "scripts" / script)
    (tmp_path / "scripts/public-evidence-retention.json").write_text("{}")
    (tmp_path / path).write_text(poison)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    result = subprocess.run(
        ["bash", str(tmp_path / "scripts/check-docs-safety.sh")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert path in result.stdout
    assert "missing" not in result.stdout


@pytest.mark.parametrize("name", ["sample.json", "sample.svg", "Dockerfile", "sample.py"])
def test_tracked_scanner_covers_text_surfaces_and_redacts(tmp_path: Path, name: str) -> None:
    from scripts.public_tree_safety import scan_repository

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/public-evidence-retention.json").write_text("{}")
    poison = "person." + "recorded_household_value"
    (tmp_path / name).write_text(f'"{poison}"')
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    (tmp_path / "untracked.md").write_text(poison)
    violations = scan_repository(tmp_path)
    assert [(v.path, v.line, v.rule) for v in violations] == [(name, 1, "non-synthetic-entity")]
    assert poison not in violations[0].diagnostic()


def test_scanner_decodes_python_and_url_identifiers() -> None:
    from scripts.public_tree_safety import scan_text

    poison = "recorded_household_value"
    for body in (f'x = "person.{poison}"', f'x = "person." "{poison}"', f'x = "person%2E{poison}"'):
        assert any(v.rule == "non-synthetic-entity" for v in scan_text("example.py", body))
    assert not scan_text("example.py", "lock." + 'acquire()\nx = "lock.example_entry"\n')
    username = "fixture" + "owner"
    assert scan_text("example.py", f'x = "\\x2fUsers\\x2f{username}\\x2fprivate"')


def test_scanner_destinations_and_sample_ranges() -> None:
    from scripts.public_tree_safety import scan_text

    host = ".".join(("10", "47", "83", "19"))
    ipv6 = "fd" + "12::123"
    for value in (host, host + "/32", ipv6, ipv6 + "/128"):
        result = scan_text("example.md", value)
        assert [v.rule for v in result] == ["concrete-private-destination"]
        assert value not in result[0].diagnostic()
    assert not scan_text("example.md", "10.0.0.0/8 127.0.0.1 0.0.0.0 192.0.2.1 2001:db8::1 ::1 /Users/example/repo")
    assert scan_text("scripts/first-listen-lab.sh", host)


def test_scanner_retention_requires_bytes_and_presence(tmp_path: Path) -> None:
    import hashlib
    import json

    from scripts.public_tree_safety import scan_repository

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "scripts").mkdir()
    name = "proof/receipt.txt"
    (tmp_path / "proof").mkdir()
    raw = ("person." + "recorded_household_value").encode()
    (tmp_path / name).write_bytes(raw)
    (tmp_path / "scripts/public-evidence-retention.json").write_text(
        json.dumps({name: hashlib.sha256(raw).hexdigest()})
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    assert not scan_repository(tmp_path)
    (tmp_path / name).write_bytes(raw + b"\n")
    assert scan_repository(tmp_path)[0].rule == "immutable-evidence-changed"
    subprocess.run(["git", "-C", str(tmp_path), "rm", "--cached", "-f", name], check=True, capture_output=True)
    (tmp_path / name).unlink()
    assert scan_repository(tmp_path)[0].rule == "immutable-evidence-untracked"


def test_scanner_git_inventory_and_symlink_targets(tmp_path: Path) -> None:
    from scripts.public_tree_safety import scan_repository

    with pytest.raises(ValueError):
        scan_repository(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/public-evidence-retention.json").write_text("{}")
    target = "/Users/" + "fixtureowner/private"
    (tmp_path / "odd\nname").symlink_to(target)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    result = scan_repository(tmp_path)
    assert len(result) == 1
    assert "\\n" in result[0].diagnostic()
    assert target not in result[0].diagnostic()


def test_install_pattern_is_portable_and_has_no_stderr() -> None:
    patterns = subprocess.run(
        ["bash", "-c", 'source scripts/lint-patterns.sh; printf "%s\\n" "${DOCS_RETIRED_INSTALL_PATTERNS[@]}"'],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    binaries = [Path("/usr/bin/grep"), Path("/opt/homebrew/opt/grep/libexec/gnubin/grep")]
    for binary in binaries:
        if not binary.exists():
            continue
        for pattern in patterns:
            for sample in (
                "Settings → Apps → App store → Repositories",
                "Settings → Apps → Install app → ⋮ → Repositories",
            ):
                result = subprocess.run([str(binary), "-iE", pattern], input=sample, capture_output=True, text=True)
                assert result.stderr == ""
                assert result.returncode in {0, 1}
        result = subprocess.run(
            [str(binary), "-iE", patterns[-1]], input="Apps → App store", capture_output=True, text=True
        )
        assert result.returncode == 0 and result.stderr == ""


def test_release_docs_preserve_install_and_home_promises() -> None:
    for name in ("README.md", "ha-addon/README.md"):
        assert "Settings → Apps → Install app → ⋮ → Repositories" in (ROOT / name).read_text()
    docs = (ROOT / "ha-addon/mammamiradio/DOCS.md").read_text()
    assert "twelve" in docs.lower()
    assert "recorded" in docs.lower()
    assert "not part of 3.0.0" in docs
    assert "daylight" in docs and "preview" in docs and "Keep Home private" in docs
    assert "goes silent because" not in docs
    assert "falls back to stock copy or silence" not in docs
    guide = (ROOT / "docs/integrations/ha-integration.md").read_text()
    assert "2.10" in guide and "alpha" in guide and "shared Docker network" in guide
    assert "synthetic" in (ROOT / "docs/integrations/now-playing.md").read_text().lower()
