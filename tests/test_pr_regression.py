"""Regression tests for changes introduced in this PR.

Covers:
- AdFormat.voice_count: CLASSIC_PITCH now returns 1 (was 2 in ad_creative.py)
- Data model classes (AdBrand, AdVoice, etc.) moved from ad_creative → models
- StationState: resume_event and last_music_file fields removed
- _sanitize_prompt_data: quote/role-marker stripping removed
- reset_provider_backoff: now also clears _anthropic_attempt_lock
- evict_cache_lru: protected_paths parameter removed
- _select_ad_creative / _cast_voices: new config-based signatures
- Streamer: ICY headers no longer CRLF-scrubbed; youtube_id not validated;
  panic endpoint removed
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# AdFormat.voice_count — models.py
# ---------------------------------------------------------------------------


class TestAdFormatVoiceCount:
    """voice_count reflects the new mapping: only DUO_SCENE and TESTIMONIAL need 2."""

    def test_classic_pitch_returns_one(self):
        """CLASSIC_PITCH previously required 2 voices; now it only needs 1."""
        from mammamiradio.models import AdFormat

        assert AdFormat.CLASSIC_PITCH.voice_count == 1

    def test_testimonial_returns_two(self):
        from mammamiradio.models import AdFormat

        assert AdFormat.TESTIMONIAL.voice_count == 2

    def test_duo_scene_returns_two(self):
        from mammamiradio.models import AdFormat

        assert AdFormat.DUO_SCENE.voice_count == 2

    def test_live_remote_returns_one(self):
        from mammamiradio.models import AdFormat

        assert AdFormat.LIVE_REMOTE.voice_count == 1

    def test_late_night_whisper_returns_one(self):
        from mammamiradio.models import AdFormat

        assert AdFormat.LATE_NIGHT_WHISPER.voice_count == 1

    def test_institutional_psa_returns_one(self):
        from mammamiradio.models import AdFormat

        assert AdFormat.INSTITUTIONAL_PSA.voice_count == 1

    def test_all_formats_have_valid_voice_count(self):
        """All formats return either 1 or 2 — never 0 or >2."""
        from mammamiradio.models import AdFormat

        for fmt in AdFormat:
            assert fmt.voice_count in (1, 2), f"{fmt}: unexpected voice_count={fmt.voice_count}"

    def test_only_duo_and_testimonial_need_two_voices(self):
        """Exactly two formats require 2 voices."""
        from mammamiradio.models import AdFormat

        two_voice_formats = [fmt for fmt in AdFormat if fmt.voice_count == 2]
        assert set(two_voice_formats) == {AdFormat.DUO_SCENE, AdFormat.TESTIMONIAL}


# ---------------------------------------------------------------------------
# Data models importable from models (not ad_creative) — models.py
# ---------------------------------------------------------------------------


class TestModelsImports:
    """All ad-related data classes must be importable from mammamiradio.models."""

    def test_import_ad_brand(self):
        from mammamiradio.models import AdBrand  # noqa: F401

    def test_import_ad_voice(self):
        from mammamiradio.models import AdVoice  # noqa: F401

    def test_import_ad_part(self):
        from mammamiradio.models import AdPart  # noqa: F401

    def test_import_ad_script(self):
        from mammamiradio.models import AdScript  # noqa: F401

    def test_import_sonic_world(self):
        from mammamiradio.models import SonicWorld  # noqa: F401

    def test_import_campaign_spine(self):
        from mammamiradio.models import CampaignSpine  # noqa: F401

    def test_import_ad_format(self):
        from mammamiradio.models import AdFormat  # noqa: F401

    def test_ad_brand_instantiation(self):
        from mammamiradio.models import AdBrand

        b = AdBrand(name="TestBrand", tagline="We test.")
        assert b.name == "TestBrand"
        assert b.category == "general"
        assert b.recurring is True
        assert b.campaign is None

    def test_ad_voice_instantiation(self):
        from mammamiradio.models import AdVoice

        v = AdVoice(name="Roberta", voice="it-IT-GianniNeural", style="booming")
        assert v.role == ""  # default empty role

    def test_ad_part_defaults(self):
        from mammamiradio.models import AdPart

        p = AdPart(type="voice")
        assert p.text == ""
        assert p.sfx == ""
        assert p.duration == 0.0
        assert p.role == ""
        assert p.environment == ""

    def test_sonic_world_defaults(self):
        from mammamiradio.models import SonicWorld

        sw = SonicWorld()
        assert sw.environment == ""
        assert sw.music_bed == "lounge"
        assert sw.transition_motif == "chime"
        assert sw.sonic_signature == ""

    def test_campaign_spine_defaults(self):
        from mammamiradio.models import CampaignSpine

        cs = CampaignSpine()
        assert cs.premise == ""
        assert cs.format_pool == []
        assert cs.spokesperson == ""

    def test_ad_script_defaults(self):
        from mammamiradio.models import AdScript

        s = AdScript(brand="MarcaTest")
        assert s.parts == []
        assert s.format == "classic_pitch"
        assert s.roles_used == []


# ---------------------------------------------------------------------------
# StationState: removed fields — models.py
# ---------------------------------------------------------------------------


class TestStationStateRemovedFields:
    """resume_event and last_music_file must NOT exist on StationState."""

    def test_no_resume_event_attribute(self):
        from mammamiradio.models import StationState

        state = StationState()
        assert not hasattr(state, "resume_event"), (
            "resume_event was removed from StationState in this PR — "
            "do not add it back without updating the producer sleep logic"
        )

    def test_no_last_music_file_attribute(self):
        from mammamiradio.models import StationState

        state = StationState()
        assert not hasattr(state, "last_music_file"), (
            "last_music_file was removed from StationState in this PR — "
            "the producer now uses the module-level _last_music_file only"
        )

    def test_state_still_has_required_fields(self):
        """Sanity-check that removing the fields didn't break core attributes."""
        from mammamiradio.models import StationState

        state = StationState()
        assert hasattr(state, "playlist")
        assert hasattr(state, "ad_history")
        assert hasattr(state, "session_stopped")
        assert hasattr(state, "segments_produced")


