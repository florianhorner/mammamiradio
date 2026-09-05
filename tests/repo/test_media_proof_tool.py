from __future__ import annotations

import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "media-proof.py"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("repo_media_proof_tool", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_sdist(path: Path, files: dict[str, bytes], *, symlink: bool = False) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for directory in (
            "mammamiradio-1.0/mammamiradio/assets/starter/tracks",
            "mammamiradio-1.0/mammamiradio/assets/starter/evidence",
        ):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
        if symlink:
            link = tarfile.TarInfo("mammamiradio-1.0/mammamiradio/assets/starter/catalog-link.json")
            link.type = tarfile.SYMTYPE
            link.linkname = "catalog.json"
            archive.addfile(link)
        for name, payload in files.items():
            info = tarfile.TarInfo(f"mammamiradio-1.0/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def test_sdist_reader_skips_directory_members(tmp_path: Path, tool) -> None:
    expected = {
        "mammamiradio/assets/starter/catalog.json": b"{}",
        "mammamiradio/assets/starter/tracks/example.mp3": b"mp3",
    }
    sdist = tmp_path / "example.tar.gz"
    _write_sdist(sdist, expected)

    assert tool._sdist_starter_files(sdist) == expected


def test_sdist_reader_rejects_symlink_starter_members(tmp_path: Path, tool) -> None:
    sdist = tmp_path / "example.tar.gz"
    _write_sdist(sdist, {"mammamiradio/assets/starter/catalog.json": b"{}"}, symlink=True)

    with pytest.raises(RuntimeError, match="symlink"):
        tool._sdist_starter_files(sdist)


def test_full_proof_defaults_to_both_images_but_can_target_one_native_arch(
    monkeypatch: pytest.MonkeyPatch, tool
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run_full(*, work_dir: Path, images: dict[str, str | None], image_arches) -> dict[str, object]:
        assert work_dir.is_dir()
        assert tuple(images) == tool.IMAGE_ARCHES
        calls.append(tuple(image_arches))
        return {"exit_code": 0}

    monkeypatch.setattr(tool, "run_full", fake_run_full)
    monkeypatch.setattr(tool, "_print_human", lambda _report: None)

    assert tool.main(["--image-arch", "aarch64", "--aarch64-image", "local:aarch64"]) == 0
    assert tool.main([]) == 0
    assert calls == [("aarch64",), tool.IMAGE_ARCHES]
