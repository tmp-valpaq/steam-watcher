import logging
import re
from typing import Optional

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

import aiosqlite

from . import db
from .config import DEFAULT_STEAM_API_KEY, DEFAULT_POLL_INTERVAL
from .models import Target, TargetState
from .steam import SteamClient, state_name, format_last_seen

logger = logging.getLogger(__name__)

router = Router()

# Regex to extract SteamID64 or vanity name from a Steam profile URL
STEAM_URL_RE = re.compile(
    r"(?:https?://)?steamcommunity\.com/(?:profiles/(\d{17})|id/([a-zA-Z0-9_-]+))"
)


def _get_api_key(user_api_key: Optional[str]) -> Optional[str]:
    """Return user's API key or the server default."""
    return user_api_key or DEFAULT_STEAM_API_KEY or None


def _help_text() -> str:
    return (
        "Steam Watcher Bot\n"
        "Мониторит Steam-профили и присылает уведомления.\n"
        "\n"
        "Что отслеживает:\n"
        "- Любые игры (все что в Steam)\n"
        "- Зашёл онлайн / вышел оффлайн\n"
        "- Начал / перестал играть\n"
        "- Сменил ник\n"
        "- Невидимки (статус offline, но наиграно растёт)\n"
        "\n"
        "Как часто: каждые 30 сек\n"
        "\n"
        "Команды:\n"
        "/add ссылка — добавить таргет (ник подтянется сам)\n"
        "/remove ссылка — удалить таргет\n"
        "/list — список таргетов\n"
        "/pause ссылка — пауза\n"
        "/resume ссылка — возобновить\n"
        "/check ссылка — мгновенная проверка\n"
        "/rename ссылка новый_ник — переименовать таргет\n"
        "\n"
        "Вместо ссылки можно писать SteamID64 (17 цифр).\n"
        "Пример: /add https://steamcommunity.com/id/gabelogannewell\n"
        "\n"
        "API ключ (опционально, если бот не работает):\n"
        "/setkey API_KEY\n"
        "https://steamcommunity.com/dev/apikey\n"
        "\n"
        "Профиль таргета должен быть Public."
    )


def _parse_steam_input(text: str) -> tuple:
    """
    Parse user input to extract steam_id.
    Returns (steam_id, vanity) where one of them is None.
    Accepts:
      - SteamID64 (17 digits)
      - Full Steam URL (profiles/ID or id/vanity)
      - Just the vanity part after /id/
    """
    text = text.strip()

    # Direct SteamID64
    if re.match(r"^\d{17}$", text):
        return text, None

    # Full URL
    m = STEAM_URL_RE.match(text)
    if m:
        steam_id = m.group(1)  # from /profiles/7656...
        vanity = m.group(2)    # from /id/vanityname
        return steam_id, vanity

    # Just a vanity name (no URL)
    if re.match(r"^[a-zA-Z0-9_-]{2,32}$", text):
        return None, text

    return None, None


