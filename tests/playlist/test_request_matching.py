"""Exact parsing and relevance tests for listener song requests."""

from __future__ import annotations

import pytest

from mammamiradio.playlist.request_matching import (
    SongRequestIdentity,
    clean_candidate_title,
    match_song_request_candidates,
    normalize_match_text,
    parse_song_request,
)


def test_parse_open_ended_artist_request_strips_dedication():
    intent = parse_song_request("Please play something by Lucio Battisti for my mother")

    assert intent is not None
    assert intent.mode == "artist"
    assert intent.artist == "Lucio Battisti"
    assert intent.title == ""
    assert intent.search_query == "Lucio Battisti official audio"


def test_parse_explicit_artist_and_title_request():
    intent = parse_song_request("Play Il mio canto libero by Lucio Battisti")

    assert intent is not None
    assert intent.mode == "artist_title"
    assert intent.artist == "Lucio Battisti"
    assert intent.title == "Il mio canto libero"
    assert intent.search_query == "Lucio Battisti Il mio canto libero official audio"


def test_parse_uses_last_by_credit_and_strips_named_dedication():
    intent = parse_song_request("Play Stand by Me by Ben E. King for Anna")

    assert intent is not None
    assert intent.mode == "artist_title"
    assert (intent.artist, intent.title) == ("Ben E. King", "Stand by Me")


@pytest.mark.parametrize(
    ("message", "artist"),
    [
        ('Play "Stand by Your Man"', ""),
        ('Play "Stand by Your Man" by Tammy Wynette', "Tammy Wynette"),
        ("Play Stand by Your Man", ""),
    ],
)
def test_parse_final_by_title_with_quotes_or_possessive_tail(message, artist):
    intent = parse_song_request(message)

    assert intent is not None
    assert intent.title == "Stand by Your Man"
    assert intent.artist == artist
    assert intent.mode == ("artist_title" if artist else "title")


def test_unquoted_final_by_keeps_metadata_verifiable_full_title_alternative():
    intent = parse_song_request("Play Killed by Death")

    assert intent is not None
    assert (intent.mode, intent.title, intent.artist) == ("artist_title", "Killed", "Death")
    assert intent.alternative_identities[0] == SongRequestIdentity(
        mode="title",
        title="Killed by Death",
        preserve_title_feature_syntax=True,
    )

    result = match_song_request_candidates(
        intent,
        [{"title": "Mot\u00f6rhead - Killed by Death", "artist": "Mot\u00f6rhead"}],
    )

    assert result.best is not None
    assert (result.best.artist, result.best.identity_title) == ("Mot\u00f6rhead", "Killed by Death")


def test_final_by_alternative_strips_dedication_from_parsed_credit():
    intent = parse_song_request("Play Killed by Death for Anna")

    assert intent is not None
    assert intent.artist == "Death"
    assert intent.alternative_identities
    assert intent.alternative_identities[0].title == "Killed by Death"


@pytest.mark.parametrize("message", ['Play "Killed by Death"', 'Play "Killed by Death" for Anna'])
def test_quoted_final_by_title_is_authoritative_without_alternative(message):
    intent = parse_song_request(message)

    assert intent is not None
    assert (intent.mode, intent.title, intent.artist) == ("title", "Killed by Death", "")
    assert not intent.alternative_identities


def test_song_by_song_full_title_resolves_through_alternative_identity():
    intent = parse_song_request("Play Song by Song")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [{"title": "Example Artist - Song by Song", "artist": "Example Artist"}],
    )

    assert result.best is not None
    assert result.best.identity_title == "Song by Song"


def test_primary_by_credit_ranks_before_earlier_full_title_alternative():
    intent = parse_song_request("Play Song by Artist")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [
            {"youtube_id": "alternative", "title": "Other Artist - Song by Artist", "artist": "Other Artist"},
            {"youtube_id": "primary", "title": "Artist - Song", "artist": "Artist"},
        ],
    )

    assert [match.metadata["youtube_id"] for match in result.matches] == ["primary", "alternative"]


def test_parse_italian_artist_and_exact_requests():
    artist = parse_song_request("Metti qualcosa di Lucio Battisti per mia madre")
    exact = parse_song_request("Suona Il mio canto libero di Lucio Battisti")

    assert artist is not None and artist.mode == "artist" and artist.artist == "Lucio Battisti"
    assert exact is not None and exact.mode == "artist_title"
    assert (exact.artist, exact.title) == ("Lucio Battisti", "Il mio canto libero")


def test_parse_title_only_does_not_guess_ambiguous_italian_di_credit():
    intent = parse_song_request("Suona La canzone di Marinella")

    assert intent is not None
    assert intent.mode == "title"
    assert intent.title == "La canzone di Marinella"


def test_parse_italian_single_word_artist_credit():
    intent = parse_song_request("Metti Albachiara di Vasco")

    assert intent is not None
    assert intent.mode == "artist_title"
    assert (intent.artist, intent.title) == ("Vasco", "Albachiara")


@pytest.mark.parametrize(
    "message",
    ["Metti Centro di gravit\u00e0 permanente", "Metti Centro di Gravit\u00e0 Permanente"],
)
def test_parse_italian_title_with_mismatched_di_arity_stays_title_only(message):
    intent = parse_song_request(message)

    assert intent is not None
    assert intent.mode == "title"
    assert intent.title.casefold() == "centro di gravit\u00e0 permanente"
    assert intent.search_query.casefold() == "centro di gravit\u00e0 permanente official audio"


def test_ambiguous_italian_di_identities_both_resolve_from_metadata():
    centro = parse_song_request("Metti Centro di Gravit\u00e0 Permanente")
    futura = parse_song_request("Metti Futura di Lucio Dalla")

    assert centro is not None and centro.alternative_identities
    assert futura is not None and futura.alternative_identities
    assert (centro.mode, centro.title) == ("title", "Centro di Gravit\u00e0 Permanente")
    assert (futura.mode, futura.title) == ("title", "Futura di Lucio Dalla")
    assert (futura.alternative_identities[0].title, futura.alternative_identities[0].artist) == (
        "Futura",
        "Lucio Dalla",
    )

    centro_result = match_song_request_candidates(
        centro,
        [{"title": "Franco Battiato - Centro di gravit\u00e0 permanente", "artist": "Franco Battiato"}],
    )
    futura_result = match_song_request_candidates(
        futura,
        [{"title": "Lucio Dalla - Futura", "artist": "Lucio Dalla"}],
    )

    assert centro_result.best is not None
    assert centro_result.best.identity_title == "Centro di gravit\u00e0 permanente"
    assert futura_result.best is not None
    assert (futura_result.best.identity_artist, futura_result.best.identity_title) == ("Lucio Dalla", "Futura")


def test_quoted_italian_di_title_is_authoritative_without_alternative():
    intent = parse_song_request('Metti "Futura di Lucio Dalla"')

    assert intent is not None
    assert (intent.mode, intent.title) == ("title", "Futura di Lucio Dalla")
    assert not intent.alternative_identities