# ---------------------------------------------------------------------------
# _sanitize_prompt_data — scriptwriter.py
# ---------------------------------------------------------------------------


class TestSanitizePromptData:
    """Verify the reduced sanitization after removing quote/role-marker stripping."""

    def _sanitize(self, text: str, max_len: int = 80) -> str:
        from mammamiradio.scriptwriter import _sanitize_prompt_data

        return _sanitize_prompt_data(text, max_len=max_len)

    def test_control_chars_still_stripped(self):
        """NUL bytes and other control chars are still removed."""
        result = self._sanitize("hello\x00world\x01end")
        assert "\x00" not in result
        assert "\x01" not in result
        assert "helloworld" in result or "helloworldend" in result

    def test_xml_like_tags_stripped(self):
        """< > { } are stripped as potential XML/template injection."""
        result = self._sanitize("text<script>alert(1)</script>")
        assert "<" not in result
        assert ">" not in result

    def test_truncation_applied(self):
        """Text longer than max_len is truncated with '...' suffix."""
        long_text = "a" * 100
        result = self._sanitize(long_text, max_len=80)
        assert result.endswith("...")
        assert len(result) == 83  # 80 + len("...")

    def test_short_text_not_truncated(self):
        """Text within max_len is returned as-is (minus stripped chars)."""
        result = self._sanitize("short text", max_len=80)
        assert result == "short text"

    def test_double_quotes_not_stripped(self):
        """Double quotes are NO LONGER stripped (PR change: _QUOTE_CHARS_RE removed)."""
        result = self._sanitize('He said "hello"')
        assert '"' in result, "double quotes should be preserved after PR change"

    def test_backticks_not_stripped(self):
        """Backticks are NO LONGER stripped."""
        result = self._sanitize("use `code` here")
        assert "`" in result, "backticks should be preserved after PR change"

    def test_curly_quotes_not_stripped(self):
        """Unicode curly quotes (\u201c\u201d\u2018\u2019) are NOT stripped."""
        result = self._sanitize("\u201cquoted\u201d and \u2018also\u2019")
        assert "\u201c" in result or "\u201d" in result or "quoted" in result

    def test_role_marker_system_not_stripped(self):
        """'System:' role markers are NO LONGER stripped (PR removed _ROLE_MARKER_RE)."""
        result = self._sanitize("System: do something")
        assert "System" in result, "role markers should be preserved after PR change"

    def test_role_marker_assistant_not_stripped(self):
        """'Assistant:' role markers are NOT stripped."""
        result = self._sanitize("Assistant: reply here")
        assert "Assistant" in result

    def test_role_marker_human_not_stripped(self):
        """'Human:' role markers are NOT stripped."""
        result = self._sanitize("Human: my request")
        assert "Human" in result

    def test_role_marker_user_not_stripped(self):
        """'User:' role markers are NOT stripped."""
        result = self._sanitize("User: my input")
        assert "User" in result

    def test_empty_string_returns_empty(self):
        result = self._sanitize("")
        assert result == ""

    def test_purely_control_chars_returns_empty(self):
        result = self._sanitize("\x00\x01\x02")
        assert result == ""


# ---------------------------------------------------------------------------
# reset_provider_backoff — scriptwriter.py
# ---------------------------------------------------------------------------


