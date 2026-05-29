from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

import aiohttp


MatchTimePrecision = Literal["minute", "hour", "day", "relative", "unknown"]
ActivityInferenceMethod = Literal["single_match", "session_cluster", "relative_hint"]
ActivityRecencyBand = Literal["fresh", "recent", "stale", "dormant", "unknown"]
LastPlayedConfidence = Literal["none", "low", "medium", "high"]
ActivitySignalStrength = Literal["none", "weak", "moderate", "strong"]
ProviderStatus = Literal[
    "found_recent_activity",
    "found_profile_no_matches",
    "profile_not_found",
    "provider_blocked",
    "parse_failed",
    "unknown",
]

DEFAULT_SESSION_GAP_SECONDS = 2 * 60 * 60
DAY_SECONDS = 24 * 60 * 60
RECENCY_FRESH_MAX_DAYS = 2
RECENCY_RECENT_MAX_DAYS = 14
RECENCY_STALE_MAX_DAYS = 90

LEETIFY_API_URL = "https://api.cs-prod.leetify.com/api/profile/id/{steam_id}"
CSSTATS_URL = "https://csstats.gg/player/{steam_id}/stats"

# Hard timeouts so a slow/hung provider can never block a bot handler.
# Per-provider HTTP call ceiling and an overall whole-lookup ceiling.
PROVIDER_HTTP_TIMEOUT_SECONDS = 6.0
LOOKUP_TIMEOUT_SECONDS = 10.0

ISO_TIMESTAMP_REGEX = re.compile(
    r"\b(20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}))\b"
)
DATE_ONLY_REGEX = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
RELATIVE_TIME_REGEX = re.compile(
    r"\b(today|yesterday|\d+\s+(?:minute|hour|day|week|month|year)s?\s+ago)\b",
    re.IGNORECASE,
)
CSSTATS_TIMESTAMP_REGEX = re.compile(r"data-timestamp\s*=\s*[\"']?(\d{9,13})[\"']?", re.IGNORECASE)
CSSTATS_MATCH_URL_REGEX = re.compile(r"/match/(\d+)")
CSSTATS_MAP_REGEX = re.compile(r"\b(de_[a-z0-9]+)\b", re.IGNORECASE)
CSSTATS_ROW_CONTEXT = 600


@dataclass(frozen=True)
class MatchActivity:
    played_at: Optional[str]
    played_at_raw: Optional[str]
    time_precision: MatchTimePrecision
    match_url: Optional[str] = None
    map_name: Optional[str] = None


@dataclass(frozen=True)
class ActivityInference:
    method: ActivityInferenceMethod
    match_count: int
    note: str


@dataclass(frozen=True)
class ActivityWindow:
    started_at_approx: Optional[str]
    ended_at_approx: Optional[str]
    started_at_raw: Optional[str]
    ended_at_raw: Optional[str]
    precision: MatchTimePrecision
    inference: ActivityInference


@dataclass(frozen=True)
class ActivitySession:
    started_at_approx: Optional[str]
    ended_at_approx: Optional[str]
    match_count: int
    precision: MatchTimePrecision


@dataclass(frozen=True)
class ActivityRecency:
    band: ActivityRecencyBand
    age_days: Optional[int]
    age_seconds: Optional[int]
    evaluated_at: str
    based_on: Optional[str]
    note: str


@dataclass(frozen=True)
class ExtractedActivity:
    recent_matches: list[MatchActivity] = field(default_factory=list)
    recent_sessions: list[ActivitySession] = field(default_factory=list)
    last_activity_window: Optional[ActivityWindow] = None
    confidence: LastPlayedConfidence = "none"
    signal_strength: ActivitySignalStrength = "none"


