import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.cs2_activity import (
    CS2ActivityResolver,
    MatchActivity,
    PROVIDER_HTTP_TIMEOUT_SECONDS,
    aggregate_sessions,
    build_activity_window,
    compute_recency,
    extract_csstats_activity,
    extract_csstats_matches,
    format_cs2_activity_lines,
    is_likely_cloudflare_block,
    parse_leetify_api_profile,
)


class _FakeResponse:
    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body


class _FakeGetCM:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeSession:
    """Minimal stand-in for aiohttp.ClientSession used by the resolver."""

    def __init__(self, status: int = 200, body: str = "", exc: Exception | None = None):
        self._status = status
        self._body = body
        self._exc = exc
        self.calls: list[tuple] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers, timeout))
        if self._exc is not None:
            raise self._exc
        return _FakeGetCM(_FakeResponse(self._status, self._body))


CSSTATS_TS_NEWER = 1776017258
CSSTATS_TS_OLDER = 1776013000
CSSTATS_STATS_HTML = (
    '<table class="matches"><tbody>'
    f"<tr onclick=\"window.location='/match/424805029'\">"
    '<td class="map">de_mirage</td>'
    f'<td><span class="relative-time" data-timestamp="{CSSTATS_TS_NEWER}">moments ago</span></td>'
    '<td><span class="score">7:13</span></td></tr>'
    f"<tr onclick=\"window.location='/match/424800000'\">"
    '<td class="map">de_inferno</td>'
    f'<td><span class="relative-time" data-timestamp="{CSSTATS_TS_OLDER}">an hour ago</span></td>'
    '<td><span class="score">14:16</span></td></tr>'
    '</tbody></table>'
)

LEETIFY_API_JSON = """{
  "meta": {"name": "debustie", "steam64Id": "76561198000000000"},
  "games": [
    {"gameFinishedAt": "2026-05-28T00:00:00.000Z", "mapName": "de_inferno"},
    {"gameFinishedAt": "2026-05-20T00:00:00.000Z", "mapName": "de_dust2"},
    {"gameFinishedAt": "2026-05-20T00:00:00.000Z", "mapName": "de_nuke"}
  ]
}"""


def test_extract_csstats_matches_parses_urls_maps_and_timestamps():
    matches = extract_csstats_matches(CSSTATS_STATS_HTML)
    assert len(matches) == 2

    newest = next(match for match in matches if match.played_at_raw == str(CSSTATS_TS_NEWER))
    assert newest.time_precision == "minute"
    assert newest.match_url == "https://csstats.gg/match/424805029"
    assert newest.map_name == "de_mirage"

    older = next(match for match in matches if match.played_at_raw == str(CSSTATS_TS_OLDER))
    assert older.match_url == "https://csstats.gg/match/424800000"
    assert older.map_name == "de_inferno"


def test_build_activity_window_is_honest_about_coarse_clusters():
    window = build_activity_window(
        [
            MatchActivity("2026-05-28T01:00:00Z", "2026-05-28", "day"),
            MatchActivity("2026-05-28T00:00:00Z", "2026-05-28", "day"),
        ]
    )
    assert window is not None
    assert window.started_at_approx is None
    assert window.ended_at_approx == "2026-05-28T01:00:00Z"
    assert window.precision == "day"
    assert window.inference.method == "session_cluster"


def test_aggregate_sessions_clusters_with_two_hour_gap():
    sessions = aggregate_sessions(
        [
            MatchActivity("2026-05-28T21:30:00Z", "a", "minute"),
            MatchActivity("2026-05-28T20:45:00Z", "b", "minute"),
            MatchActivity("2026-05-28T20:00:00Z", "c", "minute"),
            MatchActivity("2026-05-28T10:00:00Z", "d", "minute"),
        ]
    )
    assert len(sessions) == 2
    assert sessions[0].match_count == 3
    assert sessions[0].started_at_approx == "2026-05-28T20:00:00Z"
    assert sessions[1].ended_at_approx == "2026-05-28T10:00:00Z"


