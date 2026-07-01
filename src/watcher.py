import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import List, Optional, Callable, Awaitable, Dict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite
from aiogram.exceptions import TelegramForbiddenError

from . import db
from .formatting import format_duration_seconds, format_playtime_minutes
from .match_tracker import MatchTracker
from .models import Target, TargetState, Alert
from .steam import SteamClient, state_name, format_last_seen, detect_invisible
from .config import VISIBILITY_STATES, DEFAULT_STEAM_API_KEY, MATCH_POLL_INTERVAL

logger = logging.getLogger(__name__)


# Known appid → name mapping for popular games
APPID_NAMES = {
    "730": "Counter-Strike 2",
    "570": "Dota 2",
    "440": "Team Fortress 2",
    "578080": "PUBG",
    "1172470": "Apex Legends",
    "359550": "Rainbow Six Siege",
    "271590": "GTA V",
    "1245620": "ELDEN RING",
    "1091500": "Cyberpunk 2077",
    "892970": "Valheim",
    "1599340": "Lost Ark",
    "1174180": "Naraka: Bladepoint",
}


def _appid_to_name(appid: str, game_name_cache: dict = None) -> str:
    """Convert appid to game name using runtime cache or hardcoded fallback."""
    if game_name_cache and appid in game_name_cache:
        return game_name_cache[appid]
    if appid in APPID_NAMES:
        return APPID_NAMES[appid]
    return f"App {appid}"


def format_time_ago(timestamp: int) -> str:
    """Format a unix timestamp as a human-readable Russian relative time."""
    delta = max(0, int(time.time()) - int(timestamp))
    if delta < 60:
        return f"{delta} сек назад"
    if delta < 3600:
        return f"{delta // 60} мин назад"
    if delta < 86400:
        return f"{delta // 3600} ч назад"
    return f"{delta // 86400} дн назад"


def _is_dota_game(game_name: Optional[str]) -> bool:
    """True if the game name refers to Dota 2 (used to gate MMR enrichment)."""
    return bool(game_name) and "Dota" in game_name


def _match_details_url(match_id: str, source: str) -> str:
    """Return the best public match details URL for a detected Dota match."""
    if source == "dotabuff":
        return f"https://www.dotabuff.com/matches/{match_id}"
    return f"https://www.opendota.com/matches/{match_id}"


