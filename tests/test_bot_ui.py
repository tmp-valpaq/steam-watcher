"""Focused tests for Telegram bot UX helpers."""

from src.bot import (
    _build_remove_confirm_keyboard,
    _build_settings_keyboard,
    _build_target_keyboard,
    _help_text,
    _is_cancel_text,
)
from src.models import Target, UserSettings


def _flatten_button_rows(markup):
    return [[button.text for button in row] for row in markup.inline_keyboard]


def test_help_text_is_shorter_and_action_oriented():
    text = _help_text()

    assert "Начать: нажми ➕ Добавить" in text
    assert "Отслеживает: онлайн/оффлайн" in text
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
    assert rows[1] == ["⚙️ Уведомления", "🗑 Удалить", "🔍 Проверить"]


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
