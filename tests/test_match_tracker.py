"""Tests for src.match_tracker: MatchTracker (OpenDota client)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.match_tracker import (
    MatchTracker,
    OPENDOTA_API_BASE,
    DOTABUFF_BASE,
    DOTABUFF_HEADERS,
    _DotabuffFetchResult,
    _SHORT_RETRYABLE_FAILURE_TTL_SEC,
    _DOTABUFF_BREAKER_FAILURE_THRESHOLD,
)
from src.models import MatchInfo


def _make_mock_session(response_data):
    """Create a mock aiohttp.ClientSession that returns the given response data."""
    mock_response = AsyncMock()
    mock_response.json.return_value = response_data
    mock_response.text.return_value = ""
    mock_response.status = 200
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
        if isinstance(data, tuple):
            status, payload = data
        else:
            status, payload = 200, data
        mock_response.json.return_value = payload
        mock_response.text.return_value = payload if isinstance(payload, str) else ""
        mock_response.status = status
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
        mock_response.raise_for_status = MagicMock(side_effect=Exception("API error"))

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

    @pytest.mark.asyncio
    async def test_falls_back_to_dotabuff_when_opendota_empty(self):
        html = """
        <html><body>
        <h2>LATEST MATCHES</h2>
        <a href="/matches/8835491973">match</a>
        <a href="/heroes/witch-doctor">Witch Doctor</a>
        <time datetime="2026-06-02T11:22:33Z">1 day ago</time>
        <span>37:10</span>
        </body></html>
        """
        mock_session = _make_url_routed_session({
            f"{OPENDOTA_API_BASE}/players/39734273/matches?limit=1": [],
            f"{DOTABUFF_BASE}/players/39734273": (200, html),
        })
        tracker = _make_tracker(mock_session)

        result = await tracker.get_last_match("76561198000000001")

        assert result is not None
        assert result.match_id == "8835491973"
        assert result.source == "dotabuff"
        assert result.hero_name == "Witch Doctor"
        assert result.duration == 37 * 60 + 10
        assert result.start_time == 1780399353


class TestGetRecentMatches:
    def test_dotabuff_headers_include_browser_like_navigation_fields(self):
        assert DOTABUFF_HEADERS["Referer"] == "https://www.dotabuff.com/"
        assert DOTABUFF_HEADERS["Sec-Fetch-Site"] == "same-origin"
        assert DOTABUFF_HEADERS["Sec-Fetch-Mode"] == "navigate"
        assert DOTABUFF_HEADERS["Sec-Fetch-Dest"] == "document"
        assert DOTABUFF_HEADERS["Upgrade-Insecure-Requests"] == "1"

    @pytest.mark.asyncio
    async def test_recent_matches_use_dotabuff_fallback(self):
        html = """
        <html><body>
        <section>LATEST MATCHES
          <div>
            <a href="/matches/8835491973">match</a>
            <a href="/heroes/witch-doctor">Witch Doctor</a>
            <time datetime="2026-06-02T11:22:33Z">1 day ago</time>
            <span>37:10</span>
          </div>
          <div>
            <a href="/matches/8835345201">match</a>
            <a href="/heroes/grimstroke">Grimstroke</a>
            <time datetime="2026-06-02T10:10:00Z">1 day ago</time>
            <span>22:05</span>
          </div>
        </section>
        </body></html>
        """
        mock_session = _make_url_routed_session({
            f"{OPENDOTA_API_BASE}/players/39734273/matches?limit=2": [],
            f"{DOTABUFF_BASE}/players/39734273": (200, html),
        })
        tracker = _make_tracker(mock_session)

        result = await tracker.get_recent_matches("76561198000000001", limit=2)

        assert [m.match_id for m in result] == ["8835491973", "8835345201"]
        assert [m.source for m in result] == ["dotabuff", "dotabuff"]
        assert result[0].hero_name == "Witch Doctor"
        assert result[1].hero_name == "Grimstroke"
        assert result[0].duration == 37 * 60 + 10
        assert result[1].duration == 22 * 60 + 5

    def test_parse_dotabuff_browser_rows(self):
        tracker = _make_tracker(MagicMock())
        rows = [
            {
                "match_id": "8835491973",
                "hero_name": "Hoodwink",
                "hero_slug": "hoodwink",
                "duration_text": "42:35",
                "duration_sec": 2555,
                "relative_or_absolute_time": "2026-06-02T11:53:19+00:00",
            },
            {
                "match_id": "8835345201",
                "hero_name": "",
                "hero_slug": "drow-ranger",
                "duration_text": "33:01",
                "duration_sec": None,
                "relative_or_absolute_time": "a day ago",
            },
        ]

        with patch("src.match_tracker.time.time", return_value=1_800_000_000):
            result = tracker._parse_dotabuff_browser_rows(rows, "76561198000000001", limit=2)

        assert [m.match_id for m in result] == ["8835491973", "8835345201"]
        assert result[0].hero_name == "Hoodwink"
        assert result[0].duration == 2555
        assert result[0].start_time == 1780401199
        assert result[1].hero_name == "Drow Ranger"
        assert result[1].duration == 33 * 60 + 1
        assert result[1].start_time == 1_800_000_000 - 86400
        assert result[1].source == "dotabuff"

    @pytest.mark.asyncio
    async def test_dotabuff_cache_is_scoped_by_limit(self):
        html = """
        <html><body>
        <section>LATEST MATCHES
          <div>
            <a href="/matches/8835491973">match</a>
            <a href="/heroes/witch-doctor">Witch Doctor</a>
            <time datetime="2026-06-02T11:22:33Z">1 day ago</time>
            <span>37:10</span>
          </div>
          <div>
            <a href="/matches/8835345201">match</a>
            <a href="/heroes/grimstroke">Grimstroke</a>
            <time datetime="2026-06-02T10:10:00Z">1 day ago</time>
            <span>22:05</span>
          </div>
        </section>
        </body></html>
        """
        mock_session = _make_url_routed_session({
            f"{OPENDOTA_API_BASE}/players/39734273/matches?limit=1": [],
            f"{OPENDOTA_API_BASE}/players/39734273/matches?limit=2": [],
            f"{DOTABUFF_BASE}/players/39734273": (200, html),
        })
        tracker = _make_tracker(mock_session)

        last_match = await tracker.get_last_match("76561198000000001")
        recent_matches = await tracker.get_recent_matches("76561198000000001", limit=2)

        assert last_match is not None
        assert last_match.match_id == "8835491973"
        assert [m.match_id for m in recent_matches] == ["8835491973", "8835345201"]

    def test_static_dotabuff_parser_does_not_reuse_previous_row_timestamp(self):
        tracker = _make_tracker(MagicMock())
        html = """
        <html><body>
        <section>LATEST MATCHES
          <div>
            <a href="/matches/111">match</a>
            <a href="/heroes/witch-doctor">Witch Doctor</a>
            <time datetime="2026-06-02T11:22:33Z">1 day ago</time>
            <span>37:10</span>
          </div>
          <div>
            <a href="/matches/222">match</a>
            <a href="/heroes/grimstroke">Grimstroke</a>
            <span>22:05</span>
          </div>
        </section>
        </body></html>
        """

        result = tracker._parse_dotabuff_matches(html, "76561198000000001", limit=2)

        assert [m.match_id for m in result] == ["111"]

    def test_static_dotabuff_parser_handles_modern_overview_rows(self):
        tracker = _make_tracker(MagicMock())
        html = """
        <html><body>
        Latest Matches<div class="more"><a href="/players/1258333388/matches">more</a></div></header><article>
          <div class="r-table r-only-mobile-5 performances-overview">
            <div class="r-row ">
              <div class="r-fluid r-40 r-icon-text">
                <div class="r-body">
                  <a href="/heroes/drow-ranger"><img alt="Drow Ranger"/></a>
                  <a href="/matches/8838344748">Drow Ranger</a>
                </div>
              </div>
              <div class="r-fluid r-175 r-text-only r-right r-match-result">
                <div class="r-body">
                  <a class="lost" href="/matches/8838344748">Lost Match</a>
                  <div class="subtext"><time datetime="2026-06-04T16:26:35+00:00" title="Thu, 04 Jun 2026 16:26:35 +0000">2026-06-04</time></div>
                </div>
              </div>
              <div class="r-fluid r-125 r-line-graph r-duration">
                <div class="r-label">Duration</div>
                <div class="r-body">38:30<div class="bar"></div></div>
              </div>
              <div class="clearfix"></div>
            </div>
            <div class="r-row ">
              <div class="r-fluid r-40 r-icon-text">
                <div class="r-body">
                  <a href="/heroes/terrorblade"><img alt="Terrorblade"/></a>
                  <a href="/matches/8837828534">Terrorblade</a>
                </div>
              </div>
              <div class="r-fluid r-175 r-text-only r-right r-match-result">
                <div class="r-body">
                  <a class="won" href="/matches/8837828534">Won Match</a>
                  <div class="subtext"><time datetime="2026-06-04T08:50:21+00:00" title="Thu, 04 Jun 2026 08:50:21 +0000">2026-06-04</time></div>
                </div>
              </div>
              <div class="r-fluid r-125 r-line-graph r-duration">
                <div class="r-label">Duration</div>
                <div class="r-body">52:02<div class="bar"></div></div>
              </div>
              <div class="clearfix"></div>
            </div>
          </div>
        </body></html>
        """

        result = tracker._parse_dotabuff_matches(html, "76561198000000001", limit=2)

        assert [m.match_id for m in result] == ["8838344748", "8837828534"]
        assert result[0].hero_name == "Drow Ranger"
        assert result[0].duration == 38 * 60 + 30
        assert result[0].start_time == 1780590395
        assert result[1].hero_name == "Terrorblade"
        assert result[1].duration == 52 * 60 + 2
        assert result[1].start_time == 1780563021

    @pytest.mark.asyncio
    async def test_mixed_case_latest_matches_uses_static_parse_without_browser(self):
        html = """
        <html><body>
        <section>Latest Matches
          <div>
            <a href="/matches/8835491973">match</a>
            <a href="/heroes/witch-doctor">Witch Doctor</a>
            <time datetime="2026-06-02T11:22:33Z">1 day ago</time>
            <span>37:10</span>
          </div>
        </section>
        </body></html>
        """
        mock_session = _make_url_routed_session({
            f"{DOTABUFF_BASE}/players/39734273": (200, html),
        })
        tracker = _make_tracker(mock_session)
        tracker._get_recent_matches_dotabuff_browser = AsyncMock(
            return_value=_DotabuffFetchResult([], reason="browser_should_not_run")
        )

        result = await tracker._get_recent_matches_dotabuff("76561198000000001", limit=1)

        assert [m.match_id for m in result] == ["8835491973"]
        tracker._get_recent_matches_dotabuff_browser.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dotabuff_cache_reuses_partial_result_across_last_then_recent_calls(self):
        html = """
        <html><body>
        <section>Latest Matches
          <div>
            <a href="/matches/8835491973">match</a>
            <a href="/heroes/witch-doctor">Witch Doctor</a>
            <time datetime="2026-06-02T11:22:33Z">1 day ago</time>
            <span>37:10</span>
          </div>
          <div>
            <a href="/matches/8835345201">match</a>
            <a href="/heroes/grimstroke">Grimstroke</a>
            <time datetime="2026-06-02T10:10:00Z">1 day ago</time>
            <span>22:05</span>
          </div>
        </section>
        </body></html>
        """
        mock_session = _make_url_routed_session({
            f"{DOTABUFF_BASE}/players/39734273": (200, html),
        })
        tracker = _make_tracker(mock_session)
        tracker._get_last_match_opendota = AsyncMock(return_value=None)
        tracker._get_recent_matches_opendota = AsyncMock(return_value=[])
        tracker._get_recent_matches_dotabuff_browser = AsyncMock(
            return_value=_DotabuffFetchResult([], reason="browser_should_not_run")
        )

        last_match = await tracker.get_last_match("76561198000000001")
        recent_matches = await tracker.get_recent_matches("76561198000000001", limit=3)

        assert last_match is not None
        assert last_match.match_id == "8835491973"
        assert [m.match_id for m in recent_matches] == ["8835491973", "8835345201"]
        assert mock_session.get.call_count == 1
        tracker._get_recent_matches_dotabuff_browser.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_browser_budget_exhaustion_is_not_cached_as_no_matches(self):
        mock_session = _make_url_routed_session({
            f"{DOTABUFF_BASE}/players/39734273": (429, "too many requests"),
        })
        tracker = _make_tracker(mock_session)
        tracker._get_last_match_opendota = AsyncMock(return_value=None)
        tracker._dotabuff_browser_enabled = True
        tracker._browser_budget = 0
        tracker._browser_budget_window_start = 10**12

        with patch("src.match_tracker.DotabuffBrowserClient", object()):
            first = await tracker.get_last_match("76561198000000001")
            second = await tracker.get_last_match("76561198000000001")

        assert first is None
        assert second is None
        assert mock_session.get.call_count == 2
        assert "76561198000000001" not in tracker._dotabuff_cache

    @pytest.mark.asyncio
    async def test_retryable_dotabuff_failures_get_short_ttl_and_classified_reason(self):
        mock_session = _make_url_routed_session({
            f"{DOTABUFF_BASE}/players/39734273": (429, "too many requests"),
        })
        tracker = _make_tracker(mock_session)
        tracker._dotabuff_browser_enabled = False

        with patch("src.match_tracker.time.time", return_value=1_000):
            result = await tracker._get_recent_matches_dotabuff("76561198000000001", limit=1)

        assert result == []
        cached = tracker._dotabuff_cache["76561198000000001"]
        assert cached.reason == "http_429__browser_disabled"
        assert cached.expiry_ts == 1_000 + _SHORT_RETRYABLE_FAILURE_TTL_SEC

    @pytest.mark.asyncio
    async def test_stable_not_found_dotabuff_failure_keeps_negative_ttl(self):
        mock_session = _make_url_routed_session({
            f"{DOTABUFF_BASE}/players/39734273": (404, "player not found"),
        })
        tracker = _make_tracker(mock_session)
        tracker._dotabuff_browser_enabled = False

        with patch("src.match_tracker.time.time", return_value=1_000):
            result = await tracker._get_recent_matches_dotabuff("76561198000000001", limit=1)

        assert result == []
        cached = tracker._dotabuff_cache["76561198000000001"]
        assert cached.reason == "http_404__browser_disabled"
        assert cached.expiry_ts > 1_000 + _SHORT_RETRYABLE_FAILURE_TTL_SEC

    @pytest.mark.asyncio
    async def test_stable_not_found_still_caches_when_browser_budget_is_exhausted(self):
        mock_session = _make_url_routed_session({
            f"{DOTABUFF_BASE}/players/39734273": (404, "player not found"),
        })
        tracker = _make_tracker(mock_session)
        tracker._dotabuff_browser_enabled = True
        tracker._browser_budget = 0
        tracker._browser_budget_window_start = 10**12

        with patch("src.match_tracker.DotabuffBrowserClient", object()), patch(
            "src.match_tracker.time.time", return_value=1_000
        ):
            result = await tracker._get_recent_matches_dotabuff("76561198000000001", limit=1)

        assert result == []
        cached = tracker._dotabuff_cache["76561198000000001"]
        assert cached.reason == "http_404__browser_budget_exhausted"
        assert cached.expiry_ts > 1_000 + _SHORT_RETRYABLE_FAILURE_TTL_SEC

    @pytest.mark.asyncio
    async def test_repeated_throttle_failures_open_global_dotabuff_breaker(self):
        mock_session = _make_url_routed_session({
            f"{DOTABUFF_BASE}/players/39734273": (429, "too many requests"),
            f"{DOTABUFF_BASE}/players/39734274": (429, "too many requests"),
            f"{DOTABUFF_BASE}/players/39734275": (429, "too many requests"),
            f"{DOTABUFF_BASE}/players/39734276": (429, "too many requests"),
        })
        tracker = _make_tracker(mock_session)
        tracker._dotabuff_browser_enabled = False

        steam_ids = [
            "76561198000000001",
            "76561198000000002",
            "76561198000000003",
            "76561198000000004",
        ]
        first_three = steam_ids[:_DOTABUFF_BREAKER_FAILURE_THRESHOLD]
        for steam_id in first_three:
            assert await tracker._get_recent_matches_dotabuff(steam_id, limit=1) == []

        assert tracker._dotabuff_failure_streak == _DOTABUFF_BREAKER_FAILURE_THRESHOLD
        assert tracker._dotabuff_breaker_open_until > 0

        before_calls = mock_session.get.call_count
        blocked = await tracker._get_recent_matches_dotabuff(steam_ids[3], limit=1)

        assert blocked == []
        assert mock_session.get.call_count == before_calls

    @pytest.mark.asyncio
    async def test_dotabuff_breaker_resets_after_cooldown(self):
        mock_session = _make_url_routed_session({
            f"{DOTABUFF_BASE}/players/39734276": (429, "too many requests"),
        })
        tracker = _make_tracker(mock_session)
        tracker._dotabuff_browser_enabled = False
        tracker._dotabuff_failure_streak = _DOTABUFF_BREAKER_FAILURE_THRESHOLD
        tracker._dotabuff_last_failure_reason = "http_429__browser_disabled"
        tracker._dotabuff_breaker_open_until = 1_500

        with patch("src.match_tracker.time.time", return_value=1_501):
            result = await tracker._get_recent_matches_dotabuff("76561198000000004", limit=1)

        assert result == []
        assert mock_session.get.call_count == 1
        assert tracker._dotabuff_failure_streak == 1
        assert tracker._dotabuff_last_failure_reason == "http_429__browser_disabled"
        assert tracker._dotabuff_breaker_open_until == 0.0


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


def _make_counting_session(response_data, status=200):
    """Mock session whose .get records a call count via session.call_count."""
    counter = {"n": 0}

    def get(url, *args, **kwargs):
        counter["n"] += 1
        mock_response = AsyncMock()
        mock_response.json.return_value = response_data
        mock_response.text.return_value = ""
        mock_response.status = status
        mock_response.raise_for_status = MagicMock()
        ctx_mgr = MagicMock()
        ctx_mgr.__aenter__ = AsyncMock(return_value=mock_response)
        ctx_mgr.__aexit__ = AsyncMock(return_value=False)
        return ctx_mgr

    mock_session = MagicMock()
    mock_session.get.side_effect = get
    mock_session.call_counter = counter
    return mock_session


class TestGetPlayerRank:
    @pytest.mark.asyncio
    async def test_returns_parsed_dict(self):
        session = _make_counting_session({
            "mmr_estimate": {"estimate": 4200},
            "rank_tier": 54,
        })
        tracker = _make_tracker(session)
        result = await tracker.get_player_rank("76561198000000001")
        assert result == {"mmr": 4200, "rank_tier": 54}

    @pytest.mark.asyncio
    async def test_invalid_steam_id_returns_none(self):
        session = _make_counting_session({})
        tracker = _make_tracker(session)
        result = await tracker.get_player_rank("not_a_number")
        assert result is None
        # No HTTP request issued for an invalid id.
        assert session.call_counter["n"] == 0

    @pytest.mark.asyncio
    async def test_non_positive_account_id_returns_none(self):
        session = _make_counting_session({})
        tracker = _make_tracker(session)
        result = await tracker.get_player_rank("100")  # below STEAM_ID_OFFSET
        assert result is None
        assert session.call_counter["n"] == 0

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self):
        session = _make_counting_session({}, status=429)
        tracker = _make_tracker(session)
        result = await tracker.get_player_rank("76561198000000001")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_result_is_cached(self):
        # Player exists but has no MMR / rank → empty fields, still cached.
        session = _make_counting_session({"mmr_estimate": None, "rank_tier": None})
        tracker = _make_tracker(session)

        first = await tracker.get_player_rank("76561198000000001")
        second = await tracker.get_player_rank("76561198000000001")

        assert first == {"mmr": None, "rank_tier": None}
        assert second == first
        # Second call within TTL must not issue another HTTP request.
        assert session.call_counter["n"] == 1

    @pytest.mark.asyncio
    async def test_positive_result_is_cached(self):
        session = _make_counting_session({
            "mmr_estimate": {"estimate": 3000},
            "rank_tier": 42,
        })
        tracker = _make_tracker(session)

        await tracker.get_player_rank("76561198000000001")
        await tracker.get_player_rank("76561198000000001")

        assert session.call_counter["n"] == 1
