"""Regression guards for the live Record Hunt reset lifecycle."""

from pathlib import Path

ADMIN_HTML = Path(__file__).parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"


def _html() -> str:
    return ADMIN_HTML.read_text(encoding="utf-8")


def test_playlist_render_signature_tracks_hunt_identity_and_active_state() -> None:
    """Back to auto must invalidate the cached playlist render.

    Playlist rows carry Hunt-pick styling based on the current heading, while
    the playlist revision can stay unchanged when the heading is cleared. The
    signature must include both pieces of heading state so the next status poll
    removes stale matches without requiring a page reload.
    """

    html = _html()
    block = html[html.index("function playlistRenderSignature") : html.index("function sourceControlVisibility")]

    assert "const heading=_st?.heading||{};" in block
    assert "heading.active?'1':'0'" in block
    assert "heading.id||''" in block
