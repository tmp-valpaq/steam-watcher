import asyncio
import json
import os
import logging

from aiogram import Bot
from steam import fetch_player, fetch_recent_games, state_name, format_last_seen, detect_invisible
from db import get_all_active_targets, update_state

log = logging.getLogger("watcher")

# In-memory timers: target_id -> last_check timestamp
timers: dict[int, float] = {}


async def run_watcher(bot: Bot):
    """Main loop — polls all active targets and sends alerts."""
    while True:
        try:
            targets = await get_all_active_targets()

            for t in targets:
                now = asyncio.get_event_loop().time()
                last = timers.get(t["id"], 0)

                if now - last < t["interval"]:
                    continue

                timers[t["id"]] = now

                try:
                    player = await fetch_player(t["api_key"], t["steam_id"])
                    if not player:
                        continue

                    games = await fetch_recent_games(t["api_key"], t["steam_id"])
                    current_pt = {str(g["appid"]): g["playtime_forever"] for g in games}
                    prev_pt = json.loads(t["playtime_map"] or "{}")

                    new_state = player.get("personastate", 0)
                    new_game = player.get("gameextrainfo")
                    old_state = t["persona_state"] or 0
                    old_game = t["current_game"]

                    alerts = []
                    invisible = detect_invisible(new_state, current_pt, prev_pt)

                    # Status changed
                    if new_state != old_state:
                        if invisible:
                            alerts.append(f"👻 **{t['name']}** — INVISIBLE (playtime ticking!)")
                        else:
                            alerts.append(f"**{t['name']}** → {state_name(new_state)}")

                    # Game started / stopped
                    if new_game and new_game != old_game:
                        alerts.append(f"🎮 **{t['name']}** started playing **{new_game}**")
                    elif old_game and not new_game and not invisible:
                        alerts.append(f"⏹ **{t['name']}** stopped playing **{old_game}**")

                    # Playtime delta (for invisible detection)
                    if invisible and not alerts:
                        for appid, pt in current_pt.items():
                            prev = prev_pt.get(appid, 0)
                            if pt > prev:
                                game_name = next(
                                    (g["name"] for g in games if str(g["appid"]) == appid),
                                    f"appid:{appid}",
                                )
                                alerts.append(
                                    f"👻 **{t['name']}** playtime grew: {game_name} +{pt - prev}min"
                                )

                    # Send alerts
                    for alert in alerts:
                        try:
                            await bot.send_message(t["tg_id"], alert)
                        except Exception as e:
                            log.error(f"Send error: {e}")

                    # Update state
                    await update_state(t["id"], new_state, new_game, current_pt)

                except Exception as e:
                    log.error(f"Error checking {t['name']}: {e}")

            await asyncio.sleep(5)

        except Exception as e:
            log.error(f"Watcher loop error: {e}")
            await asyncio.sleep(10)
