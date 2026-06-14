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


def test_strip_relabels_real_two_word_radio_bands_aggressively():
    # Pinned contract: matching is deliberately aggressive (see module note). A
    # real band literally named "Radio <Word>" is also stripped — in the artist
    # field the caller then falls back to the station name. Accepted trade so a
    # foreign improvised station name can never reach the now-playing line.
    for band in ("Radio Birdman", "Radio Futura", "Radio Moscow", "Radio Company"):
        assert strip_foreign_station_name(band, STATION) == ""


def test_strip_drops_lowercased_foreign_station_name():
    # Metadata has no surrounding sentence, so a lowercased foreign name is just
    # as much an illusion break and must still be stripped.
    assert strip_foreign_station_name("radio italia", STATION) == ""
    assert strip_foreign_station_name("radio kiss kiss", STATION) == ""


def test_strip_drops_foreign_name_with_trailing_punctuation():
    # A trailing tail ("!", ".", quotes) must not let the foreign name slip past
    # the whole-value match.
    assert strip_foreign_station_name("Radio Kiss Kiss!", STATION) == ""
    assert strip_foreign_station_name("Radio Deejay.", STATION) == ""


def test_strip_handles_siamo_su_prefix_form():
    # The "siamo su X - Song" rescue prefix is stripped on both modes, matching
    # the spoken vocabulary so the two surfaces never drift apart.
    assert strip_foreign_station_name("siamo su Radio Deejay – Song", STATION, prefix_only=True) == "Song"
    assert strip_foreign_station_name("siamo su Radio Deejay – Song", STATION) == "Song"


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
