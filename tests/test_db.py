"""Tests for src.db: CRUD operations."""

import pytest
import pytest_asyncio
from src.db import (
    save_user,
    get_user,
    add_target,
    remove_target,
    get_targets,
    get_active_targets,
    set_target_active,
    get_target_state,
    save_target_state,
)
from src.models import User, Target, TargetState


@pytest.mark.asyncio
class TestUserCRUD:
    async def test_save_and_get_user(self, db_conn):
        user = User(telegram_id=12345, steam_api_key="abc123key")
        await save_user(db_conn, user)

        fetched = await get_user(db_conn, 12345)
        assert fetched is not None
        assert fetched.telegram_id == 12345
        assert fetched.steam_api_key == "abc123key"

    async def test_get_nonexistent_user(self, db_conn):
        fetched = await get_user(db_conn, 99999)
        assert fetched is None

    async def test_update_user_api_key(self, db_conn):
        user = User(telegram_id=12345, steam_api_key="old_key")
        await save_user(db_conn, user)

        user.steam_api_key = "new_key"
        await save_user(db_conn, user)

        fetched = await get_user(db_conn, 12345)
        assert fetched.steam_api_key == "new_key"


@pytest.mark.asyncio
class TestTargetCRUD:
    async def test_add_and_get_targets(self, db_conn):
        # First create a user
        await save_user(db_conn, User(telegram_id=111, steam_api_key="key"))

        target = Target(
            id=0, telegram_id=111, steam_id="76561198000000001",
            name="Player1", interval_seconds=300, active=True,
        )
        added = await add_target(db_conn, target)
        assert added.id > 0

        targets = await get_targets(db_conn, 111)
        assert len(targets) == 1
        assert targets[0].steam_id == "76561198000000001"
        assert targets[0].name == "Player1"
        assert targets[0].active is True

    async def test_remove_target(self, db_conn):
        await save_user(db_conn, User(telegram_id=111, steam_api_key="key"))
        target = Target(
            id=0, telegram_id=111, steam_id="76561198000000001",
            name="Player1",
        )
        await add_target(db_conn, target)

        removed = await remove_target(db_conn, 111, "76561198000000001")
        assert removed is True

        targets = await get_targets(db_conn, 111)
        assert len(targets) == 0

    async def test_remove_nonexistent_target(self, db_conn):
        removed = await remove_target(db_conn, 111, "nonexistent")
        assert removed is False

    async def test_get_active_targets(self, db_conn):
        await save_user(db_conn, User(telegram_id=111, steam_api_key="key"))

        t1 = Target(id=0, telegram_id=111, steam_id="111", name="A", active=True)
        t2 = Target(id=0, telegram_id=111, steam_id="222", name="B", active=True)
        await add_target(db_conn, t1)
        await add_target(db_conn, t2)

        # Pause one
        await set_target_active(db_conn, 111, "222", False)

        active = await get_active_targets(db_conn)
        assert len(active) == 1
        assert active[0].steam_id == "111"

    async def test_set_target_active_pause_resume(self, db_conn):
        await save_user(db_conn, User(telegram_id=111, steam_api_key="key"))
        t = Target(id=0, telegram_id=111, steam_id="76561198000000001", name="P")
        await add_target(db_conn, t)

        # Pause
        paused = await set_target_active(db_conn, 111, "76561198000000001", False)
        assert paused is True

        targets = await get_targets(db_conn, 111)
        assert targets[0].active is False

        # Resume
        resumed = await set_target_active(db_conn, 111, "76561198000000001", True)
        assert resumed is True

        targets = await get_targets(db_conn, 111)
        assert targets[0].active is True


@pytest.mark.asyncio
class TestTargetStateCRUD:
    async def test_save_and_get_state(self, db_conn):
        await save_user(db_conn, User(telegram_id=111, steam_api_key="key"))
        target = Target(id=0, telegram_id=111, steam_id="111", name="A")
        added = await add_target(db_conn, target)

        state = TargetState(
            target_id=added.id,
            persona_state=1,
            persona_name="Player1",
            game_id=None,
            game_name=None,
            playtime_forever=42,
            last_logoff=1705319400,
            last_checked=1705320000,
        )
        await save_target_state(db_conn, state)

        fetched = await get_target_state(db_conn, added.id)
        assert fetched is not None
        assert fetched.persona_state == 1
        assert fetched.persona_name == "Player1"
        assert fetched.playtime_forever == 42
        assert fetched.last_logoff == 1705319400

    async def test_update_state(self, db_conn):
        await save_user(db_conn, User(telegram_id=111, steam_api_key="key"))
        target = Target(id=0, telegram_id=111, steam_id="111", name="A")
        added = await add_target(db_conn, target)

        state = TargetState(target_id=added.id, persona_state=0, playtime_forever=10)
        await save_target_state(db_conn, state)

        # Update
        state.persona_state = 1
        state.playtime_forever = 20
        await save_target_state(db_conn, state)

        fetched = await get_target_state(db_conn, added.id)
        assert fetched.persona_state == 1
        assert fetched.playtime_forever == 20

    async def test_get_nonexistent_state(self, db_conn):
        fetched = await get_target_state(db_conn, 99999)
        assert fetched is None
