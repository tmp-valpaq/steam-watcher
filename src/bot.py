import logging
from typing import Optional

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

import aiosqlite

from . import db
from .config import DEFAULT_POLL_INTERVAL
from .models import Target, TargetState
from .steam import SteamClient, state_name, format_last_seen

logger = logging.getLogger(__name__)

router = Router()


def _help_text() -> str:
    return (
        "Steam Watcher Bot\n"
        "\n"
        "Commands:\n"
        "/setkey API_KEY - Set your Steam Web API key\n"
        "/add STEAMID64 NAME - Add a target to monitor\n"
        "/remove STEAMID64 - Remove a target\n"
        "/list - Show all your targets with current status\n"
        "/pause STEAMID64 - Pause monitoring for a target\n"
        "/resume STEAMID64 - Resume monitoring for a target\n"
        "/check STEAMID64 - Instant check of a target's profile\n"
    )


def setup_bot(bot_instance: Bot, db_conn: aiosqlite.Connection, steam_client: SteamClient) -> Dispatcher:
    """Wire up all handlers and return a configured Dispatcher."""

    @router.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        await message.answer(_help_text())

    @router.message(Command("setkey"))
    async def cmd_setkey(message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Usage: /setkey YOUR_STEAM_API_KEY")
            return

        api_key = parts[1].strip()
        # Validate key
        valid = await steam_client.validate_key(api_key)
        if not valid:
            await message.answer("Invalid API key. Please check and try again.")
            return

        user = await db.get_user(db_conn, message.from_user.id)
        if user:
            user.steam_api_key = api_key
        else:
            from .models import User
            user = User(telegram_id=message.from_user.id, steam_api_key=api_key)
        await db.save_user(db_conn, user)
        await message.answer("API key saved successfully.")

    @router.message(Command("add"))
    async def cmd_add(message: Message) -> None:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            await message.answer("Usage: /add STEAMID64 NAME")
            return

        steam_id = parts[1].strip()
        name = parts[2].strip()

        user = await db.get_user(db_conn, message.from_user.id)
        if user is None:
            await message.answer("Please set your API key first with /setkey")
            return

        # Verify the profile exists
        profile = await steam_client.get_player_summaries(user.steam_api_key, steam_id)
        if profile is None:
            await message.answer("Could not find that Steam profile. Check the SteamID64.")
            return

        target = Target(
            id=0,
            telegram_id=message.from_user.id,
            steam_id=steam_id,
            name=name,
            interval_seconds=DEFAULT_POLL_INTERVAL,
            active=True,
        )
        try:
            target = await db.add_target(db_conn, target)
            await message.answer(
                f"Added target: {name} ({profile.persona_name} on Steam). "
                f"Currently {state_name(profile.persona_state)}."
            )
        except Exception as e:
            logger.error("Failed to add target: %s", e)
            await message.answer("Failed to add target. It may already exist.")

    @router.message(Command("remove"))
    async def cmd_remove(message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Usage: /remove STEAMID64")
            return

        steam_id = parts[1].strip()
        removed = await db.remove_target(db_conn, message.from_user.id, steam_id)
        if removed:
            await message.answer("Target removed.")
        else:
            await message.answer("Target not found.")

    @router.message(Command("list"))
    async def cmd_list(message: Message) -> None:
        targets = await db.get_targets(db_conn, message.from_user.id)
        if not targets:
            await message.answer("No targets. Use /add to add one.")
            return

        lines = ["Your targets:"]
        for t in targets:
            state = await db.get_target_state(db_conn, t.id)
            status = "Paused"
            if t.active:
                if state:
                    status = state_name(state.persona_state)
                    if state.game_name:
                        status += f", playing {state.game_name}"
                else:
                    status = "Not checked yet"
            lines.append(
                f"  {t.name} ({t.steam_id}): {status} "
                f"[{'active' if t.active else 'paused'}]"
            )

        await message.answer("\n".join(lines))

    @router.message(Command("pause"))
    async def cmd_pause(message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Usage: /pause STEAMID64")
            return

        steam_id = parts[1].strip()
        updated = await db.set_target_active(db_conn, message.from_user.id, steam_id, False)
        if updated:
            await message.answer("Monitoring paused for that target.")
        else:
            await message.answer("Target not found.")

    @router.message(Command("resume"))
    async def cmd_resume(message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Usage: /resume STEAMID64")
            return

        steam_id = parts[1].strip()
        updated = await db.set_target_active(db_conn, message.from_user.id, steam_id, True)
        if updated:
            await message.answer("Monitoring resumed for that target.")
        else:
            await message.answer("Target not found.")

    @router.message(Command("check"))
    async def cmd_check(message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Usage: /check STEAMID64")
            return

        steam_id = parts[1].strip()

        user = await db.get_user(db_conn, message.from_user.id)
        if user is None:
            await message.answer("Please set your API key first with /setkey")
            return

        profile = await steam_client.get_player_summaries(user.steam_api_key, steam_id)
        if profile is None:
            await message.answer("Could not fetch that profile.")
            return

        status = state_name(profile.persona_state)
        lines = [
            f"Profile: {profile.persona_name}",
            f"SteamID: {profile.steam_id}",
            f"Status: {status}",
        ]
        if profile.game_name:
            lines.append(f"Playing: {profile.game_name}")
        if profile.last_logoff:
            lines.append(f"Last seen: {format_last_seen(profile.last_logoff)}")

        await message.answer("\n".join(lines))

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher
