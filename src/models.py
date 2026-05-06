from dataclasses import dataclass, field
from typing import Optional


@dataclass
class User:
    telegram_id: int
    steam_api_key: str


@dataclass
class Target:
    id: int
    telegram_id: int
    steam_id: str
    name: str
    interval_seconds: int = 300
    active: bool = True


@dataclass
class TargetState:
    target_id: int
    persona_state: int = 0
    persona_name: str = ""
    game_id: Optional[str] = None
    game_name: Optional[str] = None
    playtime_forever: int = 0
    last_logoff: Optional[int] = None
    last_checked: int = 0
    last_match_id: Optional[str] = None
    last_match_time: Optional[int] = None


@dataclass
class Alert:
    target: Target
    message: str


@dataclass
class SteamProfile:
    steam_id: str
    persona_name: str
    persona_state: int
    game_id: Optional[str] = None
    game_name: Optional[str] = None
    last_logoff: Optional[int] = None


@dataclass
class MatchInfo:
    match_id: str
    steam_id: str
    game: str  # "dota2", "cs2", etc.
    start_time: int  # unix timestamp
    duration: int  # seconds
    hero_name: Optional[str] = None  # for Dota


@dataclass
class RecentGames:
    steam_id: str
    games: list = field(default_factory=list)
    # list of {"appid": int, "name": str, "playtime_forever": int}
