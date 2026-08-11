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
    assert intent.alternative_identity == SongRequestIdentity(mode="title", title="Killed by Death")

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
    assert intent.alternative_identity is not None
    assert intent.alternative_identity.title == "Killed by Death"


@pytest.mark.parametrize("message", ['Play "Killed by Death"', 'Play "Killed by Death" for Anna'])
def test_quoted_final_by_title_is_authoritative_without_alternative(message):
    intent = parse_song_request(message)

    assert intent is not None
    assert (intent.mode, intent.title, intent.artist) == ("title", "Killed by Death", "")
    assert intent.alternative_identity is None


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

    assert centro is not None and centro.alternative_identity is not None
    assert futura is not None and futura.alternative_identity is not None
    assert (centro.mode, centro.title) == ("title", "Centro di Gravit\u00e0 Permanente")
    assert (futura.mode, futura.title) == ("title", "Futura di Lucio Dalla")
    assert (futura.alternative_identity.title, futura.alternative_identity.artist) == ("Futura", "Lucio Dalla")

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
    assert intent.alternative_identity is None


@pytest.mark.parametrize("message", ["Play Albachiara for Anna", "Metti Albachiara per Anna"])
def test_parse_title_only_named_dedication(message):
    intent = parse_song_request(message)

    assert intent is not None
    assert intent.mode == "title"
    assert intent.title == "Albachiara"


def test_parse_title_containing_for_before_explicit_artist_credit():
    intent = parse_song_request("Play Song for You by Chicago")

    assert intent is not None
    assert intent.mode == "artist_title"
    assert (intent.artist, intent.title) == ("Chicago", "Song for You")


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


def test_artist_cleanup_does_not_remove_a_real_music_suffix():
    intent = parse_song_request("Play something by Roxy Music")
    assert intent is not None

    result = match_song_request_candidates(intent, [{"title": "Avalon", "artist": "Roxy Music"}])

    assert result.best is not None
    assert result.best.artist == "Roxy Music"


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


@pytest.mark.parametrize(
    "candidate_title",
    [
        "Lady Gaga feat. Bradley Cooper - Shallow",
        "Lady Gaga - Shallow (feat. Bradley Cooper)",
        "Lady Gaga - Shallow ft. Bradley Cooper",
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
    assert result.best.identity_title == "Shallow"
    assert result.best.identity_artist == "Lady Gaga"


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
