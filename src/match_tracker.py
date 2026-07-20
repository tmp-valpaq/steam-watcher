import asyncio
from dataclasses import dataclass
import logging
import re
import time
from datetime import datetime, timezone
from html import unescape
from typing import Dict, List, Optional, Tuple

import aiohttp

from .config import (
    DOTABUFF_BROWSER_ENABLED,
    DOTABUFF_CACHE_TTL_SEC,
    DOTABUFF_EMPTY_CACHE_TTL_SEC,
    MATCH_POLL_INTERVAL,
)
from .models import MatchInfo
from .util import BoundedDict

try:
    from .browser_worker import BrowserWorkerError, BrowserWorkerSupervisor
except Exception:  # pragma: no cover - optional runtime dependency
    BrowserWorkerSupervisor = None

    class BrowserWorkerError(Exception):
        pass

logger = logging.getLogger(__name__)

OPENDOTA_API_BASE = "https://api.opendota.com/api"
DOTABUFF_BASE = "https://www.dotabuff.com"
STEAM_ID_OFFSET = 76561197960265728
DOTABUFF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://www.dotabuff.com/",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "Upgrade-Insecure-Requests": "1",
}
_RELATIVE_TIME_RE = re.compile(
    r"(?:(?P<article>a|an)\s+|(?P<value>\d+)\s+)"
    r"(?P<unit>second|minute|hour|day|week|month|year)s?\s+ago",
    re.IGNORECASE,
)
# Negative ("no matches found") verdicts must outlive a full match-poll cycle,
# otherwise non-Dota / Cloudflare-gated targets get re-fetched (and re-browsed)
# on every sweep. The watcher polls matches every MATCH_POLL_INTERVAL seconds, so
# cache empties for at least that long regardless of the (much shorter) base
# empty-cache TTL knob. Aliased from config so the literal lives in one place.
_MATCH_POLL_INTERVAL_SEC = MATCH_POLL_INTERVAL
_NEGATIVE_CACHE_TTL_SEC = max(DOTABUFF_EMPTY_CACHE_TTL_SEC, _MATCH_POLL_INTERVAL_SEC)
_SHORT_RETRYABLE_FAILURE_TTL_SEC = min(30, _MATCH_POLL_INTERVAL_SEC)
# Upper bound on headless-browser launches per poll cycle, so a sweep of many
# offline non-Dota targets cannot fan out into many Chromium contexts at once.
_BROWSER_LAUNCH_BUDGET_PER_CYCLE = 2
_DOTABUFF_BREAKER_FAILURE_THRESHOLD = 3
_DOTABUFF_BREAKER_COOLDOWN_SEC = _MATCH_POLL_INTERVAL_SEC
# How long an OpenDota rank lookup (positive OR empty) stays cached. Empties are
# cached too so MMR-less players aren't re-fetched on every alert.
_RANK_CACHE_TTL_SEC = 600

_MATCH_LINK_RE = re.compile(r"/matches/(\d+)")
_HERO_SLUG_RE = re.compile(r"/heroes/([a-z0-9\-]+)")
_UNIX_TS_RE = re.compile(r'(?:data-(?:timestamp|time|unix)|timestamp)="(\d{10,13})"')
_ISO_TS_RE = re.compile(r'datetime="([^"]+)"')
_DURATION_RE = re.compile(r">\s*((?:\d+:)?\d{1,2}:\d{2})\s*<")
_TITLE_TS_RE = re.compile(r'title="([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}[^\"]*)"')


@dataclass
class _DotabuffCacheEntry:
    expiry_ts: float
    matches: List[MatchInfo]
    fetched_limit: int
    reason: str


@dataclass
class _DotabuffFetchResult:
    matches: List[MatchInfo]
    reason: str
    cacheable: bool = True