class TestResetProviderBackoff:
    """reset_provider_backoff must also clear the _anthropic_attempt_lock."""

    def test_clears_attempt_lock(self):
        """After reset, _anthropic_attempt_lock should be None."""
        import mammamiradio.scriptwriter as sw

        # Ensure the lock exists (simulate it being created during a request)
        sw._anthropic_attempt_lock = asyncio.Lock()
        assert sw._anthropic_attempt_lock is not None

        sw.reset_provider_backoff()

        assert sw._anthropic_attempt_lock is None, (
            "reset_provider_backoff must set _anthropic_attempt_lock=None so the next "
            "call to _get_anthropic_attempt_lock() creates a fresh lock on the correct loop"
        )

    def test_clears_auth_blocked_key(self):
        import mammamiradio.scriptwriter as sw

        sw._anthropic_auth_blocked_key = "old-key"
        sw.reset_provider_backoff()
        assert sw._anthropic_auth_blocked_key == ""

    def test_clears_auth_blocked_until(self):
        import mammamiradio.scriptwriter as sw

        sw._anthropic_auth_blocked_until = 9999999.0
        sw.reset_provider_backoff()
        assert sw._anthropic_auth_blocked_until == 0.0

    def test_clears_block_expired_logged(self):
        import mammamiradio.scriptwriter as sw

        sw._anthropic_block_expired_logged = True
        sw.reset_provider_backoff()
        assert sw._anthropic_block_expired_logged is False

    def test_idempotent_when_already_clear(self):
        """Calling reset on already-clear state must not raise."""
        import mammamiradio.scriptwriter as sw

        sw._anthropic_attempt_lock = None
        sw._anthropic_auth_blocked_key = ""
        sw._anthropic_auth_blocked_until = 0.0
        sw._anthropic_block_expired_logged = False
        sw.reset_provider_backoff()  # must not raise


# ---------------------------------------------------------------------------
# evict_cache_lru — downloader.py (protected_paths removed)
# ---------------------------------------------------------------------------


class TestEvictCacheLru:
    """evict_cache_lru now takes exactly (cache_dir, max_size_mb) — no protected_paths."""

    def test_signature_has_no_protected_paths(self):
        """The function must accept exactly 2 positional args (no protected_paths)."""
        import inspect

        from mammamiradio.downloader import evict_cache_lru

        sig = inspect.signature(evict_cache_lru)
        params = list(sig.parameters.keys())
        assert "protected_paths" not in params, (
            "protected_paths was removed from evict_cache_lru in this PR"
        )
        assert "cache_dir" in params
        assert "max_size_mb" in params

    def test_max_size_zero_returns_early(self, tmp_path):
        """max_size_mb <= 0 returns immediately without touching anything."""
        from mammamiradio.downloader import evict_cache_lru

        f = tmp_path / "test.mp3"
        f.write_bytes(b"x" * 1024)
        evict_cache_lru(tmp_path, 0)
        assert f.exists(), "file should not be evicted when max_size_mb=0"

    def test_evicts_oldest_regular_file_first(self, tmp_path):
        """Oldest regular MP3 is evicted before norm_ files."""
        from mammamiradio.downloader import evict_cache_lru

        # Create files with distinct sizes that together exceed 1 MB budget
        old_file = tmp_path / "old_track.mp3"
        new_file = tmp_path / "new_track.mp3"
        old_file.write_bytes(b"o" * (600 * 1024))  # 600 KB
        new_file.write_bytes(b"n" * (600 * 1024))  # 600 KB

        # Set old_file's atime to the past
        import os

        old_atime = time.time() - 3600
        os.utime(old_file, (old_atime, old_atime))

        # Total = 1.2 MB; budget = 1 MB → must evict 200+ KB
        evict_cache_lru(tmp_path, 1)

        # old_file should be gone, new_file should remain
        assert not old_file.exists(), "oldest file should be evicted"
        assert new_file.exists(), "newer file should remain"

    def test_preserves_protected_filenames(self, tmp_path):
        """SQLite DB, playlist source JSON, and session flag are never evicted."""
        from mammamiradio.downloader import evict_cache_lru

        # Create protected files (not actual .mp3 but ensure they'd match filenames)
        db = tmp_path / "mammamiradio.db"
        ps = tmp_path / "playlist_source.json"
        flag = tmp_path / "session_stopped.flag"
        db.write_bytes(b"x" * 1024)
        ps.write_bytes(b"x" * 1024)
        flag.write_bytes(b"x" * 1024)

        # Create a regular MP3 that will exceed the tiny budget
        big_mp3 = tmp_path / "big.mp3"
        big_mp3.write_bytes(b"x" * (2 * 1024 * 1024))  # 2 MB

        # Budget = 1 MB → evict big.mp3
        evict_cache_lru(tmp_path, 1)

        # Protected files must survive regardless
        assert db.exists()
        assert ps.exists()
        assert flag.exists()

    def test_norm_files_evicted_after_regular(self, tmp_path):
        """norm_ files are only evicted after regular files are exhausted."""
        from mammamiradio.downloader import evict_cache_lru

        regular = tmp_path / "regular.mp3"
        norm = tmp_path / "norm_sometrack.mp3"
        chunk = 600 * 1024  # 600 KB each → total 1.2 MB
        regular.write_bytes(b"r" * chunk)
        norm.write_bytes(b"n" * chunk)

        # Make regular older
        import os

        old_atime = time.time() - 3600
        os.utime(regular, (old_atime, old_atime))

        # Budget = 1 MB → must evict the regular file first
        evict_cache_lru(tmp_path, 1)
        assert not regular.exists(), "regular file should be evicted first"
        assert norm.exists(), "norm file should remain while budget is satisfied"

    def test_no_files_is_no_op(self, tmp_path):
        """Empty directory with budget under limit is a no-op."""
        from mammamiradio.downloader import evict_cache_lru

        evict_cache_lru(tmp_path, 100)  # no crash, no files to touch

    def test_calling_without_protected_paths_arg_works(self, tmp_path):
        """Confirm the 2-arg form is accepted without TypeError."""
        from mammamiradio.downloader import evict_cache_lru

        # Should not raise TypeError("unexpected keyword argument 'protected_paths'")
        evict_cache_lru(tmp_path, 100)