@dataclass(frozen=True)
class ProviderResult:
    provider: str
    status: ProviderStatus
    profile_found: bool
    profile_url: Optional[str]
    last_activity_window: Optional[ActivityWindow]
    recent_matches: list[MatchActivity]
    recent_sessions: list[ActivitySession]
    confidence: LastPlayedConfidence
    signal_strength: ActivitySignalStrength
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CS2ActivityResult:
    status: Literal["found_recent_activity", "found_profile_no_matches", "unknown"]
    source: Optional[str]
    profile_found: bool
    profile_url: Optional[str]
    activity_window: Optional[ActivityWindow]
    recent_matches: list[MatchActivity]
    recent_sessions: list[ActivitySession]
    confidence: LastPlayedConfidence
    signal_strength: ActivitySignalStrength
    recency: ActivityRecency
    provider_diagnostics: list[dict[str, str]]


def _normalize_iso(value: str) -> str:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _coarsest_precision(precisions: list[MatchTimePrecision]) -> MatchTimePrecision:
    order = ["minute", "hour", "day", "relative", "unknown"]
    if not precisions:
        return "unknown"
    return max(precisions, key=lambda item: order.index(item))


def _band_for_age_days(age_days: int) -> ActivityRecencyBand:
    if age_days <= RECENCY_FRESH_MAX_DAYS:
        return "fresh"
    if age_days <= RECENCY_RECENT_MAX_DAYS:
        return "recent"
    if age_days <= RECENCY_STALE_MAX_DAYS:
        return "stale"
    return "dormant"


