"""Focused tests for Telegram bot UX helpers."""

from types import SimpleNamespace

import pytest

from src import bot as bot_module
from src.bot import (
    _append_last_game_observed_lines,
    _build_blacklist_confirm_keyboard,
    _build_check_lines,
    _build_interval_picker_keyboard,
    _build_remove_confirm_keyboard,
    _build_settings_keyboard,
    _build_target_keyboard,
    _help_text,
    _is_cancel_text,
    _show_session_info,
)
from src.models import SteamProfile, Target, TargetState, UserSettings


def _flatten_button_rows(markup):
    return [[button.text for button in row] for row in markup.inline_keyboard]


def test_help_text_is_shorter_and_action_oriented():
    text = _help_text()

    assert "Как начать:" in text
    assert "Если нужно, сохрани свой ключ: /setkey КЛЮЧ" in text
    assert "Что отслеживает: онлайн, игры, невидимку" in text
    assert "добавить профиль" in text
    assert "добавить таргет" not in text


def test_settings_keyboard_encodes_toggled_values():
    settings = UserSettings(
        telegram_id=1,
        daily_summary_enabled=True,
        session_updates_enabled=False,
        privacy_alerts_enabled=True,
        daily_summary_time="21:00",
        timezone="UTC",
    )

    markup = _build_settings_keyboard(settings)
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]

    assert "set_summary:0" in callbacks
    assert "set_session:1" in callbacks
    assert "set_privacy:0" in callbacks


def test_target_keyboard_uses_clearer_labels():
    target = Target(id=7, telegram_id=1, steam_id="76561198000000001", name="Player", active=True)

    markup = _build_target_keyboard(target)
    rows = _flatten_button_rows(markup)

    assert rows[0] == ["⏸ Пауза", "📜 История", "⏱ Сессия"]
    assert rows[1] == ["⏱ Интервал", "📝 Переименовать", "🔍 Проверить"]
    assert rows[2] == ["⚙️ Уведомления", "🚫 В блэклист", "🗑 Удалить"]


def test_interval_picker_marks_current_value():
    markup = _build_interval_picker_keyboard(42, 300)
    rows = _flatten_button_rows(markup)

    assert rows[0] == ["1 мин", "3 мин"]
    assert rows[1] == ["✅ 5 мин", "10 мин"]
    assert rows[2] == ["15 мин"]
    assert rows[3] == ["🔙 Назад"]


def test_blacklist_confirm_keyboard_has_safe_choices():
    markup = _build_blacklist_confirm_keyboard(42)

    buttons = [(button.text, button.callback_data) for row in markup.inline_keyboard for button in row]
    assert buttons == [
        ("🚫 Да, в блэклист", "blacklist_confirm:42"),
        ("❌ Отмена", "blacklist_cancel:42"),
    ]


def test_remove_confirm_keyboard_has_safe_choices():
    markup = _build_remove_confirm_keyboard(42)

    buttons = [(button.text, button.callback_data) for row in markup.inline_keyboard for button in row]
    assert buttons == [
        ("✅ Да, удалить", "remove_confirm:42"),
        ("❌ Отмена", "remove_cancel:42"),
    ]


def test_cancel_text_matches_common_inputs():
    assert _is_cancel_text("Отмена") is True
    assert _is_cancel_text(" /cancel ") is True
    assert _is_cancel_text("cancel") is True
    assert _is_cancel_text("https://steamcommunity.com/id/test") is False


@pytest.mark.asyncio
async def test_show_session_info_renders_playing_session(monkeypatch):
    calls = []

    async def fake_get_target_by_id(db_conn, telegram_id, target_id):
        return Target(id=target_id, telegram_id=telegram_id, steam_id="steam", name="Player")

    async def fake_get_target_state(db_conn, target_id):
        return TargetState(
            target_id=target_id,
            persona_state=1,
            persona_name="Player",
            game_name="ARC Raiders",
            game_start_time=1_700_000_000,
            last_logoff=1_699_999_000,
        )

    monkeypatch.setattr(bot_module, "_get_target_by_id", fake_get_target_by_id)
    monkeypatch.setattr(bot_module.db, "get_target_state", fake_get_target_state)
    monkeypatch.setattr(bot_module, "format_last_seen", lambda ts: "recently")
    monkeypatch.setattr(bot_module, "format_duration_seconds", lambda seconds: f"{seconds}s")
    monkeypatch.setattr(
        bot_module,
        "datetime",
        SimpleNamespace(
            now=lambda tz=None: SimpleNamespace(timestamp=lambda: 1_700_000_120),
        ),
    )

    async def fake_message_answer(text):
        calls.append(("message.answer", text))

    callback = SimpleNamespace(
        data="session:7",
        from_user=SimpleNamespace(id=111),
        message=SimpleNamespace(answer=fake_message_answer),
    )

    await _show_session_info(callback, db_conn=None)

    assert calls == [(
        "message.answer",
        "⏱ Player\n\nИграет: ARC Raiders\nДлительность сессии: 120s\nПоследний выход: recently",
    )]