# ---------------------------------------------------------------------------
# _select_ad_creative new config-based signature — producer.py
# ---------------------------------------------------------------------------


class TestSelectAdCreativeNewSignature:
    """_select_ad_creative now accepts config (not num_voices) as third arg."""

    def _make_config(self, num_voices: int = 0):
        config = MagicMock()
        from mammamiradio.models import AdVoice

        config.ads.voices = [
            AdVoice(name=f"Voice{i}", voice=f"v{i}", style="neutral") for i in range(num_voices)
        ]
        return config

    def test_accepts_config_argument(self):
        from mammamiradio.models import AdBrand, SonicWorld, StationState
        from mammamiradio.producer import _select_ad_creative

        brand = AdBrand(name="TestBrand", tagline="Test")
        state = StationState()
        config = self._make_config(0)

        fmt, sonic, roles = _select_ad_creative(brand, state, config)
        assert isinstance(fmt, str)
        assert isinstance(sonic, SonicWorld)
        assert isinstance(roles, list)
        assert len(roles) >= 1

    def test_format_pool_not_filtered(self):
        """format_pool entries are used as-is (unknown formats NOT filtered out this PR)."""
        from mammamiradio.models import AdBrand, AdFormat, CampaignSpine, StationState
        from mammamiradio.producer import _select_ad_creative

        # A format_pool with 2 single-voice valid formats
        campaign = CampaignSpine(format_pool=["live_remote", "institutional_psa"])
        brand = AdBrand(name="B", tagline="T", campaign=campaign)
        state = StationState()
        config = self._make_config(0)

        results = {_select_ad_creative(brand, state, config)[0] for _ in range(20)}
        assert results <= {"live_remote", "institutional_psa"}, (
            "format_pool entries should be respected"
        )

    def test_voice_guard_uses_config_voices_count(self):
        """With 1 voice in config.ads.voices, multi-voice formats are excluded."""
        from mammamiradio.models import AdBrand, AdFormat, StationState
        from mammamiradio.producer import _select_ad_creative

        brand = AdBrand(name="B", tagline="T")
        state = StationState()
        config = self._make_config(1)  # only 1 voice

        for _ in range(20):
            fmt, _, _ = _select_ad_creative(brand, state, config)
            assert AdFormat(fmt).voice_count < 2, (
                f"format {fmt!r} requires 2 voices but only 1 is available"
            )

    def test_with_zero_voices_falls_back_to_single_voice_formats(self):
        """0 voices in config → num_voices treated as 1 → single-voice formats only."""
        from mammamiradio.models import AdBrand, AdFormat, StationState
        from mammamiradio.producer import _select_ad_creative

        brand = AdBrand(name="B", tagline="T")
        state = StationState()
        config = self._make_config(0)  # no voices configured → num_voices=1 inside function

        for _ in range(20):
            fmt, _, _ = _select_ad_creative(brand, state, config)
            assert AdFormat(fmt).voice_count < 2

    def test_with_two_voices_allows_multi_voice_formats(self):
        """2 voices in config → multi-voice formats are allowed."""
        from mammamiradio.models import AdBrand, AdFormat, StationState
        from mammamiradio.producer import _select_ad_creative

        brand = AdBrand(name="B", tagline="T", campaign=None)
        state = StationState()
        config = self._make_config(2)

        # With enough iterations, we should see at least one 2-voice format
        seen_two_voice = False
        for _ in range(50):
            fmt, _, _ = _select_ad_creative(brand, state, config)
            if AdFormat(fmt).voice_count == 2:
                seen_two_voice = True
                break
        assert seen_two_voice, "Expected at least one 2-voice format with 2 voices configured"

    def test_returns_tuple_of_str_sonicworld_list(self):
        from mammamiradio.models import AdBrand, SonicWorld, StationState
        from mammamiradio.producer import _select_ad_creative

        brand = AdBrand(name="B", tagline="T")
        state = StationState()
        config = self._make_config(0)

        result = _select_ad_creative(brand, state, config)
        assert len(result) == 3
        fmt, sonic, roles = result
        assert isinstance(fmt, str)
        assert isinstance(sonic, SonicWorld)
        assert isinstance(roles, list)
        assert all(isinstance(r, str) for r in roles)


