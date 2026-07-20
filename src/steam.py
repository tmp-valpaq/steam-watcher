import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, List

import aiohttp

from .config import STEAM_API_BASE, PERSONA_STATES
from .util import BoundedDict
from .models import SteamProfile

logger = logging.getLogger(__name__)


def _safe_error(e: BaseException) -> str:
    """Describe an exception without leaking the request URL/params.

    The Steam API key is passed as a query param, and aiohttp's
    ClientResponseError.__str__ includes the full request URL (with the key).
    Logging the raw exception would leak the key, so we only surface the
    exception type and HTTP status (when available).
    """
    status = getattr(e, "status", None)
    if status is not None:
        return f"{type(e).__name__}: HTTP {status}"
    return type(e).__name__


def state_name(persona_state: int) -> str:
    """Convert persona_state integer to human-readable name."""
    return PERSONA_STATES.get(persona_state, f"Unknown ({persona_state})")


def format_last_seen(last_logoff: Optional[int]) -> str:
    """Format a Unix timestamp into a human-readable last-seen string."""
    if last_logoff is None:
        return "never"
    dt = datetime.fromtimestamp(last_logoff, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def detect_invisible(
    persona_state: int,
    current_playtime: int,
    previous_playtime: Optional[int],
) -> bool:
    """
    Detect if a user is likely invisible/offline but actually playing.

    A user is 'invisible' if their persona_state is 0 (offline) but
    their total playtime has increased since the last check.
    """
    if persona_state != 0:
        return False
    if previous_playtime is None:
        return False
    return current_playtime > previous_playtime


class SteamClient:
    """Steam API client with session reuse and per-key rate limiting."""

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session
        # Per-API-key rate limiting: {api_key: last_request_time}
        self._last_request_times: Dict[str, float] = BoundedDict(256)
        self._min_interval: float = 1.0  # 1 req/sec per key

    async def _rate_limit(self, api_key: str) -> None:
        """Ensure we don't exceed 1 request per second per API key."""
        now = asyncio.get_event_loop().time()
        last = self._last_request_times.get(api_key, 0.0)
        elapsed = now - last
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_request_times[api_key] = asyncio.get_event_loop().time()

    async def _get(self, url: str, params: dict, api_key: str = "") -> dict:
        """Make a rate-limited GET request to the Steam API."""
        await self._rate_limit(api_key)
        async with self._session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_player_summaries(
        self, api_key: str, steam_id: str
    ) -> Optional[SteamProfile]:
        """Fetch player summary for a single Steam ID."""
        url = f"{STEAM_API_BASE}/ISteamUser/GetPlayerSummaries/v0002/"
        params = {
            "key": api_key,
            "steamids": steam_id,
        }
        try:
            data = await self._get(url, params, api_key=api_key)
            players = data.get("response", {}).get("players", [])
            if not players:
                return None
            p = players[0]
            return SteamProfile(
                steam_id=str(p["steamid"]),
                persona_name=p.get("personaname", "Unknown"),
                persona_state=p.get("personastate", 0),
                game_id=str(p["gameid"]) if p.get("gameid") else None,
                game_name=p.get("gameextrainfo"),
                last_logoff=p.get("lastlogoff"),
                visibility_state=p.get("communityvisibilitystate", 3),
            )
        except Exception as e:
            logger.error(
                "Failed to get player summary for %s: %s", steam_id, _safe_error(e)
            )
            return None

    async def get_player_summaries_batch(
        self, api_key: str, steam_ids: List[str]
    ) -> Dict[str, SteamProfile]:
        """
        Fetch player summaries for multiple Steam IDs in a single request.
        Steam API supports up to 100 steamids per request (comma-separated).
        Returns a dict mapping steam_id -> SteamProfile.
        """
        if not steam_ids:
            return {}

        url = f"{STEAM_API_BASE}/ISteamUser/GetPlayerSummaries/v0002/"
        params = {
            "key": api_key,
            "steamids": ",".join(steam_ids),
        }
        try:
            data = await self._get(url, params, api_key=api_key)
            players = data.get("response", {}).get("players", [])
            result: Dict[str, SteamProfile] = {}
            for p in players:
                sid = str(p["steamid"])
                result[sid] = SteamProfile(
                    steam_id=sid,
                    persona_name=p.get("personaname", "Unknown"),
                    persona_state=p.get("personastate", 0),
                    game_id=str(p["gameid"]) if p.get("gameid") else None,
                    game_name=p.get("gameextrainfo"),
                    last_logoff=p.get("lastlogoff"),
                    visibility_state=p.get("communityvisibilitystate", 3),
                )
            return result
        except Exception as e:
            logger.error("Failed to get batch player summaries: %s", _safe_error(e))
            return {}

    async def resolve_vanity_url(self, api_key: str, vanity: str) -> Optional[str]:
        """Resolve a vanity URL name to a SteamID64."""
        url = f"{STEAM_API_BASE}/ISteamUser/ResolveVanityURL/v0001/"
        params = {"key": api_key, "vanityurl": vanity}
        try:
            data = await self._get(url, params, api_key=api_key)
            response = data.get("response", {})
            if response.get("success") == 1:
                return response.get("steamid")
            return None
        except Exception as e:
            logger.error(
                "Failed to resolve vanity URL %s: %s", vanity, _safe_error(e)
            )
            return None

    async def get_recently_played_games(
        self, api_key: str, steam_id: str
    ) -> List[str]:
        """Return recently played game names for a Steam profile.

        Steam's recently-played endpoint does not expose an exact last-played
        timestamp, only a bounded list of games from the recent window. We use
        it as a best-effort hint for the last visible game in manual checks.
        """
        url = f"{STEAM_API_BASE}/IPlayerService/GetRecentlyPlayedGames/v0001/"
        params = {
            "key": api_key,
            "steamid": steam_id,
            "format": "json",
            "count": 3,
        }
        try:
            data = await self._get(url, params, api_key=api_key)
            games = data.get("response", {}).get("games", [])
            names = []
            for game in games:
                name = game.get("name")
                if name:
                    names.append(name)
            return names
        except Exception as e:
            logger.error(
                "Failed to get recently played games for %s: %s",
                steam_id,
                _safe_error(e),
            )
            return []

    async def validate_key(self, api_key: str) -> bool:
        """Validate an API key by making a lightweight request."""
        url = f"{STEAM_API_BASE}/ISteamUser/GetPlayerSummaries/v0002/"
        params = {
            "key": api_key,
            "steamids": "76561197960287930",  # Gabe's well-known ID
        }
        try:
            data = await self._get(url, params, api_key=api_key)
            # If we get a response without error, the key is valid
            return "response" in data
        except Exception:
            return False