@pytest.mark.asyncio
async def test_build_check_lines_adds_badge_and_last_game_for_offline_profile():
    steam = SimpleNamespace()

    async def fake_recently_played_games(api_key, steam_id):
        return ["Example Game 2", "CS2"]

    steam.get_recently_played_games = fake_recently_played_games

    profile = SteamProfile(
        steam_id="76561198000000001",
        persona_name="Игрок Тест",
        persona_state=0,
        last_logoff=123,
    )

    lines = await _build_check_lines(steam, "k", profile, profile.steam_id)

    assert lines == [
        "Игрок Тест",
        "Статус: Offline 🔴",
        "Последний онлайн: 1970-01-01 00:02 UTC",
        "Недавняя игра: Example Game 2",
    ]


@pytest.mark.asyncio
async def test_build_check_lines_skips_recent_games_lookup_while_currently_playing():
    steam = SimpleNamespace()
    called = False

    async def fake_recently_played_games(api_key, steam_id):
        nonlocal called
        called = True
        return ["Should not be used"]

    steam.get_recently_played_games = fake_recently_played_games

    profile = SteamProfile(
        steam_id="76561198000000001",
        persona_name="Player",
        persona_state=1,
        game_name="Counter-Strike 2",
    )

    lines = await _build_check_lines(steam, "k", profile, profile.steam_id)

    assert called is False
    assert lines == [
        "Player",
        "Статус: Online 🟢",
        "Играет: Counter-Strike 2",
    ]


@pytest.mark.asyncio
async def test_build_check_lines_online_idle_prefers_live_recent_games_over_stale_cached_game():
    steam = SimpleNamespace()

    async def fake_recently_played_games(api_key, steam_id):
        return ["Live Recent Game"]

    steam.get_recently_played_games = fake_recently_played_games

    target_state = TargetState(
        target_id=7,
        persona_state=0,
        game_name="Stale Cached Game",
    )

    profile = SteamProfile(
        steam_id="76561198000000001",
        persona_name="Player",
        persona_state=1,
        game_name=None,
    )

    lines = await _build_check_lines(steam, "k", profile, profile.steam_id, target_state)

    assert lines == [
        "Player",
        "Статус: Online 🟢",
        "Недавняя игра: Live Recent Game",
    ]


@pytest.mark.asyncio
async def test_append_last_game_observed_lines_replaces_recent_game_with_exact_time(monkeypatch):
    async def fake_get_activity_log(db_conn, target_id, limit=20, event_types=None):
        return [{"game_name": "Observed Game", "detected_at": 1_700_000_000}]

    async def fake_get_user_settings(db_conn, telegram_id):
        return UserSettings(telegram_id=telegram_id, timezone="UTC")

    monkeypatch.setattr(bot_module.db, "get_activity_log", fake_get_activity_log)
    monkeypatch.setattr(bot_module.db, "get_user_settings", fake_get_user_settings)
    monkeypatch.setattr(
        bot_module,
        "datetime",
        SimpleNamespace(
            fromtimestamp=lambda ts, tz=None: __import__("datetime").datetime.fromtimestamp(ts, tz=tz),
            now=lambda tz=None: __import__("datetime").datetime.fromtimestamp(1_700_000_120, tz=tz),
        ),
    )

    lines = [
        "Player",
        "Статус: Offline 🔴",
        "Последний онлайн: 2026-01-01 00:00 UTC",
        "Недавняя игра: Stale Hint",
    ]
    target = Target(id=7, telegram_id=1, steam_id="7656", name="Player")
    profile = SteamProfile(steam_id="7656", persona_name="Player", persona_state=0)

    await _append_last_game_observed_lines(lines, None, 1, target, profile, None)

    assert lines == [
        "Player",
        "Статус: Offline 🔴",
        "Последний онлайн: 2026-01-01 00:00 UTC",
        "Последний раз: Observed Game",
        "В 22:13",
    ]


@pytest.mark.asyncio
async def test_show_session_info_missing_target_does_not_reanswer_callback(monkeypatch):
    calls = []

    async def fake_get_target_by_id(db_conn, telegram_id, target_id):
        return None

    async def fake_callback_answer(*args, **kwargs):
        calls.append(("callback.answer", args, kwargs))

    async def fake_message_answer(text):
        calls.append(("message.answer", text))

    monkeypatch.setattr(bot_module, "_get_target_by_id", fake_get_target_by_id)

    callback = SimpleNamespace(
        data="session:404",
        from_user=SimpleNamespace(id=111),
        answer=fake_callback_answer,
        message=SimpleNamespace(answer=fake_message_answer),
    )

    await _show_session_info(callback, db_conn=None)

    assert calls == []


@pytest.mark.asyncio
async def test_session_callback_ack_happens_before_message_send():
    calls = []

    async def fake_callback_answer(*args, **kwargs):
        calls.append(("callback.answer", args, kwargs))

    async def fake_message_answer(text):
        calls.append(("message.answer", text))

    callback = SimpleNamespace(
        answer=fake_callback_answer,
        message=SimpleNamespace(answer=fake_message_answer),
    )

    await callback.answer()
    await callback.message.answer("ok")

    assert [item[0] for item in calls] == ["callback.answer", "message.answer"]