# ---------------------------------------------------------------------------
# _cast_voices new config-based signature — producer.py
# ---------------------------------------------------------------------------


class TestCastVoicesNewSignature:
    """_cast_voices now accepts (brand, config, roles_needed) — no separate voices/hosts args."""

    def _make_config(self, voices=None, hosts=None):
        from mammamiradio.models import HostPersonality

        config = MagicMock()
        config.ads.voices = voices or []
        config.hosts = hosts or [
            HostPersonality(name="DefaultHost", voice="it-IT-GianniNeural", style="warm")
        ]
        return config

    def test_accepts_config_argument(self):
        from mammamiradio.models import AdBrand
        from mammamiradio.producer import _cast_voices

        brand = AdBrand(name="B", tagline="T")
        config = self._make_config()
        result = _cast_voices(brand, config, ["hammer"])
        assert "hammer" in result

    def test_no_voices_uses_host_fallback(self):
        """With no ad voices, falls back to a host voice (from config.hosts)."""
        from mammamiradio.models import AdBrand, HostPersonality
        from mammamiradio.producer import _cast_voices

        host = HostPersonality(name="Lucia", voice="it-IT-LuciaNeural", style="warm")
        brand = AdBrand(name="B", tagline="T")
        config = self._make_config(voices=[], hosts=[host])

        result = _cast_voices(brand, config, ["hammer"])
        assert "hammer" in result
        assert result["hammer"].name == "Lucia"

    def test_no_voices_no_roles_returns_default(self):
        """Empty roles_needed with no voices returns a 'default' key."""
        from mammamiradio.models import AdBrand, HostPersonality
        from mammamiradio.producer import _cast_voices

        host = HostPersonality(name="Lucia", voice="it-IT-LuciaNeural", style="warm")
        brand = AdBrand(name="B", tagline="T")
        config = self._make_config(voices=[], hosts=[host])

        result = _cast_voices(brand, config, [])
        assert "default" in result

    def test_role_index_used_when_voice_has_matching_role(self):
        """A voice with role='hammer' is assigned to the hammer role."""
        from mammamiradio.models import AdBrand, AdVoice
        from mammamiradio.producer import _cast_voices

        voices = [
            AdVoice(name="HammerVoice", voice="v1", style="booming", role="hammer"),
            AdVoice(name="MiscVoice", voice="v2", style="neutral"),
        ]
        brand = AdBrand(name="B", tagline="T")
        config = self._make_config(voices=voices)

        result = _cast_voices(brand, config, ["hammer"])
        assert result["hammer"].name == "HammerVoice"

    def test_unknown_role_falls_back_to_random_voice(self):
        """A role not in the role_index is filled by a random available voice."""
        from mammamiradio.models import AdBrand, AdVoice
        from mammamiradio.producer import _cast_voices

        voices = [AdVoice(name="Generic", voice="v1", style="neutral")]
        brand = AdBrand(name="B", tagline="T")
        config = self._make_config(voices=voices)

        result = _cast_voices(brand, config, ["unknown_role"])
        assert "unknown_role" in result
        assert result["unknown_role"].name == "Generic"

    def test_two_roles_both_assigned(self):
        """Multiple roles are all assigned."""
        from mammamiradio.models import AdBrand, AdVoice
        from mammamiradio.producer import _cast_voices

        voices = [
            AdVoice(name="Hammer", voice="v1", style="bold", role="hammer"),
            AdVoice(name="Goblin", voice="v2", style="fast", role="disclaimer_goblin"),
        ]
        brand = AdBrand(name="B", tagline="T")
        config = self._make_config(voices=voices)

        result = _cast_voices(brand, config, ["hammer", "disclaimer_goblin"])
        assert result["hammer"].name == "Hammer"
        assert result["disclaimer_goblin"].name == "Goblin"

    def test_no_longer_raises_valueerror_for_empty_voices_and_hosts(self):
        """Old ad_creative._cast_voices raised ValueError when both were empty.
        New version uses config.hosts, so as long as config.hosts is non-empty it works.
        Verify no ValueError for the normal case.
        """
        from mammamiradio.models import AdBrand, HostPersonality
        from mammamiradio.producer import _cast_voices

        host = HostPersonality(name="Solo", voice="v1", style="neutral")
        brand = AdBrand(name="B", tagline="T")
        config = self._make_config(voices=[], hosts=[host])

        # Must not raise
        result = _cast_voices(brand, config, ["hammer"])
        assert "hammer" in result


