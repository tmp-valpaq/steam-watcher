import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from db import init_db, register_user, get_user, add_target, remove_target, list_targets, toggle_target
from steam import fetch_player, state_name, format_last_seen
from watcher import run_watcher

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

TOKEN = os.environ.get("STEAM_BOT_TOKEN")
if not TOKEN:
    print("Set STEAM_BOT_TOKEN env var")
    exit(1)

router = Router()


def ensure_user(func):
    """Decorator — checks user is registered (has API key)."""
    async def wrapper(message: types.Message, *args, **kwargs):
        user = await get_user(message.from_user.id)
        if not user:
            await message.answer(
                "❌ Сначала добавь Steam API ключ:\n"
                "/setkey <твой_api_ключ>\n\n"
                "Получить ключ: https://steamcommunity.com/dev/apikey"
            )
            return
        return await func(message, user, *args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper


# === Commands ===

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🕵️ **Steam Watcher Bot**\n\n"
        "Мониторит Steam-профили: онлайн, игры, невидимки.\n\n"
        "**Настройка:**\n"
        "1. /setkey <api_key> — добавить Steam API ключ\n"
        "2. /add <steamid64> <имя> — добавить таргет\n"
        "3. /list — список таргетов\n"
        "4. /remove <steamid64> — убрать таргет\n"
        "5. /pause <steamid64> — пауза\n"
        "6. /resume <steamid64> — возобновить\n"
        "7. /check <steamid64> — ручная проверка\n\n"
        "API ключ: https://steamcommunity.com/dev/apikey"
    )


@router.message(Command("setkey"))
async def cmd_setkey(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: /setkey <steam_api_key>")
        return
    api_key = parts[1].strip()
    await register_user(message.from_user.id, api_key)
    await message.answer("✅ API ключ сохранён.")


@router.message(Command("add"))
@ensure_user
async def cmd_add(message: types.Message, user: dict):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Usage: /add <steamid64> <имя>")
        return
    steam_id = parts[1].strip()
    name = parts[2].strip()

    # Verify we can fetch this profile
    player = await fetch_player(user["steam_api_key"], steam_id)
    if not player:
        await message.answer("❌ Профиль не найден. Проверь SteamID64 и убедись что профиль публичный.")
        return

    target_id = await add_target(message.from_user.id, steam_id, name)
    if target_id is None:
        await message.answer("❌ Таргет уже добавлен.")
        return

    persona = player.get("personaname", "?")
    await message.answer(
        f"✅ Добавлен: **{name}** ({persona})\n"
        f"SteamID: `{steam_id}`"
    )


@router.message(Command("remove"))
@ensure_user
async def cmd_remove(message: types.Message, user: dict):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: /remove <steamid64>")
        return
    await remove_target(message.from_user.id, parts[1].strip())
    await message.answer("✅ Удалён.")


@router.message(Command("list"))
@ensure_user
async def cmd_list(message: types.Message, user: dict):
    targets = await list_targets(message.from_user.id)
    if not targets:
        await message.answer("📭 Нет таргетов. /add <steamid64> <имя>")
        return

    lines = []
    for t in targets:
        status = "▶" if t["is_active"] else "⏸"
        state = state_name(t["persona_state"] or 0) if t["persona_state"] is not None else "—"
        game = f", игра: {t['current_game']}" if t.get("current_game") else ""
        lines.append(f"{status} **{t['name']}** — {state}{game}\n  `{t['steam_id']}` | интервал: {t['interval']}с")

    await message.answer("\n".join(lines))


@router.message(Command("pause"))
@ensure_user
async def cmd_pause(message: types.Message, user: dict):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: /pause <steamid64>")
        return
    await toggle_target(message.from_user.id, parts[1].strip(), False)
    await message.answer("⏸ Пауза.")


@router.message(Command("resume"))
@ensure_user
async def cmd_resume(message: types.Message, user: dict):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: /resume <steamid64>")
        return
    await toggle_target(message.from_user.id, parts[1].strip(), True)
    await message.answer("▶ Возобновлён.")


@router.message(Command("check"))
@ensure_user
async def cmd_check(message: types.Message, user: dict):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: /check <steamid64>")
        return
    steam_id = parts[1].strip()

    player = await fetch_player(user["steam_api_key"], steam_id)
    if not player:
        await message.answer("❌ Профиль не найден / приватный.")
        return

    games = await __import__("steam").fetch_recent_games(user["steam_api_key"], steam_id)
    state = state_name(player.get("personastate", 0))
    name = player.get("personaname", "?")
    game = player.get("gameextrainfo", "—")
    last = format_last_seen(player.get("lastlogoff"))

    lines = [
        f"👤 **{name}**",
        f"Статус: {state}",
        f"Игра: {game}",
        f"Последний онлайн: {last}",
    ]

    if games:
        lines.append("\n📊 Недавние:")
        for g in games[:5]:
            lines.append(f"  {g['name']}: {g['playtime_forever']}ч (2н: {g.get('playtime_2weeks', 0)}ч)")

    await message.answer("\n".join(lines))


# === Main ===

async def main():
    await init_db()

    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    dp = Dispatcher()
    dp.include_router(router)

    # Start watcher in background
    asyncio.create_task(run_watcher(bot))

    log.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
