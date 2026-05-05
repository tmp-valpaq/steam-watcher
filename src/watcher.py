import asyncio
import logging
import time
from typing import List, Optional, Callable, Awaitable

import aiosqlite

from . import db
from .models import Target, TargetState, Alert
from .steam import SteamClient, state_name, format_last_seen, detect_invisible

logger = logging.getLogger(__name__)


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
        # First check — just note we've seen them
        status = state_name(current_state.persona_state)
        if current_state.game_name:
            alerts.append(Alert(
                target=target,
                message=f"{name}: First check. Currently {status}, playing {current_state.game_name}.",
            ))
        else:
            alerts.append(Alert(
                target=target,
                message=f"{name}: First check. Status is {status}.",
            ))
        return alerts

    # Detect state changes
    prev_status = state_name(previous_state.persona_state)
    curr_status = state_name(current_state.persona_state)

    # Invisible detection
    if is_invisible:
        alerts.append(Alert(
            target=target,
            message=(
                f"{name}: INVISIBLE DETECTED. Shows as Offline but playtime increased "
                f"from {previous_state.playtime_forever} to {current_state.playtime_forever} minutes."
            ),
        ))

    # Status change
    if previous_state.persona_state != current_state.persona_state:
        if current_state.persona_state > 0:
            alerts.append(Alert(
                target=target,
                message=f"{name}: Came online. Now {curr_status}.",
            ))
        else:
            last_seen = format_last_seen(current_state.last_logoff)
            alerts.append(Alert(
                target=target,
                message=f"{name}: Went offline. Was {prev_status}. Last seen: {last_seen}.",
            ))

    # Game change
    current_game = current_state.game_name
    previous_game = previous_state.game_name

    if current_game and current_game != previous_game:
        # They're now playing a different game (or started playing)
        if previous_game:
            alerts.append(Alert(
                target=target,
                message=f"{name}: Stopped playing {previous_game}.",
            ))
        alerts.append(Alert(
            target=target,
            message=f"{name}: Started playing {current_game}.",
        ))
    elif previous_game and not current_game:
        alerts.append(Alert(
            target=target,
            message=f"{name}: Stopped playing {previous_game}.",
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
                f"{name}: Changed display name from "
                f"{previous_state.persona_name} to {current_state.persona_name}."
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
    ):
        self._db = db_conn
        self._steam = steam_client
        self._send_alert = send_alert
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        """Start the background watcher task."""
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Watcher started")

    async def stop(self) -> None:
        """Stop the background watcher task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Watcher stopped")

    async def _run_loop(self) -> None:
        """Main polling loop."""
        while self._running:
            try:
                await self._poll_all()
            except Exception as e:
                logger.error("Error in watcher loop: %s", e)
            await asyncio.sleep(10)  # Check for work every 10 seconds

    async def _poll_all(self) -> None:
        """Poll all active targets that are due for a check."""
        targets = await db.get_active_targets(self._db)
        now = int(time.time())

        # Group targets by user (API key)
        user_targets: dict = {}  # telegram_id -> [Target]
        for target in targets:
            user_targets.setdefault(target.telegram_id, []).append(target)

        for telegram_id, user_target_list in user_targets.items():
            user = await db.get_user(self._db, telegram_id)
            if user is None:
                continue

            for target in user_target_list:
                state = await db.get_target_state(self._db, target.id)
                if state and (now - state.last_checked) < target.interval_seconds:
                    continue  # Not due yet

                await self._check_target(user.steam_api_key, target)

    async def _check_target(self, api_key: str, target: Target) -> None:
        """Check a single target profile and generate alerts."""
        now = int(time.time())

        profile = await self._steam.get_player_summaries(api_key, target.steam_id)
        if profile is None:
            logger.warning("Could not fetch profile for target %s", target.name)
            return

        recent = await self._steam.get_recently_played(api_key, target.steam_id)

        # Calculate total playtime from recently played games
        total_playtime = sum(g["playtime_forever"] for g in recent.games) if recent.games else 0

        # Get previous state
        previous_state = await db.get_target_state(self._db, target.id)

        # Build current state
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

        # Check for invisible
        prev_playtime = previous_state.playtime_forever if previous_state else None
        is_invisible = detect_invisible(
            profile.persona_state,
            total_playtime,
            prev_playtime,
        )

        # Generate and send alerts
        alerts = generate_alerts(target, previous_state, current_state, is_invisible)
        for alert in alerts:
            try:
                await self._send_alert(target.telegram_id, alert.message)
            except Exception as e:
                logger.error("Failed to send alert: %s", e)

        # Save current state
        await db.save_target_state(self._db, current_state)
