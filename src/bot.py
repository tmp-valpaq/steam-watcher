import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, BaseMiddleware, Dispatcher, Router, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, \
    ReplyKeyboardRemove, ForceReply, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

import aiosqlite

from . import db
from .config import (
    DEFAULT_STEAM_API_KEY,
    DEFAULT_POLL_INTERVAL,
    ALLOWED_TELEGRAM_IDS,
    CHECK_MIN_INTERVAL_SEC,
)
from .formatting import format_duration_seconds
from .models import Target, TargetState, UserSettings, SteamProfileBlacklistEntry
from .steam import SteamClient, state_name, format_last_seen
from .cs2_activity import CS2ActivityResolver, format_cs2_activity_lines

logger = logging.getLogger(__name__)

router = Router()

# Per-user timestamp of the last expensive Steam-hitting action, for anti-flood.
_last_check_at: Dict[int, float] = {}


class AllowlistMiddleware(BaseMiddleware):
    """Reject updates from users not on the allowlist.

    When ALLOWED_TELEGRAM_IDS is empty the middleware is a no-op (open to all),
    preserving the original behavior.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if not ALLOWED_TELEGRAM_IDS:
            return await handler(event, data)

        user = data.get("event_from_user")
        if user is not None and user.id in ALLOWED_TELEGRAM_IDS:
            return await handler(event, data)

        # Politely refuse without invoking the handler (no Steam access).
        refusal = "Извини, у тебя нет доступа к этому боту."
        if isinstance(event, CallbackQuery):
            await event.answer(refusal, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(refusal)
        return None


def _check_too_soon(telegram_id: int) -> bool:
    """Anti-flood for expensive Steam actions.

    Returns True if the user must wait (called too soon). On a permitted call it
    records the current time so the next call is throttled. Disabled when the
    configured interval is <= 0.
    """
    if CHECK_MIN_INTERVAL_SEC <= 0:
        return False
    now = time.monotonic()
    last = _last_check_at.get(telegram_id)
    if last is not None and (now - last) < CHECK_MIN_INTERVAL_SEC:
        return True
    _last_check_at[telegram_id] = now
    return False

# Regex to extract SteamID64 or vanity name from a Steam profile URL
STEAM_URL_RE = re.compile(
    r"(?:https?://)?steamcommunity\.com/(?:profiles/(\d{17})|id/([a-zA-Z0-9_-]+))"
)

# Per-user pending states for button-driven flows
_pending_add: Dict[int, bool] = {}
_pending_rename: Dict[int, int] = {}

INTERVAL_PRESETS = [60, 180, 300, 600, 900]
_INTERVAL_LABELS = {
    60: "1 мин",
    180: "3 мин",
    300: "5 мин",
    600: "10 мин",
    900: "15 мин",
}
RECENT_GAMES_LOOKUP_TIMEOUT_SEC = 3.0

# Common timezones offered in the settings picker. Stdlib zoneinfo names.
TIMEZONES = [
    "UTC",
    "Europe/Moscow",
    "Europe/Kiev",
    "Europe/Berlin",
    "Europe/London",
    "US/Eastern",
    "US/Pacific",
    "Asia/Tokyo",
]


def _get_api_key(user_api_key: Optional[str]) -> Optional[str]:
    """Return user's API key or the server default."""
    return user_api_key or DEFAULT_STEAM_API_KEY or None


def _build_main_menu() -> ReplyKeyboardMarkup:
    """Build the persistent bottom menu with 4 buttons."""
    builder = ReplyKeyboardBuilder()
    builder.button(text="➕ Добавить")
    builder.button(text="📋 Мой список")
    builder.button(text="🔍 Проверить")
    builder.button(text="⚙️ Настройки")
    builder.adjust(2, 2)
    return builder.as_markup(resize_keyboard=True)


