import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp

from .config import (
    DOTABUFF_BROWSER_ENABLED,
    DOTABUFF_BROWSER_OUTPUT_DIR,
    DOTABUFF_BROWSER_PROFILE_DIR,
    DOTABUFF_BROWSER_SETTLE_TIMEOUT_MS,
    DOTABUFF_BROWSER_TIMEOUT_MS,
    DOTABUFF_BROWSER_TOTAL_TIMEOUT_MS,
    DOTABUFF_BROWSER_WAIT_MS,
    DOTABUFF_CACHE_TTL_SEC,
    DOTABUFF_EMPTY_CACHE_TTL_SEC,
)
from .models import MatchInfo

try:
    from .dotabuff_playwright import DotabuffBrowserClient
except Exception:  # pragma: no cover - optional runtime dependency
    DotabuffBrowserClient = None

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
}
_RELATIVE_TIME_RE = re.compile(
    r"(?:(?P<article>a|an)\s+|(?P<value>\d+)\s+)"
    r"(?P<unit>second|minute|hour|day|week|month|year)s?\s+ago",
    re.IGNORECASE,
)
_MATCH_LINK_RE = re.compile(r"/matches/(\d+)")
_HERO_SLUG_RE = re.compile(r"/heroes/([a-z0-9\-]+)")
_UNIX_TS_RE = re.compile(r'(?:data-(?:timestamp|time|unix)|timestamp)="(\d{10,13})"')
_ISO_TS_RE = re.compile(r'datetime="([^"]+)"')
_DURATION_RE = re.compile(r">\s*((?:\d+:)?\d{1,2}:\d{2})\s*<")
_TITLE_TS_RE = re.compile(r'title="([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}[^\"]*)"')


class MatchTracker:
    """Dota match tracker: OpenDota primary, Dotabuff best-effort fallback."""

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session
        self._hero_cache: Dict[int, str] = {}
        self._dotabuff_cache: Dict[str, Tuple[float, List[MatchInfo]]] = {}
        self._last_request_time: float = 0.0
        self._min_interval: float = 1.0  # 1 req/sec for OpenDota free tier
        self._dotabuff_browser_enabled: bool = DOTABUFF_BROWSER_ENABLED

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
        latest_idx = html.find("LATEST MATCHES")
        if latest_idx >= 0:
            section = html[latest_idx: latest_idx + 50000]

        seen = set()
        matches: List[MatchInfo] = []
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

    async def _get_recent_matches_dotabuff_browser(self, steam_id: str, limit: int) -> List[MatchInfo]:
        if not self._dotabuff_browser_enabled or DotabuffBrowserClient is None:
            return []
        browser_client_cls = DotabuffBrowserClient

        account_id = self._to_account_id(steam_id)
        if account_id is None or account_id <= 0:
            return []

        async def _run_browser_fetch() -> dict:
            async with browser_client_cls(
                profile_dir=Path(DOTABUFF_BROWSER_PROFILE_DIR),
                timeout_ms=DOTABUFF_BROWSER_TIMEOUT_MS,
                headless=True,
            ) as client:
                return await client.fetch_player_matches(
                    str(account_id),
                    limit=limit,
                    output_dir=Path(DOTABUFF_BROWSER_OUTPUT_DIR),
                    wait_after_load_ms=DOTABUFF_BROWSER_WAIT_MS,
                    settle_timeout_ms=DOTABUFF_BROWSER_SETTLE_TIMEOUT_MS,
                )

        try:
            result = await asyncio.wait_for(
                _run_browser_fetch(),
                timeout=DOTABUFF_BROWSER_TOTAL_TIMEOUT_MS / 1000,
            )
        except Exception as e:
            logger.warning("Dotabuff browser fetch failed for %s: %s", steam_id, e)
            return []

        if result.get("status") != "accessible_recent_matches":
            logger.info("Dotabuff browser returned %s for %s", result.get("status"), steam_id)
            return []

        matches = self._parse_dotabuff_browser_rows(result.get("rows") or [], steam_id, limit=limit)
        if matches:
            logger.info(
                "Dotabuff browser fallback returned %d match(es) for %s",
                len(matches),
                steam_id,
            )
        return matches

    async def _get_recent_matches_dotabuff(self, steam_id: str, limit: int) -> List[MatchInfo]:
        fetch_limit = max(limit, 3)
        cached = self._dotabuff_cache.get(steam_id)
        now = time.time()
        if cached and cached[0] > now:
            if not cached[1]:
                return []
            if len(cached[1]) >= limit:
                return cached[1][:limit]

        account_id = self._to_account_id(steam_id)
        if account_id is None or account_id <= 0:
            return []

        url = f"{DOTABUFF_BASE}/players/{account_id}"
        try:
            status, html = await self._get_text(url, headers=DOTABUFF_HEADERS)
        except Exception as e:
            logger.warning("Dotabuff fetch failed for %s: %s", steam_id, e)
            return []

        if status != 200:
            logger.info("Dotabuff returned %s for %s", status, steam_id)
            matches = await self._get_recent_matches_dotabuff_browser(steam_id, fetch_limit)
            ttl = DOTABUFF_CACHE_TTL_SEC if matches else DOTABUFF_EMPTY_CACHE_TTL_SEC
            self._dotabuff_cache[steam_id] = (now + ttl, matches)
            return matches[:limit]

        if "LATEST MATCHES" not in html or "Just a moment" in html:
            logger.info("Dotabuff page for %s did not expose match rows", steam_id)
            matches = await self._get_recent_matches_dotabuff_browser(steam_id, fetch_limit)
            ttl = DOTABUFF_CACHE_TTL_SEC if matches else DOTABUFF_EMPTY_CACHE_TTL_SEC
            self._dotabuff_cache[steam_id] = (now + ttl, matches)
            return matches[:limit]

        matches = self._parse_dotabuff_matches(html, steam_id, limit=fetch_limit)
        if not matches:
            matches = await self._get_recent_matches_dotabuff_browser(steam_id, fetch_limit)
        ttl = DOTABUFF_CACHE_TTL_SEC if matches else DOTABUFF_EMPTY_CACHE_TTL_SEC
        self._dotabuff_cache[steam_id] = (now + ttl, matches)
        if matches:
            logger.info(
                "Dotabuff fallback returned %d match(es) for %s",
                len(matches),
                steam_id,
            )
        return matches[:limit]

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
