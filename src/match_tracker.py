import asyncio
import logging
from typing import Dict, List, Optional

import aiohttp

from .models import MatchInfo

logger = logging.getLogger(__name__)

OPENDOTA_API_BASE = "https://api.opendota.com/api"
STEAM_ID_OFFSET = 76561197960265728


class MatchTracker:
    """OpenDota API client with rate limiting and hero name caching."""

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session
        self._hero_cache: Dict[int, str] = {}
        self._last_request_time: float = 0.0
        self._min_interval: float = 1.0  # 1 req/sec for OpenDota free tier

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

    async def get_last_match(self, steam_id: str) -> Optional[MatchInfo]:
        """Fetch the most recent Dota 2 match for a Steam ID."""
        account_id = self._to_account_id(steam_id)
        if account_id is None or account_id <= 0:
            return None

        url = f"{OPENDOTA_API_BASE}/players/{account_id}/matches?limit=1"
        try:
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

            return MatchInfo(
                match_id=str(match_id),
                steam_id=steam_id,
                game="dota2",
                start_time=int(start_time),
                duration=int(duration),
                hero_name=hero_name,
            )
        except Exception as e:
            logger.error("Failed to get last match for %s: %s", steam_id, e)
            return None

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