@pytest.mark.parametrize("message", ["Play Albachiara for Anna", "Metti Albachiara per Anna"])
def test_parse_title_only_named_dedication(message):
    intent = parse_song_request(message)

    assert intent is not None
    assert intent.mode == "title"
    assert intent.title in {"Albachiara for Anna", "Albachiara per Anna"}
    assert intent.alternative_identities[0] == SongRequestIdentity(mode="title", title="Albachiara")

    result = match_song_request_candidates(
        intent,
        [{"title": "Albachiara", "track_artist": "Vasco Rossi"}],
    )

    assert result.best is not None
    assert result.best.identity_title == "Albachiara"


def test_named_dedication_alternative_cannot_outrank_full_title():
    intent = parse_song_request("Play Waiting for You")
    assert intent is not None
    assert intent.title == "Waiting for You"
    assert intent.alternative_identities[0] == SongRequestIdentity(mode="title", title="Waiting")

    result = match_song_request_candidates(
        intent,
        [
            {"youtube_id": "wrong", "title": "Waiting", "track_artist": "Wrong Artist"},
            {"youtube_id": "right", "title": "Waiting for You", "track_artist": "F4"},
        ],
    )

    assert result.best is not None
    assert result.best.metadata["youtube_id"] == "right"


@pytest.mark.parametrize(
    ("message", "full_title", "base_title"),
    [
        ("Can you play Imagine for me?", "Imagine for me", "Imagine"),
        ("Could you put on Imagine for us?", "Imagine for us", "Imagine"),
        ("Mi puoi mettere Albachiara per me?", "Albachiara per me", "Albachiara"),
        ("Mi puoi mettere Albachiara per noi?", "Albachiara per noi", "Albachiara"),
    ],
)
def test_recipient_pronoun_tail_is_candidate_verified_alternative(message, full_title, base_title):
    intent = parse_song_request(message)

    assert intent is not None
    assert intent.title == full_title
    assert intent.alternative_identities[0] == SongRequestIdentity(mode="title", title=base_title)
    match = match_song_request_candidates(intent, [{"title": base_title, "track_artist": "Verified Artist"}]).best
    assert match is not None
    assert match.identity_title == base_title


@pytest.mark.parametrize(
    "message",
    [
        "Per favore, puoi mettere Albachiara?",
        "Cara radio, per favore, puoi mettere Albachiara?",
        "Radio, puoi mettere Albachiara?",
    ],
)
def test_polite_radio_address_punctuation_still_parses_song_command(message):
    intent = parse_song_request(message)

    assert intent is not None
    assert (intent.mode, intent.title) == ("title", "Albachiara")


@pytest.mark.parametrize("separator", [" - ", " – "])
def test_pasted_artist_title_credit_is_an_exact_metadata_interpretation(separator):
    intent = parse_song_request(f"Play Lucio Battisti{separator}Emozioni")

    assert intent is not None
    assert intent.title == f"Lucio Battisti{separator}Emozioni"
    assert intent.alternative_identities[0] == SongRequestIdentity(
        mode="artist_title",
        artist="Lucio Battisti",
        title="Emozioni",
    )
    match = match_song_request_candidates(
        intent,
        [{"title": f"Lucio Battisti{separator}Emozioni", "track_artist": "Lucio Battisti"}],
    ).best
    assert match is not None
    assert (match.station_artist, match.identity_title) == ("Lucio Battisti", "Emozioni")


def test_parse_title_containing_for_before_explicit_artist_credit():
    intent = parse_song_request("Play Song for You by Chicago")

    assert intent is not None
    assert intent.mode == "artist_title"
    assert (intent.artist, intent.title) == ("Chicago", "Song for You")


def test_structured_artist_disproves_dash_as_title_credit():
    full_intent = parse_song_request("Play Bang Bang - My Baby Shot Me Down")
    suffix_intent = parse_song_request("Play My Baby Shot Me Down")
    assert full_intent is not None and suffix_intent is not None
    candidate = {
        "title": "Bang Bang - My Baby Shot Me Down",
        "track_artist": "Nancy Sinatra",
    }

    assert match_song_request_candidates(full_intent, [candidate]).best is not None
    assert match_song_request_candidates(suffix_intent, [candidate]).best is None


def test_structured_artist_confirms_normal_dash_credit():
    intent = parse_song_request("Play Bang Bang")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [{"title": "Nancy Sinatra - Bang Bang", "track_artist": "Nancy Sinatra"}],
    )

    assert result.best is not None
    assert result.best.identity_title == "Bang Bang"


def test_dedication_words_do_not_become_requested_recording_qualifiers():
    intent = parse_song_request("Play Hallelujah by Jeff Buckley for my live show")

    assert intent is not None
    assert (intent.artist, intent.title) == ("Jeff Buckley", "Hallelujah")
    assert intent.requested_qualifiers == frozenset()


def test_parse_multiword_title_ending_in_for_phrase_is_not_shortened():
    intent = parse_song_request("Play This Is for Real")

    assert intent is not None
    assert intent.mode == "title"
    assert intent.title == "This Is for Real"


def test_parse_stand_by_me_without_artist_stays_title_only():
    intent = parse_song_request("Play Stand by Me")

    assert intent is not None
    assert intent.mode == "title"
    assert intent.title == "Stand by Me"


def test_parse_strips_sentence_punctuation_from_identity():
    title = parse_song_request("Puoi mettere Albachiara?")
    exact = parse_song_request("Play Albachiara by Vasco Rossi!")

    assert title is not None and title.title == "Albachiara"
    assert exact is not None and (exact.title, exact.artist) == ("Albachiara", "Vasco Rossi")


@pytest.mark.parametrize(
    ("message", "expected_artist"),
    [
        ("Dear Radio, please play Albachiara by Vasco Rossi", "Vasco Rossi"),
        ("Mi puoi mettere Albachiara di Vasco?", "Vasco"),
    ],
)
def test_parse_conversational_radio_request_prefixes(message, expected_artist):
    intent = parse_song_request(message)

    assert intent is not None
    assert (intent.title, intent.artist) == ("Albachiara", expected_artist)


def test_parse_requires_a_whole_supported_command_not_keyword_substring():
    assert parse_song_request("Open my playlist please") is None
    assert parse_song_request("This display looks strange") is None


def test_normalization_handles_accents_unicode_dashes_and_punctuation():
    assert normalize_match_text("  L\u00daCIO\u2014Batt\u00ecsti! ") == "lucio battisti"


@pytest.mark.parametrize(
    ("punctuated", "compact", "normalized"),
    [
        ("L'Italiano", "LItaliano", "litaliano"),
        ("AC/DC", "ACDC", "acdc"),
        ("Jay-Z", "JayZ", "jayz"),
        ("P.I.M.P.", "PIMP", "pimp"),
        ("L_Italiano", "LItaliano", "litaliano"),
    ],
)
def test_normalization_joins_intra_word_punctuation(punctuated, compact, normalized):
    assert normalize_match_text(punctuated) == normalize_match_text(compact) == normalized


def test_normalization_keeps_spaced_and_typographic_dashes_as_boundaries():
    assert normalize_match_text("Lucio - Battisti") == "lucio battisti"
    assert normalize_match_text("Lucio\u2014Battisti") == "lucio battisti"