def aggregate_sessions(
    matches: list[MatchActivity], gap_seconds: int = DEFAULT_SESSION_GAP_SECONDS
) -> list[ActivitySession]:
    timed: list[tuple[MatchActivity, int]] = []
    for match in matches:
        if not match.played_at:
            continue
        try:
            ts = int(datetime.fromisoformat(match.played_at.replace("Z", "+00:00")).timestamp())
        except ValueError:
            continue
        timed.append((match, ts))

    timed.sort(key=lambda item: item[1], reverse=True)
    if not timed:
        return []

    clusters: list[list[tuple[MatchActivity, int]]] = [[timed[0]]]
    for entry in timed[1:]:
        current = clusters[-1]
        previous_ts = current[-1][1]
        if previous_ts - entry[1] <= gap_seconds:
            current.append(entry)
        else:
            clusters.append([entry])

    sessions: list[ActivitySession] = []
    for cluster in clusters:
        newest = cluster[0]
        oldest = cluster[-1]
        multi = len(cluster) > 1
        sessions.append(
            ActivitySession(
                started_at_approx=datetime.fromtimestamp(oldest[1], tz=timezone.utc).isoformat().replace("+00:00", "Z") if multi else None,
                ended_at_approx=datetime.fromtimestamp(newest[1], tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                match_count=len(cluster),
                precision=_coarsest_precision([item[0].time_precision for item in cluster]),
            )
        )
    return sessions


def build_activity_window(
    timed_matches: list[MatchActivity],
    relative_hints: Optional[list[MatchActivity]] = None,
    gap_seconds: int = DEFAULT_SESSION_GAP_SECONDS,
) -> Optional[ActivityWindow]:
    relative_hints = relative_hints or []
    timed: list[tuple[MatchActivity, int]] = []
    for match in timed_matches:
        if not match.played_at:
            continue
        try:
            ts = int(datetime.fromisoformat(match.played_at.replace("Z", "+00:00")).timestamp())
        except ValueError:
            continue
        timed.append((match, ts))

    timed.sort(key=lambda item: item[1], reverse=True)
    if timed:
        cluster: list[tuple[MatchActivity, int]] = [timed[0]]
        for entry in timed[1:]:
            previous_ts = cluster[-1][1]
            if previous_ts - entry[1] <= gap_seconds:
                cluster.append(entry)
            else:
                break

        newest = cluster[0]
        oldest = cluster[-1]
        multi = len(cluster) > 1
        precision = _coarsest_precision([item[0].time_precision for item in cluster])
        has_concrete_bounds = multi and all(
            item[0].time_precision in ("minute", "hour") for item in cluster
        )
        return ActivityWindow(
            started_at_approx=datetime.fromtimestamp(oldest[1], tz=timezone.utc).isoformat().replace("+00:00", "Z") if has_concrete_bounds else None,
            ended_at_approx=datetime.fromtimestamp(newest[1], tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            started_at_raw=oldest[0].played_at_raw if has_concrete_bounds else None,
            ended_at_raw=newest[0].played_at_raw,
            precision=precision,
            inference=ActivityInference(
                method="session_cluster" if multi else "single_match",
                match_count=len(cluster),
                note=(
                    f"Aggregated {len(cluster)} recent match timestamps into one approximate activity window."
                    if has_concrete_bounds
                    else (
                        f"Observed {len(cluster)} coarse timestamps close together, but the precision is too weak to claim a reliable session start."
                        if multi
                        else "Only one recent match timestamp was found, which is a weak end-of-activity signal rather than a known session start."
                    )
                ),
            ),
        )

    if relative_hints:
        hint = relative_hints[0]
        return ActivityWindow(
            started_at_approx=None,
            ended_at_approx=None,
            started_at_raw=None,
            ended_at_raw=hint.played_at_raw,
            precision="relative",
            inference=ActivityInference(
                method="relative_hint",
                match_count=len(relative_hints),
                note="Only a relative time hint was found; exact activity start/end are unknown.",
            ),
        )

    return None


def compute_recency(window: Optional[ActivityWindow], now_ts: Optional[int] = None) -> ActivityRecency:
    now_ts = int(time.time()) if now_ts is None else int(now_ts)
    evaluated_at = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    ended_at = window.ended_at_approx if window else None
    if not ended_at:
        return ActivityRecency(
            band="unknown",
            age_days=None,
            age_seconds=None,
            evaluated_at=evaluated_at,
            based_on=None,
            note=(
                "An activity signal exists but has no exact end instant to age, so recency is unknown."
                if window
                else "No activity window was discovered, so recency is unknown."
            ),
        )

    ended_ts = int(datetime.fromisoformat(ended_at.replace("Z", "+00:00")).timestamp())
    age_seconds = max(0, now_ts - ended_ts)
    age_days = age_seconds // DAY_SECONDS
    band = _band_for_age_days(age_days)
    precision = window.precision if window else "unknown"
    caveat = (
        " End time is coarse, so age is approximate."
        if precision not in ("minute", "hour")
        else ""
    )
    return ActivityRecency(
        band=band,
        age_days=age_days,
        age_seconds=age_seconds,
        evaluated_at=evaluated_at,
        based_on="ended_at_approx",
        note=f"Measured about {age_days} day(s) since the approximate end of the last discoverable activity.{caveat}",
    )


def confidence_for_window(window: Optional[ActivityWindow]) -> LastPlayedConfidence:
    if not window:
        return "none"
    if window.inference.method == "relative_hint":
        return "low"
    if window.inference.method == "single_match":
        return "low"
    if window.precision in ("minute", "hour"):
        return "high" if window.inference.match_count >= 3 else "medium"
    return "low"


def signal_strength_for_window(window: Optional[ActivityWindow]) -> ActivitySignalStrength:
    if not window:
        return "none"
    if window.inference.method == "relative_hint":
        return "weak"
    if window.inference.method == "single_match":
        return "weak"
    if window.precision in ("minute", "hour"):
        return "strong" if window.inference.match_count >= 3 else "moderate"
    return "weak"


def is_likely_cloudflare_block(status: int, body: str) -> bool:
    if status in (403, 429, 503):
        return True
    return bool(
        re.search(
            r"__cf\$cv\$params|cdn-cgi/challenge-platform|attention required|cf-browser-verification",
            body,
            re.IGNORECASE,
        )
    )


def _match_anchor_positions(body: str) -> list[int]:
    return [found.start() for found in CSSTATS_MATCH_URL_REGEX.finditer(body)]


def _near_any_anchor(index: int, anchors: list[int], window: int) -> bool:
    return any(abs(index - anchor) <= window for anchor in anchors)


def extract_relative_time_hints(
    body: str,
    anchors: Optional[list[int]] = None,
    window: int = CSSTATS_ROW_CONTEXT,
) -> list[MatchActivity]:
    unique: set[str] = set()
    hints: list[MatchActivity] = []
    for found in RELATIVE_TIME_REGEX.finditer(body):
        if anchors is not None and not _near_any_anchor(found.start(), anchors, window):
            continue
        value = found.group(1)
        key = value.lower().strip()
        if key in unique:
            continue
        unique.add(key)
        hints.append(MatchActivity(None, value.strip(), "relative"))
    return hints


def extract_machine_readable_matches(
    body: str,
    anchors: Optional[list[int]] = None,
    window: int = CSSTATS_ROW_CONTEXT,
) -> list[MatchActivity]:
    seen: set[str] = set()
    matches: list[MatchActivity] = []
    for found in ISO_TIMESTAMP_REGEX.finditer(body):
        if anchors is not None and not _near_any_anchor(found.start(), anchors, window):
            continue
        value = found.group(1)
        if value in seen:
            continue
        seen.add(value)
        precision: MatchTimePrecision = "minute" if len(value) >= 16 else "unknown"
        matches.append(MatchActivity(_normalize_iso(value), value, precision))
    if matches:
        return matches
    for found in DATE_ONLY_REGEX.finditer(body):
        if anchors is not None and not _near_any_anchor(found.start(), anchors, window):
            continue
        value = found.group(1)
        if value in seen:
            continue
        seen.add(value)
        matches.append(MatchActivity(f"{value}T00:00:00Z", value, "day"))
    return matches


def _nearest_capture(haystack: str, regex: re.Pattern[str], anchor: int) -> Optional[str]:
    best_value: Optional[str] = None
    best_distance: Optional[int] = None
    for found in regex.finditer(haystack):
        if found.lastindex is None:
            continue
        value = found.group(1)
        distance = abs(found.start() - anchor)
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_value = value
    return best_value


def extract_csstats_matches(body: str) -> list[MatchActivity]:
    seen: set[str] = set()
    matches: list[MatchActivity] = []
    for found in CSSTATS_TIMESTAMP_REGEX.finditer(body):
        raw = found.group(1)
        if not raw or raw in seen:
            continue
        seen.add(raw)
        if len(raw) == 13:
            seconds = int(raw) // 1000
        elif len(raw) == 10:
            seconds = int(raw)
        else:
            continue
        played_at = datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        index = found.start()
        slice_start = max(0, index - CSSTATS_ROW_CONTEXT)
        context = body[slice_start:index + CSSTATS_ROW_CONTEXT]
        anchor = index - slice_start
        match_id = _nearest_capture(context, CSSTATS_MATCH_URL_REGEX, anchor)
        map_name = _nearest_capture(context, CSSTATS_MAP_REGEX, anchor)
        matches.append(
            MatchActivity(
                played_at=played_at,
                played_at_raw=raw,
                time_precision="minute",
                match_url=f"https://csstats.gg/match/{match_id}" if match_id else None,
                map_name=map_name.lower() if map_name else None,
            )
        )
    return matches


def extract_csstats_activity(body: str, max_matches: int = 10) -> ExtractedActivity:
    matches = sorted(
        extract_csstats_matches(body),
        key=lambda item: item.played_at or "",
        reverse=True,
    )
    if not matches:
        return extract_activity_from_html(body, max_matches=max_matches)
    window = build_activity_window(matches)
    return ExtractedActivity(
        recent_matches=matches[:max_matches],
        recent_sessions=aggregate_sessions(matches),
        last_activity_window=window,
        confidence=confidence_for_window(window),
        signal_strength=signal_strength_for_window(window),
    )


def extract_activity_from_html(body: str, max_matches: int = 10) -> ExtractedActivity:
    # Only trust timestamps/relative phrases that sit next to a /match/ link.
    # Bare page timestamps (footers, build dates, schema.org metadata, unrelated
    # UI copy) must not be promoted into a fabricated activity window.
    anchors = _match_anchor_positions(body)
    exact_matches = sorted(
        extract_machine_readable_matches(body, anchors=anchors),
        key=lambda item: item.played_at or "",
        reverse=True,
    )
    relative_hints = [] if exact_matches else extract_relative_time_hints(body, anchors=anchors)
    recent_matches = (exact_matches + relative_hints)[:max_matches]
    window = build_activity_window(exact_matches, relative_hints)
    return ExtractedActivity(
        recent_matches=recent_matches,
        recent_sessions=aggregate_sessions(exact_matches),
        last_activity_window=window,
        confidence=confidence_for_window(window),
        signal_strength=signal_strength_for_window(window),
    )


def parse_leetify_api_profile(body: str) -> Optional[tuple[Optional[str], ExtractedActivity]]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    games = payload.get("games")
    meta = payload.get("meta") or {}
    if not isinstance(games, list):
        return None
    matches: list[MatchActivity] = []
    for game in games:
        if not isinstance(game, dict):
            continue
        finished_at = game.get("gameFinishedAt")
        if not isinstance(finished_at, str):
            continue
        try:
            normalized = _normalize_iso(finished_at)
        except ValueError:
            continue
        matches.append(
            MatchActivity(
                played_at=normalized,
                played_at_raw=finished_at,
                time_precision="day" if normalized.endswith("T00:00:00Z") else "minute",
                map_name=game.get("mapName") if isinstance(game.get("mapName"), str) else None,
            )
        )
    matches.sort(key=lambda item: item.played_at or "", reverse=True)
    window = build_activity_window(matches)
    activity = ExtractedActivity(
        recent_matches=matches[:10],
        recent_sessions=aggregate_sessions(matches),
        last_activity_window=window,
        confidence=confidence_for_window(window),
        signal_strength=signal_strength_for_window(window),
    )
    profile_name = meta.get("name") if isinstance(meta.get("name"), str) else None
    return profile_name, activity


STATUS_RANK = {
    "found_recent_activity": 5,
    "found_profile_no_matches": 4,
    "profile_not_found": 2,
    "provider_blocked": 1,
    "parse_failed": 1,
    "unknown": 0,
}
SIGNAL_RANK = {"strong": 3, "moderate": 2, "weak": 1, "none": 0}
PRECISION_RANK = {"minute": 4, "hour": 3, "day": 2, "relative": 1, "unknown": 0}


class CS2ActivityResolver:
    def __init__(self, session: aiohttp.ClientSession):
        self._session = session

    async def lookup(self, steam_id: str) -> CS2ActivityResult:
        try:
            return await asyncio.wait_for(
                self._lookup(steam_id), timeout=LOOKUP_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            return self._to_response(
                None,
                [
                    {
                        "provider": "all",
                        "status": "unknown",
                        "note": f"CS2 lookup exceeded {LOOKUP_TIMEOUT_SECONDS:g}s and was aborted.",
                    }
                ],
            )

    async def _lookup(self, steam_id: str) -> CS2ActivityResult:
        diagnostics: list[dict[str, str]] = []
        results = [
            await self._lookup_leetify(steam_id),
            await self._lookup_csstats(steam_id),
        ]
        for result in results:
            diagnostics.append(
                {
                    "provider": result.provider,
                    "status": result.status,
                    "note": result.notes[0] if result.notes else "",
                }
            )
        winner = self._pick_best(results)
        return self._to_response(winner, diagnostics)

    async def _lookup_leetify(self, steam_id: str) -> ProviderResult:
        profile_url = f"https://leetify.com/public/profile/{steam_id}"
        url = LEETIFY_API_URL.format(steam_id=steam_id)
        try:
            status, body = await self._http_get(url)
        except Exception as exc:
            return self._blank("leetify", "unknown", profile_url, False, [f"Leetify fetch failed: {exc}"])
        if status >= 400 or is_likely_cloudflare_block(status, body):
            return self._blank("leetify", "provider_blocked", profile_url, False, ["Leetify blocked or failed."])
        parsed = parse_leetify_api_profile(body)
        if not parsed:
            return self._blank("leetify", "parse_failed", profile_url, True, ["Leetify API parse failed."])
        profile_name, activity = parsed
        if activity.last_activity_window:
            return ProviderResult(
                provider="leetify",
                status="found_recent_activity",
                profile_found=True,
                profile_url=profile_url,
                last_activity_window=activity.last_activity_window,
                recent_matches=activity.recent_matches,
                recent_sessions=activity.recent_sessions,
                confidence=activity.confidence,
                signal_strength=activity.signal_strength,
                notes=[
                    f"Recovered via Leetify API for {profile_name}." if profile_name else "Recovered via Leetify API.",
                ],
            )
        return ProviderResult(
            provider="leetify",
            status="found_profile_no_matches",
            profile_found=True,
            profile_url=profile_url,
            last_activity_window=None,
            recent_matches=[],
            recent_sessions=[],
            confidence="low",
            signal_strength="none",
            notes=["Leetify profile found but no recent CS2 matches were exposed."],
        )

    async def _lookup_csstats(self, steam_id: str) -> ProviderResult:
        profile_url = f"https://csstats.gg/player/{steam_id}"
        url = CSSTATS_URL.format(steam_id=steam_id)
        headers = {
            "x-requested-with": "XMLHttpRequest",
            "referer": profile_url,
            "accept": "*/*",
        }
        try:
            status, body = await self._http_get(url, headers)
        except Exception as exc:
            return self._blank("csstats", "unknown", profile_url, False, [f"CSStats fetch failed: {exc}"])
        if status == 404:
            return self._blank("csstats", "profile_not_found", profile_url, False, ["CSStats returned 404."])
        if is_likely_cloudflare_block(status, body):
            return self._blank("csstats", "provider_blocked", profile_url, False, ["CSStats blocked the request."])
        try:
            activity = extract_csstats_activity(body)
        except Exception as exc:
            return self._blank("csstats", "parse_failed", profile_url, True, [f"CSStats parse failed: {exc}"])
        if activity.last_activity_window:
            return ProviderResult(
                provider="csstats",
                status="found_recent_activity",
                profile_found=True,
                profile_url=profile_url,
                last_activity_window=activity.last_activity_window,
                recent_matches=activity.recent_matches,
                recent_sessions=activity.recent_sessions,
                confidence=activity.confidence,
                signal_strength=activity.signal_strength,
                notes=["CSStats exposed server-rendered match rows."],
            )
        return ProviderResult(
            provider="csstats",
            status="found_profile_no_matches",
            profile_found=True,
            profile_url=profile_url,
            last_activity_window=None,
            recent_matches=[],
            recent_sessions=[],
            confidence="low",
            signal_strength="none",
            notes=["CSStats profile reachable but no machine-readable match rows found."],
        )

    async def _http_get(self, url: str, headers: Optional[dict[str, str]] = None) -> tuple[int, str]:
        # Single transport (the shared aiohttp session) with an explicit hard
        # timeout so a stalled connection or slow read cannot hang the caller.
        timeout = aiohttp.ClientTimeout(total=PROVIDER_HTTP_TIMEOUT_SECONDS)
        async with self._session.get(url, headers=headers, timeout=timeout) as resp:
            return resp.status, await resp.text()

    def _pick_best(self, results: list[ProviderResult]) -> Optional[ProviderResult]:
        best: Optional[ProviderResult] = None
        for result in results:
            if best is None or self._is_stronger(result, best):
                best = result
        return best

    def _is_stronger(self, candidate: ProviderResult, incumbent: ProviderResult) -> bool:
        status_delta = STATUS_RANK[candidate.status] - STATUS_RANK[incumbent.status]
        if status_delta != 0:
            return status_delta > 0
        signal_delta = SIGNAL_RANK[candidate.signal_strength] - SIGNAL_RANK[incumbent.signal_strength]
        if signal_delta != 0:
            return signal_delta > 0
        candidate_precision = candidate.last_activity_window.precision if candidate.last_activity_window else "unknown"
        incumbent_precision = incumbent.last_activity_window.precision if incumbent.last_activity_window else "unknown"
        return PRECISION_RANK[candidate_precision] > PRECISION_RANK[incumbent_precision]

    def _to_response(
        self,
        winner: Optional[ProviderResult],
        diagnostics: list[dict[str, str]],
    ) -> CS2ActivityResult:
        if winner is None:
            return CS2ActivityResult(
                status="unknown",
                source=None,
                profile_found=False,
                profile_url=None,
                activity_window=None,
                recent_matches=[],
                recent_sessions=[],
                confidence="none",
                signal_strength="none",
                recency=compute_recency(None),
                provider_diagnostics=diagnostics,
            )
        public_status: Literal["found_recent_activity", "found_profile_no_matches", "unknown"]
        if winner.status == "found_recent_activity":
            public_status = "found_recent_activity"
        elif winner.profile_found:
            public_status = "found_profile_no_matches"
        else:
            public_status = "unknown"
        signal_strength = winner.signal_strength if winner.last_activity_window else "none"
        return CS2ActivityResult(
            status=public_status,
            source=winner.provider if public_status != "unknown" else None,
            profile_found=winner.profile_found,
            profile_url=winner.profile_url,
            activity_window=winner.last_activity_window,
            recent_matches=winner.recent_matches,
            recent_sessions=winner.recent_sessions,
            confidence=winner.confidence,
            signal_strength=signal_strength,
            recency=compute_recency(winner.last_activity_window),
            provider_diagnostics=diagnostics,
        )

    @staticmethod
    def _blank(
        provider: str,
        status: ProviderStatus,
        profile_url: Optional[str],
        profile_found: bool,
        notes: list[str],
    ) -> ProviderResult:
        return ProviderResult(
            provider=provider,
            status=status,
            profile_found=profile_found,
            profile_url=profile_url,
            last_activity_window=None,
            recent_matches=[],
            recent_sessions=[],
            confidence="none",
            signal_strength="none",
            notes=notes,
        )


def format_cs2_activity_lines(result: CS2ActivityResult) -> list[str]:
    if result.status == "unknown":
        return []
    lines = ["", "🎯 CS2 активность:"]
    if result.status == "found_profile_no_matches":
        lines.append("CS2 профиль найден, но матч-сигналов не видно.")
        if result.source:
            lines.append(f"Источник: {result.source}")
        return lines

    if result.source:
        lines.append(f"Источник: {result.source}")
    if result.recency.band != "unknown":
        label = {
            "fresh": "свежая",
            "recent": "недавняя",
            "stale": "старая",
            "dormant": "давняя",
        }.get(result.recency.band, result.recency.band)
        age = f" (~{result.recency.age_days} дн назад)" if result.recency.age_days is not None else ""
        lines.append(f"Давность: {label}{age}")
    window = result.activity_window
    if window and window.started_at_approx and window.ended_at_approx:
        lines.append(f"Окно: {window.started_at_approx} → {window.ended_at_approx}")
    elif window and window.ended_at_approx:
        lines.append(f"Последний сигнал: {window.ended_at_approx}")
    if result.recent_matches:
        newest = result.recent_matches[0]
        if newest.map_name:
            lines.append(f"Карта: {newest.map_name}")
        if newest.match_url:
            lines.append(f"Матч: {newest.match_url}")
    return lines