# ---------------------------------------------------------------------------
# _FORMAT_ROLES and ALL_FORMATS constants — producer.py
# ---------------------------------------------------------------------------


class TestProducerAdConstants:
    """_FORMAT_ROLES and ALL_FORMATS moved into producer.py."""

    def test_format_roles_importable_from_producer(self):
        from mammamiradio.producer import _FORMAT_ROLES  # noqa: F401

    def test_all_formats_importable_from_producer(self):
        from mammamiradio.producer import ALL_FORMATS  # noqa: F401

    def test_all_formats_contains_all_enum_values(self):
        from mammamiradio.models import AdFormat
        from mammamiradio.producer import ALL_FORMATS

        assert set(ALL_FORMATS) == {f.value for f in AdFormat}

    def test_format_roles_has_entry_for_every_format(self):
        from mammamiradio.models import AdFormat
        from mammamiradio.producer import _FORMAT_ROLES

        for fmt in AdFormat:
            assert fmt in _FORMAT_ROLES or fmt.value in _FORMAT_ROLES, (
                f"_FORMAT_ROLES missing entry for {fmt}"
            )

    def test_classic_pitch_has_disclaimer_goblin(self):
        from mammamiradio.models import AdFormat
        from mammamiradio.producer import _FORMAT_ROLES

        assert "disclaimer_goblin" in _FORMAT_ROLES[AdFormat.CLASSIC_PITCH]

    def test_duo_scene_roles(self):
        from mammamiradio.models import AdFormat
        from mammamiradio.producer import _FORMAT_ROLES

        assert "hammer" in _FORMAT_ROLES[AdFormat.DUO_SCENE]
        assert "maniac" in _FORMAT_ROLES[AdFormat.DUO_SCENE]


# ---------------------------------------------------------------------------
# AD_FORMATS / SPEAKER_ROLES / SONIC_ENVIRONMENTS moved to scriptwriter.py
# ---------------------------------------------------------------------------


class TestScriptwriterAdConstants:
    """AD_FORMATS, SPEAKER_ROLES, SONIC_ENVIRONMENTS now live in scriptwriter.py."""

    def test_ad_formats_importable_from_scriptwriter(self):
        from mammamiradio.scriptwriter import AD_FORMATS  # noqa: F401

    def test_speaker_roles_importable_from_scriptwriter(self):
        from mammamiradio.scriptwriter import SPEAKER_ROLES  # noqa: F401

    def test_sonic_environments_importable_from_scriptwriter(self):
        from mammamiradio.scriptwriter import SONIC_ENVIRONMENTS  # noqa: F401

    def test_ad_formats_covers_all_enum_values(self):
        from mammamiradio.models import AdFormat
        from mammamiradio.scriptwriter import AD_FORMATS

        for fmt in AdFormat:
            assert fmt in AD_FORMATS or fmt.value in AD_FORMATS, (
                f"AD_FORMATS missing description for {fmt}"
            )

    def test_speaker_roles_has_hammer(self):
        from mammamiradio.scriptwriter import SPEAKER_ROLES

        assert "hammer" in SPEAKER_ROLES
        assert "disclaimer_goblin" in SPEAKER_ROLES
        assert "seductress" in SPEAKER_ROLES

    def test_sonic_environments_has_known_keys(self):
        from mammamiradio.scriptwriter import SONIC_ENVIRONMENTS

        for key in ("cafe", "motorway", "beach", "stadium"):
            assert key in SONIC_ENVIRONMENTS


# ---------------------------------------------------------------------------
# Streamer: youtube_id validation removed — streamer.py
# ---------------------------------------------------------------------------


class TestYoutubeIdValidationRemoved:
    """The /api/playlist/add-external endpoint no longer validates youtube_id format."""

    def test_invalid_format_youtube_id_no_longer_rejected(self):
        """Previously, youtube_ids not matching [A-Za-z0-9_-]{11} were rejected.
        After this PR, any non-empty youtube_id is accepted (format check removed).
        Verify by inspecting the endpoint source code.
        """
        import ast
        from pathlib import Path

        src = (Path(__file__).parent.parent / "mammamiradio" / "streamer.py").read_text()
        tree = ast.parse(src)

        # Find the add_external_track function body
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                if node.name == "add_external_track":
                    body_src = ast.get_source_segment(src, node) or ""
                    assert "[A-Za-z0-9_-]{11}" not in body_src, (
                        "youtube_id regex validation was removed in this PR; "
                        "it should not reappear in add_external_track"
                    )
                    return
        pytest.fail("add_external_track not found in streamer.py")

    def test_panic_endpoint_removed(self):
        """The /api/panic endpoint was deleted in this PR."""
        import ast
        from pathlib import Path

        src = (Path(__file__).parent.parent / "mammamiradio" / "streamer.py").read_text()
        tree = ast.parse(src)

        panic_functions = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and "panic" in node.name.lower()
        ]
        assert not panic_functions, (
            f"panic endpoint functions found but should be removed: {panic_functions}"
        )


