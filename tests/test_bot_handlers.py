"""Command-handler tests.

Handlers are closures registered on the module-level router by setup_bot, and
a router can only be wired into a dispatcher once per process — so one test
drives all scenarios over a single setup_bot() call, invoking the registered
callbacks directly with fake messages.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from src import bot as bot_module
from src.bot import setup_bot
from src.db import init_db


def _fake_message(text: str, user_id: int):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
    )


def _handlers():
    return {h.callback.__name__: h.callback for h in bot_module.router.message.handlers}


def _last_answer_text(message) -> str:
    args, kwargs = message.answer.call_args
    return args[0] if args else kwargs.get("text", "")


@pytest.mark.asyncio
async def test_command_handlers():
    async with aiosqlite.connect(":memory:") as db_conn:
        await init_db(db_conn)
        setup_bot(MagicMock(), db_conn, MagicMock())
        handlers = _handlers()

        # /start replies with help text and the main menu keyboard
        msg = _fake_message("/start", user_id=9001)
        await handlers["cmd_start"](msg)
        assert "Как начать:" in _last_answer_text(msg)
        assert msg.answer.call_args.kwargs.get("reply_markup") is not None

        # /add without an argument explains usage instead of crashing
        msg = _fake_message("/add", user_id=9001)
        await handlers["cmd_add"](msg)
        assert "Нужна ссылка" in _last_answer_text(msg)

        # ➕ Добавить sets the pending flag; «Отмена» clears it
        msg = _fake_message("➕ Добавить", user_id=9002)
        await handlers["btn_add"](msg)
        assert bot_module._pending_add.get(9002) is True
        msg = _fake_message("Отмена", user_id=9002)
        await handlers["handle_pending_add"](msg)
        assert 9002 not in bot_module._pending_add
        assert "отменено" in _last_answer_text(msg)

        # pending rename: cancel pops the pending state
        bot_module._pending_rename[9003] = 1
        msg = _fake_message("Отмена", user_id=9003)
        await handlers["handle_pending_add"](msg)
        assert 9003 not in bot_module._pending_rename
        assert "Переименование отменено" in _last_answer_text(msg)

        # /check without argument explains usage
        msg = _fake_message("/check", user_id=9004)
        await handlers["cmd_check"](msg)
        assert "Укажи ссылку" in _last_answer_text(msg)

        # /check with an argument but no stored API key asks for /setkey;
        # an immediate second call trips the anti-flood guard
        msg = _fake_message("/check 76561198000000001", user_id=9005)
        await handlers["cmd_check"](msg)
        assert "/setkey" in _last_answer_text(msg)
        msg = _fake_message("/check 76561198000000001", user_id=9005)
        await handlers["cmd_check"](msg)
        assert "Слишком часто" in _last_answer_text(msg)
