"""Static contract tests for the HACS media-source glue.

Home Assistant is not installed in the repo test environment, so these tests pin
that the integration ships the media_source entry point and resolves the same
stream path advertised by the now-playing contract.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MEDIA_SOURCE = ROOT / "custom_components" / "mammamiradio" / "media_source.py"
CONST = ROOT / "custom_components" / "mammamiradio" / "const.py"
DOC = ROOT / "docs" / "integrations" / "ha-integration.md"


def test_hacs_integration_exposes_media_source_entry_point() -> None:
    source = MEDIA_SOURCE.read_text(encoding="utf-8")
    assert "async_get_media_source" in source
    assert "MediaSource" in source
    assert "media-source://mammamiradio/live" in source
    assert 'PlayMedia(self._stream_url(), "audio/mpeg")' in source


def test_media_source_uses_shared_stream_path_constant() -> None:
    const = CONST.read_text(encoding="utf-8")
    source = MEDIA_SOURCE.read_text(encoding="utf-8")
    assert 'STREAM_PATH = "/stream"' in const
    assert "from .const import DOMAIN, STREAM_PATH" in source
    assert 'return f"http://{host}:{port}{STREAM_PATH}"' in source


def test_ha_docs_no_longer_defer_media_source() -> None:
    doc = DOC.read_text(encoding="utf-8")
    assert "media-source://mammamiradio/live" in doc
    assert "Follow Me Music" in doc
    assert "`media_source.py` (casting the stream to other HA speakers)" not in doc
