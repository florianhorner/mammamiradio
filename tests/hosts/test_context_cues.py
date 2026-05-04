"""Tests for context_cues module: temporal awareness and uncanny host cues."""

from __future__ import annotations

import datetime
from unittest.mock import patch

from mammamiradio.hosts.context_cues import (
    compute_context_block,
    generate_impossible_line,
)


def _freeze_time(hour: int, weekday: int = 0, month: int = 6):
    """Return a mock datetime fixed to the given hour/weekday/month."""
    # weekday 0=Monday, 4=Friday, 5=Saturday, 6=Sunday
    # Find the next date matching the target weekday from a known Monday
    base = datetime.datetime(2026, 6, 1)  # Monday
    delta = (weekday - base.weekday()) % 7
    target = base + datetime.timedelta(days=delta)
    return target.replace(hour=hour, minute=30, month=month)


class TestShowSegments:
    """Verify correct show segment selection based on time of day."""

    def test_early_morning(self):
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(6)
            block = compute_context_block()
        assert "Alba dei Dannati" in block

    def test_morning_commute(self):
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(9)
            block = compute_context_block()
        assert "Mattina Pericolosa" in block

    def test_lunch_break(self):
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(13)
            block = compute_context_block()
        assert "Pausa Pranzo Sacra" in block

    def test_afternoon(self):
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(15)
            block = compute_context_block()
        assert "Pomeriggio Infinito" in block

    def test_evening(self):
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(19)
            block = compute_context_block()
        assert "Aperitivo Selvaggio" in block

    def test_late_evening(self):
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(22)
            block = compute_context_block()
        assert "Frequenza Notturna" in block

    def test_deep_night(self):
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(3)
            block = compute_context_block()
        assert "Radio Fantasma" in block


class TestDayOfWeek:
    """Verify day-of-week energy labels appear in context."""

    def test_monday_damage_control(self):
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(10, weekday=0)
            block = compute_context_block()
        assert "Controllo Danni" in block

    def test_friday_pre_game(self):
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(17, weekday=4)
            block = compute_context_block()
        assert "Pre-Game Set" in block

    def test_sunday_slow_spin(self):
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(11, weekday=6)
            block = compute_context_block()
        assert "Slow Spin" in block

    def test_weekend_flag(self):
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(14, weekday=5)
            block = compute_context_block()
        assert "weekend" in block

    def test_weekday_flag(self):
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(14, weekday=2)
            block = compute_context_block()
        assert "weekday" in block


class TestContextRules:
    """Verify the structural rules of context block output."""

    def test_contains_context_rules_guidance(self):
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(10)
            block = compute_context_block()
        assert "CONTEXT RULES" in block
        assert "at most ONE" in block

    def test_contains_show_segment(self):
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(10)
            block = compute_context_block()
        assert "SHOW SEGMENT:" in block

    def test_seasonal_cue_appears_for_valid_month(self):
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(10, month=12)
            block = compute_context_block()
        assert "Seasonal" in block

    def test_fourth_wall_rare_early(self):
        """Fourth wall should never appear in first 5 segments."""
        import random

        random.seed(42)
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(10)
            appearances = 0
            for _ in range(100):
                block = compute_context_block(segments_produced=3)
                if "Fourth wall" in block:
                    appearances += 1
        assert appearances == 0

    def test_fourth_wall_can_appear_later(self):
        """Fourth wall should be possible (but rare) after 5 segments."""
        import random

        random.seed(42)
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(10)
            appearances = 0
            for _ in range(200):
                block = compute_context_block(segments_produced=20)
                if "Fourth wall" in block:
                    appearances += 1
        # ~10% chance, so in 200 tries we should see some but not most
        assert 5 < appearances < 50


class TestListenerBehavior:
    """Verify behavioral cue injection."""

    def test_pause_resume_cue(self):
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(10)
            block = compute_context_block(listener_paused=True)
        assert "pausa" in block or "aspettato" in block


class TestGenerateImpossibleLine:
    """Tests for generate_impossible_line — zero-config uncanny DJ lines."""

    def test_first_listener_returns_special_line(self):
        line = generate_impossible_line(is_first_listener=True)
        assert isinstance(line, str)
        assert len(line) > 0

    def test_new_listener_early_segments(self):
        line = generate_impossible_line(is_new_listener=True, segments_produced=1)
        assert isinstance(line, str)
        assert len(line) > 0

    def test_new_listener_later_segments_uses_normal_path(self):
        # segments_produced >= 3 bypasses the new-listener early path
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(10)
            line = generate_impossible_line(is_new_listener=True, segments_produced=5)
        assert isinstance(line, str)

    def test_with_listener_patterns(self):
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(14)
            line = generate_impossible_line(listener_patterns=["energy_seeker"])
        assert isinstance(line, str)
        assert len(line) > 0

    def test_without_listener_patterns(self):
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(22)
            line = generate_impossible_line()
        assert isinstance(line, str)
        assert len(line) > 0

    def test_unknown_listener_pattern_falls_back(self):
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(10)
            line = generate_impossible_line(listener_patterns=["unknown_pattern"])
        assert isinstance(line, str)
        assert len(line) > 0

    def test_day_of_week_lines_included_probabilistically(self):
        import random

        random.seed(0)
        with patch("mammamiradio.hosts.context_cues.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _freeze_time(10, weekday=4)  # Friday
            lines = [generate_impossible_line() for _ in range(30)]
        assert all(isinstance(line, str) and len(line) > 0 for line in lines)