def test_open_ended_artist_rejects_incident_candidate_and_keeps_relevant_result():
    intent = parse_song_request("Please play something by Lucio Battisti for my mother")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [
            {
                "youtube_id": "wrong000001",
                "title": "Phoebe Cates - Theme From Paradise LIVE SD (with lyrics) 1982",
                "artist": "Shane Mercury",
            },
            {
                "youtube_id": "right000001",
                "title": "Lucio Battisti - Emozioni (Official Audio) [HD]",
                "artist": "Lucio Battisti - Topic",
            },
        ],
    )

    assert result.matched is True
    assert [match.metadata["youtube_id"] for match in result.matches] == ["right000001"]
    assert (result.best.artist, result.best.title) == ("Lucio Battisti", "Emozioni")


def test_no_exact_artist_evidence_is_low_confidence_not_a_match():
    intent = parse_song_request("Play something by Lucio Battisti")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [{"title": "Il mio canto libero", "artist": "Lucio Battist", "youtube_id": "close000001"}],
    )

    assert result.matched is False
    assert result.best is None
    assert result.failure_reason == "low_confidence"


def test_concatenated_vevo_uploader_is_exact_compact_artist_evidence():
    intent = parse_song_request("Play something by Lucio Battisti")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [{"title": "Emozioni (Official Video)", "artist": "LucioBattistiVEVO"}],
    )

    assert result.best is not None
    assert (result.best.artist, result.best.title) == ("Lucio Battisti", "Emozioni")
    assert (result.best.station_artist, result.best.identity_artist) == ("LucioBattisti", "LucioBattisti")

    oddly_spaced = parse_song_request("Play something by Luci oBattisti")
    assert oddly_spaced is not None
    oddly_spaced_match = match_song_request_candidates(
        oddly_spaced,
        [{"title": "Emozioni (Official Video)", "artist": "LucioBattistiVEVO"}],
    ).best
    assert oddly_spaced_match is not None
    assert oddly_spaced_match.station_artist == result.best.station_artist
    assert oddly_spaced_match.identity_artist == result.best.identity_artist


def test_artist_cleanup_does_not_remove_a_real_music_suffix():
    intent = parse_song_request("Play something by Roxy Music")
    assert intent is not None

    result = match_song_request_candidates(intent, [{"title": "Avalon", "artist": "Roxy Music"}])

    assert result.best is not None
    assert result.best.artist == "Roxy Music"


@pytest.mark.parametrize(
    "uploader",
    ["Lucio Battisti - Topic", "Lucio Battisti Official Channel", "Lucio Battisti Official"],
)
def test_explicit_platform_artist_wrappers_remain_exact_artist_evidence(uploader):
    intent = parse_song_request("Play something by Lucio Battisti")
    assert intent is not None

    match = match_song_request_candidates(intent, [{"title": "Emozioni", "artist": uploader}]).best

    assert match is not None
    assert (match.station_artist, match.identity_artist) == ("Lucio Battisti", "Lucio Battisti")


def test_explicit_request_requires_both_artist_and_title():
    intent = parse_song_request("Play Il mio canto libero by Lucio Battisti")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [
            {"title": "Lucio Battisti - Emozioni", "artist": "Lucio Battisti"},
            {"title": "Il mio canto libero", "artist": "Another Artist"},
        ],
    )

    assert result.failure_reason == "low_confidence"


def test_title_only_uses_title_prefix_as_display_artist():
    intent = parse_song_request("Play Il mio canto libero")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [{"title": "Lucio Battisti - Il mio canto libero (Official Video)", "artist": "BattistiVEVO"}],
    )

    assert result.best is not None
    assert (result.best.artist, result.best.title) == ("Lucio Battisti", "Il mio canto libero")


def test_structured_track_identity_is_strong_evidence():
    intent = parse_song_request("Play Il mio canto libero by Lucio Battisti")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [
            {
                "title": "Provided to YouTube by Distributor",
                "artist": "Distributor Channel",
                "track_title": "Il mio canto libero",
                "track_artist": "Lucio Battisti",
            }
        ],
    )

    assert result.best is not None
    assert (result.best.artist, result.best.title) == ("Lucio Battisti", "Il mio canto libero")


def test_authoritative_structured_artist_rejects_contradictory_title_and_uploader_credit():
    john_lennon = parse_song_request("Play Imagine by John Lennon")
    ariana_grande = parse_song_request("Play Imagine by Ariana Grande")
    assert john_lennon is not None and ariana_grande is not None
    reproduction = {
        "title": "John Lennon - Imagine",
        "track_title": "Imagine",
        "track_artist": "Ariana Grande",
        "uploader": "John Lennon - Topic",
    }

    assert match_song_request_candidates(john_lennon, [reproduction]).best is None
    match = match_song_request_candidates(ariana_grande, [reproduction]).best
    assert match is not None
    assert (match.artist, match.station_artist, match.identity_title) == (
        "Ariana Grande",
        "Ariana Grande",
        "Imagine",
    )


@pytest.mark.parametrize("structured_artist", ["", "Various Artists"])
def test_absent_or_generic_structured_artist_keeps_corroborated_title_prefix_evidence(structured_artist):
    intent = parse_song_request("Play Imagine by John Lennon")
    assert intent is not None
    candidate = {
        "title": "John Lennon - Imagine",
        "track_title": "Imagine",
        "track_artist": structured_artist,
        "uploader": "Generic Distributor",
    }

    match = match_song_request_candidates(intent, [candidate]).best
    assert match is not None
    assert (match.station_artist, match.identity_title) == ("John Lennon", "Imagine")


def test_structured_title_keeps_artist_prefix_from_display_title_as_evidence():
    intent = parse_song_request("Play Emozioni by Lucio Battisti")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [
            {
                "title": "Lucio Battisti - Emozioni",
                "track_title": "Emozioni",
                "artist": "Generic Distributor",
            }
        ],
    )

    assert result.best is not None
    assert (result.best.artist, result.best.title) == ("Lucio Battisti", "Emozioni")


def test_matching_title_prefix_owns_station_identity_over_conflicting_metadata():
    intent = parse_song_request("Play Shallow by Lady Gaga")
    assert intent is not None

    match = match_song_request_candidates(
        intent,
        [
            {
                "title": "Lady Gaga - Shallow",
                "track_artist": "Various Artists",
                "artist": "Generic Channel",
            }
        ],
    ).best

    assert match is not None
    assert (match.artist, match.station_artist, match.identity_artist) == (
        "Lady Gaga",
        "Lady Gaga",
        "Lady Gaga",
    )


@pytest.mark.parametrize(
    "candidate_title",
    [
        "Lady Gaga feat. Bradley Cooper - Shallow",
        "Lady Gaga - Shallow (feat. Bradley Cooper)",
        "Lady Gaga - Shallow - ft. Bradley Cooper",
    ],
)
def test_featured_artist_credits_remain_strong_identity_evidence(candidate_title):
    intent = parse_song_request("Play Shallow by Lady Gaga")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [{"title": candidate_title, "artist": "Generic Channel"}],
    )

    assert result.best is not None
    assert (result.best.station_artist, result.best.identity_title) == ("Lady Gaga", "Shallow")
    assert result.best.identity_artist == "Lady Gaga"


