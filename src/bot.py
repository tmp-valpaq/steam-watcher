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
        "Мониторит Steam-профили: онлайн, игры, невидимки.\n"
        "\n"
        "Команды:\n"
        "/setkey API_KEY - добавить Steam API ключ\n"
        "/add STEAMID64 ИМЯ - добавить таргет\n"
        "/remove STEAMID64 - удалить таргет\n"
        "/list - список таргетов\n"
        "/pause STEAMID64 - пауза мониторинга\n"
        "/resume STEAMID64 - возобновить мониторинг\n"
        "/check STEAMID64 - мгновенная проверка профиля\n"
        "\n"
        "Как получить API ключ:\n"
        "https://steamcommunity.com/dev/apikey\n"
        "(бесплатно, в поле Domain пиши localhost)\n"
        "\n"
        "Как узнать SteamID64:\n"
        "https://steamid.io\n"
        "(вставь ссылку на профиль, получишь 17-значный ID)\n"
        "\n"
        "Важно: профиль должен быть публичным (Public).\n"
        "Невидимки детектятся по росту наигранных часов."
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
            await message.answer("Использование: /setkey API_КЛЮЧ")
            return

        api_key = parts[1].strip()
        valid = await steam_client.validate_key(api_key)
        if not valid:
            await message.answer(
                "Неверный API ключ.\n"
                "Получить ключ: https://steamcommunity.com/dev/apikey"
            )
            return

        user = await db.get_user(db_conn, message.from_user.id)
        if user:
            user.steam_api_key = api_key
        else:
            from .models import User
            user = User(telegram_id=message.from_user.id, steam_api_key=api_key)
        await db.save_user(db_conn, user)
        await message.answer("API ключ сохранён.")

    @router.message(Command("add"))
    async def cmd_add(message: Message) -> None:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            await message.answer("Использование: /add STEAMID64 ИМЯ\nУзнать SteamID64: https://steamid.io")
            return

        steam_id = parts[1].strip()
        name = parts[2].strip()

        user = await db.get_user(db_conn, message.from_user.id)
        if user is None:
            await message.answer("Сначала добавь API ключ: /setkey API_КЛЮЧ")
            return

        profile = await steam_client.get_player_summaries(user.steam_api_key, steam_id)
        if profile is None:
            await message.answer(
                "Профиль не найден.\n"
                "Проверь SteamID64 и убедись что профиль публичный."
            )
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
                f"Добавлен: {name} ({profile.persona_name})\n"
                f"Статус: {state_name(profile.persona_state)}\n"
                f"Мониторинг каждые 30 сек."
            )
        except Exception as e:
            logger.error("Failed to add target: %s", e)
            await message.answer("Не удалось добавить. Возможно таргет уже существует.")

    @router.message(Command("remove"))
    async def cmd_remove(message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Использование: /remove STEAMID64")
            return

        steam_id = parts[1].strip()
        removed = await db.remove_target(db_conn, message.from_user.id, steam_id)
        if removed:
            await message.answer("Таргет удалён.")
        else:
            await message.answer("Таргет не найден.")

    @router.message(Command("list"))
    async def cmd_list(message: Message) -> None:
        targets = await db.get_targets(db_conn, message.from_user.id)
        if not targets:
            await message.answer("Нет таргетов. Добавь: /add STEAMID64 ИМЯ")
            return

        lines = ["Твои таргеты:"]
        for t in targets:
            state = await db.get_target_state(db_conn, t.id)
            status = "Пауза"
            if t.active:
                if state:
                    status = state_name(state.persona_state)
                    if state.game_name:
                        status += f", играет: {state.game_name}"
                else:
                    status = "Ещё не проверен"
            marker = "активен" if t.active else "пауза"
            lines.append(
                f"  {t.name} ({t.steam_id}): {status} [{marker}]"
            )

        await message.answer("\n".join(lines))

    @router.message(Command("pause"))
    async def cmd_pause(message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Использование: /pause STEAMID64")
            return

        steam_id = parts[1].strip()
        updated = await db.set_target_active(db_conn, message.from_user.id, steam_id, False)
        if updated:
            await message.answer("Мониторинг на паузе.")
        else:
            await message.answer("Таргет не найден.")

    @router.message(Command("resume"))
    async def cmd_resume(message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Использование: /resume STEAMID64")
            return

        steam_id = parts[1].strip()
        updated = await db.set_target_active(db_conn, message.from_user.id, steam_id, True)
        if updated:
            await message.answer("Мониторинг возобновлён.")
        else:
            await message.answer("Таргет не найден.")

    @router.message(Command("check"))
    async def cmd_check(message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Использование: /check STEAMID64")
            return

        steam_id = parts[1].strip()

        user = await db.get_user(db_conn, message.from_user.id)
        if user is None:
            await message.answer("Сначала добавь API ключ: /setkey API_КЛЮЧ")
            return

        profile = await steam_client.get_player_summaries(user.steam_api_key, steam_id)
        if profile is None:
            await message.answer(
                "Профиль не найден.\n"
                "Проверь SteamID64 или профиль может быть приватным."
            )
            return

        status = state_name(profile.persona_state)
        lines = [
            f"Профиль: {profile.persona_name}",
            f"SteamID: {profile.steam_id}",
            f"Статус: {status}",
        ]
        if profile.game_name:
            lines.append(f"Играет: {profile.game_name}")
        if profile.last_logoff:
            lines.append(f"Последний онлайн: {format_last_seen(profile.last_logoff)}")

        await message.answer("\n".join(lines))

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher
