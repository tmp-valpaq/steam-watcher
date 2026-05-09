import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import List, Optional, Callable, Awaitable, Dict

import aiosqlite

from . import db
from .match_tracker import MatchTracker
from .models import Target, TargetState, Alert
from .steam import SteamClient, state_name, format_last_seen, detect_invisible
from .config import VISIBILITY_STATES

logger = logging.getLogger(__name__)

MATCH_POLL_INTERVAL = 300


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


def _appid_to_name(appid: str, recent_games: list = None) -> str:
    """Convert appid to game name using lookup or recent games data."""
    if appid in APPID_NAMES:
        return APPID_NAMES[appid]
    if recent_games:
        for g in recent_games:
            if str(g["appid"]) == appid:
                return g.get("name", f"App {appid}")
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


def format_duration_seconds(seconds: int) -> str:
    """Format duration in seconds to 'Xч Yмин' string."""
    if seconds < 60:
        return f"{seconds}с"
    minutes = seconds // 60
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}мин {secs}с"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}ч {mins}мин"


def generate_alerts(
    target: Target,
    previous_state: Optional[TargetState],
    current_state: TargetState,
    is_invisible: bool,
    grown_games: List[dict] = None,
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
            ))
        else:
            alerts.append(Alert(
                target=target,
                message=f"{name}: первая проверка. Статус: {status}",
            ))
        return alerts

    prev_status = state_name(previous_state.persona_state)
    curr_status = state_name(current_state.persona_state)

    # Invisible
    if is_invisible:
        if grown_games:
            top = grown_games[0]
            game_name = _appid_to_name(top["appid"])
            delta_min = top["delta"]
            alert_msg = (
                f"👻 {name}: НЕВИДИМКА! Играет в {game_name} "
                f"(+{delta_min} мин)"
            )
            # If multiple games grew, list others too
            if len(grown_games) > 1:
                others = []
                for g in grown_games[1:3]:  # max 2 more
                    others.append(f"{_appid_to_name(g['appid'])} (+{g['delta']} мин)")
                if others:
                    alert_msg += "\nТакже: " + ", ".join(others)
        else:
            alert_msg = (
                f"👻 {name}: НЕВИДИМКА! Статус offline, "
                f"но наиграно {current_state.playtime_forever} мин "
                f"(было {previous_state.playtime_forever})"
            )
        alerts.append(Alert(target=target, message=alert_msg))

    # Visibility change
    if (
        previous_state.visibility_state != current_state.visibility_state
        and current_state.visibility_state != 3
    ):
        vis_name = VISIBILITY_STATES.get(current_state.visibility_state, "Unknown")
        alerts.append(Alert(
            target=target,
            message=f"🔒 {name} изменил видимость профиля: {vis_name}",
        ))

    # Status change
    if previous_state.persona_state != current_state.persona_state:
        if current_state.persona_state > 0:
            alerts.append(Alert(
                target=target,
                message=f"🟢 {name} зашёл онлайн. {curr_status}",
            ))
        else:
            last_seen = format_last_seen(current_state.last_logoff)
            alerts.append(Alert(
                target=target,
                message=f"⚫ {name} вышел оффлайн. Был {prev_status}. Последний раз: {last_seen}",
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
            ))
        alerts.append(Alert(
            target=target,
            message=f"🎮 {name} начал играть в {current_game}",
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
        self._opendota_rank_cache: Dict[str, dict] = {}  # steam_id -> rank data
        self._opendota_cache_ttl: float = 0.0

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

            await asyncio.sleep(10)

    async def _poll_all(self) -> None:
        """Poll all active targets that are due for a check using batch API."""
        targets = await db.get_active_targets(self._db)
        now = int(time.time())

        # Group by user (API key)
        user_targets: Dict[int, List[Target]] = {}
        for target in targets:
            user_targets.setdefault(target.telegram_id, []).append(target)

        for telegram_id, user_target_list in user_targets.items():
            user = await db.get_user(self._db, telegram_id)
            if user is None:
                # Use default API key for users without their own
                from .config import DEFAULT_STEAM_API_KEY
                api_key = DEFAULT_STEAM_API_KEY
                if not api_key:
                    continue
            else:
                from .config import DEFAULT_STEAM_API_KEY
                api_key = user.steam_api_key or DEFAULT_STEAM_API_KEY
                if not api_key:
                    continue

            # Filter targets that are due for a check
            due_targets: List[Target] = []
            for target in user_target_list:
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

        recent = await self._steam.get_recently_played(api_key, target.steam_id)

        total_playtime = sum(g["playtime_forever"] for g in recent.games) if recent.games else 0

        # Build per-game playtime map: {appid_str: minutes}
        current_game_pts = {}
        if recent.games:
            for g in recent.games:
                current_game_pts[str(g["appid"])] = g["playtime_forever"]

        previous_state = await db.get_target_state(self._db, target.id)

        # Parse previous per-game playtimes
        prev_game_pts = {}
        if previous_state and previous_state.game_playtimes:
            try:
                prev_game_pts = json.loads(previous_state.game_playtimes)
            except (json.JSONDecodeError, TypeError):
                pass

        # Determine game_start_time:
        # If target just started playing (new game, wasn't playing before), set start time
        game_start_time = previous_state.game_start_time if previous_state else None
        if profile.game_name and profile.persona_state > 0:
            was_playing = previous_state and previous_state.game_name and previous_state.persona_state > 0
            if not was_playing or (previous_state and previous_state.game_name != profile.game_name):
                # New game session started
                game_start_time = now
            # else: continuing same game, keep existing game_start_time
        else:
            # Not playing anymore, clear game_start_time (will be used for duration calc in alerts)
            pass

        # Initialize daily_playtime_snapshot if needed
        today_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        daily_playtime_snapshot = previous_state.daily_playtime_snapshot if previous_state else None
        if daily_playtime_snapshot is None:
            # First ever check — snapshot current playtimes
            daily_playtime_snapshot = json.dumps(current_game_pts)
        else:
            # Check if snapshot is from a previous day
            try:
                # We don't store the date of the snapshot directly, so we check
                # by comparing against last_checked date
                if previous_state and previous_state.last_checked:
                    snapshot_date = datetime.fromtimestamp(
                        previous_state.last_checked, tz=timezone.utc
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
            game_name=profile.game_name,
            playtime_forever=total_playtime,
            last_logoff=profile.last_logoff,
            last_checked=now,
            game_playtimes=json.dumps(current_game_pts),
            visibility_state=profile.visibility_state,
            game_start_time=game_start_time,
            last_session_update=previous_state.last_session_update if previous_state else None,
            daily_playtime_snapshot=daily_playtime_snapshot,
        )

        prev_playtime = previous_state.playtime_forever if previous_state else None
        is_invisible = detect_invisible(
            profile.persona_state,
            total_playtime,
            prev_playtime,
        )

        # Detect which games grew (for invisible alerts)
        grown_games = self._detect_grown_games(current_game_pts, prev_game_pts)

        alerts = generate_alerts(target, previous_state, current_state, is_invisible, grown_games)
        # Load per-target settings once for the loop
        target_settings = await db.get_target_settings(self._db, target.id)
        for alert in alerts:
            # Check privacy alerts setting (global toggle)
            if "изменил видимость" in alert.message:
                settings = await db.get_user_settings(self._db, target.telegram_id)
                if not settings.privacy_alerts_enabled:
                    continue
                # Also check per-target privacy setting
                if not target_settings.get("alert_privacy", True):
                    continue

            # Per-target alert type filtering
            msg = alert.message
            if "зашёл онлайн" in msg or "вышел оффлайн" in msg:
                if not target_settings.get("alert_online", True):
                    continue
            if "начал играть" in msg or "перестал играть" in msg:
                if not target_settings.get("alert_game_start", True):
                    continue
            if "НЕВИДИМКА" in msg or "скрытая активность" in msg:
                if not target_settings.get("alert_invisible", True):
                    continue
            if "сменил ник" in msg:
                if not target_settings.get("alert_name_change", True):
                    continue

            # Enrich Dota 2 "started playing" alerts with MMR/rank
            if "начал играть" in alert.message and "Dota" in alert.message:
                # Check per-target MMR setting — skip enrichment but still send base alert
                if target_settings.get("alert_mmr", True):
                    enriched = await self._enrich_dota_alert(target, alert.message)
                    if enriched:
                        alert.message = enriched
            try:
                await self._send_alert(target.telegram_id, alert.message)
            except Exception as e:
                logger.error("Failed to send alert: %s", e)

        await db.save_target_state(self._db, current_state)

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

            try:
                await self._send_alert(target.telegram_id, message)
                # Update last_session_update
                state.last_session_update = now
                await db.save_target_state(self._db, state)
            except Exception as e:
                logger.error("Failed to send session update: %s", e)

    async def _send_daily_summaries(self) -> None:
        """Send daily playtime summaries to users who are due."""
        users = await db.get_users_due_summary(self._db)
        if not users:
            return

        today_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

        for telegram_id, summary_time in users:
            try:
                targets = await db.get_targets(self._db, telegram_id)
                if not targets:
                    await db.mark_summary_sent(self._db, telegram_id, today_str)
                    continue

                # Aggregate playtime across all active targets
                aggregated: Dict[str, int] = {}  # game_name -> minutes played today

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

                    # Calculate per-game delta
                    for appid, playtime in current_pts.items():
                        prev = snapshot_pts.get(appid, 0)
                        delta = playtime - prev
                        if delta > 0:
                            game_name = _appid_to_name(appid)
                            aggregated[game_name] = aggregated.get(game_name, 0) + delta

                if not aggregated:
                    # No playtime today, still mark as sent
                    await db.mark_summary_sent(self._db, telegram_id, today_str)
                    continue

                # Build summary message
                lines = ["📊 Ежедневная сводка", ""]
                sorted_games = sorted(aggregated.items(), key=lambda x: x[1], reverse=True)
                for game_name, minutes in sorted_games[:10]:
                    lines.append(f"• {game_name}: {_format_playtime_minutes(minutes)}")

                total = sum(aggregated.values())
                lines.append("")
                lines.append(f"Всего: {_format_playtime_minutes(total)}")

                await self._send_alert(telegram_id, "\n".join(lines))
                await db.mark_summary_sent(self._db, telegram_id, today_str)

            except Exception as e:
                logger.error("Failed to send daily summary for %s: %s", telegram_id, e)

    @staticmethod
    def _format_playtime_minutes(minutes: int) -> str:
        """Format playtime in minutes to 'Xч Yмин' string."""
        hours = minutes // 60
        mins = minutes % 60
        if hours > 0 and mins > 0:
            return f"{hours}ч {mins}мин"
        elif hours > 0:
            return f"{hours}ч"
        else:
            return f"{mins}мин"

    async def _enrich_dota_alert(self, target: Target, base_message: str) -> Optional[str]:
        """Fetch Dota 2 MMR/rank from OpenDota and append to alert message."""
        steam_id = target.steam_id
        now = time.time()

        # Cache for 10 minutes
        if now < self._opendota_cache_ttl or steam_id in self._opendota_rank_cache:
            cached = self._opendota_rank_cache.get(steam_id)
            if cached:
                parts = [base_message]
                if cached.get("mmr"):
                    parts.append(f"MMR: {cached['mmr']}")
                if cached.get("rank_tier"):
                    parts.append(f"Ранг: {cached['rank_tier']}")
                return "\n".join(parts)

        # Only refresh cache if expired
        if now >= self._opendota_cache_ttl:
            self._opendota_rank_cache.clear()
            self._opendota_cache_ttl = now + 600

        try:
            import aiohttp
            from .match_tracker import STEAM_ID_OFFSET

            account_id = int(steam_id) - STEAM_ID_OFFSET
            url = f"https://api.opendota.com/api/players/{account_id}"

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()

            mmr = data.get("mmr_estimate", {}).get("estimate")
            rank_tier = data.get("rank_tier")

            if mmr or rank_tier:
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

                self._opendota_rank_cache[steam_id] = rank_info
                parts = [base_message]
                if rank_info.get("mmr"):
                    parts.append(f"MMR: {rank_info['mmr']}")
                if rank_info.get("rank_tier"):
                    parts.append(f"Ранг: {rank_info['rank_tier']}")
                return "\n".join(parts)

            self._opendota_rank_cache[steam_id] = {}
        except Exception as e:
            logger.error("OpenDota enrich failed for %s: %s", target.name, e)

        return None

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

            if match is None:
                continue

            if match.match_id == state.last_match_id:
                continue

            state.last_match_id = match.match_id
            state.last_match_time = now
            await db.save_target_state(self._db, state)

            time_ago = format_time_ago(match.start_time)
            message = (
                f"🕵️ {target.name}: скрытая активность! "
                f"Катал Dota 2 {time_ago} (матч {match.match_id})"
            )
            # Check per-target invisible alert setting
            target_settings = await db.get_target_settings(self._db, target.id)
            if target_settings.get("alert_invisible", True):
                try:
                    await self._send_alert(target.telegram_id, message)
                except Exception as e:
                    logger.error("Failed to send hidden_activity alert: %s", e)