def test_featured_artist_match_carries_every_verified_credit_for_policy_checks():
    intent = parse_song_request("Play Shallow by Bradley Cooper")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [{"title": "Lady Gaga feat. Bradley Cooper - Shallow", "artist": "Generic Channel"}],
    )

    assert result.best is not None
    assert result.best.artist == "Lady Gaga feat. Bradley Cooper"
    assert result.best.station_artist == "Lady Gaga"
    assert result.best.identity_artist == "Bradley Cooper"
    assert result.best.credited_artists == (
        "Lady Gaga feat. Bradley Cooper",
        "Bradley Cooper",
        "Lady Gaga",
    )


@pytest.mark.parametrize(
    "candidate_title",
    [
        "Lady Gaga - Shallow (feat. Bradley Cooper)",
        "Lady Gaga - Shallow - ft. Bradley Cooper",
    ],
)
def test_title_side_feature_credit_is_artist_evidence(candidate_title):
    intent = parse_song_request("Play Shallow by Bradley Cooper")
    assert intent is not None

    match = match_song_request_candidates(
        intent,
        [{"title": candidate_title, "artist": "Generic Channel"}],
    ).best

    assert match is not None
    assert (match.station_artist, match.identity_artist, match.identity_title) == (
        "Lady Gaga",
        "Bradley Cooper",
        "Shallow",
    )
    assert set(match.credited_artists) == {
        "Lady Gaga",
        "Bradley Cooper",
        "Lady Gaga feat. Bradley Cooper",
    }


@pytest.mark.parametrize(
    ("request_text", "candidate_title"),
    [
        ("Play live version of Shallow by Bradley Cooper", "Lady Gaga - Shallow (feat. Bradley Cooper) (Live)"),
        (
            "Play remastered version of Shallow by Bradley Cooper",
            "Lady Gaga - Shallow (feat. Bradley Cooper) (Remastered)",
        ),
    ],
)
def test_title_side_feature_group_survives_a_following_recording_variant(request_text, candidate_title):
    intent = parse_song_request(request_text)
    assert intent is not None

    match = match_song_request_candidates(
        intent,
        [{"title": candidate_title, "artist": "Generic Channel"}],
    ).best

    assert match is not None
    assert (match.station_artist, match.identity_artist, match.identity_title) == (
        "Lady Gaga",
        "Bradley Cooper",
        "Shallow",
    )


@pytest.mark.parametrize(
    "candidate_title",
    [
        "Lady Gaga - Shallow - feat. Bradley Cooper (Club Mix)",
    ],
)
def test_compound_separator_feature_tail_is_not_guessed(candidate_title):
    intent = parse_song_request("Play Shallow by Bradley Cooper")
    assert intent is not None

    result = match_song_request_candidates(intent, [{"title": candidate_title, "artist": "Generic Channel"}])

    assert result.best is None
    assert result.failure_reason == "low_confidence"


def test_title_side_guest_never_uses_unverified_uploader_as_station_artist():
    intent = parse_song_request("Play Shallow by Bradley Cooper")
    assert intent is not None

    match = match_song_request_candidates(
        intent,
        [{"title": "Shallow (feat. Bradley Cooper)", "artist": "Generic Channel"}],
    ).best

    assert match is not None
    assert (match.artist, match.station_artist, match.identity_artist) == (
        "Bradley Cooper",
        "Bradley Cooper",
        "Bradley Cooper",
    )
    assert "Generic Channel" in match.credited_artists


@pytest.mark.parametrize("credit", ["feat. Bradley Cooper", "ft. Bradley Cooper", "featuring Bradley Cooper"])
def test_artist_side_feature_credit_uses_request_independent_station_artist(credit):
    lady_gaga = parse_song_request("Play Shallow by Lady Gaga")
    bradley_cooper = parse_song_request("Play Shallow by Bradley Cooper")
    assert lady_gaga is not None and bradley_cooper is not None
    metadata = [{"title": f"Lady Gaga {credit} - Shallow", "artist": "Generic Channel"}]

    lady_gaga_match = match_song_request_candidates(lady_gaga, metadata).best
    bradley_cooper_match = match_song_request_candidates(bradley_cooper, metadata).best

    assert lady_gaga_match is not None and bradley_cooper_match is not None
    assert (lady_gaga_match.station_artist, lady_gaga_match.identity_title) == ("Lady Gaga", "Shallow")
    assert (bradley_cooper_match.station_artist, bradley_cooper_match.identity_title) == ("Lady Gaga", "Shallow")


def test_feature_credit_layouts_share_one_station_identity():
    intent = parse_song_request("Play Shallow by Lady Gaga")
    assert intent is not None

    matches = [
        match_song_request_candidates(intent, [{"title": title, "artist": "Generic Channel"}]).best
        for title in (
            "Lady Gaga feat. Bradley Cooper - Shallow",
            "Lady Gaga - Shallow (feat. Bradley Cooper)",
        )
    ]

    assert all(match is not None for match in matches)
    assert {(match.station_artist, match.identity_title) for match in matches if match is not None} == {
        ("Lady Gaga", "Shallow")
    }


def test_listener_feature_credit_requires_candidate_backed_guest_evidence():
    intent = parse_song_request("Play Shallow feat. Bradley Cooper by Lady Gaga")
    assert intent is not None
    assert (intent.title, intent.artist, intent.requested_feature_artist) == (
        "Shallow",
        "Lady Gaga",
        "Bradley Cooper",
    )

    result = match_song_request_candidates(
        intent,
        [
            {"youtube_id": "plain", "title": "Lady Gaga - Shallow", "artist": "Lady Gaga"},
            {
                "youtube_id": "wrong-guest",
                "title": "Lady Gaga - Shallow (feat. Tony Bennett)",
                "artist": "Lady Gaga",
            },
            {
                "youtube_id": "title-credit",
                "title": "Lady Gaga - Shallow (feat. Bradley Cooper)",
                "artist": "Lady Gaga",
            },
            {
                "youtube_id": "artist-credit",
                "title": "Shallow",
                "track_artist": "Lady Gaga feat. Bradley Cooper",
            },
        ],
    )

    assert [match.metadata["youtube_id"] for match in result.matches] == [
        "title-credit",
        "artist-credit",
    ]


def test_station_artist_keeps_equal_billing_collaborators_intact():
    intent = parse_song_request("Play Shallow by Lady Gaga & Bradley Cooper")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [{"title": "Lady Gaga & Bradley Cooper - Shallow", "artist": "Generic Channel"}],
    )

    assert result.best is not None
    assert result.best.artist == "Lady Gaga & Bradley Cooper"
    assert result.best.station_artist == "Lady Gaga & Bradley Cooper"


@pytest.mark.parametrize("requested_artist", ["Earth", "Wind", "Fire"])
def test_band_name_connectors_do_not_prove_individual_artist(requested_artist):
    intent = parse_song_request(f"Play September by {requested_artist}")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [{"title": "Earth, Wind & Fire - September", "artist": "Generic Channel"}],
    )

    assert result.best is None
    assert result.failure_reason == "low_confidence"


def test_featured_band_credit_stays_one_artist_identity():
    full_band = parse_song_request("Play Celebration by Earth, Wind & Fire")
    individual = parse_song_request("Play Celebration by Fire")
    assert full_band is not None and individual is not None
    metadata = [{"title": "Taylor Swift feat. Earth, Wind & Fire - Celebration", "artist": "Generic Channel"}]

    full_match = match_song_request_candidates(full_band, metadata).best
    individual_result = match_song_request_candidates(individual, metadata)

    assert full_match is not None
    assert full_match.identity_artist == "Earth, Wind & Fire"
    assert individual_result.best is None
    assert individual_result.failure_reason == "low_confidence"


