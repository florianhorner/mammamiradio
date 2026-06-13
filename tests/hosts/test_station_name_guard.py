"""Unit tests for the station-name illusion guard.

Two behaviours, one detection vocabulary: spoken text *replaces* a foreign
station name with ours; now-playing metadata *strips* it so the caller falls
back (a song's artist is never "Radio X").
"""

from mammamiradio.hosts.station_name_guard import (
    sanitize_spoken_station_name,
    strip_foreign_station_name,
)

STATION = "Mamma Mi Radio"


# ---- strip_foreign_station_name (metadata fields) ----------------------------


def test_strip_drops_whole_foreign_station_name():
    # The exact production incident: a poisoned sidecar artist.
    assert strip_foreign_station_name("Radio Sabrina Sensatione", STATION) == ""


def test_strip_keeps_song_when_foreign_name_is_a_leading_prefix():
    # The rescue display form "Artist - Title" where the artist is a station.
    assert strip_foreign_station_name("Radio Sabrina Sensatione – Be Without U", STATION) == "Be Without U"


def test_strip_leaves_ordinary_single_token_band():
    # "Radiohead" is one token — no following Title-Case word, so not a station.
    assert strip_foreign_station_name("Radiohead", STATION) == "Radiohead"


def test_strip_leaves_band_that_does_not_start_with_radio():
    # "The Radio Dept." does not begin with "Radio" → never treated as a station.
    assert strip_foreign_station_name("The Radio Dept.", STATION) == "The Radio Dept."


def test_strip_leaves_normal_artist():
    assert strip_foreign_station_name("Neon NiteClub", STATION) == "Neon NiteClub"
    assert strip_foreign_station_name("Jonathan Dimmel", STATION) == "Jonathan Dimmel"


def test_strip_keeps_our_own_station_name():
    ours = "Radio PenthouseFlo FM"
    assert strip_foreign_station_name(ours, ours) == ours


def test_strip_handles_empty_and_none():
    assert strip_foreign_station_name("", STATION) == ""
    assert strip_foreign_station_name(None, STATION) == ""


def test_strip_prefix_only_never_blanks_a_real_radio_titled_song():
    # Title field: a real song literally named "Radio X" must survive (prefix_only
    # skips the whole-value match that would blank it). Blanking the now-playing
    # title is itself an illusion break.
    for title in ("Radio Ga Ga", "Radio Free Europe", "Radio Nowhere"):
        assert strip_foreign_station_name(title, STATION, prefix_only=True) == title


def test_strip_prefix_only_still_strips_rescue_display_prefix():
    # But the rescue display form "Foreign - Song" still loses the foreign prefix.
    assert (
        strip_foreign_station_name("Radio Sabrina Sensatione – Be Without U", STATION, prefix_only=True)
        == "Be Without U"
    )


# ---- sanitize_spoken_station_name (script text) ------------------------------


def test_spoken_replaces_competitor_radio_name():
    out = sanitize_spoken_station_name("Radio Kiss Kiss vi dà il benvenuto.", STATION)
    assert "Kiss Kiss" not in out
    assert STATION in out


def test_spoken_replaces_siamo_su_competitor():
    out = sanitize_spoken_station_name("Siamo su Radio Deejay Milano e si balla!", STATION)
    assert "Deejay" not in out
    assert STATION in out


def test_spoken_keeps_our_station_name_untouched():
    text = f"Siamo su {STATION} sempre!"
    assert sanitize_spoken_station_name(text, STATION) == text


def test_spoken_leaves_text_without_station_name():
    text = "E adesso la musica."
    assert sanitize_spoken_station_name(text, STATION) == text
