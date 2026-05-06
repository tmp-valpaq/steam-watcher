import asyncio
import logging
import time
from typing import List, Optional, Callable, Awaitable, Dict

import aiosqlite

from . import db
from .match_tracker import MatchTracker
from .models import Target, TargetState, Alert
from .steam import SteamClient, state_name, format_last_seen, detect_invisible

logger = logging.getLogger(__name__)

MATCH_POLL_INTERVAL = 300


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


def generate_alerts(
    target: Target,
    previous_state: Optional[TargetState],
    current_state: TargetState,
    is_invisible: bool,
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
        alerts.append(Alert(
            target=target,
            message=(
                f"👻 {name}: НЕВИДИМКА! Статус offline, "
                f"но наиграно {current_state.playtime_forever} мин "
                f"(было {previous_state.playtime_forever})"
            ),
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
            alerts.append(Alert(
                target=target,
                message=f"{name} перестал играть в {previous_game}",
            ))
        alerts.append(Alert(
            target=target,
            message=f"🎮 {name} начал играть в {current_game}",
        ))
    elif previous_game and not current_game:
        alerts.append(Alert(
            target=target,
            message=f"⏹ {name} перестал играть в {previous_game}",
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

        previous_state = await db.get_target_state(self._db, target.id)

        current_state = TargetState(
            target_id=target.id,
            persona_state=profile.persona_state,
            persona_name=profile.persona_name,
            game_id=profile.game_id,
            game_name=profile.game_name,
            playtime_forever=total_playtime,
            last_logoff=profile.last_logoff,
            last_checked=now,
        )

        prev_playtime = previous_state.playtime_forever if previous_state else None
        is_invisible = detect_invisible(
            profile.persona_state,
            total_playtime,
            prev_playtime,
        )

        alerts = generate_alerts(target, previous_state, current_state, is_invisible)
        for alert in alerts:
            try:
                await self._send_alert(target.telegram_id, alert.message)
            except Exception as e:
                logger.error("Failed to send alert: %s", e)

        await db.save_target_state(self._db, current_state)

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
            try:
                await self._send_alert(target.telegram_id, message)
            except Exception as e:
                logger.error("Failed to send hidden_activity alert: %s", e)