class MatchTracker:
    """Dota match tracker: OpenDota primary, Dotabuff best-effort fallback."""

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session
        self._hero_cache: Dict[int, str] = {}
        self._dotabuff_cache: Dict[str, _DotabuffCacheEntry] = BoundedDict(512)
        # steam_id -> (expiry_ts, rank_dict_or_None). Caches positive and empty
        # OpenDota rank lookups so players with no MMR aren't re-fetched per alert.
        self._rank_cache: Dict[str, Tuple[float, Optional[dict]]] = BoundedDict(512)
        self._last_request_time: float = 0.0
        self._min_interval: float = 1.0  # 1 req/sec for OpenDota free tier
        self._dotabuff_browser_enabled: bool = DOTABUFF_BROWSER_ENABLED
        # One resident browser worker for all fetches; killed by process group
        # on timeout, respawned lazily. Never more than one chromium at a time.
        self._browser_supervisor: Optional["BrowserWorkerSupervisor"] = None
        # Per-cycle browser-launch budget. Replenished whenever a full match-poll
        # interval has elapsed since the window opened, so a single sweep of many
        # offline non-Dota targets can launch at most a handful of browsers.
        self._browser_budget: int = _BROWSER_LAUNCH_BUDGET_PER_CYCLE
        self._browser_budget_window_start: float = 0.0
        self._dotabuff_failure_streak: int = 0
        self._dotabuff_breaker_open_until: float = 0.0
        self._dotabuff_last_failure_reason: Optional[str] = None

    async def aclose(self) -> None:
        """Kill the resident browser worker, if any."""
        supervisor = self._browser_supervisor
        self._browser_supervisor = None
        if supervisor is not None:
            await supervisor.aclose()

    async def _rate_limit(self) -> None:
        """Ensure we don't exceed 1 request per second to OpenDota."""
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_request_time
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_request_time = asyncio.get_event_loop().time()

    async def _get(self, url: str) -> Optional[dict]:
        """Make a rate-limited GET request to OpenDota. Returns parsed JSON."""
        await self._rate_limit()
        async with self._session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _get_text(
        self,
        url: str,
        *,
        headers: Optional[dict] = None,
        rate_limit: bool = False,
    ) -> Tuple[int, str]:
        """Fetch text response, returning (status, text) without raising on HTTP status."""
        if rate_limit:
            await self._rate_limit()
        async with self._session.get(url, headers=headers) as resp:
            text = await resp.text()
            return resp.status, text

    async def _load_hero_cache(self) -> None:
        """Fetch /heroes once and populate hero_id -> localized_name cache."""
        try:
            data = await self._get(f"{OPENDOTA_API_BASE}/heroes")
            if not isinstance(data, list):
                return
            for hero in data:
                hid = hero.get("id")
                name = hero.get("localized_name")
                if hid is not None and name:
                    self._hero_cache[hid] = name
        except Exception as e:
            logger.error("Failed to load hero cache: %s", e)

    async def _get_hero_name(self, hero_id: int) -> Optional[str]:
        """Lookup hero name from cache, lazy-loading on first call."""
        if not self._hero_cache:
            await self._load_hero_cache()
        return self._hero_cache.get(hero_id)

    @staticmethod
    def _to_account_id(steam_id: str) -> Optional[int]:
        try:
            return int(steam_id) - STEAM_ID_OFFSET
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _make_match_info(
        steam_id: str,
        match_id: str,
        duration: int,
        start_time: Optional[int],
        hero_name: Optional[str],
        *,
        source: str,
    ) -> MatchInfo:
        return MatchInfo(
            match_id=str(match_id),
            steam_id=steam_id,
            game="dota2",
            duration=int(duration),
            start_time=int(start_time) if start_time is not None else None,
            hero_name=hero_name,
            source=source,
        )

    async def _get_last_match_opendota(self, steam_id: str) -> Optional[MatchInfo]:
        account_id = self._to_account_id(steam_id)
        if account_id is None or account_id <= 0:
            return None

        url = f"{OPENDOTA_API_BASE}/players/{account_id}/matches?limit=1"
        data = await self._get(url)
        if not isinstance(data, list) or not data:
            return None
        m = data[0]
        match_id = m.get("match_id")
        start_time = m.get("start_time")
        duration = m.get("duration")
        hero_id = m.get("hero_id")
        if match_id is None or start_time is None or duration is None:
            return None

        hero_name = await self._get_hero_name(hero_id) if hero_id else None
        return self._make_match_info(
            steam_id,
            str(match_id),
            int(duration),
            int(start_time),
            hero_name,
            source="opendota",
        )

    async def _get_recent_matches_opendota(self, steam_id: str, limit: int) -> List[MatchInfo]:
        account_id = self._to_account_id(steam_id)
        if account_id is None or account_id <= 0:
            return []

        url = f"{OPENDOTA_API_BASE}/players/{account_id}/matches?limit={limit}"
        data = await self._get(url)
        if not isinstance(data, list) or not data:
            return []

        matches = []
        for m in data:
            match_id = m.get("match_id")
            start_time = m.get("start_time")
            duration = m.get("duration")
            hero_id = m.get("hero_id")
            if match_id is None or start_time is None or duration is None:
                continue
            hero_name = await self._get_hero_name(hero_id) if hero_id else None
            matches.append(
                self._make_match_info(
                    steam_id,
                    str(match_id),
                    int(duration),
                    int(start_time),
                    hero_name,
                    source="opendota",
                )
            )
        return matches

    @staticmethod
    def _parse_duration(raw: Optional[str]) -> int:
        if not raw:
            return 0
        parts = [int(p) for p in raw.strip().split(":")]
        if len(parts) == 2:
            minutes, seconds = parts
            return minutes * 60 + seconds
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return hours * 3600 + minutes * 60 + seconds
        return 0

    @staticmethod
    def _hero_name_from_slug(slug: Optional[str]) -> Optional[str]:
        if not slug:
            return None
        pieces = [part for part in slug.split("-") if part]
        if not pieces:
            return None
        return " ".join(piece.capitalize() for piece in pieces)

    @staticmethod
    def _normalize_dotabuff_time(raw: Optional[str]) -> Optional[int]:
        if not raw:
            return None
        value = raw.strip()
        try:
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            return int(datetime.fromisoformat(value).timestamp())
        except ValueError:
            pass
        return MatchTracker._relative_time_to_ts(raw)

    @staticmethod
    def _relative_time_to_ts(raw: Optional[str]) -> Optional[int]:
        if not raw:
            return None
        match = _RELATIVE_TIME_RE.search(raw)
        if not match:
            return None
        value = 1 if match.group("article") else int(match.group("value"))
        unit = match.group("unit").lower()
        seconds = {
            "second": 1,
            "minute": 60,
            "hour": 3600,
            "day": 86400,
            "week": 7 * 86400,
            "month": 30 * 86400,
            "year": 365 * 86400,
        }.get(unit)
        if seconds is None:
            return None
        return int(time.time()) - value * seconds

    @staticmethod
    def _parse_timestamp_from_chunk(chunk: str) -> Optional[int]:
        ts_match = _UNIX_TS_RE.search(chunk)
        if ts_match:
            raw = ts_match.group(1)
            ts = int(raw)
            if len(raw) == 13:
                ts //= 1000
            return ts

        iso_match = _ISO_TS_RE.search(chunk)
        if iso_match:
            raw = iso_match.group(1).strip()
            try:
                if raw.endswith("Z"):
                    raw = raw[:-1] + "+00:00"
                return int(datetime.fromisoformat(raw).timestamp())
            except ValueError:
                pass

        title_match = _TITLE_TS_RE.search(chunk)
        if title_match:
            raw = unescape(title_match.group(1)).strip()
            for fmt in (
                "%b %d, %Y %I:%M %p",
                "%b %d, %Y %H:%M",
                "%B %d, %Y %I:%M %p",
                "%B %d, %Y %H:%M",
            ):
                try:
                    dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                    return int(dt.timestamp())
                except ValueError:
                    continue

        text = re.sub(r"<[^>]+>", " ", chunk)
        text = unescape(text)
        return MatchTracker._relative_time_to_ts(text)

    def _parse_dotabuff_matches(
        self,
        html: str,
        steam_id: str,
        *,
        limit: int,
    ) -> List[MatchInfo]:
        section = html
        latest_idx = html.lower().find("latest matches")
        if latest_idx >= 0:
            section = html[latest_idx: latest_idx + 50000]

        row_chunks = section.split('<div class="r-row ')
        if len(row_chunks) > 1:
            matches: List[MatchInfo] = []
            seen = set()
            for row in row_chunks[1:]:
                chunk = row
                match = _MATCH_LINK_RE.search(chunk)
                if not match:
                    continue
                match_id = match.group(1)
                if match_id in seen:
                    continue
                seen.add(match_id)

                hero_match = _HERO_SLUG_RE.search(chunk)
                hero_name = self._hero_name_from_slug(hero_match.group(1) if hero_match else None)
                duration_match = _DURATION_RE.search(chunk)
                duration = self._parse_duration(duration_match.group(1) if duration_match else None)
                start_time = self._parse_timestamp_from_chunk(chunk)
                if start_time is None or duration <= 0:
                    continue

                matches.append(
                    self._make_match_info(
                        steam_id,
                        match_id,
                        duration,
                        start_time,
                        hero_name,
                        source="dotabuff",
                    )
                )
                if len(matches) >= limit:
                    break
            if matches:
                return matches

        seen = set()
        matches = []
        match_links = list(_MATCH_LINK_RE.finditer(section))
        for idx, match in enumerate(match_links):
            match_id = match.group(1)
            if match_id in seen:
                continue
            seen.add(match_id)

            next_start = match_links[idx + 1].start() if idx + 1 < len(match_links) else len(section)
            chunk = section[match.start():next_start]

            hero_match = _HERO_SLUG_RE.search(chunk)
            hero_name = self._hero_name_from_slug(hero_match.group(1) if hero_match else None)
            duration_match = _DURATION_RE.search(chunk)
            duration = self._parse_duration(duration_match.group(1) if duration_match else None)
            start_time = self._parse_timestamp_from_chunk(chunk)
            if start_time is None or duration <= 0:
                continue

            matches.append(
                self._make_match_info(
                    steam_id,
                    match_id,
                    duration,
                    start_time,
                    hero_name,
                    source="dotabuff",
                )
            )
            if len(matches) >= limit:
                break
        return matches

    def _parse_dotabuff_browser_rows(
        self,
        rows: List[dict],
        steam_id: str,
        *,
        limit: int,
    ) -> List[MatchInfo]:
        matches: List[MatchInfo] = []
        seen = set()
        for row in rows:
            match_id = row.get("match_id")
            if not match_id or match_id in seen:
                continue
            seen.add(match_id)
            hero_name = row.get("hero_name") or self._hero_name_from_slug(row.get("hero_slug"))
            duration = row.get("duration_sec") or self._parse_duration(row.get("duration_text"))
            start_time = self._normalize_dotabuff_time(row.get("relative_or_absolute_time"))
            if start_time is None or int(duration or 0) <= 0:
                continue

            matches.append(
                self._make_match_info(
                    steam_id,
                    str(match_id),
                    int(duration or 0),
                    start_time,
                    hero_name,
                    source="dotabuff",
                )
            )
            if len(matches) >= limit:
                break
        return matches

    def _try_acquire_browser_budget(self) -> bool:
        """Consume one headless-browser launch from the per-cycle budget.

        Returns True if a launch is allowed. The budget refills once a full
        match-poll interval has elapsed, bounding the number of Chromium
        contexts a single sweep of offline non-Dota targets can spin up.
        """
        now = time.time()
        if now - self._browser_budget_window_start >= _MATCH_POLL_INTERVAL_SEC:
            self._browser_budget_window_start = now
            self._browser_budget = _BROWSER_LAUNCH_BUDGET_PER_CYCLE
        if self._browser_budget <= 0:
            return False
        self._browser_budget -= 1
        return True

    @staticmethod
    def _classify_dotabuff_http_outcome(status: int, html: str) -> str:
        lowered = (html or "")[:5000].lower()
        if status == 200:
            if "just a moment" in lowered or "cloudflare" in lowered:
                return "http_challenge_blocked"
            return "http_200"
        if status == 403:
            return "http_403"
        if status == 404:
            return "http_404"
        if status == 429:
            return "http_429"
        if status == 503:
            return "http_503"
        return f"http_{status}"

    @staticmethod
    def _split_dotabuff_reason(reason: str) -> List[str]:
        return [part for part in (reason or "").split("__") if part]

    @classmethod
    def _combine_dotabuff_reasons(cls, *reasons: Optional[str]) -> str:
        parts: List[str] = []
        for reason in reasons:
            if not reason:
                continue
            for part in cls._split_dotabuff_reason(reason):
                if part not in parts:
                    parts.append(part)
        return "__".join(parts)

    @classmethod
    def _is_dotabuff_throttle_reason(cls, reason: str) -> bool:
        throttle_parts = {
            "http_403",
            "http_429",
            "http_503",
            "http_challenge_blocked",
            "browser_challenge_blocked",
        }
        return any(part in throttle_parts for part in cls._split_dotabuff_reason(reason))

    @classmethod
    def _is_dotabuff_stable_empty_reason(cls, reason: str) -> bool:
        stable_parts = {
            "invalid_account",
            "http_404",
            "browser_not_found",
        }
        parts = cls._split_dotabuff_reason(reason)
        return any(part in stable_parts for part in parts) and not cls._is_dotabuff_throttle_reason(reason)

    @classmethod
    def _is_dotabuff_retryable_failure_reason(cls, reason: str) -> bool:
        parts = cls._split_dotabuff_reason(reason)
        if not parts:
            return False
        if cls._is_dotabuff_throttle_reason(reason):
            return True
        retryable_parts = {
            "http_fetch_failed",
            "static_parser_empty",
            "browser_disabled",
            "browser_budget_exhausted",
            "browser_failed",
            "browser_login_gated",
            "browser_unknown",
            "browser_parser_empty",
        }
        return any(part in retryable_parts for part in parts)

    def _dotabuff_breaker_is_open(self) -> bool:
        now = time.time()
        if self._dotabuff_breaker_open_until and now >= self._dotabuff_breaker_open_until:
            self._dotabuff_breaker_open_until = 0.0
            self._dotabuff_failure_streak = 0
            self._dotabuff_last_failure_reason = None
        return self._dotabuff_breaker_open_until > now

    def _record_dotabuff_result(self, result: _DotabuffFetchResult) -> None:
        if result.matches:
            self._dotabuff_failure_streak = 0
            self._dotabuff_breaker_open_until = 0.0
            self._dotabuff_last_failure_reason = None
            return

        reason = result.reason
        if self._is_dotabuff_throttle_reason(reason):
            self._dotabuff_failure_streak += 1
            self._dotabuff_last_failure_reason = reason
            if self._dotabuff_failure_streak >= _DOTABUFF_BREAKER_FAILURE_THRESHOLD:
                self._dotabuff_breaker_open_until = max(
                    self._dotabuff_breaker_open_until,
                    time.time() + _DOTABUFF_BREAKER_COOLDOWN_SEC,
                )
            return

        self._dotabuff_failure_streak = 0
        self._dotabuff_last_failure_reason = reason if reason else None

    @classmethod
    def _dotabuff_cache_ttl_for_reason(cls, reason: str, has_matches: bool) -> int:
        if has_matches:
            return DOTABUFF_CACHE_TTL_SEC
        if cls._is_dotabuff_stable_empty_reason(reason):
            return _NEGATIVE_CACHE_TTL_SEC
        if cls._is_dotabuff_throttle_reason(reason) or cls._is_dotabuff_retryable_failure_reason(reason):
            return _SHORT_RETRYABLE_FAILURE_TTL_SEC
        return _NEGATIVE_CACHE_TTL_SEC

    async def _get_recent_matches_dotabuff_browser(
        self, steam_id: str, limit: int
    ) -> _DotabuffFetchResult:
        if not self._dotabuff_browser_enabled or BrowserWorkerSupervisor is None:
            return _DotabuffFetchResult([], reason="browser_disabled")
        if not self._try_acquire_browser_budget():
            logger.info(
                "Dotabuff browser budget exhausted this cycle; skipping browser fetch for %s",
                steam_id,
            )
            return _DotabuffFetchResult([], reason="browser_budget_exhausted", cacheable=False)

        account_id = self._to_account_id(steam_id)
        if account_id is None or account_id <= 0:
            return _DotabuffFetchResult([], reason="invalid_account")

        if self._browser_supervisor is None:
            self._browser_supervisor = BrowserWorkerSupervisor()

        try:
            result = await self._browser_supervisor.fetch_player_matches(
                str(account_id), limit
            )
        except BrowserWorkerError as e:
            logger.warning("Dotabuff browser fetch failed for %s: %s", steam_id, e)
            return _DotabuffFetchResult([], reason="browser_failed")

        status = result.get("status")
        if status != "accessible_recent_matches":
            logger.info("Dotabuff browser returned %s for %s", status, steam_id)
            return _DotabuffFetchResult([], reason=f"browser_{status or 'unknown'}")

        matches = self._parse_dotabuff_browser_rows(result.get("rows") or [], steam_id, limit=limit)
        if matches:
            logger.info(
                "Dotabuff browser fallback returned %d match(es) for %s",
                len(matches),
                steam_id,
            )
            return _DotabuffFetchResult(matches, reason="browser_success")
        return _DotabuffFetchResult([], reason="browser_parser_empty")

    async def _get_recent_matches_dotabuff(self, steam_id: str, limit: int) -> List[MatchInfo]:
        fetch_limit = max(limit, 3)
        cached = self._dotabuff_cache.get(steam_id)
        now = time.time()
        if cached and cached.expiry_ts > now and cached.fetched_limit >= fetch_limit:
            if not cached.matches:
                return []
            return cached.matches[:limit]

        account_id = self._to_account_id(steam_id)
        if account_id is None or account_id <= 0:
            return []

        if self._dotabuff_breaker_is_open():
            logger.info(
                "Dotabuff breaker open until %.0f; skipping fetch for %s after %s",
                self._dotabuff_breaker_open_until,
                steam_id,
                self._dotabuff_last_failure_reason or "unknown failure",
            )
            return []

        url = f"{DOTABUFF_BASE}/players/{account_id}"
        try:
            status, html = await self._get_text(url, headers=DOTABUFF_HEADERS)
        except Exception as e:
            logger.warning("Dotabuff fetch failed for %s: %s", steam_id, e)
            result = _DotabuffFetchResult([], reason="http_fetch_failed")
            self._record_dotabuff_result(result)
            ttl = self._dotabuff_cache_ttl_for_reason(result.reason, False)
            self._dotabuff_cache[steam_id] = _DotabuffCacheEntry(
                expiry_ts=now + ttl,
                matches=[],
                fetched_limit=fetch_limit,
                reason=result.reason,
            )
            return []

        http_reason = self._classify_dotabuff_http_outcome(status, html)
        result: _DotabuffFetchResult
        if http_reason != "http_200":
            logger.info("Dotabuff returned %s for %s", http_reason, steam_id)
            browser_result = await self._get_recent_matches_dotabuff_browser(steam_id, fetch_limit)
            combined_reason = self._combine_dotabuff_reasons(http_reason, browser_result.reason)
            result = _DotabuffFetchResult(
                browser_result.matches,
                reason=combined_reason,
                cacheable=browser_result.cacheable or self._is_dotabuff_stable_empty_reason(http_reason),
            )
        else:
            matches = await asyncio.to_thread(
                self._parse_dotabuff_matches, html, steam_id, limit=fetch_limit
            )
            if matches:
                result = _DotabuffFetchResult(matches, reason="static_parse_success")
            else:
                browser_result = await self._get_recent_matches_dotabuff_browser(steam_id, fetch_limit)
                result = _DotabuffFetchResult(
                    browser_result.matches,
                    reason=self._combine_dotabuff_reasons("static_parser_empty", browser_result.reason),
                    cacheable=browser_result.cacheable,
                )

        self._record_dotabuff_result(result)
        if result.cacheable:
            ttl = self._dotabuff_cache_ttl_for_reason(result.reason, bool(result.matches))
            self._dotabuff_cache[steam_id] = _DotabuffCacheEntry(
                expiry_ts=now + ttl,
                matches=result.matches,
                fetched_limit=fetch_limit,
                reason=result.reason,
            )
        if result.matches:
            logger.info(
                "Dotabuff fallback returned %d match(es) for %s",
                len(result.matches),
                steam_id,
            )
        return result.matches[:limit]

    async def get_last_match(self, steam_id: str) -> Optional[MatchInfo]:
        """Fetch the most recent Dota 2 match for a Steam ID."""
        try:
            match = await self._get_last_match_opendota(steam_id)
            if match is not None:
                return match
        except Exception as e:
            logger.error("Failed to get OpenDota last match for %s: %s", steam_id, e)

        matches = await self._get_recent_matches_dotabuff(steam_id, limit=1)
        return matches[0] if matches else None

    async def get_recent_matches(self, steam_id: str, limit: int = 5) -> List[MatchInfo]:
        """Fetch recent Dota 2 matches for a Steam ID."""
        try:
            matches = await self._get_recent_matches_opendota(steam_id, limit)
            if matches:
                return matches
        except Exception as e:
            logger.error("Failed to get OpenDota recent matches for %s: %s", steam_id, e)

        return await self._get_recent_matches_dotabuff(steam_id, limit)

    async def get_player_rank(self, steam_id: str) -> Optional[dict]:
        """Fetch a player's Dota MMR/rank tier from OpenDota.

        Returns {"mmr": Optional[int], "rank_tier": Optional[int]} with the RAW
        rank_tier int (callers decode it into a human name). Returns None for an
        invalid steam_id or on any HTTP/parse error. Results — including empty
        ones — are cached for _RANK_CACHE_TTL_SEC so MMR-less players are not
        re-fetched on every alert.
        """
        account_id = self._to_account_id(steam_id)
        if account_id is None or account_id <= 0:
            return None

        now = time.time()
        cached = self._rank_cache.get(steam_id)
        if cached and cached[0] > now:
            return cached[1]

        url = f"{OPENDOTA_API_BASE}/players/{account_id}"
        try:
            await self._rate_limit()
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    logger.info("OpenDota rank returned %s for %s", resp.status, steam_id)
                    return None
                data = await resp.json()
        except Exception as e:
            logger.warning("OpenDota rank fetch failed for %s: %s", steam_id, e)
            return None

        if not isinstance(data, dict):
            return None

        mmr_estimate = data.get("mmr_estimate")
        mmr = mmr_estimate.get("estimate") if isinstance(mmr_estimate, dict) else None
        rank = {"mmr": mmr, "rank_tier": data.get("rank_tier")}
        # Cache positive and empty results alike (empty = both fields None).
        self._rank_cache[steam_id] = (now + _RANK_CACHE_TTL_SEC, rank)
        return rank

    async def get_last_matches_batch(
        self, steam_ids: List[str]
    ) -> Dict[str, MatchInfo]:
        """Fetch last match for multiple Steam IDs. Skips IDs with no matches."""
        result: Dict[str, MatchInfo] = {}
        for sid in steam_ids:
            match = await self.get_last_match(sid)
            if match is not None:
                result[sid] = match
        return result
