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
    # JSON: {appid_str: playtime_minutes_int, ...}
    game_playtimes: Optional[str] = None
    visibility_state: int = 3
    game_start_time: Optional[int] = None
    last_session_update: Optional[int] = None
    daily_playtime_snapshot: Optional[str] = None


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
    visibility_state: int = 3


@dataclass
class MatchInfo:
    match_id: str
    steam_id: str
    game: str  # "dota2", "cs2", etc.
    duration: int  # seconds
    start_time: Optional[int] = None  # unix timestamp when available
    hero_name: Optional[str] = None  # for Dota
    source: str = "opendota"


@dataclass
class RecentGames:
    steam_id: str
    games: list = field(default_factory=list)
    # list of {"appid": int, "name": str, "playtime_forever": int}


@dataclass
class UserSettings:
    telegram_id: int
    daily_summary_enabled: bool = True
    daily_summary_time: str = "21:00"
    session_updates_enabled: bool = True
    session_update_interval: int = 900
    privacy_alerts_enabled: bool = True
    last_summary_date: str = ""
    timezone: str = "UTC"
