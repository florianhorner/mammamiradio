"""Contract tests for the provenance-tag CI guard on the imaging manifest."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts import check_provenance_tags as guard
from scripts import validate_audio_asset_pack as validator

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.test",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.test",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> tuple[Path, list[str]]:
    """A bare remote plus two commits the work clone can tag."""
    bare = tmp_path / "remote.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "--bare", "-q", str(bare))
    _git(tmp_path, "init", "-q", str(work))
    commits = []
    for index in range(2):
        (work / "file.txt").write_text(f"{index}\n", encoding="utf-8")
        _git(work, "add", "file.txt")
        _git(work, "commit", "-q", "-m", f"commit {index}")
        commits.append(_git(work, "rev-parse", "HEAD"))
    _git(work, "remote", "add", "origin", str(bare))
    return bare, commits


def _push_tag(bare: Path, name: str, target: str, *, annotated: bool) -> None:
    work = bare.parent / "work"
    if annotated:
        _git(work, "tag", "-a", name, target, "-m", "keep it fetchable")
    else:
        _git(work, "tag", name, target)
    _git(work, "push", "-q", "origin", f"refs/tags/{name}")


def _manifest(*revisions: str) -> dict[str, object]:
    return {"sources": [{"id": f"s{i}", "generator": {"revision": rev}} for i, rev in enumerate(revisions)]}


def test_tag_name_matches_the_validator() -> None:
    revision = "7a903df26748dbf40b9dd3e0dd6bb524b861491a"
    assert guard.provenance_tag_for(revision) == "provenance/audio-pack-7a903df2"
    assert guard.provenance_tag_for is validator.provenance_tag_for


def test_pinned_revisions_are_distinct_and_well_formed() -> None:
    a, b = "a" * 40, "b" * 40
    manifest = {
        "sources": [
            {"generator": {"revision": a}},
            {"generator": {"revision": a}},
            {"generator": {"revision": "not-a-sha"}},
            {"source_url": "https://example.test/no-generator"},
            "garbage",
            {"generator": {"revision": b}},
        ]
    }
    assert guard.pinned_revisions(manifest) == [a, b]
    assert guard.pinned_revisions({}) == []


def test_parse_ls_remote_peels_annotated_and_keeps_lightweight() -> None:
    output = (
        "1111111111111111111111111111111111111111\trefs/tags/provenance/audio-pack-aaaaaaaa\n"
        "2222222222222222222222222222222222222222\trefs/tags/provenance/audio-pack-aaaaaaaa^{}\n"
        "3333333333333333333333333333333333333333\trefs/tags/provenance/audio-pack-bbbbbbbb\n"
    )
    assert guard.parse_ls_remote(output) == {
        "refs/tags/provenance/audio-pack-aaaaaaaa": "2" * 40,
        "refs/tags/provenance/audio-pack-bbbbbbbb": "3" * 40,
    }


@pytest.mark.parametrize("annotated", [True, False])
def test_passes_when_tag_peels_to_the_pinned_revision(remote: tuple[Path, list[str]], annotated: bool) -> None:
    bare, (first, _second) = remote
    _push_tag(bare, guard.provenance_tag_for(first), first, annotated=annotated)
    assert guard.check_provenance_tags([first], remote=str(bare)) == []


def test_fails_when_tag_is_missing(remote: tuple[Path, list[str]]) -> None:
    bare, (first, _second) = remote
    errors = guard.check_provenance_tags([first], remote=str(bare))
    tag = guard.provenance_tag_for(first)
    assert len(errors) == 1
    assert f"the tag {tag!r} is not on" in errors[0]
    assert f"git push origin refs/tags/{tag}" in errors[0]


def test_fails_when_tag_points_at_a_different_commit(remote: tuple[Path, list[str]]) -> None:
    bare, (first, second) = remote
    tag = guard.provenance_tag_for(first)
    _push_tag(bare, tag, second, annotated=True)
    errors = guard.check_provenance_tags([first], remote=str(bare))
    assert len(errors) == 1
    assert f"points at {second}, not the pinned generator revision {first}" in errors[0]
    assert "never moved" in errors[0]


def test_reports_each_revision_independently(remote: tuple[Path, list[str]]) -> None:
    bare, (first, second) = remote
    _push_tag(bare, guard.provenance_tag_for(first), first, annotated=True)
    errors = guard.check_provenance_tags([first, second], remote=str(bare))
    assert len(errors) == 1
    assert guard.provenance_tag_for(second) in errors[0]


def test_local_tag_does_not_satisfy_the_remote_check(remote: tuple[Path, list[str]]) -> None:
    """A tag that exists only in the clone (or a fork) must not count."""
    bare, (first, _second) = remote
    work = bare.parent / "work"
    _git(work, "tag", "-a", guard.provenance_tag_for(first), first, "-m", "local only")
    assert len(guard.check_provenance_tags([first], remote=str(bare))) == 1


def test_no_revisions_skips_the_remote() -> None:
    def explode(_remote: str, _pattern: str) -> str:
        raise AssertionError("ls-remote must not run without pinned revisions")

    assert guard.check_provenance_tags([], remote="unused", ls_remote=explode) == []


def test_unreachable_remote_is_a_plain_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard.time, "sleep", lambda _seconds: None)
    with pytest.raises(guard.ProvenanceTagError, match=r"could not list tags on .* run the check again"):
        guard.check_provenance_tags(["a" * 40], remote=str(tmp_path / "missing.git"))


def test_main_exit_codes(remote: tuple[Path, list[str]], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bare, (first, _second) = remote
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_manifest(first)), encoding="utf-8")
    args = ["--manifest", str(manifest), "--remote", str(bare)]

    assert guard.main(args) == 1
    assert guard.provenance_tag_for(first) in capsys.readouterr().err

    _push_tag(bare, guard.provenance_tag_for(first), first, annotated=True)
    assert guard.main(args) == 0
    assert "Provenance tags OK: 1 pinned revision(s)" in capsys.readouterr().out

    manifest.write_text("[]", encoding="utf-8")
    assert guard.main(args) == 2
    assert guard.main(["--manifest", str(tmp_path / "absent.json"), "--remote", str(bare)]) == 2


def test_checked_in_manifest_pins_are_well_formed() -> None:
    """The real manifest yields at least one revision, so the CI step is not vacuous."""
    manifest = json.loads(guard.DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    revisions = guard.pinned_revisions(manifest)
    assert revisions
    assert all(validator.GIT_REVISION_RE.fullmatch(revision) for revision in revisions)
