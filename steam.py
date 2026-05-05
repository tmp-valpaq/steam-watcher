import aiohttp
from datetime import datetime

BASE = "https://api.steampowered.com"
STATE_NAMES = {
    0: "⚫ Offline",
    1: "🟢 Online",
    2: "🔴 Busy",
    3: "🟡 Away",
    4: "😴 Snooze",
    5: "💱 Looking to trade",
    6: "🎮 Looking to play",
}


async def fetch_player(api_key: str, steam_id: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{BASE}/ISteamUser/GetPlayerSummaries/v2/",
            params={"key": api_key, "steamids": steam_id},
        ) as r:
            data = await r.json()
            players = data.get("response", {}).get("players", [])
            return players[0] if players else None


async def fetch_recent_games(api_key: str, steam_id: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{BASE}/IPlayerService/GetRecentlyPlayedGames/v1/",
            params={"key": api_key, "steamid": steam_id},
        ) as r:
            data = await r.json()
            return data.get("response", {}).get("games", [])


def state_name(state: int) -> str:
    return STATE_NAMES.get(state, f"Unknown({state})")


def format_last_seen(timestamp: int | None) -> str:
    if not timestamp:
        return "unknown"
    return datetime.fromtimestamp(timestamp).strftime("%d.%m %H:%M")


def detect_invisible(
    persona_state: int, current_playtime: dict, prev_playtime: dict
) -> bool:
    """Status says offline but playtime grew → invisible."""
    if persona_state != 0:
        return False
    for appid, pt in current_playtime.items():
        prev = prev_playtime.get(str(appid), 0)
        if pt > prev:
            return True
    return False
