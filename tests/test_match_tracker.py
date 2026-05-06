"""Tests for src.match_tracker: MatchTracker (OpenDota client)."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.match_tracker import MatchTracker, OPENDOTA_API_BASE
from src.models import MatchInfo


def _make_mock_session(response_data):
    """Create a mock aiohttp.ClientSession that returns the given response data."""
    mock_response = AsyncMock()
    mock_response.json.return_value = response_data
    mock_response.raise_for_status = MagicMock()

    ctx_mgr = MagicMock()
    ctx_mgr.__aenter__ = AsyncMock(return_value=mock_response)
    ctx_mgr.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get.return_value = ctx_mgr
    return mock_session


def _make_url_routed_session(url_to_data: dict):
    """Mock session.get that returns different JSON depending on URL."""
    def get(url, *args, **kwargs):
        data = url_to_data.get(url)
        for key, val in url_to_data.items():
            if url.startswith(key):
                data = val
                break
        mock_response = AsyncMock()
        mock_response.json.return_value = data
        mock_response.raise_for_status = MagicMock()
        ctx_mgr = MagicMock()
        ctx_mgr.__aenter__ = AsyncMock(return_value=mock_response)
        ctx_mgr.__aexit__ = AsyncMock(return_value=False)
        return ctx_mgr

    mock_session = MagicMock()
    mock_session.get.side_effect = get
    return mock_session


def _make_tracker(session):
    tracker = MatchTracker(session)
    tracker._min_interval = 0.0  # disable rate limiting for tests
    return tracker


class TestToAccountId:
    def test_valid_steam_id(self):
        # 76561198000000001 - 76561197960265728 = 39734273
        assert MatchTracker._to_account_id("76561198000000001") == 39734273

    def test_invalid_steam_id(self):
        assert MatchTracker._to_account_id("not_a_number") is None

    def test_negative_account_id(self):
        # A steam_id well below the offset gives a non-positive account_id;
        # get_last_match treats account_id <= 0 as invalid.
        # Here we just check _to_account_id returns the (negative) value.
        result = MatchTracker._to_account_id("100")
        assert result is not None
        assert result <= 0


class TestGetLastMatch:
    @pytest.mark.asyncio
    async def test_returns_match_info(self):
        mock_session = _make_mock_session([
            {
                "match_id": 1234567890,
                "start_time": 1705319400,
                "duration": 2400,
                "hero_id": 0,  # falsy → no hero lookup
            }
        ])
        tracker = _make_tracker(mock_session)
        result = await tracker.get_last_match("76561198000000001")

        assert isinstance(result, MatchInfo)
        assert result.match_id == "1234567890"
        assert result.steam_id == "76561198000000001"
        assert result.game == "dota2"
        assert result.start_time == 1705319400
        assert result.duration == 2400
        assert result.hero_name is None

    @pytest.mark.asyncio
    async def test_no_matches_returns_none(self):
        mock_session = _make_mock_session([])
        tracker = _make_tracker(mock_session)
        result = await tracker.get_last_match("76561198000000001")
        assert result is None

    @pytest.mark.asyncio
    async def test_api_error_returns_none(self):
        mock_response = AsyncMock()
        mock_response.raise_for_status.side_effect = Exception("API error")

        ctx_mgr = MagicMock()
        ctx_mgr.__aenter__ = AsyncMock(return_value=mock_response)
        ctx_mgr.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get.return_value = ctx_mgr

        tracker = _make_tracker(mock_session)
        result = await tracker.get_last_match("76561198000000001")
        assert result is None

    @pytest.mark.asyncio
    async def test_hero_name_resolved(self):
        heroes_url = f"{OPENDOTA_API_BASE}/heroes"
        matches_url = f"{OPENDOTA_API_BASE}/players/39734273/matches"
        mock_session = _make_url_routed_session({
            heroes_url: [
                {"id": 1, "localized_name": "Anti-Mage"},
                {"id": 14, "localized_name": "Pudge"},
            ],
            matches_url: [
                {
                    "match_id": 7777,
                    "start_time": 1700000000,
                    "duration": 1800,
                    "hero_id": 14,
                }
            ],
        })

        tracker = _make_tracker(mock_session)
        result = await tracker.get_last_match("76561198000000001")

        assert result is not None
        assert result.hero_name == "Pudge"

    @pytest.mark.asyncio
    async def test_hero_cache_loaded_once(self):
        heroes_url = f"{OPENDOTA_API_BASE}/heroes"
        matches_url = f"{OPENDOTA_API_BASE}/players/39734273/matches"
        mock_session = _make_url_routed_session({
            heroes_url: [{"id": 1, "localized_name": "Anti-Mage"}],
            matches_url: [
                {
                    "match_id": 1,
                    "start_time": 1700000000,
                    "duration": 100,
                    "hero_id": 1,
                }
            ],
        })

        tracker = _make_tracker(mock_session)
        await tracker.get_last_match("76561198000000001")
        await tracker.get_last_match("76561198000000001")

        heroes_calls = [
            c for c in mock_session.get.call_args_list
            if c.args and c.args[0] == heroes_url
        ]
        assert len(heroes_calls) == 1


class TestGetLastMatchesBatch:
    @pytest.mark.asyncio
    async def test_multiple_ids(self):
        # Map matches URLs per account_id; one id returns no matches.
        url_map = {
            f"{OPENDOTA_API_BASE}/players/39734273/matches": [
                {"match_id": 1, "start_time": 100, "duration": 10, "hero_id": 0}
            ],
            f"{OPENDOTA_API_BASE}/players/39734274/matches": [
                {"match_id": 2, "start_time": 200, "duration": 20, "hero_id": 0}
            ],
            f"{OPENDOTA_API_BASE}/players/39734275/matches": [],
        }
        mock_session = _make_url_routed_session(url_map)

        tracker = _make_tracker(mock_session)
        result = await tracker.get_last_matches_batch([
            "76561198000000001",
            "76561198000000002",
            "76561198000000003",
        ])

        assert len(result) == 2
        assert "76561198000000001" in result
        assert "76561198000000002" in result
        assert "76561198000000003" not in result

    @pytest.mark.asyncio
    async def test_empty_list(self):
        mock_session = MagicMock()
        tracker = _make_tracker(mock_session)
        result = await tracker.get_last_matches_batch([])
        assert result == {}

    @pytest.mark.asyncio
    async def test_all_fail(self):
        mock_session = _make_mock_session([])  # all return empty arrays
        tracker = _make_tracker(mock_session)
        result = await tracker.get_last_matches_batch([
            "76561198000000001",
            "76561198000000002",
        ])
        assert result == {}
