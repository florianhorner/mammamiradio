"""First Listen mini-show eligibility and package contracts."""

from __future__ import annotations

import json
import shutil
from types import SimpleNamespace

import pytest

from mammamiradio.core.first_listen import (
    FirstListenInstallOriginStatus,
    FirstListenInstallOriginV1,
    FirstListenReceiptLoadStatus,
    FirstListenReceiptV1,
)
from mammamiradio.core.first_listen_show import (
    FIRST_LISTEN_SHOW_CHUNK_BYTES,
    approved_first_listen_show_path,
    first_listen_show_required,
    iter_first_listen_show_chunks,
)


def _state(
    origin: FirstListenInstallOriginStatus,
    *,
    cold: bool = False,
    heard: bool = False,
    stopped: bool = False,
    load_status: FirstListenReceiptLoadStatus = FirstListenReceiptLoadStatus.MISSING,
):
    receipt = (
        FirstListenReceiptV1(heard_at=100.0)
        if heard
        else FirstListenReceiptV1()
        if load_status is FirstListenReceiptLoadStatus.PRESENT
        else None
    )
    return SimpleNamespace(
        station_state=SimpleNamespace(session_stopped=stopped),
        first_listen_cold_install=cold,
        first_listen_install_origin=FirstListenInstallOriginV1(origin),
        first_listen_receipt=receipt,
        first_listen_receipt_load_status=load_status,
    )


@pytest.mark.parametrize("english", [False, True])
def test_shipped_first_listen_show_is_reviewed_nontrivial_mp3(english) -> None:
    path = approved_first_listen_show_path(english=english)

    assert path is not None
    assert path.name == ("first_listen_admin_show.mp3" if english else "first_listen_show.mp3")
    assert path.stat().st_size > 100_000
    assert path.read_bytes()[:3] in {b"ID3", b"\xff\xfb", b"\xff\xf3"}


def test_only_fresh_unfinished_running_install_gets_show() -> None:
    assert first_listen_show_required(_state(FirstListenInstallOriginStatus.FRESH)) is True
    assert first_listen_show_required(_state(FirstListenInstallOriginStatus.UNKNOWN, cold=True)) is True
    assert first_listen_show_required(_state(FirstListenInstallOriginStatus.EXISTING)) is False
    assert first_listen_show_required(_state(FirstListenInstallOriginStatus.UNKNOWN)) is False
    assert first_listen_show_required(_state(FirstListenInstallOriginStatus.FRESH, heard=True)) is False
    assert first_listen_show_required(_state(FirstListenInstallOriginStatus.FRESH, stopped=True)) is False


@pytest.mark.parametrize(
    ("load_status", "heard", "expected"),
    [
        (FirstListenReceiptLoadStatus.PENDING, False, False),
        (FirstListenReceiptLoadStatus.MISSING, False, True),
        (FirstListenReceiptLoadStatus.PRESENT, False, True),
        (FirstListenReceiptLoadStatus.PRESENT, True, False),
        (FirstListenReceiptLoadStatus.UNAVAILABLE, False, False),
    ],
)
def test_later_boot_requires_a_trusted_receipt_load_outcome(load_status, heard, expected) -> None:
    state = _state(FirstListenInstallOriginStatus.FRESH, heard=heard, load_status=load_status)

    assert first_listen_show_required(state) is expected


@pytest.mark.parametrize(
    ("load_status", "expected"),
    [
        (FirstListenReceiptLoadStatus.PENDING, True),
        (FirstListenReceiptLoadStatus.MISSING, True),
        (FirstListenReceiptLoadStatus.PRESENT, True),
        (FirstListenReceiptLoadStatus.UNAVAILABLE, False),
    ],
)
def test_cold_install_bypasses_only_trusted_or_pending_receipt_bootstrap(load_status, expected) -> None:
    incomplete = _state(
        FirstListenInstallOriginStatus.UNKNOWN,
        cold=True,
        load_status=load_status,
    )
    completed = _state(
        FirstListenInstallOriginStatus.UNKNOWN,
        cold=True,
        heard=True,
        load_status=load_status,
    )

    assert first_listen_show_required(incomplete) is expected
    assert first_listen_show_required(completed) is False


def test_show_chunks_reassemble_asset_and_are_bounded() -> None:
    path = approved_first_listen_show_path()
    assert path is not None

    chunks = list(iter_first_listen_show_chunks(path))

    assert len(chunks) > 1
    assert all(0 < len(chunk) <= FIRST_LISTEN_SHOW_CHUNK_BYTES for chunk in chunks)
    assert b"".join(chunks) == path.read_bytes()


def test_show_chunk_size_must_be_positive(tmp_path) -> None:
    path = tmp_path / "show.mp3"
    path.write_bytes(b"audio")

    with pytest.raises(ValueError, match="positive"):
        list(iter_first_listen_show_chunks(path, chunk_bytes=0))


@pytest.mark.parametrize("damage", ["file", "entry", "hash", "manifest"])
def test_rejected_english_opening_never_substitutes_italian(tmp_path, damage):
    original = approved_first_listen_show_path(english=True)
    assert original is not None
    shutil.copytree(original.parents[1], tmp_path / "demo")
    root = tmp_path / "demo"
    clip = root / "first_listen" / original.name
    manifest_path = root / "spoken_assets.json"
    if damage == "file":
        clip.unlink()
    elif damage == "hash":
        clip.write_bytes(b"unapproved replacement")
    elif damage == "manifest":
        manifest_path.write_text("broken JSON")
    else:
        manifest = json.loads(manifest_path.read_text())
        manifest["assets"] = [entry for entry in manifest["assets"] if not entry["path"].endswith(original.name)]
        manifest_path.write_text(json.dumps(manifest))
    assert approved_first_listen_show_path(assets_root=root, english=True) is None
    if damage != "manifest":
        assert approved_first_listen_show_path(assets_root=root).name == "first_listen_show.mp3"
