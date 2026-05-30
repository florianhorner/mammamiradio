"""MPEG-1 Layer III frame-header parsing for the live audio path.

Extracted verbatim from ``web/streamer.py`` (god-module split). The playback
loop calls :func:`_skip_id3_and_xing_header` on every segment so concatenated
MP3s look like one continuous ICEcast feed — see the function docstring for the
Safari ``<audio>`` cutoff bug this defends against. Pure byte/integer logic, no
imports, no module state.
"""

from __future__ import annotations

_MPEG1_L3_BITRATES_KBPS = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_MPEG1_SAMPLE_RATES = (44100, 48000, 32000)


def _is_mpeg1_l3_header(frame_header: bytes, *, allow_free_bitrate: bool) -> bool:
    """Return whether ``frame_header`` is a plausible MPEG-1 Layer III frame."""
    if len(frame_header) < 4 or frame_header[0] != 0xFF or (frame_header[1] & 0xE0) != 0xE0:
        return False

    version = (frame_header[1] >> 3) & 0x03
    layer = (frame_header[1] >> 1) & 0x03
    bitrate_idx = (frame_header[2] >> 4) & 0x0F
    sample_rate_idx = (frame_header[2] >> 2) & 0x03

    if version != 3 or layer != 1 or sample_rate_idx == 3 or bitrate_idx == 0x0F:
        return False
    return not (not allow_free_bitrate and bitrate_idx == 0)


def _skip_id3_and_xing_header(f) -> None:
    """Advance the file pointer past any leading ID3v2 tag and Xing/Info metadata frame.

    Safari's ``<audio>`` element honors the Xing/Info duration header of each
    concatenated segment as end-of-track, causing short segments (banter ~9 s,
    news flash ~6 s) to fire ``ended`` at the declared duration instead of
    playing through the ongoing stream. Long music segments (180 s+) don't
    trip this because the listener tops up buffered bytes before the counter
    expires. Stripping the tag on every segment makes the stream look like a
    continuous ICECast feed, which all browsers handle correctly.

    The helper is defensive: any unexpected header shape rewinds to the start,
    so the worst case is "did nothing" rather than "cut a real audio frame".
    """
    header = f.read(10)
    if len(header) == 10 and header[:3] == b"ID3":
        size = ((header[6] & 0x7F) << 21) | ((header[7] & 0x7F) << 14) | ((header[8] & 0x7F) << 7) | (header[9] & 0x7F)
        f.seek(10 + size)
    else:
        f.seek(0)

    frame_start = f.tell()
    frame_header = f.read(4)
    if not _is_mpeg1_l3_header(frame_header, allow_free_bitrate=True):
        f.seek(frame_start)
        return

    bitrate_idx = (frame_header[2] >> 4) & 0x0F
    sample_rate_idx = (frame_header[2] >> 2) & 0x03
    padding = (frame_header[2] >> 1) & 0x01
    channel_mode = (frame_header[3] >> 6) & 0x03

    magic_offset = 21 if channel_mode == 3 else 36
    f.seek(frame_start + magic_offset)
    magic = f.read(4)
    if magic not in (b"Xing", b"Info"):
        f.seek(frame_start)
        return

    if bitrate_idx == 0:
        # VBR info frame (free-format): frame_length is unknown from the header alone.
        # Scan forward from just after the Xing magic and only accept plausible
        # MPEG-1 Layer III headers so sync-like metadata bytes are ignored.
        f.seek(frame_start + magic_offset + 4)
        data = f.read(8192)
        sync_pos = -1
        for i in range(len(data) - 3):
            if _is_mpeg1_l3_header(data[i : i + 4], allow_free_bitrate=False):
                sync_pos = i
                break
        if sync_pos >= 0:
            f.seek(frame_start + magic_offset + 4 + sync_pos)
        else:
            f.seek(frame_start)
        return

    bitrate_kbps = _MPEG1_L3_BITRATES_KBPS[bitrate_idx]
    sample_rate = _MPEG1_SAMPLE_RATES[sample_rate_idx]
    frame_length = (144 * bitrate_kbps * 1000 // sample_rate) + padding
    f.seek(frame_start + frame_length)
