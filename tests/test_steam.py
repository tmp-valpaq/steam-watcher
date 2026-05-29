"""Tests for src.steam: detect_invisible, state_name, format_last_seen, batch API, rate limiter."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.steam import detect_invisible, state_name, format_last_seen, SteamClient
from src.models import SteamProfile


def _make_mock_session(response_data: dict):
    """Create a mock aiohttp.ClientSession that returns the given response data."""
    mock_response = AsyncMock()
    mock_response.json.return_value = response_data
    mock_response.raise_for_status = MagicMock()

    # Create a proper async context manager
    ctx_mgr = MagicMock()
    ctx_mgr.__aenter__ = AsyncMock(return_value=mock_response)
    ctx_mgr.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get.return_value = ctx_mgr
    return mock_session


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


class TestBatchGetPlayerSummaries:
    """Tests for the batch get_player_summaries_batch method."""

    @pytest.mark.asyncio
    async def test_batch_single_profile(self):
        """Batch with a single steam_id returns one profile."""
        mock_session = _make_mock_session({
            "response": {
                "players": [
                    {
                        "steamid": "76561198000000001",
                        "personaname": "TestUser",
                        "personastate": 1,
                    }
                ]
            }
        })

        client = SteamClient(mock_session)
        result = await client.get_player_summaries_batch(
            "test_key", ["76561198000000001"]
        )

        assert "76561198000000001" in result
        assert result["76561198000000001"].persona_name == "TestUser"
        assert result["76561198000000001"].persona_state == 1

    @pytest.mark.asyncio
    async def test_batch_multiple_profiles(self):
        """Batch with multiple steam_ids returns multiple profiles."""
        mock_session = _make_mock_session({
            "response": {
                "players": [
                    {
                        "steamid": "76561198000000001",
                        "personaname": "User1",
                        "personastate": 1,
                        "gameid": "730",
                        "gameextrainfo": "Counter-Strike 2",
                    },
                    {
                        "steamid": "76561198000000002",
                        "personaname": "User2",
                        "personastate": 0,
                        "lastlogoff": 1705320000,
                    },
                ]
            }
        })

        client = SteamClient(mock_session)
        result = await client.get_player_summaries_batch(
            "test_key",
            ["76561198000000001", "76561198000000002"],
        )

        assert len(result) == 2
        assert result["76561198000000001"].persona_name == "User1"
        assert result["76561198000000001"].game_name == "Counter-Strike 2"
        assert result["76561198000000002"].persona_state == 0
        assert result["76561198000000002"].last_logoff == 1705320000

    @pytest.mark.asyncio
    async def test_batch_empty_list(self):
        """Batch with empty list returns empty dict."""
        mock_session = MagicMock()
        client = SteamClient(mock_session)
        result = await client.get_player_summaries_batch("test_key", [])
        assert result == {}

    @pytest.mark.asyncio
    async def test_batch_api_error_returns_empty(self):
        """Batch request that throws returns empty dict."""
        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock(side_effect=Exception("API error"))

        ctx_mgr = MagicMock()
        ctx_mgr.__aenter__ = AsyncMock(return_value=mock_response)
        ctx_mgr.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get.return_value = ctx_mgr

        client = SteamClient(mock_session)
        result = await client.get_player_summaries_batch(
            "test_key", ["76561198000000001"]
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_batch_partial_results(self):
        """If API returns fewer profiles than requested, only those are returned."""
        mock_session = _make_mock_session({
            "response": {
                "players": [
                    {
                        "steamid": "76561198000000001",
                        "personaname": "User1",
                        "personastate": 1,
                    }
                ]
            }
        })

        client = SteamClient(mock_session)
        result = await client.get_player_summaries_batch(
            "test_key",
            ["76561198000000001", "76561198000000099"],
        )

        assert len(result) == 1
        assert "76561198000000001" in result
        assert "76561198000000099" not in result

    @pytest.mark.asyncio
    async def test_batch_sends_comma_separated_steamids(self):
        """Verify the batch method sends comma-separated steamids in the request."""
        mock_session = _make_mock_session({"response": {"players": []}})

        client = SteamClient(mock_session)
        await client.get_player_summaries_batch(
            "test_key",
            ["76561198000000001", "76561198000000002", "76561198000000003"],
        )

        # Verify the params passed to session.get
        call_args = mock_session.get.call_args
        params = call_args[1]["params"] if "params" in call_args[1] else call_args[0][1]
        assert params["steamids"] == "76561198000000001,76561198000000002,76561198000000003"


class TestPerKeyRateLimiter:
    """Tests for per-API-key rate limiting in SteamClient."""

    @pytest.mark.asyncio
    async def test_per_key_independent_rate_limits(self):
        """Different API keys have independent rate limits."""
        mock_session = _make_mock_session({"response": {"players": []}})

        client = SteamClient(mock_session)
        client._min_interval = 0.5  # 0.5s for faster testing

        # First call with key_a should not sleep
        import time
        start = time.monotonic()
        await client.get_player_summaries("key_a", "76561198000000001")
        elapsed_a1 = time.monotonic() - start

        # Immediate second call with same key should sleep
        start = time.monotonic()
        await client.get_player_summaries("key_a", "76561198000000002")
        elapsed_a2 = time.monotonic() - start

        # Call with different key_b should NOT sleep (independent)
        start = time.monotonic()
        await client.get_player_summaries("key_b", "76561198000000001")
        elapsed_b1 = time.monotonic() - start

        # The second call with key_a should have taken longer due to rate limit
        assert elapsed_a2 > elapsed_a1
        # The first call with key_b should be fast (no rate limit)
        assert elapsed_b1 < elapsed_a2

    @pytest.mark.asyncio
    async def test_same_key_rate_limited(self):
        """Rapid consecutive calls with same key are rate-limited."""
        mock_session = _make_mock_session({"response": {"players": []}})

        client = SteamClient(mock_session)
        client._min_interval = 0.3

        import time
        start = time.monotonic()
        await client.get_player_summaries("key_x", "76561198000000001")
        await client.get_player_summaries("key_x", "76561198000000002")
        elapsed = time.monotonic() - start

        # Two calls with same key and 0.3s interval should take at least 0.3s total
        assert elapsed >= 0.25  # Allow small margin

    @pytest.mark.asyncio
    async def test_different_keys_no_delay(self):
        """Calls with different keys don't delay each other."""
        mock_session = _make_mock_session({"response": {"players": []}})

        client = SteamClient(mock_session)
        client._min_interval = 1.0  # 1 second

        import time
        start = time.monotonic()
        await client.get_player_summaries("key_1", "76561198000000001")
        await client.get_player_summaries("key_2", "76561198000000001")
        await client.get_player_summaries("key_3", "76561198000000001")
        elapsed = time.monotonic() - start

        # Three calls with different keys should be fast (< 0.5s)
        assert elapsed < 0.5