def generate_alerts(
    target: Target,
    previous_state: Optional[TargetState],
    current_state: TargetState,
    is_invisible: bool,
    grown_games: List[dict] = None,
    game_name_cache: dict = None,
) -> List[Alert]:
    """
    Compare previous and current state, generating alerts for changes.
    Pure function — no I/O, fully testable.
    """
    alerts: List[Alert] = []
    name = target.name

    if previous_state is None:
        # First check
        status = state_name(current_state.persona_state)
        if current_state.game_name:
            alerts.append(Alert(
                target=target,
                message=f"{name}: первая проверка. {status}, играет: {current_state.game_name}",
                event_type="first_check",
                game_name=current_state.game_name,
                is_dota=_is_dota_game(current_state.game_name),
            ))
        else:
            alerts.append(Alert(
                target=target,
                message=f"{name}: первая проверка. Статус: {status}",
                event_type="first_check",
            ))
        return alerts

    prev_status = state_name(previous_state.persona_state)
    curr_status = state_name(current_state.persona_state)

    # Invisible
    if is_invisible:
        invisible_game_name = None
        if grown_games:
            top = grown_games[0]
            game_name = _appid_to_name(top["appid"], game_name_cache)
            invisible_game_name = game_name
            # delta is stored in SECONDS; display minutes (>=1).
            delta_min = max(1, top["delta"] // 60)
            alert_msg = (
                f"👻 {name}: НЕВИДИМКА! Играет в {game_name} "
                f"(+{delta_min} мин)"
            )
            # If multiple games grew, list others too
            if len(grown_games) > 1:
                others = []
                for g in grown_games[1:3]:  # max 2 more
                    g_min = max(1, g["delta"] // 60)
                    others.append(f"{_appid_to_name(g['appid'], game_name_cache)} (+{g_min} мин)")
                if others:
                    alert_msg += "\nТакже: " + ", ".join(others)
        else:
            current_min = max(1, current_state.playtime_forever // 60)
            previous_min = max(0, previous_state.playtime_forever // 60)
            alert_msg = (
                f"👻 {name}: НЕВИДИМКА! Статус offline, "
                f"но наиграно {current_min} мин "
                f"(было {previous_min})"
            )
        alerts.append(Alert(
            target=target,
            message=alert_msg,
            event_type="invisible",
            game_name=invisible_game_name,
            is_dota=_is_dota_game(invisible_game_name),
        ))

    # Visibility change
    if (
        previous_state.visibility_state != current_state.visibility_state
        and current_state.visibility_state != 3
    ):
        vis_name = VISIBILITY_STATES.get(current_state.visibility_state, "Unknown")
        alerts.append(Alert(
            target=target,
            message=f"🔒 {name} изменил видимость профиля: {vis_name}",
            event_type="visibility_change",
        ))

    # Status change
    if previous_state.persona_state != current_state.persona_state:
        if current_state.persona_state > 0:
            alerts.append(Alert(
                target=target,
                message=f"🟢 {name} зашёл онлайн. {curr_status}",
                event_type="online",
            ))
        else:
            last_seen = format_last_seen(current_state.last_logoff)
            alerts.append(Alert(
                target=target,
                message=f"⚫ {name} вышел оффлайн. Был {prev_status}. Последний раз: {last_seen}",
                event_type="offline",
            ))

    # Game change
    current_game = current_state.game_name
    previous_game = previous_state.game_name

    if current_game and current_game != previous_game:
        if previous_game:
            # Compute session duration from game_start_time
            duration_str = ""
            if previous_state.game_start_time:
                elapsed = int(time.time()) - previous_state.game_start_time
                if elapsed > 0:
                    duration_str = f" ({format_duration_seconds(elapsed)})"
            alerts.append(Alert(
                target=target,
                message=f"⏹ {name} перестал играть в {previous_game}{duration_str}",
                event_type="game_stop",
                game_name=previous_game,
                is_dota=_is_dota_game(previous_game),
            ))
        alerts.append(Alert(
            target=target,
            message=f"🎮 {name} начал играть в {current_game}",
            event_type="game_start",
            game_name=current_game,
            is_dota=_is_dota_game(current_game),
        ))
    elif previous_game and not current_game:
        # Stopped playing entirely
        duration_str = ""
        if previous_state.game_start_time:
            elapsed = int(time.time()) - previous_state.game_start_time
            if elapsed > 0:
                duration_str = f" ({format_duration_seconds(elapsed)})"
        alerts.append(Alert(
            target=target,
            message=f"⏹ {name} перестал играть в {previous_game}{duration_str}",
            event_type="game_stop",
            game_name=previous_game,
            is_dota=_is_dota_game(previous_game),
        ))

    # Persona name change
    if (
        previous_state.persona_name
        and current_state.persona_name
        and previous_state.persona_name != current_state.persona_name
    ):
        alerts.append(Alert(
            target=target,
            message=(
                f"{name} сменил ник: "
                f"{previous_state.persona_name} → {current_state.persona_name}"
            ),
            event_type="name_change",
        ))

    return alerts


class Watcher:
    """Background watcher that polls Steam profiles and generates alerts."""

    def __init__(
        self,
        db_conn: aiosqlite.Connection,
        steam_client: SteamClient,
        send_alert: Callable[[int, str], Awaitable[None]],
        match_tracker: Optional[MatchTracker] = None,
    ):
        self._db = db_conn
        self._steam = steam_client
        self._send_alert = send_alert
        self._match_tracker = match_tracker
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_match_poll: float = 0.0
        self._last_session_poll: float = 0.0
        self._last_summary_poll: float = 0.0
        self._recent_matches_cache: Dict[str, list] = {}  # steam_id -> list[MatchInfo]
        self._recent_matches_cache_ttl: float = 0.0
        self._last_cleanup_date: str = ""
        self._blocked_user_ids: set[int] = set()
        # appid (str) -> game name; seeded with hardcoded fallbacks, enriched from Steam profile responses
        self._game_name_cache: Dict[str, str] = dict(APPID_NAMES)

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Watcher started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Watcher stopped")

    async def get_cached_recent_matches(self, steam_id: str) -> list:
        """Get cached recent matches, or fetch if cache is cold."""
        cached = self._recent_matches_cache.get(steam_id)
        if cached:
            return cached
        if self._match_tracker:
            try:
                matches = await self._match_tracker.get_recent_matches(steam_id, limit=3)
                if matches:
                    self._recent_matches_cache[steam_id] = matches
                return matches
            except Exception:
                pass
        return []

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._poll_all()
            except Exception as e:
                logger.error("Error in watcher loop: %s", e)

            if self._match_tracker is not None:
                now = time.time()
                if now - self._last_match_poll >= MATCH_POLL_INTERVAL:
                    self._last_match_poll = now
                    try:
                        await self._poll_matches()
                    except Exception as e:
                        logger.error("Error in match poll: %s", e)

            # Periodic tasks: session updates and daily summaries (every 60s)
            now = time.time()
            if now - self._last_session_poll >= 60:
                self._last_session_poll = now
                try:
                    await self._check_session_updates()
                except Exception as e:
                    logger.error("Error in session updates: %s", e)

            if now - self._last_summary_poll >= 60:
                self._last_summary_poll = now
                try:
                    await self._send_daily_summaries()
                except Exception as e:
                    logger.error("Error in daily summaries: %s", e)

            # Activity log cleanup (once per day)
            today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
            if today != self._last_cleanup_date:
                self._last_cleanup_date = today
                try:
                    deleted = await db.cleanup_activity_log(self._db, retention_days=180)
                    if deleted > 0:
                        logger.info("Cleaned up %d old activity_log entries", deleted)
                except Exception as e:
                    logger.error("Activity log cleanup failed: %s", e)

            await asyncio.sleep(10)

    async def _poll_all(self) -> None:
        """Poll all active targets that are due for a check using batch API."""
        targets = await db.get_active_targets(self._db)
        now = int(time.time())
        self._blocked_user_ids.clear()

        # Group by user (API key)
        user_targets: Dict[int, List[Target]] = {}
        for target in targets:
            user_targets.setdefault(target.telegram_id, []).append(target)

        for telegram_id, user_target_list in user_targets.items():
            user = await db.get_user(self._db, telegram_id)
            if user is None:
                # Use default API key for users without their own
                api_key = DEFAULT_STEAM_API_KEY
                if not api_key:
                    continue
            else:
                api_key = user.steam_api_key or DEFAULT_STEAM_API_KEY
                if not api_key:
                    continue

            # Filter targets that are due for a check
            due_targets: List[Target] = []
            for target in user_target_list:
                if target.telegram_id in self._blocked_user_ids:
                    continue
                state = await db.get_target_state(self._db, target.id)
                if state and (now - state.last_checked) < target.interval_seconds:
                    continue
                due_targets.append(target)

            if not due_targets:
                continue

            # Batch fetch all profiles for this API key in one call
            steam_ids = [t.steam_id for t in due_targets]
            profiles = await self._steam.get_player_summaries_batch(api_key, steam_ids)

            # Process each due target
            for target in due_targets:
                if target.telegram_id in self._blocked_user_ids:
                    continue
                profile = profiles.get(target.steam_id)
                if profile is None:
                    logger.warning("Could not fetch profile for target %s", target.name)
                    continue

                await self._check_target(api_key, target, profile)

    async def _check_target(
        self, api_key: str, target: Target, profile=None
    ) -> None:
        """Check a single target. If profile is None, fetch it individually."""
        now = int(time.time())

        if profile is None:
            profile = await self._steam.get_player_summaries(api_key, target.steam_id)
        if profile is None:
            logger.warning("Could not fetch profile for target %s", target.name)
            return

        # Cache game name whenever Steam includes one — repopulates on restart
        if profile.game_id and profile.game_name:
            self._game_name_cache[str(profile.game_id)] = profile.game_name

        # If Steam returned an app id but no name, fall back to the cache
        resolved_game_name = profile.game_name
        if profile.game_id and not resolved_game_name:
            resolved_game_name = self._game_name_cache.get(str(profile.game_id))

        previous_state = await db.get_target_state(self._db, target.id)

        # Parse previous per-game playtimes (our own tracking, not Steam's unreliable API)
        prev_game_pts = {}
        if previous_state and previous_state.game_playtimes:
            try:
                prev_game_pts = json.loads(previous_state.game_playtimes)
            except (json.JSONDecodeError, TypeError):
                pass

        # Start current playtimes from previous; increment for the actively
        # playing game. Stored unit is SECONDS (see model/readers).
        # We accumulate REAL elapsed wall-time since the previous check rather
        # than a fixed interval, and we do NOT gate on persona_state — Steam can
        # expose a game while reporting offline (persona_state == 0), and that
        # is exactly the invisible-playtime case detect_invisible() must catch.
        current_game_pts = dict(prev_game_pts)
        if profile.game_id:
            appid = str(profile.game_id)
            if previous_state and previous_state.last_checked:
                elapsed = now - previous_state.last_checked
                # Cap so downtime (e.g. bot was offline for hours) doesn't
                # create a huge jump, and guard against clock skew.
                elapsed = max(0, min(elapsed, target.interval_seconds * 3))
            else:
                elapsed = 0
            if elapsed > 0:
                current_game_pts[appid] = current_game_pts.get(appid, 0) + elapsed

        total_playtime = sum(current_game_pts.values())

        # Determine game_start_time:
        # If target just started playing (new game, wasn't playing before), set start time
        game_start_time = previous_state.game_start_time if previous_state else None
        last_observed_game_name = previous_state.last_observed_game_name if previous_state else None
        last_observed_game_time = previous_state.last_observed_game_time if previous_state else None
        if resolved_game_name and profile.persona_state > 0:
            was_playing = previous_state and previous_state.game_name and previous_state.persona_state > 0
            if not was_playing or (previous_state and previous_state.game_name != resolved_game_name):
                # New game session started
                game_start_time = now
            # else: continuing same game, keep existing game_start_time
            observed_at = game_start_time or now
            last_observed_game_name = resolved_game_name
            last_observed_game_time = observed_at
        elif previous_state and previous_state.game_name and previous_state.game_start_time:
            # Preserve the last completed observed session even after the user
            # leaves the game/offline so manual checks can still show it long
            # after activity_log cleanup.
            last_observed_game_name = previous_state.game_name
            last_observed_game_time = previous_state.game_start_time
        else:
            # Not playing anymore, keep the durable last observed game fields.
            pass

        # Initialize daily_playtime_snapshot if needed.
        # The "day" boundary must use the user's timezone so it lines up with
        # the day window used by get_users_due_summary / _send_daily_summaries
        # (which also compute "today" in the user's tz). Using UTC here would
        # misalign the reset for non-UTC users.
        summary_settings = await db.get_user_settings(self._db, target.telegram_id)
        try:
            user_tz = ZoneInfo(summary_settings.timezone)
        except ZoneInfoNotFoundError:
            user_tz = ZoneInfo("UTC")
        today_str = datetime.now(tz=user_tz).strftime("%Y-%m-%d")
        daily_playtime_snapshot = previous_state.daily_playtime_snapshot if previous_state else None
        if daily_playtime_snapshot is None:
            # First ever check — snapshot current playtimes
            daily_playtime_snapshot = json.dumps(current_game_pts)
        else:
            # Check if snapshot is from a previous day
            try:
                # We don't store the date of the snapshot directly, so we check
                # by comparing against last_checked date (in the user's tz).
                if previous_state and previous_state.last_checked:
                    snapshot_date = datetime.fromtimestamp(
                        previous_state.last_checked, tz=user_tz
                    ).strftime("%Y-%m-%d")
                    if snapshot_date != today_str:
                        daily_playtime_snapshot = json.dumps(current_game_pts)
            except (OSError, ValueError):
                daily_playtime_snapshot = json.dumps(current_game_pts)

        current_state = TargetState(
            target_id=target.id,
            persona_state=profile.persona_state,
            persona_name=profile.persona_name,
            game_id=profile.game_id,
            game_name=resolved_game_name,
            playtime_forever=total_playtime,
            last_logoff=profile.last_logoff,
            last_checked=now,
            last_match_id=previous_state.last_match_id if previous_state else None,
            last_match_time=previous_state.last_match_time if previous_state else None,
            game_playtimes=json.dumps(current_game_pts),
            visibility_state=profile.visibility_state,
            game_start_time=game_start_time,
            last_session_update=previous_state.last_session_update if previous_state else None,
            daily_playtime_snapshot=daily_playtime_snapshot,
            last_observed_game_name=last_observed_game_name,
            last_observed_game_time=last_observed_game_time,
        )

        prev_playtime = previous_state.playtime_forever if previous_state else None
        is_invisible = detect_invisible(
            profile.persona_state,
            total_playtime,
            prev_playtime,
        )

        # Detect which games grew (for invisible alerts)
        grown_games = self._detect_grown_games(current_game_pts, prev_game_pts)

        alerts = generate_alerts(
            target, previous_state, current_state, is_invisible, grown_games, self._game_name_cache,
        )
        # Load per-target settings once for the loop
        target_settings = await db.get_target_settings(self._db, target.id)
        for alert in alerts:
            # Check privacy alerts setting (global toggle)
            if alert.event_type == "visibility_change":
                settings = await db.get_user_settings(self._db, target.telegram_id)
                if not settings.privacy_alerts_enabled:
                    continue
                # Also check per-target privacy setting
                if not target_settings.get("alert_privacy", True):
                    continue

            # Per-target alert type filtering
            if alert.event_type in ("online", "offline"):
                if not target_settings.get("alert_online", True):
                    continue
            if alert.event_type in ("game_start", "game_stop"):
                if not target_settings.get("alert_game_start", True):
                    continue
            if alert.event_type == "invisible":
                if not target_settings.get("alert_invisible", True):
                    continue
            if alert.event_type == "name_change":
                if not target_settings.get("alert_name_change", True):
                    continue

            # Enrich Dota 2 "started playing" alerts with MMR/rank
            if alert.event_type == "game_start" and alert.is_dota:
                # Check per-target MMR setting — skip enrichment but still send base alert
                if target_settings.get("alert_mmr", True):
                    enriched = await self._enrich_dota_alert(target, alert.message)
                    if enriched:
                        alert.message = enriched
            await self._send_to_target(target, alert.message)
            if not target.active:
                # User blocked the bot — target was just deactivated; stop
                # sending the remaining alerts to this unreachable target.
                break

        # Log activity events for history tracking
        await self._log_activity_from_alerts(target, alerts, previous_state, current_state)

        await db.save_target_state(self._db, current_state)

    async def _log_activity_from_alerts(
        self, target: Target, alerts: List[Alert], prev: Optional[TargetState], curr: TargetState,
    ) -> None:
        """Persist key state-change events to activity_log based on generated alerts."""
        now = int(time.time())
        entries = []
        # Only these structured event types are persisted to activity_log.
        loggable = {"online", "offline", "game_start", "game_stop", "name_change", "invisible"}
        for alert in alerts:
            event_type = alert.event_type if alert.event_type in loggable else None
            game_name = None
            details = None

            if event_type == "game_start":
                game_name = curr.game_name
            elif event_type == "game_stop":
                if prev and prev.game_name:
                    game_name = prev.game_name
                if prev and prev.game_start_time:
                    elapsed = now - prev.game_start_time
                    if elapsed > 0:
                        details = format_duration_seconds(elapsed)
            elif event_type == "name_change":
                if prev:
                    details = f"{prev.persona_name} → {curr.persona_name}"
            elif event_type == "invisible":
                if curr.game_name:
                    game_name = curr.game_name

            if event_type:
                entries.append({
                    "target_id": target.id,
                    "event_type": event_type,
                    "game_name": game_name,
                    "details": details,
                    "detected_at": now,
                })

        if entries:
            try:
                await db.save_activity_batch(self._db, entries)
            except Exception as e:
                logger.error("Failed to log activities: %s", e)

    @staticmethod
    def _detect_grown_games(
        current: Dict[str, int], previous: Dict[str, int]
    ) -> List[dict]:
        """Compare per-game playtimes, return list of {appid, name, delta_min} for games that grew."""
        grown = []
        for appid, playtime in current.items():
            prev = previous.get(appid, 0)
            delta = playtime - prev
            if delta > 0:
                grown.append({"appid": appid, "playtime": playtime, "delta": delta})
        # Sort by delta descending
        grown.sort(key=lambda x: x["delta"], reverse=True)
        return grown

    async def _check_session_updates(self) -> None:
        """Send periodic session duration updates for targets currently playing."""
        targets = await db.get_active_targets(self._db)
        now = int(time.time())

        for target in targets:
            state = await db.get_target_state(self._db, target.id)
            if state is None:
                continue

            # Only check targets currently playing (game set and online)
            if not state.game_name or state.persona_state <= 0:
                continue

            if state.game_start_time is None:
                continue

            settings = await db.get_user_settings(self._db, target.telegram_id)
            if not settings.session_updates_enabled:
                continue

            # Check per-target session alert setting
            target_settings = await db.get_target_settings(self._db, target.id)
            if not target_settings.get("alert_session", True):
                continue

            elapsed = now - state.game_start_time
            if elapsed < 60:
                continue  # Don't send updates for very short sessions

            last_update = state.last_session_update or state.game_start_time
            if now - last_update < settings.session_update_interval:
                continue  # Not time for an update yet

            # Send session duration update
            name = state.persona_name or target.name
            duration_str = format_duration_seconds(elapsed)
            message = f"🎮 {name} играет в {state.game_name} ({duration_str})"

            if await self._send_to_target(target, message):
                # Update last_session_update
                state.last_session_update = now
                try:
                    await db.save_target_state(self._db, state)
                except Exception as e:
                    logger.error("Failed to send session update: %s", e)

    async def _send_daily_summaries(self) -> None:
        """Send daily playtime summaries to users who are due."""
        users = await db.get_users_due_summary(self._db)
        if not users:
            return

        now_utc = datetime.now(tz=timezone.utc)

        for telegram_id, summary_time, tz_name in users:
            try:
                try:
                    user_tz = ZoneInfo(tz_name)
                except ZoneInfoNotFoundError:
                    user_tz = ZoneInfo("UTC")
                today_str = now_utc.astimezone(user_tz).strftime("%Y-%m-%d")
                targets = await db.get_targets(self._db, telegram_id)
                if not targets:
                    await db.mark_summary_sent(self._db, telegram_id, today_str)
                    continue

                # Aggregate playtime per target so the midnight report keeps
                # the nickname / player grouping instead of collapsing all
                # games from all monitored profiles into one flat list.
                per_target: list[tuple[str, Dict[str, int], int]] = []
                overall_total = 0

                for target in targets:
                    if not target.active:
                        continue
                    state = await db.get_target_state(self._db, target.id)
                    if state is None:
                        continue

                    if not state.game_playtimes:
                        continue

                    try:
                        current_pts = json.loads(state.game_playtimes)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    # Parse snapshot
                    snapshot_pts = {}
                    if state.daily_playtime_snapshot:
                        try:
                            snapshot_pts = json.loads(state.daily_playtime_snapshot)
                        except (json.JSONDecodeError, TypeError):
                            snapshot_pts = {}

                    # Calculate per-game delta for this target.
                    target_games: Dict[str, int] = {}
                    for appid, playtime in current_pts.items():
                        prev = snapshot_pts.get(appid, 0)
                        delta = playtime - prev
                        if delta > 0:
                            game_name = _appid_to_name(appid, self._game_name_cache)
                            target_games[game_name] = target_games.get(game_name, 0) + delta

                    if target_games:
                        target_total = sum(target_games.values())
                        display_name = (state.persona_name if state else None) or target.name
                        per_target.append((display_name, target_games, target_total))
                        overall_total += target_total

                if not per_target:
                    # No playtime today, still mark as sent
                    await db.mark_summary_sent(self._db, telegram_id, today_str)
                    continue

                # Build summary message. Aggregated values are SECONDS; the
                # formatter takes minutes, so convert at the boundary.
                lines = ["📊 Ежедневная сводка", ""]
                per_target.sort(key=lambda item: item[2], reverse=True)
                for idx, (display_name, target_games, target_total) in enumerate(per_target):
                    if idx > 0:
                        lines.append("")
                    lines.append(display_name)
                    sorted_games = sorted(target_games.items(), key=lambda x: x[1], reverse=True)
                    for game_name, seconds in sorted_games[:10]:
                        lines.append(f"• {game_name}: {format_playtime_minutes(seconds // 60)}")
                    lines.append(f"Всего: {format_playtime_minutes(target_total // 60)}")

                if len(per_target) > 1:
                    lines.append("")
                    lines.append(f"Итого по всем: {format_playtime_minutes(overall_total // 60)}")

                try:
                    await self._send_alert(telegram_id, "\n".join(lines))
                except TelegramForbiddenError:
                    logger.info(
                        "Bot blocked by %s during daily summary; deactivating all targets",
                        telegram_id,
                    )
                    await self._deactivate_blocked_user(telegram_id)
                await db.mark_summary_sent(self._db, telegram_id, today_str)

            except Exception as e:
                logger.error("Failed to send daily summary for %s: %s", telegram_id, e)

    async def _deactivate_blocked_user(self, telegram_id: int) -> None:
        try:
            changed = await db.set_all_targets_active(self._db, telegram_id, False)
            self._blocked_user_ids.add(telegram_id)
            logger.info("Deactivated %s targets for blocked user %s", changed, telegram_id)
        except Exception as e:
            logger.error("Failed to deactivate blocked user %s: %s", telegram_id, e)

    async def _send_to_target(self, target: Target, message: str) -> bool:
        """Send a message tied to a specific target.

        On a regular send failure we log and return False. If the user has
        blocked the bot (TelegramForbiddenError), polling all targets for that
        Telegram user is pointless forever, so deactivate the entire user slice.
        """
        try:
            await self._send_alert(target.telegram_id, message)
            return True
        except TelegramForbiddenError:
            logger.info(
                "Bot blocked by %s; deactivating all targets for user after target %s",
                target.telegram_id,
                target.name,
            )
            await self._deactivate_blocked_user(target.telegram_id)
            target.active = False
            return False
        except Exception as e:
            logger.error("Failed to send alert: %s", e)
            return False

    async def _enrich_dota_alert(self, target: Target, base_message: str) -> Optional[str]:
        """Append Dota 2 MMR/rank to an alert via the match tracker's rank lookup."""
        if self._match_tracker is None:
            return None

        try:
            rank = await self._match_tracker.get_player_rank(target.steam_id)
        except Exception as e:
            logger.error("OpenDota enrich failed for %s: %s", target.name, e)
            return None

        if not rank:
            return None

        mmr = rank.get("mmr")
        rank_tier = rank.get("rank_tier")
        if not (mmr or rank_tier):
            return None

        rank_info = {}
        if mmr:
            rank_info["mmr"] = mmr
        # Decode rank_tier: first digit = rank (Herald=1..Divine=8), second = star
        if rank_tier:
            rank_names = {
                1: "Herald", 2: "Guardian", 3: "Crusader", 4: "Archon",
                5: "Legend", 6: "Ancient", 7: "Divine", 8: "Immortal",
            }
            rank_num = rank_tier // 10
            stars = rank_tier % 10
            if rank_num in rank_names:
                if rank_num == 8:
                    rank_str = f"Immortal (rank {stars})"
                else:
                    rank_str = f"{rank_names[rank_num]} {stars}"
                rank_info["rank_tier"] = rank_str
            else:
                rank_info["rank_tier"] = str(rank_tier)

        parts = [base_message]
        if rank_info.get("mmr"):
            parts.append(f"MMR: {rank_info['mmr']}")
        if rank_info.get("rank_tier"):
            parts.append(f"Ранг: {rank_info['rank_tier']}")
        return "\n".join(parts)

    async def _poll_matches(self) -> None:
        """Best-effort poll for new Dota 2 matches on offline/invisible targets."""
        if self._match_tracker is None:
            return

        targets = await db.get_active_targets(self._db)
        now = int(time.time())

        for target in targets:
            state = await db.get_target_state(self._db, target.id)
            if state is None:
                continue

            # Only poll offline/invisible targets — online players don't need this check
            if state.persona_state != 0:
                continue

            if state.last_match_time and (now - state.last_match_time) < MATCH_POLL_INTERVAL:
                continue

            try:
                match = await self._match_tracker.get_last_match(target.steam_id)
            except Exception as e:
                logger.error("Match lookup failed for %s: %s", target.name, e)
                continue

            try:
                recent = await self._match_tracker.get_recent_matches(target.steam_id, limit=3)
                if recent:
                    self._recent_matches_cache[target.steam_id] = recent
            except Exception:
                pass

            if match is None:
                continue

            if match.match_id == state.last_match_id:
                continue

            if await db.has_activity(self._db, target.id, "match", match_id=match.match_id):
                state.last_match_id = match.match_id
                state.last_match_time = now
                await db.save_target_state(self._db, state)
                continue

            state.last_match_id = match.match_id
            state.last_match_time = now
            await db.save_target_state(self._db, state)

            # Log match to activity history
            try:
                await db.save_activity(
                    self._db, target.id, "match",
                    game_name="Dota 2",
                    match_id=match.match_id,
                    hero_name=match.hero_name,
                    duration_seconds=match.duration,
                    detected_at=now,
                )
            except Exception as e:
                logger.error("Failed to log match activity: %s", e)

            if match.start_time is not None:
                time_ago = format_time_ago(match.start_time)
                timing = f"Катал Dota 2 {time_ago}"
            else:
                timing = "Засветился матч Dota 2"

            source_suffix = " через Dotabuff" if match.source == "dotabuff" else ""
            match_url = _match_details_url(match.match_id, match.source)
            message = (
                f"🕵️ {target.name}: скрытая активность! "
                f"{timing}{source_suffix} (матч {match.match_id})\n"
                f"{match_url}"
            )
            # Check per-target invisible alert setting
            target_settings = await db.get_target_settings(self._db, target.id)
            if target_settings.get("alert_invisible", True):
                await self._send_to_target(target, message)