@pytest.mark.parametrize(
    ("requested_title", "candidate_title"),
    [("L'Italiano", "LItaliano"), ("LItaliano", "L'Italiano")],
)
def test_intra_word_apostrophe_difference_matches_italian_title_identity(requested_title, candidate_title):
    intent = parse_song_request(f"Play {requested_title} by Toto Cutugno")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [{"title": f"Toto Cutugno - {candidate_title}", "artist": "Toto Cutugno"}],
    )

    assert result.best is not None
    assert result.best.identity_title == candidate_title


def test_verified_candidate_artist_spelling_wins_over_listener_spelling():
    intent = parse_song_request("Play Albachiara by V\u00e1sco Rossi")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [{"title": "Vasco Rossi - Albachiara", "artist": "Vasco Rossi"}],
    )

    assert result.best is not None
    assert result.best.artist == "Vasco Rossi"


def test_unrequested_cover_and_karaoke_are_rejected():
    intent = parse_song_request("Play Hallelujah by Jeff Buckley")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [
            {"title": "Jeff Buckley - Hallelujah (cover)", "artist": "Jeff Buckley"},
            {"title": "Jeff Buckley - Hallelujah Karaoke", "artist": "Jeff Buckley"},
            {"title": "Jeff Buckley - Hallelujah nightcore", "artist": "Jeff Buckley"},
        ],
    )

    assert result.failure_reason == "low_confidence"


def test_bare_whitespace_suffix_is_song_identity_not_a_variant_wrapper():
    under = parse_song_request("Play Under by Example Artist")
    under_cover = parse_song_request("Play Under Cover by Example Artist")
    assert under is not None and under_cover is not None
    metadata = [{"title": "Example Artist - Under Cover", "artist": "Example Artist"}]

    wrong_song = match_song_request_candidates(under, metadata)
    exact_song = match_song_request_candidates(under_cover, metadata)

    assert wrong_song.failure_reason == "low_confidence"
    assert exact_song.best is not None
    assert exact_song.best.variant == "standard"
    assert exact_song.best.identity_title == "Under Cover"


def test_explicitly_requested_live_version_is_allowed_and_required():
    intent = parse_song_request("Play live version of Il mio canto libero by Lucio Battisti")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [
            {"title": "Lucio Battisti - Il mio canto libero", "artist": "Lucio Battisti"},
            {"title": "Lucio Battisti - Il mio canto libero (Live)", "artist": "Lucio Battisti"},
        ],
    )

    assert result.best is not None
    assert result.best.variant == "live"
    assert result.best.title == "Il mio canto libero (Live)"


@pytest.mark.parametrize(
    ("request_prefix", "candidate_suffix", "variant"),
    [
        ("remastered version of", "(Remastered 2021)", "remaster"),
        ("nightcore version of", "(Nightcore)", "nightcore"),
        ("slowed version of", "(Slowed Down)", "slowed"),
        ("sped up version of", "(Sped Up)", "sped_up"),
    ],
)
def test_explicitly_requested_variant_prefix_matches_base_identity(request_prefix, candidate_suffix, variant):
    intent = parse_song_request(f"Play {request_prefix} Hallelujah by Jeff Buckley")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [{"title": f"Jeff Buckley - Hallelujah {candidate_suffix}", "artist": "Jeff Buckley"}],
    )

    assert result.best is not None
    assert result.best.variant == variant
    assert result.best.identity_title == "Hallelujah"


def test_artist_or_channel_variant_word_is_not_recording_variant_evidence():
    intent = parse_song_request("Play live version of Hallelujah by Jeff Buckley")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [{"title": "Hallelujah", "artist": "Jeff Buckley", "uploader": "Live Nation"}],
    )

    assert result.failure_reason == "low_confidence"


def test_artist_name_in_display_prefix_is_not_recording_variant_evidence():
    intent = parse_song_request("Play Twilight by Cover Drive")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [{"title": "Cover Drive - Twilight", "artist": "Cover Drive"}],
    )

    assert result.best is not None
    assert result.best.variant == "standard"


@pytest.mark.parametrize(
    ("message", "standard_title", "variant_title", "expected_matches"),
    [
        (
            "Play Cover Me by Bruce Springsteen",
            "Bruce Springsteen - Cover Me",
            "Bruce Springsteen - Cover Me (Cover)",
            1,
        ),
        ("Play Live and Let Die by Wings", "Wings - Live and Let Die", "Wings - Live and Let Die (Live)", 2),
    ],
)
def test_words_inside_song_identity_do_not_request_a_recording_variant(
    message,
    standard_title,
    variant_title,
    expected_matches,
):
    intent = parse_song_request(message)
    assert intent is not None
    assert intent.requested_qualifiers == frozenset()

    result = match_song_request_candidates(
        intent,
        [
            {"title": variant_title, "artist": intent.artist},
            {"title": standard_title, "artist": intent.artist},
        ],
    )

    assert result.best is not None
    assert result.best.variant == "standard"
    assert len(result.matches) == expected_matches


@pytest.mark.parametrize("subtitle", ["Live and Let Die", "Cover Me"])
def test_bracketed_title_subtitle_is_not_a_partial_recording_qualifier(subtitle):
    intent = parse_song_request(f"Play Song ({subtitle}) by Example Artist")
    assert intent is not None
    assert intent.requested_qualifiers == frozenset()

    exact = match_song_request_candidates(
        intent,
        [{"title": f"Example Artist - Song ({subtitle})", "artist": "Example Artist"}],
    )
    base_only = parse_song_request("Play Song by Example Artist")
    assert base_only is not None
    partial = match_song_request_candidates(
        base_only,
        [{"title": f"Example Artist - Song ({subtitle})", "artist": "Example Artist"}],
    )

    assert exact.best is not None
    assert exact.best.variant == "standard"
    assert exact.best.identity_title == f"Song ({subtitle})"
    assert partial.best is None


@pytest.mark.parametrize(
    ("group", "variant"),
    [("Live at Wembley", "live"), ("Acoustic Version", "acoustic"), ("Club Remix", "remix")],
)
def test_complete_bracketed_recording_qualifier_group_still_matches(group, variant):
    intent = parse_song_request("Play Song by Example Artist")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [{"title": f"Example Artist - Song ({group})", "artist": "Example Artist"}],
    )

    assert result.best is not None
    assert result.best.variant == variant
    assert result.best.identity_title == "Song"


def test_duplicate_structured_title_does_not_invent_live_variant():
    intent = parse_song_request("Play Live and Let Die by Wings")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [
            {
                "title": "Wings - Live and Let Die",
                "track_title": "Live and Let Die",
                "track_artist": "Wings",
            }
        ],
    )

    assert result.best is not None
    assert result.best.variant == "standard"


def test_duplicate_structured_title_cannot_satisfy_explicit_live_request():
    intent = parse_song_request("Play live version of Live and Let Die by Wings")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [
            {
                "title": "Wings - Live and Let Die",
                "track_title": "Live and Let Die",
                "track_artist": "Wings",
            }
        ],
    )

    assert result.failure_reason == "low_confidence"