def test_parse_leetify_api_profile_keeps_day_precision_cluster_weak():
    parsed = parse_leetify_api_profile(LEETIFY_API_JSON)
    assert parsed is not None
    profile_name, activity = parsed
    assert profile_name == "debustie"
    assert activity.last_activity_window is not None
    assert activity.last_activity_window.precision == "day"
    assert activity.last_activity_window.started_at_approx is None
    assert activity.signal_strength == "weak"
    assert activity.confidence == "low"


def test_compute_recency_bands():
    window = build_activity_window([MatchActivity("2026-05-28T21:30:00Z", "x", "minute")])
    # Timezone-independent: build the "now" instant from an explicit UTC datetime
    # instead of time.mktime, which interprets the tuple as local time.
    now_ts = int(datetime(2026, 5, 30, 21, 30, 0, tzinfo=timezone.utc).timestamp())
    recency = compute_recency(window, now_ts=now_ts)
    assert recency.band == "fresh"
    assert recency.age_days == 2


@pytest.mark.asyncio
async def test_resolver_prefers_precise_csstats_over_coarse_leetify():
    resolver = CS2ActivityResolver(MagicMock())

    async def fake_leetify(_: str):
        parsed = parse_leetify_api_profile(LEETIFY_API_JSON)
        assert parsed is not None
        profile_name, activity = parsed
        assert profile_name == "debustie"
        from src.cs2_activity import ProviderResult

        return ProviderResult(
            provider="leetify",
            status="found_recent_activity",
            profile_found=True,
            profile_url="https://leetify.com/public/profile/76561198000000000",
            last_activity_window=activity.last_activity_window,
            recent_matches=activity.recent_matches,
            recent_sessions=activity.recent_sessions,
            confidence=activity.confidence,
            signal_strength=activity.signal_strength,
            notes=["Recovered via Leetify API."],
        )

    async def fake_csstats(_: str):
        from src.cs2_activity import extract_csstats_activity, ProviderResult

        activity = extract_csstats_activity(CSSTATS_STATS_HTML)
        return ProviderResult(
            provider="csstats",
            status="found_recent_activity",
            profile_found=True,
            profile_url="https://csstats.gg/player/76561198000000000",
            last_activity_window=activity.last_activity_window,
            recent_matches=activity.recent_matches,
            recent_sessions=activity.recent_sessions,
            confidence=activity.confidence,
            signal_strength=activity.signal_strength,
            notes=["CSStats exposed server-rendered rows."],
        )

    resolver._lookup_leetify = fake_leetify  # type: ignore[method-assign]
    resolver._lookup_csstats = fake_csstats  # type: ignore[method-assign]

    result = await resolver.lookup("76561198000000000")
    assert result.source == "csstats"
    assert result.signal_strength in ("moderate", "strong")
    assert result.activity_window is not None
    assert result.activity_window.precision == "minute"


def test_format_lines_for_found_activity():
    from src.cs2_activity import ExtractedActivity, ProviderResult

    activity = build_activity_window([MatchActivity("2026-05-28T21:30:00Z", "x", "minute")])
    provider_result = ProviderResult(
        provider="csstats",
        status="found_recent_activity",
        profile_found=True,
        profile_url="https://csstats.gg/player/1",
        last_activity_window=activity,
        recent_matches=[MatchActivity("2026-05-28T21:30:00Z", "x", "minute", "https://csstats.gg/match/1", "de_mirage")],
        recent_sessions=[],
        confidence="low",
        signal_strength="weak",
        notes=[],
    )
    resolver = CS2ActivityResolver(MagicMock())
    result = resolver._to_response(provider_result, [])
    lines = format_cs2_activity_lines(result)
    assert any("CS2 активность" in line for line in lines)
    assert any("Источник: csstats" == line for line in lines)
    assert any("Карта: de_mirage" == line for line in lines)


