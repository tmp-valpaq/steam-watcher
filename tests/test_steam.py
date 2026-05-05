"""Tests for src.steam: detect_invisible, state_name, format_last_seen."""

import pytest
from src.steam import detect_invisible, state_name, format_last_seen


class TestStateName:
    def test_offline(self):
        assert state_name(0) == "Offline"

    def test_online(self):
        assert state_name(1) == "Online"

    def test_busy(self):
        assert state_name(2) == "Busy"

    def test_away(self):
        assert state_name(3) == "Away"

    def test_snooze(self):
        assert state_name(4) == "Snooze"

    def test_looking_to_trade(self):
        assert state_name(5) == "Looking to Trade"

    def test_looking_to_play(self):
        assert state_name(6) == "Looking to Play"

    def test_unknown_state(self):
        result = state_name(99)
        assert "Unknown" in result
        assert "99" in result


class TestFormatLastSeen:
    def test_none_returns_never(self):
        assert format_last_seen(None) == "never"

    def test_valid_timestamp(self):
        # 2024-01-15 12:30:00 UTC
        ts = 1705319400
        result = format_last_seen(ts)
        assert "2024" in result
        assert "UTC" in result

    def test_zero_timestamp(self):
        result = format_last_seen(0)
        assert "1970" in result


class TestDetectInvisible:
    def test_not_offline(self):
        """If persona_state != 0, never invisible."""
        assert detect_invisible(1, 100, 50) is False

    def test_offline_no_previous(self):
        """If no previous playtime, can't detect."""
        assert detect_invisible(0, 100, None) is False

    def test_offline_playtime_increased(self):
        """Offline + playtime increased = invisible."""
        assert detect_invisible(0, 100, 50) is True

    def test_offline_playtime_same(self):
        """Offline + playtime same = not invisible."""
        assert detect_invisible(0, 100, 100) is False

    def test_offline_playtime_decreased(self):
        """Offline + playtime decreased (edge case) = not invisible."""
        assert detect_invisible(0, 50, 100) is False

    def test_offline_playtime_zero_previous(self):
        """Offline + previous was 0 + now > 0 = invisible."""
        assert detect_invisible(0, 10, 0) is True

    def test_offline_playtime_both_zero(self):
        """Offline + both zero = not invisible."""
        assert detect_invisible(0, 0, 0) is False

    def test_busy_with_playtime_increase(self):
        """Busy state even with playtime increase should not be invisible."""
        assert detect_invisible(2, 200, 100) is False