def _help_text() -> str:
    return (
        "Steam Watcher — мониторинг Steam-профилей\n"
        "\n"
        "Что отслеживает: онлайн, игры, невидимку, смену ника и приватность.\n"
        "Проверка: каждые 30 сек по умолчанию, но для каждого профиля можно выбрать свой интервал.\n"
        "\n"
        "Как начать:\n"
        "1. Если нужно, сохрани свой ключ: /setkey КЛЮЧ\n"
        "2. Нажми ➕ Добавить и пришли ссылку на профиль Steam\n"
        "3. Открой 📋 Мой список и управляй профилем кнопками\n"
        "\n"
        "Главное:\n"
        "- добавить профиль — через ➕ Добавить\n"
        "- проверить сейчас — через 🔍 Проверить\n"
        "- сменить имя, интервал и blacklist — с карточки профиля\n"
        "\n"
        "Вместо ссылки можно писать SteamID64 (17 цифр).\n"
        "Профиль должен быть Public."
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


def _is_cancel_text(text: str) -> bool:
    """Return True when the user clearly cancels the pending add flow."""
    normalized = text.strip().lower()
    return normalized in {"отмена", "cancel", "/cancel"}


def _format_interval_label(seconds: int) -> str:
    return _INTERVAL_LABELS.get(seconds, f"{seconds} сек")


def _online_badge(persona_state: int) -> str:
    """Manual check UI: green for any non-offline state, red for offline.

    Product requirement here is binary presence signaling, not a separate color
    palette for Busy/Away/Snooze.
    """
    return "🔴" if persona_state == 0 else "🟢"


def render_target_card(target: Target, state: Optional[TargetState]) -> str:
    """Render the one-line 'name [marker]: status' target card.

    Centralizes the status/marker formatting copy-pasted across the list and
    callback handlers. Output is identical to the previous inline blocks:
    a paused target shows "Пауза"; an active target shows its persona-state
    name (plus ", играет: <game>" when in a game), or "Ещё не проверен" when
    no state has been recorded yet.
    """
    status = "Пауза"
    if target.active:
        if state:
            status = state_name(state.persona_state)
            if state.game_name:
                status += f", играет: {state.game_name}"
        else:
            status = "Ещё не проверен"
    marker = "активен" if target.active else "пауза"
    return f"{target.name} [{marker}]: {status}"


async def _build_check_lines(
    steam_client: SteamClient,
    api_key: str,
    profile,
    steam_id: str,
    target_state: Optional[TargetState] = None,
) -> list[str]:
    status = state_name(profile.persona_state)
    lines = [
        f"{profile.persona_name}",
        f"Статус: {status} {_online_badge(profile.persona_state)}",
    ]
    if profile.game_name:
        lines.append(f"Играет: {profile.game_name}")

    if profile.persona_state == 0 and profile.last_logoff:
        lines.append(f"Последний онлайн: {format_last_seen(profile.last_logoff)}")

    recent_game = None
    if not profile.game_name:
        if (
            profile.persona_state == 0
            and target_state
            and target_state.game_name
            and target_state.persona_state <= 0
        ):
            recent_game = target_state.game_name
        else:
            try:
                recent_games = await asyncio.wait_for(
                    steam_client.get_recently_played_games(api_key, steam_id),
                    timeout=RECENT_GAMES_LOOKUP_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                logger.debug("Recent games lookup timed out for %s", steam_id)
                recent_games = []
            except Exception:
                recent_games = []
            recent_game = recent_games[0] if recent_games else None
    if recent_game:
        lines.append(f"Недавняя игра: {recent_game}")

    return lines


def _build_target_keyboard(target: Target) -> types.InlineKeyboardMarkup:
    """Build inline keyboard for a single target."""
    builder = InlineKeyboardBuilder()
    if target.active:
        builder.button(text="⏸ Пауза", callback_data=f"pause:{target.id}")
    else:
        builder.button(text="▶️ Возобновить", callback_data=f"resume:{target.id}")
    builder.button(text="📜 История", callback_data=f"consistency:{target.id}")
    builder.button(text="⏱ Сессия", callback_data=f"session:{target.id}")
    builder.button(text="⏱ Интервал", callback_data=f"interval:{target.id}")
    builder.button(text="📝 Переименовать", callback_data=f"rename_prompt:{target.id}")
    builder.button(text="🔍 Проверить", callback_data=f"check:{target.id}")
    builder.button(text="⚙️ Уведомления", callback_data=f"tset:{target.id}")
    builder.button(text="🚫 В блэклист", callback_data=f"blacklist:{target.id}")
    builder.button(text="🗑 Удалить", callback_data=f"remove:{target.id}")
    builder.adjust(3, 3, 3)
    return builder.as_markup()


def _format_last_game_activity_time(ts: int, tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")

    dt_local = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz)
    now_local = datetime.now(tz=timezone.utc).astimezone(tz)
    if dt_local.date() == now_local.date():
        return dt_local.strftime("%H:%M")
    return dt_local.strftime("%Y-%m-%d %H:%M")


async def _append_last_game_observed_lines(
    lines: list[str],
    db_conn: aiosqlite.Connection,
    telegram_id: int,
    target: Target,
    profile,
    target_state: Optional[TargetState],
) -> None:
    """Add exact monitored last-game time when watcher actually observed it."""
    game_name = None
    detected_at = None

    if profile.game_name and target_state and target_state.game_start_time:
        game_name = profile.game_name
        detected_at = target_state.game_start_time
    else:
        activity = await db.get_activity_log(
            db_conn,
            target.id,
            limit=5,
            event_types=["game_start", "game_stop", "invisible"],
        )
        for entry in activity:
            if entry.get("game_name") and entry.get("detected_at"):
                game_name = entry["game_name"]
                detected_at = entry["detected_at"]
                break

    if not game_name or not detected_at:
        return

    lines[:] = [line for line in lines if not line.startswith("Недавняя игра:")]

    settings = await db.get_user_settings(db_conn, telegram_id)
    tz_name = settings.timezone if settings else "UTC"
    lines.append(f"Последний раз: {game_name}")
    lines.append(f"В {_format_last_game_activity_time(detected_at, tz_name)}")


def _build_interval_picker_keyboard(target_id: int, current_interval: int) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for seconds in INTERVAL_PRESETS:
        marker = "✅ " if seconds == current_interval else ""
        builder.button(
            text=f"{marker}{_format_interval_label(seconds)}",
            callback_data=f"interval_pick:{target_id}:{seconds}",
        )
    builder.button(text="🔙 Назад", callback_data=f"interval_back:{target_id}")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()


def _build_blacklist_confirm_keyboard(target_id: int) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🚫 Да, в блэклист", callback_data=f"blacklist_confirm:{target_id}")
    builder.button(text="❌ Отмена", callback_data=f"blacklist_cancel:{target_id}")
    builder.adjust(1, 1)
    return builder.as_markup()


def _build_remove_confirm_keyboard(target_id: int) -> types.InlineKeyboardMarkup:
    """Build confirmation keyboard for destructive profile removal."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, удалить", callback_data=f"remove_confirm:{target_id}")
    builder.button(text="❌ Отмена", callback_data=f"remove_cancel:{target_id}")
    builder.adjust(2)
    return builder.as_markup()


def _build_settings_keyboard(settings: UserSettings) -> types.InlineKeyboardMarkup:
    """Build inline keyboard for user settings."""
    builder = InlineKeyboardBuilder()
    summary_label = "✅ ВКЛ" if settings.daily_summary_enabled else "❌ ВЫКЛ"
    session_label = "✅ ВКЛ" if settings.session_updates_enabled else "❌ ВЫКЛ"
    privacy_label = "✅ ВКЛ" if settings.privacy_alerts_enabled else "❌ ВЫКЛ"
    # Encode desired NEW state (inverted) so handler can set it directly
    summary_val = "0" if settings.daily_summary_enabled else "1"
    session_val = "0" if settings.session_updates_enabled else "1"
    privacy_val = "0" if settings.privacy_alerts_enabled else "1"

    builder.button(text=f"📊 Сводка: {summary_label}", callback_data=f"set_summary:{summary_val}")
    builder.button(text=f"🕐 Время сводки: {settings.daily_summary_time}", callback_data="set_time:show")
    builder.button(text=f"🌐 Часовой пояс: {settings.timezone}", callback_data="set_tz:show")
    builder.button(text=f"⏱ Сессии: {session_label}", callback_data=f"set_session:{session_val}")
    builder.button(text=f"🔒 Приватность: {privacy_label}", callback_data=f"set_privacy:{privacy_val}")
    builder.adjust(2, 1, 2)
    return builder.as_markup()


def _settings_panel_text(settings: UserSettings) -> str:
    """Header for the settings panel showing the active timezone."""
    return f"⚙️ Настройки ({settings.timezone}):"


async def _show_session_info(
    callback: CallbackQuery,
    db_conn: aiosqlite.Connection,
    target: Optional[Target] = None,
) -> None:
    """Render current session info for a target and always finish after callback ack.

    Keep the Telegram callback acknowledgement separate from the message send so
    the inline button spinner never hangs while we gather state and format the
    response.
    """
    if target is None:
        target_id = int(callback.data.split(":")[1])
        target = await _get_target_by_id(db_conn, callback.from_user.id, target_id)
    if not target:
        return

    state = await db.get_target_state(db_conn, target.id)

    if not state:
        await callback.message.answer(
            f"⏱ {target.name}\n\nДанных пока нет. Подожди первую проверку."
        )
        return

    is_playing = state.persona_state > 0 and state.game_name
    name = state.persona_name or target.name

    if is_playing:
        lines = [f"⏱ {name}", "", f"Играет: {state.game_name}"]

        if state.game_start_time:
            now = int(datetime.now(tz=timezone.utc).timestamp())
            elapsed = now - state.game_start_time
            if elapsed > 0:
                lines.append(f"Длительность сессии: {format_duration_seconds(elapsed)}")

        if state.last_logoff:
            lines.append(f"Последний выход: {format_last_seen(state.last_logoff)}")

        await callback.message.answer("\n".join(lines))
        return

    lines = [f"⏱ {name}", ""]
    status = state_name(state.persona_state)
    lines.append(f"Статус: {status}")

    if state.last_logoff:
        lines.append(f"Последний онлайн: {format_last_seen(state.last_logoff)}")

    if state.game_name:
        lines.append(f"Последняя игра: {state.game_name}")

    await callback.message.answer("\n".join(lines))


def _build_time_picker_keyboard() -> types.InlineKeyboardMarkup:
    """Build time picker keyboard with preset times."""
    builder = InlineKeyboardBuilder()
    times = ["18:00", "19:00", "20:00", "21:00", "22:00", "23:00"]
    for t in times:
        builder.button(text=t, callback_data=f"pick_time:{t}")
    builder.button(text="🔙 Назад", callback_data="settings:back")
    builder.adjust(3, 3)
    return builder.as_markup()


def _build_tz_picker_keyboard(current_tz: str) -> types.InlineKeyboardMarkup:
    """Build timezone picker keyboard with preset zones."""
    builder = InlineKeyboardBuilder()
    for tz in TIMEZONES:
        marker = "✅ " if tz == current_tz else ""
        builder.button(text=f"{marker}{tz}", callback_data=f"pick_tz:{tz}")
    builder.button(text="🔙 Назад", callback_data="settings:back")
    builder.adjust(2, 2, 2, 2, 1)
    return builder.as_markup()


def _build_target_settings_keyboard(target_id: int, settings: dict) -> types.InlineKeyboardMarkup:
    """Build inline keyboard for per-target alert settings."""
    builder = InlineKeyboardBuilder()

    def _label(key: str, emoji: str, name: str) -> str:
        val = "✅" if settings.get(key, True) else "❌"
        return f"{emoji} {name}: {val}"

    builder.button(
        text=_label("alert_online", "🟢", "Онлайн"),
        callback_data=f"tset_online:{target_id}:{1 if settings.get('alert_online', True) else 0}",
    )
    builder.button(
        text=_label("alert_game_start", "🎮", "Игра"),
        callback_data=f"tset_game:{target_id}:{1 if settings.get('alert_game_start', True) else 0}",
    )
    builder.button(
        text=_label("alert_invisible", "👻", "Невидимка"),
        callback_data=f"tset_invisible:{target_id}:{1 if settings.get('alert_invisible', True) else 0}",
    )
    builder.button(
        text=_label("alert_privacy", "🔒", "Приватность"),
        callback_data=f"tset_privacy:{target_id}:{1 if settings.get('alert_privacy', True) else 0}",
    )
    builder.button(
        text=_label("alert_mmr", "📊", "MMR Dota"),
        callback_data=f"tset_mmr:{target_id}:{1 if settings.get('alert_mmr', True) else 0}",
    )
    builder.button(
        text=_label("alert_session", "⏱", "Сессии"),
        callback_data=f"tset_session:{target_id}:{1 if settings.get('alert_session', True) else 0}",
    )
    builder.button(
        text=_label("alert_name_change", "📝", "Смена ника"),
        callback_data=f"tset_name:{target_id}:{1 if settings.get('alert_name_change', True) else 0}",
    )
    builder.button(text="🔙 Назад", callback_data=f"tset_back:{target_id}")
    builder.adjust(2, 2, 2, 1, 1)
    return builder.as_markup()


def setup_bot(
    bot_instance: Bot,
    db_conn: aiosqlite.Connection,
    steam_client: SteamClient,
    match_tracker=None,
    watcher=None,
    cs2_resolver: Optional[CS2ActivityResolver] = None,
) -> Dispatcher:

    async def _append_cs2_activity(lines: list[str], steam_id: str) -> None:
        if cs2_resolver is None:
            return
        try:
            result = await cs2_resolver.lookup(steam_id)
        except Exception as exc:
            logger.warning("CS2 activity lookup failed for %s: %s", steam_id, exc)
            return
        lines.extend(format_cs2_activity_lines(result))

    @router.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        await message.answer(_help_text(), reply_markup=_build_main_menu())

    @router.message(Command("hide"))
    async def cmd_hide(message: Message) -> None:
        await message.answer("Меню скрыто. Кнопки /start или /list по-прежнему работают.",
                             reply_markup=ReplyKeyboardRemove())

    @router.message(Command("setkey"))
    async def cmd_setkey(message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer(
                "Не вижу API ключ.\n"
                "Отправь команду так: /setkey API_КЛЮЧ"
            )
            return

        api_key = parts[1].strip()
        valid = await steam_client.validate_key(api_key)
        if not valid:
            await message.answer(
                "Ключ не подошёл.\n"
                "Получить: https://steamcommunity.com/dev/apikey"
            )
            return

        from .models import User
        user = User(telegram_id=message.from_user.id, steam_api_key=api_key)
        await db.save_user(db_conn, user)
        await message.answer("API ключ сохранён. Теперь бот будет использовать его для твоих запросов.")

    # ── Add target (command) ──────────────────────────────────────

    @router.message(Command("add"))
    async def cmd_add(message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer(
                "Нужна ссылка на профиль.\n"
                "Отправь так: /add ссылка_на_профиль\n"
                "Пример: /add https://steamcommunity.com/id/gabelogannewell"
            )
            return

        input_text = parts[1].strip()
        await _do_add_target(message, input_text)

    # ── Add target (button flow) ──────────────────────────────────

    async def _do_add_target(message: Message, input_text: str) -> None:
        """Shared logic for adding a target from text input."""
        steam_id, vanity = _parse_steam_input(input_text)

        if not steam_id and not vanity:
            await message.answer(
                "Не понял ссылку.\n"
                "Пришли ссылку на профиль Steam или SteamID64.\n"
                "Пример: /add https://steamcommunity.com/id/gabelogannewell"
            )
            return

        if _check_too_soon(message.from_user.id):
            await message.answer("Слишком часто, подожди пару секунд.")
            return

        # Get API key
        user = await db.get_user(db_conn, message.from_user.id)
        api_key = _get_api_key(user.steam_api_key if user else None)
        if not api_key:
            await message.answer(
                "Сначала нужен API ключ.\n"
                "Сохрани его так: /setkey API_КЛЮЧ\n"
                "Получить: https://steamcommunity.com/dev/apikey"
            )
            return

        # Resolve vanity → SteamID64 if needed
        if not steam_id and vanity:
            steam_id = await steam_client.resolve_vanity_url(api_key, vanity)
            if not steam_id:
                await message.answer("Не удалось найти профиль по этой ссылке. Проверь, нет ли опечатки.")
                return

        if await db.is_steam_profile_blacklisted(db_conn, steam_id):
            await message.answer("Этот профиль запрещён для мониторинга.")
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
                f"Мониторинг: {_format_interval_label(DEFAULT_POLL_INTERVAL)}.\n"
                "Дальше можешь открыть список кнопкой «📋 Мой список»."
            )
        except Exception as e:
            logger.error("Failed to add target: %s", e)
            await message.answer("Не удалось добавить профиль. Возможно, ты уже его отслеживаешь.")

    @router.message(Command("remove"))
    async def cmd_remove(message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Укажи ссылку или SteamID64: /remove ссылка_или_SteamID64")
            return

        steam_id = await _resolve_target_id(db_conn, steam_client, message, parts[1])
        if not steam_id:
            return

        removed = await db.remove_target(db_conn, message.from_user.id, steam_id)
        if removed:
            await message.answer("Профиль удалён из списка.")
        else:
            await message.answer("Не нашёл такой профиль в твоём списке.")

    @router.message(Command("list"))
    async def cmd_list(message: Message) -> None:
        targets = await db.get_targets(db_conn, message.from_user.id)
        if not targets:
            await message.answer("Список пока пуст. Нажми ➕ Добавить или отправь /add ссылка_на_профиль")
            return

        for t in targets:
            state = await db.get_target_state(db_conn, t.id)
            text = render_target_card(t, state)

            keyboard = _build_target_keyboard(t)
            await message.answer(text, reply_markup=keyboard)

    @router.message(Command("pause"))
    async def cmd_pause(message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Укажи ссылку или SteamID64: /pause ссылка_или_SteamID64")
            return

        steam_id = await _resolve_target_id(db_conn, steam_client, message, parts[1])
        if not steam_id:
            return

        updated = await db.set_target_active(db_conn, message.from_user.id, steam_id, False)
        if updated:
            await message.answer("Профиль поставлен на паузу.")
        else:
            await message.answer("Не нашёл такой профиль.")

    @router.message(Command("resume"))
    async def cmd_resume(message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Укажи ссылку или SteamID64: /resume ссылка_или_SteamID64")
            return

        steam_id = await _resolve_target_id(db_conn, steam_client, message, parts[1])
        if not steam_id:
            return

        updated = await db.set_target_active(db_conn, message.from_user.id, steam_id, True)
        if updated:
            await message.answer("Профиль снят с паузы.")
        else:
            await message.answer("Не нашёл такой профиль.")

    @router.message(Command("check"))
    async def cmd_check(message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Укажи ссылку или SteamID64: /check ссылка_или_SteamID64")
            return

        if _check_too_soon(message.from_user.id):
            await message.answer("Слишком часто, подожди пару секунд.")
            return

        user = await db.get_user(db_conn, message.from_user.id)
        api_key = _get_api_key(user.steam_api_key if user else None)
        if not api_key:
            await message.answer("Сначала сохрани API ключ: /setkey API_КЛЮЧ")
            return

        steam_id, vanity = _parse_steam_input(parts[1])
        if not steam_id and vanity:
            steam_id = await steam_client.resolve_vanity_url(api_key, vanity)
        if not steam_id:
            await message.answer("Не понял ссылку или SteamID64. Проверь формат и попробуй ещё раз.")
            return

        profile = await steam_client.get_player_summaries(api_key, steam_id)
        if profile is None:
            await message.answer("Профиль не найден или приватный.")
            return

        lines = await _build_check_lines(steam_client, api_key, profile, steam_id)
        await _append_cs2_activity(lines, steam_id)

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

    # ── Menu button handlers (specific text matches FIRST) ────────

    @router.message(F.text == "➕ Добавить")
    async def btn_add(message: Message) -> None:
        """Handle '➕ Добавить' menu button — set pending state and ask for link."""
        _pending_add[message.from_user.id] = True
        await message.answer(
            "Пришли ссылку на Steam профиль (или напиши «Отмена»):",
            reply_markup=ForceReply(selective=True),
        )

    @router.message(F.text == "📋 Мой список")
    async def btn_list(message: Message) -> None:
        """Handle '📋 Мой список' menu button — same as /list."""
        targets = await db.get_targets(db_conn, message.from_user.id)
        if not targets:
            await message.answer("Список пуст. Нажми ➕ Добавить или отправь /add ссылка_на_профиль")
            return

        for t in targets:
            state = await db.get_target_state(db_conn, t.id)
            text = render_target_card(t, state)

            keyboard = _build_target_keyboard(t)
            await message.answer(text, reply_markup=keyboard)

    @router.message(F.text == "🔍 Проверить")
    async def btn_check_picker(message: Message) -> None:
        """Handle '🔍 Проверить' menu button — show targets to pick for instant check."""
        targets = await db.get_targets(db_conn, message.from_user.id)
        if not targets:
            await message.answer("Список пуст. Добавь профили через ➕ Добавить.")
            return

        builder = InlineKeyboardBuilder()
        for t in targets:
            builder.button(text=t.name, callback_data=f"pick_check:{t.id}")
        builder.adjust(1)
        await message.answer("Выбери профиль для проверки:", reply_markup=builder.as_markup())

    @router.message(F.text == "⚙️ Настройки")
    async def btn_settings(message: Message) -> None:
        """Handle '⚙️ Настройки' menu button — show settings panel."""
        settings = await db.get_user_settings(db_conn, message.from_user.id)
        keyboard = _build_settings_keyboard(settings)
        await message.answer(_settings_panel_text(settings), reply_markup=keyboard)

    # ── Catch-all for pending add flow (AFTER specific handlers) ──

    @router.message(F.text & ~F.text.startswith("/"))
    async def handle_pending_add(message: Message) -> None:
        """Catch text messages from users who are in pending state.
        Must be registered AFTER button handlers so they get priority."""
        user_id = message.from_user.id
        input_text = message.text.strip()

        if user_id in _pending_rename:
            target_id = _pending_rename.pop(user_id)
            if _is_cancel_text(input_text):
                await message.answer("Переименование отменено.")
                return
            if not input_text:
                await message.answer("Имя не должно быть пустым.")
                return
            target = await _get_target_by_id(db_conn, user_id, target_id)
            if not target:
                await message.answer("Не найден.")
                return
            updated = await db.rename_target(db_conn, user_id, target.steam_id, input_text)
            if not updated:
                await message.answer("Не найден.")
                return
            await message.answer(f"Новое имя: {input_text}")
            return

        # Steam add flow
        if _pending_add.get(user_id):
            _pending_add.pop(user_id, None)
            if _is_cancel_text(input_text):
                await message.answer("Добавление отменено. Когда будешь готов, нажми ➕ Добавить.")
                return
            await _do_add_target(message, input_text)
            return

    # ── Inline button callback handlers ──────────────────────────

    @router.callback_query(F.data.startswith("pause:"))
    async def cb_pause(callback: CallbackQuery) -> None:
        target_id = int(callback.data.split(":")[1])
        # Get target to verify ownership and get steam_id
        target = await _get_target_by_id(db_conn, callback.from_user.id, target_id)
        if not target:
            await callback.answer("Не найден.", show_alert=True)
            return

        await db.set_target_active(db_conn, callback.from_user.id, target.steam_id, False)
        await callback.answer("Пауза.")
        # Update the keyboard
        target.active = False
        keyboard = _build_target_keyboard(target)
        state = await db.get_target_state(db_conn, target.id)
        # Note: this intentionally still shows the LIVE status (not "Пауза")
        # alongside the "пауза" marker, so the user sees what the target was
        # doing at the moment of pausing. render_target_card() would force
        # "Пауза" for an inactive target, so it is deliberately not used here.
        status = "Пауза"
        if state:
            status = state_name(state.persona_state)
            if state.game_name:
                status += f", играет: {state.game_name}"
        marker = "пауза"
        text = f"{target.name} [{marker}]: {status}"
        await callback.message.edit_text(text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("resume:"))
    async def cb_resume(callback: CallbackQuery) -> None:
        target_id = int(callback.data.split(":")[1])
        target = await _get_target_by_id(db_conn, callback.from_user.id, target_id)
        if not target:
            await callback.answer("Не найден.", show_alert=True)
            return

        await db.set_target_active(db_conn, callback.from_user.id, target.steam_id, True)
        await callback.answer("Возобновлён.")
        target.active = True
        keyboard = _build_target_keyboard(target)
        state = await db.get_target_state(db_conn, target.id)
        text = render_target_card(target, state)
        await callback.message.edit_text(text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("interval:"))
    async def cb_interval(callback: CallbackQuery) -> None:
        target_id = int(callback.data.split(":")[1])
        target = await _get_target_by_id(db_conn, callback.from_user.id, target_id)
        if not target:
            await callback.answer("Не найден.", show_alert=True)
            return

        await callback.answer()
        keyboard = _build_interval_picker_keyboard(target.id, target.interval_seconds)
        await callback.message.edit_text(
            f"⏱ Интервал для {target.name}: { _format_interval_label(target.interval_seconds) }",
            reply_markup=keyboard,
        )

    @router.callback_query(F.data.startswith("interval_pick:"))
    async def cb_interval_pick(callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        target_id = int(parts[1])
        interval_seconds = int(parts[2])
        if interval_seconds not in INTERVAL_PRESETS:
            await callback.answer("Неизвестный интервал.", show_alert=True)
            return
        target = await _get_target_by_id(db_conn, callback.from_user.id, target_id)
        if not target:
            await callback.answer("Не найден.", show_alert=True)
            return

        updated = await db.set_target_interval(db_conn, callback.from_user.id, target.steam_id, interval_seconds)
        if not updated:
            await callback.answer("Не удалось обновить интервал.", show_alert=True)
            return
        target.interval_seconds = interval_seconds
        await callback.answer(f"Интервал: {_format_interval_label(interval_seconds)}")
        state = await db.get_target_state(db_conn, target.id)
        text = render_target_card(target, state)
        keyboard = _build_target_keyboard(target)
        await callback.message.edit_text(text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("interval_back:"))
    async def cb_interval_back(callback: CallbackQuery) -> None:
        target_id = int(callback.data.split(":")[1])
        target = await _get_target_by_id(db_conn, callback.from_user.id, target_id)
        if not target:
            await callback.answer("Не найден.", show_alert=True)
            return
        state = await db.get_target_state(db_conn, target.id)
        text = render_target_card(target, state)
        keyboard = _build_target_keyboard(target)
        await callback.message.edit_text(text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("rename_prompt:"))
    async def cb_rename_prompt(callback: CallbackQuery) -> None:
        target_id = int(callback.data.split(":")[1])
        target = await _get_target_by_id(db_conn, callback.from_user.id, target_id)
        if not target:
            await callback.answer("Не найден.", show_alert=True)
            return
        _pending_rename[callback.from_user.id] = target.id
        await callback.answer("Жду новое имя.")
        await callback.message.answer(
            f"Пришли новое имя для {target.name} (или напиши «Отмена»).",
            reply_markup=ForceReply(selective=True),
        )

    @router.callback_query(F.data.startswith("blacklist:"))
    async def cb_blacklist(callback: CallbackQuery) -> None:
        target_id = int(callback.data.split(":")[1])
        target = await _get_target_by_id(db_conn, callback.from_user.id, target_id)
        if not target:
            await callback.answer("Не найден.", show_alert=True)
            return
        await callback.answer()
        await callback.message.edit_text(
            f"Добавить профиль {target.name} в блэклист?",
            reply_markup=_build_blacklist_confirm_keyboard(target.id),
        )

    @router.callback_query(F.data.startswith("blacklist_confirm:"))
    async def cb_blacklist_confirm(callback: CallbackQuery) -> None:
        target_id = int(callback.data.split(":")[1])
        target = await _get_target_by_id(db_conn, callback.from_user.id, target_id)
        if not target:
            await callback.answer("Не найден.", show_alert=True)
            return
        if await db.is_steam_profile_blacklisted(db_conn, target.steam_id):
            await callback.answer("Профиль уже в блэклисте.")
            await callback.message.edit_text("Профиль уже в блэклисте.")
            return
        entry = SteamProfileBlacklistEntry(
            steam_id=target.steam_id,
            reason=f"blocked by telegram user {callback.from_user.id}",
            created_at=int(time.time()),
            created_by=callback.from_user.id,
        )
        await db.add_steam_profile_blacklist_entry(db_conn, entry)
        changed = await db.deactivate_targets_by_steam_id(db_conn, target.steam_id)
        await callback.answer("Добавлен в блэклист.")
        await callback.message.edit_text(
            f"Профиль добавлен в блэклист. Активные наблюдения отключены: {changed}."
        )

    @router.callback_query(F.data.startswith("blacklist_cancel:"))
    async def cb_blacklist_cancel(callback: CallbackQuery) -> None:
        target_id = int(callback.data.split(":")[1])
        target = await _get_target_by_id(db_conn, callback.from_user.id, target_id)
        if not target:
            await callback.answer("Не найден.", show_alert=True)
            return
        await callback.answer("Отменено.")
        state = await db.get_target_state(db_conn, target.id)
        text = render_target_card(target, state)
        keyboard = _build_target_keyboard(target)
        await callback.message.edit_text(text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("remove:"))
    async def cb_remove(callback: CallbackQuery) -> None:
        target_id = int(callback.data.split(":")[1])
        target = await _get_target_by_id(db_conn, callback.from_user.id, target_id)
        if not target:
            await callback.answer("Не найден.", show_alert=True)
            return

        await callback.answer()
        await callback.message.edit_text(
            f"Удалить профиль {target.name}?",
            reply_markup=_build_remove_confirm_keyboard(target_id),
        )

    @router.callback_query(F.data.startswith("remove_confirm:"))
    async def cb_remove_confirm(callback: CallbackQuery) -> None:
        target_id = int(callback.data.split(":")[1])
        target = await _get_target_by_id(db_conn, callback.from_user.id, target_id)
        if not target:
            await callback.answer("Не найден.", show_alert=True)
            return

        await db.remove_target(db_conn, callback.from_user.id, target.steam_id)
        await callback.answer("Удалён.")
        await callback.message.edit_text(f"{target.name} — удалён.")

    @router.callback_query(F.data.startswith("remove_cancel:"))
    async def cb_remove_cancel(callback: CallbackQuery) -> None:
        target_id = int(callback.data.split(":")[1])
        target = await _get_target_by_id(db_conn, callback.from_user.id, target_id)
        if not target:
            await callback.answer("Не найден.", show_alert=True)
            return

        await callback.answer("Отменено.")
        state = await db.get_target_state(db_conn, target.id)
        text = render_target_card(target, state)
        keyboard = _build_target_keyboard(target)
        await callback.message.edit_text(text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("check:"))
    async def cb_check(callback: CallbackQuery) -> None:
        target_id = int(callback.data.split(":")[1])
        target = await _get_target_by_id(db_conn, callback.from_user.id, target_id)
        if not target:
            await callback.answer("Не найден.", show_alert=True)
            return

        user = await db.get_user(db_conn, callback.from_user.id)
        api_key = _get_api_key(user.steam_api_key if user else None)
        if not api_key:
            await callback.answer("Нужен API ключ: /setkey API_КЛЮЧ", show_alert=True)
            return

        if _check_too_soon(callback.from_user.id):
            await callback.answer("Слишком часто, подожди пару секунд.")
            return

        await callback.answer("Проверяю...")

        profile = await steam_client.get_player_summaries(api_key, target.steam_id)
        if profile is None:
            await callback.message.answer("Профиль не найден или приватный.")
            return

        state = await db.get_target_state(db_conn, target.id)
        lines = await _build_check_lines(steam_client, api_key, profile, target.steam_id, state)
        await _append_last_game_observed_lines(lines, db_conn, callback.from_user.id, target, profile, state)
        await _append_cs2_activity(lines, target.steam_id)

        await callback.message.answer("\n".join(lines))

    @router.callback_query(F.data.startswith("pick_check:"))
    async def cb_pick_check(callback: CallbackQuery) -> None:
        """Handle target selection from the '🔍 Проверить' menu button."""
        target_id = int(callback.data.split(":")[1])
        target = await _get_target_by_id(db_conn, callback.from_user.id, target_id)
        if not target:
            await callback.answer("Не найден.", show_alert=True)
            return

        user = await db.get_user(db_conn, callback.from_user.id)
        api_key = _get_api_key(user.steam_api_key if user else None)
        if not api_key:
            await callback.answer("Нужен API ключ: /setkey API_КЛЮЧ", show_alert=True)
            return

        if _check_too_soon(callback.from_user.id):
            await callback.answer("Слишком часто, подожди пару секунд.")
            return

        await callback.answer("Проверяю...")

        profile = await steam_client.get_player_summaries(api_key, target.steam_id)
        if profile is None:
            await callback.message.edit_text("Профиль не найден или приватный.")
            return

        state = await db.get_target_state(db_conn, target.id)
        lines = await _build_check_lines(steam_client, api_key, profile, target.steam_id, state)
        await _append_last_game_observed_lines(lines, db_conn, callback.from_user.id, target, profile, state)
        await _append_cs2_activity(lines, target.steam_id)

        await callback.message.edit_text("\n".join(lines))

    @router.callback_query(F.data.startswith("consistency:"))
    async def cb_consistency(callback: CallbackQuery) -> None:
        """Show tracked activity history + live OpenDota recent matches."""
        target_id = int(callback.data.split(":")[1])
        target = await _get_target_by_id(db_conn, callback.from_user.id, target_id)
        if not target:
            await callback.answer("Не найден.", show_alert=True)
            return

        await callback.answer("Проверяю...")

        state = await db.get_target_state(db_conn, target.id)
        name = state.persona_name if state else target.name

        lines = [f"📊 {name}", ""]

        # 1) Current state
        if state:
            status = state_name(state.persona_state)
            lines.append(f"Статус: {status}")
            if state.game_name and state.persona_state > 0:
                lines.append(f"Сейчас играет: {state.game_name}")
            if state.last_logoff:
                lines.append(f"Последний онлайн: {format_last_seen(state.last_logoff)}")
        else:
            lines.append("Бот ещё не проверял этого игрока.")

        # 2) Activity log from DB (bot-tracked events)
        activity = await db.get_activity_log(db_conn, target.id, limit=10)
        if activity:
            lines.append("")
            lines.append("📜 История (бот):")
            EVENT_ICONS = {
                "game_start": "🎮",
                "game_stop": "⏹",
                "match": "⚔️",
                "online": "🟢",
                "offline": "⚫",
                "invisible": "👻",
                "name_change": "📝",
            }
            for ev in activity:
                icon = EVENT_ICONS.get(ev["event_type"], "•")
                ts = format_last_seen(ev["detected_at"])
                parts = [f"{icon} {ts}"]
                if ev["game_name"]:
                    parts.append(ev["game_name"])
                if ev["event_type"] == "match":
                    if ev["hero_name"]:
                        parts.append(f"({ev['hero_name']})")
                    if ev["duration_seconds"]:
                        parts.append(f"{ev['duration_seconds'] // 60}мин")
                elif ev["details"]:
                    parts.append(ev["details"])
                lines.append("  " + " ".join(parts))
        else:
            lines.append("")
            lines.append("История пуста — событий пока не было.")

        # 3) Live OpenDota recent matches (cached by watcher when possible)
        if watcher is not None:
            try:
                matches = await watcher.get_cached_recent_matches(target.steam_id)
                if matches:
                    lines.append("")
                    lines.append("🎯 Последние матчи Dota 2:")
                    for m in matches:
                        ts = format_last_seen(m.start_time) if m.start_time else "время скрыто"
                        hero = m.hero_name or "?"
                        dur = f"{m.duration // 60}мин" if m.duration else "длительность ?"
                        src = " [Dotabuff]" if m.source == "dotabuff" else ""
                        lines.append(f"  {ts} — {hero} ({dur}){src}")
            except Exception:
                pass

        await _append_cs2_activity(lines, target.steam_id)

        await callback.message.answer("\n".join(lines))

    @router.callback_query(F.data.startswith("session:"))
    async def cb_session(callback: CallbackQuery) -> None:
        """Show current session info for a target."""
        target_id = int(callback.data.split(":")[1])
        target = await _get_target_by_id(db_conn, callback.from_user.id, target_id)
        if not target:
            await callback.answer("Не найден.", show_alert=True)
            return
        await callback.answer()
        await _show_session_info(callback, db_conn, target=target)

    # ── Settings callbacks ────────────────────────────────────────

    @router.callback_query(F.data.startswith("set_summary:"))
    async def cb_set_summary(callback: CallbackQuery) -> None:
        """Toggle daily summary setting."""
        val = callback.data.split(":")[1]
        telegram_id = callback.from_user.id
        settings = await db.get_user_settings(db_conn, telegram_id)
        settings.daily_summary_enabled = val == "1"
        await db.save_user_settings(db_conn, settings)
        status = "включена" if settings.daily_summary_enabled else "выключена"
        await callback.answer(f"Ежедневная сводка {status}.")
        keyboard = _build_settings_keyboard(settings)
        await callback.message.edit_text(_settings_panel_text(settings), reply_markup=keyboard)

    @router.callback_query(F.data == "set_time:show")
    async def cb_set_time_show(callback: CallbackQuery) -> None:
        """Show time picker sub-keyboard."""
        keyboard = _build_time_picker_keyboard()
        await callback.message.edit_text("🕐 Выбери время для ежедневной сводки:", reply_markup=keyboard)

    @router.callback_query(F.data.startswith("pick_time:"))
    async def cb_pick_time(callback: CallbackQuery) -> None:
        """Set daily summary time from picker."""
        time_val = callback.data.split(":")[1] + ":" + callback.data.split(":")[2]
        telegram_id = callback.from_user.id
        settings = await db.get_user_settings(db_conn, telegram_id)
        settings.daily_summary_time = time_val
        await db.save_user_settings(db_conn, settings)
        await callback.answer(f"Время сводки: {time_val}")
        keyboard = _build_settings_keyboard(settings)
        await callback.message.edit_text(_settings_panel_text(settings), reply_markup=keyboard)

    @router.callback_query(F.data == "set_tz:show")
    async def cb_set_tz_show(callback: CallbackQuery) -> None:
        """Show timezone picker sub-keyboard."""
        telegram_id = callback.from_user.id
        settings = await db.get_user_settings(db_conn, telegram_id)
        keyboard = _build_tz_picker_keyboard(settings.timezone)
        await callback.message.edit_text(
            "🌐 Выбери часовой пояс:", reply_markup=keyboard
        )

    @router.callback_query(F.data.startswith("pick_tz:"))
    async def cb_pick_tz(callback: CallbackQuery) -> None:
        """Set user timezone from picker."""
        tz_val = callback.data.split(":", 1)[1]
        if tz_val not in TIMEZONES:
            await callback.answer("Неизвестный часовой пояс.", show_alert=True)
            return
        telegram_id = callback.from_user.id
        settings = await db.get_user_settings(db_conn, telegram_id)
        settings.timezone = tz_val
        await db.save_user_settings(db_conn, settings)
        await callback.answer(f"Часовой пояс: {tz_val}")
        keyboard = _build_settings_keyboard(settings)
        await callback.message.edit_text(_settings_panel_text(settings), reply_markup=keyboard)

    @router.callback_query(F.data.startswith("set_session:"))
    async def cb_set_session(callback: CallbackQuery) -> None:
        """Toggle session updates setting."""
        val = callback.data.split(":")[1]
        telegram_id = callback.from_user.id
        settings = await db.get_user_settings(db_conn, telegram_id)
        settings.session_updates_enabled = val == "1"
        await db.save_user_settings(db_conn, settings)
        status = "включены" if settings.session_updates_enabled else "выключены"
        await callback.answer(f"Обновления сессий {status}.")
        keyboard = _build_settings_keyboard(settings)
        await callback.message.edit_text(_settings_panel_text(settings), reply_markup=keyboard)

    @router.callback_query(F.data.startswith("set_privacy:"))
    async def cb_set_privacy(callback: CallbackQuery) -> None:
        """Toggle privacy alerts setting."""
        val = callback.data.split(":")[1]
        telegram_id = callback.from_user.id
        settings = await db.get_user_settings(db_conn, telegram_id)
        settings.privacy_alerts_enabled = val == "1"
        await db.save_user_settings(db_conn, settings)
        status = "включены" if settings.privacy_alerts_enabled else "выключены"
        await callback.answer(f"Уведомления приватности {status}.")
        keyboard = _build_settings_keyboard(settings)
        await callback.message.edit_text(_settings_panel_text(settings), reply_markup=keyboard)

    @router.callback_query(F.data == "settings:back")
    async def cb_settings_back(callback: CallbackQuery) -> None:
        """Go back to settings menu from time picker."""
        telegram_id = callback.from_user.id
        settings = await db.get_user_settings(db_conn, telegram_id)
        keyboard = _build_settings_keyboard(settings)
        await callback.message.edit_text(_settings_panel_text(settings), reply_markup=keyboard)

    # ── Target alert settings callbacks ────────────────────────────

    async def _show_target_settings(callback: CallbackQuery, target_id: int) -> None:
        """Show the target alert settings panel."""
        target = await _get_target_by_id(db_conn, callback.from_user.id, target_id)
        if not target:
            await callback.answer("Не найден.", show_alert=True)
            return
        settings = await db.get_target_settings(db_conn, target_id)
        keyboard = _build_target_settings_keyboard(target_id, settings)
        await callback.message.edit_text(
            f"⚙️ Алерты для {target.name}:", reply_markup=keyboard
        )

    @router.callback_query(F.data.startswith("tset:") & ~F.data.startswith("tset_"))
    async def cb_target_settings(callback: CallbackQuery) -> None:
        """Open target alert settings panel."""
        target_id = int(callback.data.split(":")[1])
        await _show_target_settings(callback, target_id)

    async def _toggle_target_setting(
        callback: CallbackQuery, target_id: int, setting_key: str, current_val: str
    ) -> None:
        """Toggle a single target alert setting."""
        new_val = current_val == "0"  # toggle: if current is "0", new is True
        target = await _get_target_by_id(db_conn, callback.from_user.id, target_id)
        if not target:
            await callback.answer("Не найден.", show_alert=True)
            return
        settings = await db.get_target_settings(db_conn, target_id)
        settings[setting_key] = new_val
        await db.save_target_settings(db_conn, target_id, settings)
        keyboard = _build_target_settings_keyboard(target_id, settings)
        status = "включены" if new_val else "выключены"
        await callback.answer(status)
        await callback.message.edit_text(
            f"⚙️ Алерты для {target.name}:", reply_markup=keyboard
        )

    @router.callback_query(F.data.startswith("tset_online:"))
    async def cb_tset_online(callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        await _toggle_target_setting(callback, int(parts[1]), "alert_online", parts[2])

    @router.callback_query(F.data.startswith("tset_game:"))
    async def cb_tset_game(callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        await _toggle_target_setting(callback, int(parts[1]), "alert_game_start", parts[2])

    @router.callback_query(F.data.startswith("tset_invisible:"))
    async def cb_tset_invisible(callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        await _toggle_target_setting(callback, int(parts[1]), "alert_invisible", parts[2])

    @router.callback_query(F.data.startswith("tset_privacy:"))
    async def cb_tset_privacy(callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        await _toggle_target_setting(callback, int(parts[1]), "alert_privacy", parts[2])

    @router.callback_query(F.data.startswith("tset_mmr:"))
    async def cb_tset_mmr(callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        await _toggle_target_setting(callback, int(parts[1]), "alert_mmr", parts[2])

    @router.callback_query(F.data.startswith("tset_session:"))
    async def cb_tset_session(callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        await _toggle_target_setting(callback, int(parts[1]), "alert_session", parts[2])

    @router.callback_query(F.data.startswith("tset_name:"))
    async def cb_tset_name(callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        await _toggle_target_setting(callback, int(parts[1]), "alert_name_change", parts[2])

    @router.callback_query(F.data.startswith("tset_back:"))
    async def cb_tset_back(callback: CallbackQuery) -> None:
        """Go back to target card from target settings."""
        target_id = int(callback.data.split(":")[1])
        target = await _get_target_by_id(db_conn, callback.from_user.id, target_id)
        if not target:
            await callback.answer("Не найден.", show_alert=True)
            return

        state = await db.get_target_state(db_conn, target.id)
        text = render_target_card(target, state)
        keyboard = _build_target_keyboard(target)
        await callback.message.edit_text(text, reply_markup=keyboard)

    dispatcher = Dispatcher()
    # Optional allowlist enforcement (no-op when ALLOWED_TELEGRAM_IDS is empty).
    allowlist = AllowlistMiddleware()
    dispatcher.message.outer_middleware(allowlist)
    dispatcher.callback_query.outer_middleware(allowlist)
    dispatcher.include_router(router)
    return dispatcher


async def _get_target_by_id(
    db_conn: aiosqlite.Connection,
    telegram_id: int,
    target_id: int,
) -> Optional[Target]:
    """Get a target by ID, verifying it belongs to the given telegram user."""
    return await db.get_target_by_id(db_conn, target_id, telegram_id)


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
