import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.cs2_activity import (
    CS2ActivityResolver,
    MatchActivity,
    aggregate_sessions,
    build_activity_window,
    compute_recency,
    extract_csstats_matches,
    format_cs2_activity_lines,
    parse_leetify_api_profile,
)


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
    recency = compute_recency(window, now_ts=int(time.mktime((2026, 5, 30, 21, 30, 0, 0, 0, 0))))
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