@pytest.mark.parametrize("title", ["I Live for You", "Long Live", "Long Live Rock"])
def test_live_inside_multiword_song_title_is_not_a_variant(title):
    intent = parse_song_request(f"Play {title} by Example Artist")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [{"title": f"Example Artist - {title}", "artist": "Example Artist"}],
    )

    assert intent.requested_qualifiers == frozenset()
    assert result.best is not None
    assert result.best.variant == "standard"
    assert result.best.identity_title == title


def test_standard_recording_ranks_before_unrequested_acceptable_variants():
    intent = parse_song_request("Play Il mio canto libero by Lucio Battisti")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [
            {"title": "Lucio Battisti - Il mio canto libero (Live)", "artist": "Lucio Battisti"},
            {"title": "Lucio Battisti - Il mio canto libero (Remastered 2021)", "artist": "Lucio Battisti"},
            {"title": "Lucio Battisti - Il mio canto libero (Official Audio)", "artist": "Lucio Battisti"},
        ],
    )

    assert [match.variant for match in result.matches] == ["standard", "remaster", "live"]
    assert result.best is not None and result.best.title == "Il mio canto libero"


def test_exact_match_accepts_unbracketed_semantic_variant_labels():
    intent = parse_song_request("Play Il mio canto libero by Lucio Battisti")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [
            {"title": "Lucio Battisti - Il mio canto libero - Live 1972", "artist": "Lucio Battisti"},
            {"title": "Lucio Battisti - Il mio canto libero - 2018 Remaster", "artist": "Lucio Battisti"},
        ],
    )

    assert [match.variant for match in result.matches] == ["remaster", "live"]


@pytest.mark.parametrize(
    ("request_text", "candidate_title", "expected_identity", "expected_variant"),
    [
        ("Play High by Lighthouse Family", "High - Live", "High", "live"),
        (
            'Play "Bang Bang - My Baby Shot Me Down" by Nancy Sinatra',
            "Bang Bang - My Baby Shot Me Down",
            "Bang Bang - My Baby Shot Me Down",
            "standard",
        ),
    ],
)
def test_separate_artist_evidence_keeps_a_title_only_dash(
    request_text,
    candidate_title,
    expected_identity,
    expected_variant,
):
    intent = parse_song_request(request_text)
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [{"title": candidate_title, "artist": intent.artist}],
    )

    assert result.best is not None
    assert result.best.artist == intent.artist
    assert result.best.title == candidate_title
    assert result.best.identity_title == expected_identity
    assert result.best.variant == expected_variant


def test_uncorroborated_dash_prefix_is_not_a_second_title_candidate():
    intent = parse_song_request("Play Live by Lighthouse Family")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [{"title": "High - Live", "artist": "Lighthouse Family"}],
    )

    assert result.best is None
    assert result.failure_reason == "low_confidence"


def test_relevant_longform_identity_survives_for_downstream_admission():
    intent = parse_song_request("Play Il mio canto libero by Lucio Battisti")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [
            {
                "youtube_id": "concert0001",
                "title": "Lucio Battisti - Il mio canto libero (Full Concert)",
                "artist": "Lucio Battisti",
                "duration_ms": 7_200_000,
            },
            {
                "youtube_id": "unrelated01",
                "title": "Phoebe Cates - Theme From Paradise",
                "artist": "Shane Mercury",
                "duration_ms": 180_000,
            },
        ],
    )

    assert result.matched is True
    assert [match.metadata["youtube_id"] for match in result.matches] == ["concert0001"]


def test_clean_candidate_title_strips_platform_noise_but_preserves_live_identity():
    assert clean_candidate_title("Emozioni (Official Music Video) [4K]") == "Emozioni"
    assert clean_candidate_title("Theme From Paradise LIVE SD (with lyrics) 1982") == "Theme From Paradise LIVE 1982"
    assert clean_candidate_title("Il mio canto libero (Live at Teatro 1972)") == (
        "Il mio canto libero (Live at Teatro 1972)"
    )


def test_candidate_packaging_suffix_requires_a_real_identity_boundary():
    assert clean_candidate_title("Claudio") == "Claudio"
    assert clean_candidate_title("Il silenzio di Claudio") == "Il silenzio di Claudio"
    assert clean_candidate_title("Claudio (Official Audio)") == "Claudio"

    wrong_request = parse_song_request("Play Cl by Example Artist")
    exact_request = parse_song_request("Play Claudio by Example Artist")
    assert wrong_request is not None and exact_request is not None
    metadata = [{"title": "Example Artist - Claudio", "artist": "Example Artist"}]

    assert match_song_request_candidates(wrong_request, metadata).failure_reason == "low_confidence"
    exact = match_song_request_candidates(exact_request, metadata)
    assert exact.best is not None
    assert exact.best.title == "Claudio"


@pytest.mark.parametrize("title", ["Audio", "Lyrics", "Visualizer", "HD", "HQ", "4K"])
def test_exact_packaging_word_title_survives_display_and_structured_cleanup(title):
    assert clean_candidate_title(title) == title
    intent = parse_song_request(f"Play {title} by Example Artist")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [
            {
                "title": "Provided to YouTube by Distributor",
                "artist": "Distributor Channel",
                "track_title": title,
                "track_artist": "Example Artist",
            }
        ],
    )

    assert result.best is not None
    assert result.best.title == title
    assert result.best.identity_title == title


@pytest.mark.parametrize(
    ("title", "packaged_title"),
    [
        ("HD", "HD (Official Audio)"),
        ("HQ", "HQ - Official Audio"),
        ("4K", "4K (Official Video)"),
        ("SD", "SD | Lyrics"),
    ],
)
def test_quality_word_title_survives_wrapped_direct_candidate_metadata(title, packaged_title):
    assert clean_candidate_title(packaged_title) == title
    intent = parse_song_request(f"Play {title} by Example Artist")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [{"title": f"Example Artist - {packaged_title}", "artist": "Example Artist"}],
    )

    assert result.best is not None
    assert result.best.title == title
    assert result.best.identity_title == title


@pytest.mark.parametrize(
    ("title", "variant"),
    [
        ("HD (Live)", "live"),
        ("4K (Remastered)", "remaster"),
        ("HQ - Acoustic", "acoustic"),
        ("SD (Radio Edit)", "radio_edit"),
    ],
)
def test_quality_word_title_keeps_explicit_musical_variant_identity(title, variant):
    assert clean_candidate_title(title) == title
    intent = parse_song_request(f'Play "{title}" by Example Artist')
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [
            {
                "title": "Provided to YouTube by Distributor",
                "artist": "Distributor Channel",
                "track_title": title,
                "track_artist": "Example Artist",
            }
        ],
    )

    assert result.best is not None
    assert result.best.title == title
    assert result.best.identity_title in {"HD", "4K", "HQ", "SD"}
    assert result.best.variant == variant


@pytest.mark.parametrize(
    ("ordinary_title", "cleaned"),
    [
        ("Emozioni HD (Live)", "Emozioni (Live)"),
        ("Città vuota 4K (Remastered)", "Città vuota (Remastered)"),
        ("Albachiara HQ - Acoustic", "Albachiara - Acoustic"),
        ("Futura SD (Radio Edit)", "Futura (Radio Edit)"),
    ],
)
def test_quality_labels_remain_noise_when_an_ordinary_title_identity_exists(ordinary_title, cleaned):
    assert clean_candidate_title(ordinary_title) == cleaned