def setup_bot(bot_instance: Bot, db_conn: aiosqlite.Connection, steam_client: SteamClient) -> Dispatcher:

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
                "Получить: https://steamcommunity.com/dev/apikey"
            )
            return

        from .models import User
        user = User(telegram_id=message.from_user.id, steam_api_key=api_key)
        await db.save_user(db_conn, user)
        await message.answer("API ключ сохранён. Твои запросы теперь через него.")

    @router.message(Command("add"))
    async def cmd_add(message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer(
                "Использование: /add ссылка_на_профиль\n"
                "Пример: /add https://steamcommunity.com/id/gabelogannewell"
            )
            return

        input_text = parts[1].strip()
        steam_id, vanity = _parse_steam_input(input_text)

        if not steam_id and not vanity:
            await message.answer(
                "Не понял. Пришли ссылку на профиль или SteamID64.\n"
                "Пример: /add https://steamcommunity.com/id/gabelogannewell"
            )
            return

        # Get API key
        user = await db.get_user(db_conn, message.from_user.id)
        api_key = _get_api_key(user.steam_api_key if user else None)
        if not api_key:
            await message.answer(
                "Нужен API ключ. Добавь: /setkey API_КЛЮЧ\n"
                "Получить: https://steamcommunity.com/dev/apikey"
            )
            return

        # Resolve vanity → SteamID64 if needed
        if not steam_id and vanity:
            steam_id = await steam_client.resolve_vanity_url(api_key, vanity)
            if not steam_id:
                await message.answer("Не удалось найти профиль по этой ссылке.")
                return

        # Fetch profile to get nickname
        profile = await steam_client.get_player_summaries(api_key, steam_id)
        if profile is None:
            await message.answer(
                "Профиль не найден или приватный.\n"
                "Проверь ссылку и убедись что профиль Public."
            )
            return

        name = profile.persona_name

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
                f"Добавлен: {name}\n"
                f"SteamID: {steam_id}\n"
                f"Статус: {state_name(profile.persona_state)}\n"
                f"Мониторинг каждые 30 сек."
            )
        except Exception as e:
            logger.error("Failed to add target: %s", e)
            await message.answer("Не удалось добавить. Возможно уже отслеживаешь.")

    @router.message(Command("remove"))
    async def cmd_remove(message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Использование: /remove ссылка_или_SteamID64")
            return

        steam_id = await _resolve_target_id(db_conn, steam_client, message, parts[1])
        if not steam_id:
            return

        removed = await db.remove_target(db_conn, message.from_user.id, steam_id)
        if removed:
            await message.answer("Удалён.")
        else:
            await message.answer("Не найден в твоём списке.")

    @router.message(Command("list"))
    async def cmd_list(message: Message) -> None:
        targets = await db.get_targets(db_conn, message.from_user.id)
        if not targets:
            await message.answer("Пока никого не отслеживаешь.\nДобавь: /add ссылка_на_профиль")
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
            lines.append(f"  {t.name} [{marker}]: {status}")

        await message.answer("\n".join(lines))

    @router.message(Command("pause"))
    async def cmd_pause(message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Использование: /pause ссылка_или_SteamID64")
            return

        steam_id = await _resolve_target_id(db_conn, steam_client, message, parts[1])
        if not steam_id:
            return

        updated = await db.set_target_active(db_conn, message.from_user.id, steam_id, False)
        if updated:
            await message.answer("Пауза.")
        else:
            await message.answer("Не найден.")

    @router.message(Command("resume"))
    async def cmd_resume(message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Использование: /resume ссылка_или_SteamID64")
            return

        steam_id = await _resolve_target_id(db_conn, steam_client, message, parts[1])
        if not steam_id:
            return

        updated = await db.set_target_active(db_conn, message.from_user.id, steam_id, True)
        if updated:
            await message.answer("Возобновлён.")
        else:
            await message.answer("Не найден.")

    @router.message(Command("check"))
    async def cmd_check(message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Использование: /check ссылка_или_SteamID64")
            return

        user = await db.get_user(db_conn, message.from_user.id)
        api_key = _get_api_key(user.steam_api_key if user else None)
        if not api_key:
            await message.answer("Нужен API ключ: /setkey API_КЛЮЧ")
            return

        steam_id, vanity = _parse_steam_input(parts[1])
        if not steam_id and vanity:
            steam_id = await steam_client.resolve_vanity_url(api_key, vanity)
        if not steam_id:
            await message.answer("Не понял ссылку.")
            return

        profile = await steam_client.get_player_summaries(api_key, steam_id)
        if profile is None:
            await message.answer("Профиль не найден или приватный.")
            return

        status = state_name(profile.persona_state)
        lines = [
            f"{profile.persona_name}",
            f"Статус: {status}",
        ]
        if profile.game_name:
            lines.append(f"Играет: {profile.game_name}")
        if profile.last_logoff:
            lines.append(f"Последний онлайн: {format_last_seen(profile.last_logoff)}")

        await message.answer("\n".join(lines))

    @router.message(Command("rename"))
    async def cmd_rename(message: Message) -> None:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            await message.answer("Использование: /rename ссылка_или_SteamID64 новый_ник")
            return

        steam_id = await _resolve_target_id(db_conn, steam_client, message, parts[1])
        if not steam_id:
            return

        new_name = parts[2].strip()
        updated = await db.rename_target(db_conn, message.from_user.id, steam_id, new_name)
        if updated:
            await message.answer(f"Переименован в: {new_name}")
        else:
            await message.answer("Не найден.")

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher


async def _resolve_target_id(
    db_conn: aiosqlite.Connection,
    steam_client: SteamClient,
    message: Message,
    input_text: str,
) -> Optional[str]:
    """Resolve user input (URL/SteamID64/vanity) to a SteamID64.
    First checks existing targets by name, then tries Steam API."""
    text = input_text.strip()

    # Check if it's already a SteamID64
    steam_id, vanity = _parse_steam_input(text)
    if steam_id:
        return steam_id

    # Check if text matches a target name
    targets = await db.get_targets(db_conn, message.from_user.id)
    for t in targets:
        if t.name.lower() == text.lower():
            return t.steam_id

    # Try to resolve vanity via Steam API
    user = await db.get_user(db_conn, message.from_user.id)
    api_key = _get_api_key(user.steam_api_key if user else None)
    if api_key and vanity:
        resolved = await steam_client.resolve_vanity_url(api_key, vanity)
        if resolved:
            return resolved

    await message.answer(
        "Не понял. Пришли ссылку, SteamID64 или имя таргета."
    )
    return None