def test_is_likely_cloudflare_block():
    assert is_likely_cloudflare_block(403, "")
    assert is_likely_cloudflare_block(429, "")
    assert is_likely_cloudflare_block(503, "")
    assert is_likely_cloudflare_block(200, "...cdn-cgi/challenge-platform...")
    assert is_likely_cloudflare_block(200, "Attention Required! | Cloudflare")
    assert not is_likely_cloudflare_block(200, "<html><body>normal page</body></html>")


def test_html_fallback_ignores_unrelated_page_timestamps():
    # No data-timestamp rows and no /match/ anchors -> must NOT fabricate a window
    # from footer/build/relative boilerplate.
    body = (
        "<footer>© 2026-01-01 csstats.gg</footer>"
        '<meta name="build" content="2026-05-20T10:00:00Z">'
        "<p>Last updated yesterday</p>"
        "<span>2026-05-27</span>"
    )
    activity = extract_csstats_activity(body)
    assert activity.last_activity_window is None
    assert activity.recent_matches == []
    assert activity.confidence == "none"
    assert activity.signal_strength == "none"


def test_html_fallback_accepts_timestamp_anchored_to_match_link():
    # A timestamp adjacent to a /match/ link is a genuine signal and should
    # survive the anchoring filter even on the no-data-timestamp fallback path.
    body = (
        "<footer>© 2026-01-01 csstats.gg</footer>"
        "<tr onclick=\"window.location='/match/424805029'\">"
        "<td>2026-05-28T21:30:00Z</td></tr>"
    )
    activity = extract_csstats_activity(body)
    assert activity.last_activity_window is not None
    assert activity.last_activity_window.ended_at_approx == "2026-05-28T21:30:00Z"


@pytest.mark.asyncio
async def test_lookup_csstats_parses_and_sets_hard_timeout():
    session = _FakeSession(status=200, body=CSSTATS_STATS_HTML)
    resolver = CS2ActivityResolver(session)
    result = await resolver._lookup_csstats("76561198000000000")
    assert result.status == "found_recent_activity"
    assert result.last_activity_window is not None
    # An explicit per-call timeout must be supplied to the transport.
    _, _, timeout = session.calls[0]
    assert timeout is not None
    assert timeout.total == PROVIDER_HTTP_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_lookup_csstats_handles_timeout_failure():
    session = _FakeSession(exc=asyncio.TimeoutError())
    resolver = CS2ActivityResolver(session)
    result = await resolver._lookup_csstats("76561198000000000")
    assert result.status == "unknown"
    assert result.profile_found is False
    assert result.last_activity_window is None


@pytest.mark.asyncio
async def test_lookup_csstats_handles_404():
    session = _FakeSession(status=404, body="not found")
    resolver = CS2ActivityResolver(session)
    result = await resolver._lookup_csstats("76561198000000000")
    assert result.status == "profile_not_found"
    assert result.profile_found is False


@pytest.mark.asyncio
async def test_lookup_csstats_handles_cloudflare_block():
    session = _FakeSession(status=403, body="Attention Required! | Cloudflare")
    resolver = CS2ActivityResolver(session)
    result = await resolver._lookup_csstats("76561198000000000")
    assert result.status == "provider_blocked"
    assert result.profile_found is False


@pytest.mark.asyncio
async def test_lookup_times_out_gracefully(monkeypatch):
    monkeypatch.setattr("src.cs2_activity.LOOKUP_TIMEOUT_SECONDS", 0.05)
    resolver = CS2ActivityResolver(MagicMock())

    async def slow_lookup(_: str):
        await asyncio.sleep(1)
        raise AssertionError("inner lookup should have been cancelled")

    resolver._lookup = slow_lookup  # type: ignore[method-assign]

    result = await resolver.lookup("76561198000000000")
    assert result.status == "unknown"
    assert result.source is None
    assert result.activity_window is None
    assert result.provider_diagnostics
    assert result.provider_diagnostics[0]["provider"] == "all"