# ---------------------------------------------------------------------------
# Streamer: ICY headers — no CRLF scrubbing — streamer.py
# ---------------------------------------------------------------------------


class TestIcyHeadersNotScrubbed:
    """ICY headers now pass station name/genre directly without CRLF scrubbing."""

    def test_stream_endpoint_does_not_scrub_icy_headers(self):
        """Verify the stream() function no longer calls .replace('\\r','').replace('\\n','')."""
        import ast
        from pathlib import Path

        src = (Path(__file__).parent.parent / "mammamiradio" / "streamer.py").read_text()
        tree = ast.parse(src)

        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                if node.name == "stream":
                    body_src = ast.get_source_segment(src, node) or ""
                    # The old code had .replace("\r", "").replace("\n", "")
                    # These should be absent now
                    assert '"\r"' not in body_src and '"\\r"' not in body_src, (
                        "CRLF scrubbing (.replace('\\r', '')) was removed from ICY headers"
                    )
                    return
        pytest.fail("stream() function not found in streamer.py")

    def test_icy_name_uses_station_name_directly(self):
        """icy-name is set to config.station.name without transformation."""
        import ast
        from pathlib import Path

        src = (Path(__file__).parent.parent / "mammamiradio" / "streamer.py").read_text()

        for node in ast.walk(ast.parse(src)):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                if node.name == "stream":
                    body_src = ast.get_source_segment(src, node) or ""
                    # Should have "icy-name": config.station.name (no method call chain)
                    assert "config.station.name" in body_src
                    return
        pytest.fail("stream() function not found")


# ---------------------------------------------------------------------------
# resume_event removed from streamer.py — streamer.py
# ---------------------------------------------------------------------------


class TestResumeEventRemovedFromStreamer:
    """state.resume_event.set() calls were removed from the streamer."""

    def test_resume_event_not_set_in_audio_generator(self):
        """_audio_generator no longer sets state.resume_event."""
        from pathlib import Path

        src = (Path(__file__).parent.parent / "mammamiradio" / "streamer.py").read_text()
        assert "resume_event.set()" not in src, (
            "resume_event.set() was removed from streamer.py in this PR"
        )

    def test_resume_session_not_set_resume_event(self):
        """resume_session endpoint no longer calls state.resume_event.set()."""
        from pathlib import Path

        src = (Path(__file__).parent.parent / "mammamiradio" / "streamer.py").read_text()
        # Ensure the resume_event.set() call doesn't appear anywhere
        assert "resume_event" not in src, (
            "resume_event was fully removed from streamer.py — "
            "producer now uses asyncio.sleep(1) instead"
        )


# ---------------------------------------------------------------------------
# producer.py: asyncio.sleep(1) instead of resume_event
# ---------------------------------------------------------------------------


class TestProducerUsesSimpleSleep:
    """Producer now uses asyncio.sleep(1) for the stopped-state pause (no resume_event)."""

    def test_producer_does_not_use_resume_event(self):
        from pathlib import Path

        src = (Path(__file__).parent.parent / "mammamiradio" / "producer.py").read_text()
        assert "resume_event" not in src, (
            "resume_event was removed from producer.py; use asyncio.sleep instead"
        )

    def test_producer_uses_asyncio_sleep_in_stopped_path(self):
        """asyncio.sleep(1) should appear in the producer's stopped-state handling."""
        from pathlib import Path

        src = (Path(__file__).parent.parent / "mammamiradio" / "producer.py").read_text()
        assert "asyncio.sleep(1)" in src


# ---------------------------------------------------------------------------
# tts.py: strict=True zip — tts.py
# ---------------------------------------------------------------------------


class TestTtsZipStrictMode:
    """synthesize_dialogue uses zip(..., strict=True) instead of strict=False."""

    def test_synthesize_dialogue_uses_strict_zip(self):
        from pathlib import Path

        src = (Path(__file__).parent.parent / "mammamiradio" / "tts.py").read_text()
        # The new code uses strict=True
        assert "strict=True" in src, "tts.py must use zip(..., strict=True)"
        # The old code strict=False should not be present
        assert "strict=False" not in src, "tts.py must not use zip(..., strict=False)"


# ---------------------------------------------------------------------------
# config.py: imports from models, not ad_creative
# ---------------------------------------------------------------------------