@pytest.mark.parametrize(
    ("requested_title", "candidate_title"),
    [
        ("HD (Live)", "Summer HD (Live)"),
        ("4K (Remastered)", "Resolution 4K (Remastered)"),
        ("HQ - Acoustic", "Signal HQ - Acoustic"),
        ("SD (Radio Edit)", "Broadcast SD (Radio Edit)"),
    ],
)
def test_quality_variant_identity_does_not_match_a_longer_ordinary_title(requested_title, candidate_title):
    intent = parse_song_request(f'Play "{requested_title}" by Example Artist')
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [{"title": f"Example Artist - {candidate_title}", "artist": "Example Artist"}],
    )

    assert result.best is None
    assert result.failure_reason == "low_confidence"


def test_legitimate_audio_title_is_not_erased_as_platform_noise():
    intent = parse_song_request("Play Audio by Sia")
    assert intent is not None

    result = match_song_request_candidates(
        intent,
        [{"title": "Sia - Audio (Official Audio)", "artist": "Sia"}],
    )

    assert clean_candidate_title("Audio") == "Audio"
    assert result.best is not None
    assert result.best.identity_title == "Audio"


@pytest.mark.parametrize(
    "message",
    [
        "Play Shallow by Lady Gaga feat. Bradley Cooper",
        'Play "Shallow" by Lady Gaga feat. Bradley Cooper',
        "Metti Shallow di Lady Gaga feat. Bradley Cooper",
    ],
)
def test_artist_side_requested_feature_requires_the_named_guest(message):
    intent = parse_song_request(message)
    assert intent is not None
    assert (intent.artist, intent.requested_feature_artist) == ("Lady Gaga", "Bradley Cooper")

    result = match_song_request_candidates(
        intent,
        [
            {"youtube_id": "plain", "title": "Lady Gaga - Shallow", "track_artist": "Lady Gaga"},
            {
                "youtube_id": "wrong",
                "title": "Lady Gaga feat. Tony Bennett - Shallow",
                "track_artist": "Lady Gaga feat. Tony Bennett",
            },
            {
                "youtube_id": "right",
                "title": "Lady Gaga feat. Bradley Cooper - Shallow",
                "track_artist": "Lady Gaga feat. Bradley Cooper",
            },
        ],
    )

    assert [match.metadata["youtube_id"] for match in result.matches] == ["right"]


def test_distinct_title_and_artist_feature_credits_are_both_required():
    intent = parse_song_request("Play Song feat. X by Artist feat. Y")

    assert intent is not None
    assert intent.requested_feature_artist == "Y"
    assert intent.preserve_title_feature_syntax is True

    result = match_song_request_candidates(
        intent,
        [
            {
                "youtube_id": "wrong-title-guest",
                "title": "Artist feat. Y - Song feat. Z",
                "track_artist": "Artist feat. Y",
                "track_title": "Song feat. Z",
            },
            {
                "youtube_id": "right-guests",
                "title": "Artist feat. Y - Song feat. X",
                "track_artist": "Artist feat. Y",
                "track_title": "Song feat. X",
            },
        ],
    )

    assert [match.metadata["youtube_id"] for match in result.matches] == ["right-guests"]


def test_artist_only_requested_feature_requires_the_named_guest():
    intent = parse_song_request("Play a song by Lady Gaga feat. Bradley Cooper")
    assert intent is not None
    assert (intent.mode, intent.artist, intent.requested_feature_artist) == (
        "artist",
        "Lady Gaga",
        "Bradley Cooper",
    )

    result = match_song_request_candidates(
        intent,
        [
            {"youtube_id": "plain", "title": "Poker Face", "track_artist": "Lady Gaga"},
            {
                "youtube_id": "wrong",
                "title": "Cheek to Cheek",
                "track_artist": "Lady Gaga feat. Tony Bennett",
            },
            {
                "youtube_id": "right",
                "title": "Shallow",
                "track_artist": "Lady Gaga feat. Bradley Cooper",
            },
        ],
    )

    assert [match.metadata["youtube_id"] for match in result.matches] == ["right"]


@pytest.mark.parametrize(
    "message",
    ["Play Shallow by Lady Gaga", "Play Shallow by Bradley Cooper", "Play Shallow feat. Bradley Cooper"],
)
def test_punctuated_unbracketed_candidate_feature_credit_is_verifiable(message):
    intent = parse_song_request(message)
    assert intent is not None

    match = match_song_request_candidates(
        intent,
        [{"title": "Lady Gaga - Shallow ft. Bradley Cooper", "track_artist": "Lady Gaga"}],
    ).best

    assert match is not None
    assert (match.station_artist, match.identity_title) == ("Lady Gaga", "Shallow")


def test_unpunctuated_feature_words_keep_exact_full_title_ahead_of_guest_interpretation():
    literal = parse_song_request("Play The Song Featuring Tomorrow by Example Artist")
    explicit_guest = parse_song_request("Play Shallow featuring Bradley Cooper by Lady Gaga")
    assert literal is not None and explicit_guest is not None

    literal_result = match_song_request_candidates(
        literal,
        [
            {"youtube_id": "wrong-base", "title": "Example Artist - The Song", "track_artist": "Example Artist"},
            {
                "youtube_id": "literal",
                "title": "Example Artist - The Song Featuring Tomorrow",
                "track_artist": "Example Artist",
            },
        ],
    )
    guest_result = match_song_request_candidates(
        explicit_guest,
        [
            {"youtube_id": "plain", "title": "Lady Gaga - Shallow", "track_artist": "Lady Gaga"},
            {
                "youtube_id": "wrong-guest",
                "title": "Lady Gaga - Shallow (feat. Tony Bennett)",
                "track_artist": "Lady Gaga",
            },
            {
                "youtube_id": "right-guest",
                "title": "Lady Gaga - Shallow (feat. Bradley Cooper)",
                "track_artist": "Lady Gaga",
            },
        ],
    )

    assert [match.metadata["youtube_id"] for match in literal_result.matches] == ["literal"]
    assert [match.metadata["youtube_id"] for match in guest_result.matches] == ["right-guest"]


def test_dash_live_title_phrase_is_not_collapsed_into_a_recording_variant():
    base = parse_song_request("Play Song by Example Artist")
    exact = parse_song_request("Play Song - Live and Let Die by Example Artist")
    assert base is not None and exact is not None
    candidate = {
        "title": "Example Artist - Song - Live and Let Die",
        "track_artist": "Example Artist",
    }

    assert match_song_request_candidates(base, [candidate]).best is None
    exact_match = match_song_request_candidates(exact, [candidate]).best
    assert exact_match is not None
    assert (exact_match.identity_title, exact_match.variant) == ("Song - Live and Let Die", "standard")