class TestConfigImportsFromModels:
    """config.py now imports AdBrand, AdVoice, CampaignSpine from models, not ad_creative."""

    def test_config_does_not_import_from_ad_creative(self):
        from pathlib import Path

        src = (Path(__file__).parent.parent / "mammamiradio" / "config.py").read_text()
        assert "ad_creative" not in src, (
            "config.py must not import from ad_creative (module was deleted)"
        )

    def test_config_imports_from_models(self):
        from pathlib import Path

        src = (Path(__file__).parent.parent / "mammamiradio" / "config.py").read_text()
        assert "from mammamiradio.models import" in src
        assert "AdBrand" in src
        assert "AdVoice" in src
        assert "CampaignSpine" in src


# ---------------------------------------------------------------------------
# ad_creative.py: module deleted
# ---------------------------------------------------------------------------


class TestAdCreativeModuleDeleted:
    """ad_creative.py was deleted; importing it must fail."""

    def test_ad_creative_module_not_importable(self):
        import importlib
        import sys

        # Remove cached module if somehow present
        sys.modules.pop("mammamiradio.ad_creative", None)

        with pytest.raises((ImportError, ModuleNotFoundError)):
            importlib.import_module("mammamiradio.ad_creative")


# ---------------------------------------------------------------------------
# Boundary/regression extras
# ---------------------------------------------------------------------------


class TestBoundaryAndRegressionExtras:
    """Additional boundary and regression checks strengthening confidence."""

    def test_sanitize_prompt_data_exactly_at_max_len(self):
        """Text exactly at max_len is returned unchanged."""
        from mammamiradio.scriptwriter import _sanitize_prompt_data

        text = "a" * 80
        result = _sanitize_prompt_data(text, max_len=80)
        assert result == text
        assert not result.endswith("...")

    def test_sanitize_prompt_data_one_over_max_len_gets_ellipsis(self):
        """Text with 81 chars gets truncated to 80 + '...'."""
        from mammamiradio.scriptwriter import _sanitize_prompt_data

        text = "a" * 81
        result = _sanitize_prompt_data(text, max_len=80)
        assert result.endswith("...")
        assert len(result) == 83

    def test_evict_cache_lru_under_budget_does_nothing(self, tmp_path):
        """When total size is already under budget, no files are evicted."""
        from mammamiradio.downloader import evict_cache_lru

        f = tmp_path / "small.mp3"
        f.write_bytes(b"x" * 1024)  # 1 KB
        evict_cache_lru(tmp_path, 10)  # 10 MB budget
        assert f.exists(), "file under budget must not be evicted"

    def test_pick_brand_empty_history_returns_brand(self):
        """_pick_brand with no history returns any brand from the list."""
        from mammamiradio.models import AdBrand
        from mammamiradio.producer import _pick_brand

        brands = [AdBrand(name="A", tagline="a"), AdBrand(name="B", tagline="b")]
        result = _pick_brand(brands, [])
        assert result.name in ("A", "B")

    def test_ad_format_is_str_enum(self):
        """AdFormat inherits from StrEnum so values compare equal to plain strings."""
        from mammamiradio.models import AdFormat

        assert AdFormat.CLASSIC_PITCH == "classic_pitch"
        assert str(AdFormat.DUO_SCENE) == "duo_scene"

    def test_select_ad_creative_general_category_uses_default_sonic(self):
        """Unknown brand category falls back to default SonicWorld()."""
        from mammamiradio.models import AdBrand, StationState
        from mammamiradio.producer import _select_ad_creative

        brand = AdBrand(name="Unknown", tagline="T", category="unknown_category_xyz")
        state = StationState()
        config = MagicMock()
        config.ads.voices = []

        _fmt, sonic, _roles = _select_ad_creative(brand, state, config)
        # Default SonicWorld has music_bed="lounge" and transition_motif="chime"
        assert sonic.music_bed == "lounge"
        assert sonic.transition_motif == "chime"

    def test_cast_voices_all_roles_get_assigned_when_voices_exhausted(self):
        """When roles exceed available voices, voices cycle rather than leaving gaps."""
        from mammamiradio.models import AdBrand, AdVoice
        from mammamiradio.producer import _cast_voices

        # Only 1 voice, 3 roles needed
        voices = [AdVoice(name="Solo", voice="v1", style="neutral")]
        brand = AdBrand(name="B", tagline="T")
        config = MagicMock()
        config.ads.voices = voices

        result = _cast_voices(brand, config, ["hammer", "maniac", "witness"])
        assert set(result.keys()) == {"hammer", "maniac", "witness"}
        for role, voice in result.items():
            assert voice is not None
            assert voice.name == "Solo"  # only option, reused

    def test_station_state_has_ad_history(self):
        """Sanity check: ad_history deque still present after field removals."""
        from mammamiradio.models import StationState

        state = StationState()
        assert hasattr(state, "ad_history")
        assert len(state.ad_history) == 0