@pytest.mark.parametrize(
    ("message", "candidate_title", "feature_artist"),
    [
        ("Play Lucio Battisti - Emozioni for me", "Lucio Battisti - Emozioni", ""),
        (
            "Play Lucio Battisti - Emozioni (feat. Mina)",
            "Lucio Battisti - Emozioni (feat. Mina)",
            "Mina",
        ),
        ("Play Lucio Battisti - Emozioni please", "Lucio Battisti - Emozioni", ""),
    ],
)
def test_pasted_artist_title_composes_with_other_ranked_ambiguities(message, candidate_title, feature_artist):
    intent = parse_song_request(message)
    assert intent is not None

    match = match_song_request_candidates(
        intent,
        [{"title": candidate_title, "track_artist": "Lucio Battisti"}],
    ).best

    assert match is not None
    assert (match.station_artist, match.identity_title) == ("Lucio Battisti", "Emozioni")
    if feature_artist:
        assert feature_artist in match.credited_artists


@pytest.mark.parametrize(
    ("message", "title"),
    [
        ("Play Imagine please", "Imagine"),
        ("Can you play Imagine, please?", "Imagine"),
        ("Metti Albachiara per favore", "Albachiara"),
    ],
)
def test_trailing_politeness_is_a_ranked_candidate_verified_alternative(message, title):
    intent = parse_song_request(message)
    assert intent is not None
    assert intent.title != title

    match = match_song_request_candidates(
        intent,
        [{"title": title, "track_artist": "Verified Artist"}],
    ).best

    assert match is not None
    assert match.identity_title == title


@pytest.mark.parametrize("title", ["Waiting, for You", "Song; per Te"])
def test_quoted_title_protects_internal_dedication_punctuation(title):
    intent = parse_song_request(f'Play "{title}"')
    assert intent is not None
    assert (intent.mode, intent.title, intent.alternative_identities) == ("title", title, ())

    match = match_song_request_candidates(
        intent,
        [{"title": title, "track_artist": "Verified Artist"}],
    ).best

    assert match is not None
    assert match.identity_title == title


@pytest.mark.parametrize("title", ["Digital Audio", "Final Lyrics", "Night Visualizer", "Signal HD"])
def test_bare_multiword_platform_looking_suffix_stays_song_identity(title):
    assert clean_candidate_title(title) == title
    full = parse_song_request(f"Play {title} by Example Artist")
    shortened = parse_song_request(f"Play {title.rsplit(maxsplit=1)[0]} by Example Artist")
    assert full is not None and shortened is not None

    candidate = {"title": f"Example Artist - {title}", "track_artist": "Example Artist"}
    assert match_song_request_candidates(full, [candidate]).best is not None
    assert match_song_request_candidates(shortened, [candidate]).best is None


@pytest.mark.parametrize(
    ("full_title", "short_title"),
    [
        ("Living With Lyrics", "Living"),
        ("HD Signal", "Signal"),
        ("Ultra HD Dreams", "Ultra Dreams"),
    ],
)
def test_unseparated_platform_words_do_not_create_title_equivalence(full_title, short_title):
    assert clean_candidate_title(full_title) == full_title
    full = parse_song_request(f"Play {full_title} by Example Artist")
    short = parse_song_request(f"Play {short_title} by Example Artist")
    assert full is not None and short is not None

    full_candidate = {"title": f"Example Artist - {full_title}", "track_artist": "Example Artist"}
    short_candidate = {"title": f"Example Artist - {short_title}", "track_artist": "Example Artist"}
    assert match_song_request_candidates(full, [full_candidate]).best is not None
    assert match_song_request_candidates(short, [full_candidate]).best is None
    assert match_song_request_candidates(full, [short_candidate]).best is None


@pytest.mark.parametrize("hyphen", ["\u2010", "\u2011"])
def test_unicode_intra_word_hyphens_match_compact_title_identity(hyphen):
    intent = parse_song_request("Play SpiderMan by Example Artist")
    assert intent is not None

    match = match_song_request_candidates(
        intent,
        [{"title": f"Example Artist - Spider{hyphen}Man", "track_artist": "Example Artist"}],
    ).best

    assert match is not None
    assert match.identity_title == f"Spider{hyphen}Man"


def test_platform_artist_disproves_a_false_title_dash_prefix():
    intent = parse_song_request("Play Bang Bang - My Baby Shot Me Down")
    suffix = parse_song_request("Play My Baby Shot Me Down")
    assert intent is not None and suffix is not None
    candidate = {
        "title": "Bang Bang - My Baby Shot Me Down",
        "uploader": "Nancy Sinatra - Topic",
    }

    match = match_song_request_candidates(intent, [candidate]).best

    assert match is not None
    assert (match.station_artist, match.identity_title) == (
        "Nancy Sinatra",
        "Bang Bang - My Baby Shot Me Down",
    )
    assert match_song_request_candidates(suffix, [candidate]).best is None


@pytest.mark.parametrize("marker", ["ft.", "feat"])
def test_requested_guest_corroborates_contextual_candidate_credit(marker):
    intent = parse_song_request("Play Song feat. Pitbull by Example Artist")
    assert intent is not None

    match = match_song_request_candidates(
        intent,
        [{"title": f"Example Artist - Song {marker} Pitbull", "uploader": "Example Artist"}],
    ).best

    assert match is not None
    assert (match.station_artist, match.identity_title) == ("Example Artist", "Song")
    assert "Pitbull" in match.credited_artists


@pytest.mark.parametrize("title", ["Welcome to Ft. Lauderdale", "A Feat of Strength"])
def test_title_cased_feature_words_remain_literal_candidate_identity(title):
    exact = parse_song_request(f"Play {title} by Example Artist")
    shortened = parse_song_request(f"Play {title.split(' Ft. ')[0].split(' Feat ')[0]} by Example Artist")
    assert exact is not None and shortened is not None
    candidate = {"title": f"Example Artist - {title}", "uploader": "Example Artist"}

    assert match_song_request_candidates(exact, [candidate]).best is not None
    assert match_song_request_candidates(shortened, [candidate]).best is None


def test_feature_credit_composes_with_recording_qualifiers_in_either_order():
    intent = parse_song_request("Play Song feat. Guest (Remix) by Example Artist")
    assert intent is not None
    assert intent.requested_feature_artist == "Guest"
    assert intent.requested_qualifiers == frozenset({"remix"})

    result = match_song_request_candidates(
        intent,
        [
            {"title": "Example Artist - Song (Remix) feat. Guest", "uploader": "Example Artist"},
            {"title": "Example Artist - Song - feat. Guest - Live", "uploader": "Example Artist"},
        ],
    )

    assert [(match.identity_title, match.variant) for match in result.matches] == [("Song", "remix")]

    live_intent = parse_song_request("Play live version of Song feat. Guest by Example Artist")
    assert live_intent is not None
    live_match = match_song_request_candidates(
        live_intent,
        [{"title": "Example Artist - Song - feat. Guest - Live", "uploader": "Example Artist"}],
    ).best
    assert live_match is not None
    assert (live_match.identity_title, live_match.variant) == ("Song", "live")


@pytest.mark.parametrize(
    "message",
    [
        "Play Imagine by John Lennon please",
        'Play "Imagine" by John Lennon please',
        "Metti Imagine di John Lennon per favore",
    ],
)
def test_terminal_courtesy_after_artist_credit_is_not_part_of_artist(message):
    intent = parse_song_request(message)

    assert intent is not None
    assert (intent.artist, intent.title) == ("John Lennon", "Imagine")


def test_literal_title_ending_in_please_keeps_its_primary_identity():
    intent = parse_song_request("Play Imagine Please")

    assert intent is not None
    assert intent.title == "Imagine Please"
