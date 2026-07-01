"""Tests for src.db: CRUD operations."""

import pytest
import pytest_asyncio
from src.db import (
    init_db,
    save_user,
    get_user,
    add_target,
    remove_target,
    get_targets,
    get_active_targets,
    set_target_active,
    set_all_targets_active,
    set_target_interval,
    rename_target,
    is_steam_profile_blacklisted,
    get_steam_profile_blacklist_entry,
    add_steam_profile_blacklist_entry,
    remove_steam_profile_blacklist_entry,
    deactivate_targets_by_steam_id,
    get_target_state,
    save_target_state,
)
from src.models import User, Target, TargetState, SteamProfileBlacklistEntry


@pytest.mark.asyncio
class TestInitDbPragmas:
    async def test_busy_timeout_set(self, db_conn):
        async with db_conn.execute("PRAGMA busy_timeout") as cur:
            row = await cur.fetchone()
        assert row[0] == 5000

    async def test_foreign_keys_enabled(self, db_conn):
        async with db_conn.execute("PRAGMA foreign_keys") as cur:
            row = await cur.fetchone()
        assert row[0] == 1


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

    async def test_set_all_targets_active_for_user(self, db_conn):
        await save_user(db_conn, User(telegram_id=111, steam_api_key="key"))
        await add_target(db_conn, Target(id=0, telegram_id=111, steam_id="111", name="A"))
        await add_target(db_conn, Target(id=0, telegram_id=111, steam_id="222", name="B"))

        changed = await set_all_targets_active(db_conn, 111, False)
        assert changed == 2

        targets = await get_targets(db_conn, 111)
        assert len(targets) == 2
        assert all(t.active is False for t in targets)

    async def test_set_target_interval_updates_only_owned_target(self, db_conn):
        await save_user(db_conn, User(telegram_id=111, steam_api_key="key"))
        await save_user(db_conn, User(telegram_id=222, steam_api_key="key2"))
        await add_target(db_conn, Target(id=0, telegram_id=111, steam_id="111", name="A", interval_seconds=30))
        await add_target(db_conn, Target(id=0, telegram_id=222, steam_id="111", name="A2", interval_seconds=30))

        updated = await set_target_interval(db_conn, 111, "111", 600)
        assert updated is True

        user_one_targets = await get_targets(db_conn, 111)
        user_two_targets = await get_targets(db_conn, 222)
        assert user_one_targets[0].interval_seconds == 600
        assert user_two_targets[0].interval_seconds == 30

    async def test_rename_target_updates_name(self, db_conn):
        await save_user(db_conn, User(telegram_id=111, steam_api_key="key"))
        await add_target(db_conn, Target(id=0, telegram_id=111, steam_id="111", name="Old"))

        renamed = await rename_target(db_conn, 111, "111", "New")
        assert renamed is True

        targets = await get_targets(db_conn, 111)
        assert targets[0].name == "New"


@pytest.mark.asyncio
class TestSteamProfileBlacklistCRUD:
    async def test_blacklist_is_empty_by_default(self, db_conn):
        assert await is_steam_profile_blacklisted(db_conn, "76561198000000001") is False
        assert await get_steam_profile_blacklist_entry(db_conn, "76561198000000001") is None

    async def test_add_and_get_blacklist_entry(self, db_conn):
        entry = SteamProfileBlacklistEntry(
            steam_id="76561198000000001",
            reason="manual block",
            created_at=1710000000,
            created_by=111,
        )

        await add_steam_profile_blacklist_entry(db_conn, entry)

        assert await is_steam_profile_blacklisted(db_conn, entry.steam_id) is True
        fetched = await get_steam_profile_blacklist_entry(db_conn, entry.steam_id)
        assert fetched is not None
        assert fetched.steam_id == entry.steam_id
        assert fetched.reason == "manual block"
        assert fetched.created_at == 1710000000
        assert fetched.created_by == 111

    async def test_remove_blacklist_entry(self, db_conn):
        entry = SteamProfileBlacklistEntry(
            steam_id="76561198000000001",
            created_at=1710000000,
        )
        await add_steam_profile_blacklist_entry(db_conn, entry)

        removed = await remove_steam_profile_blacklist_entry(db_conn, entry.steam_id)
        assert removed is True
        assert await is_steam_profile_blacklisted(db_conn, entry.steam_id) is False

    async def test_deactivate_targets_by_steam_id(self, db_conn):
        await save_user(db_conn, User(telegram_id=111, steam_api_key="key"))
        await save_user(db_conn, User(telegram_id=222, steam_api_key="key2"))
        await add_target(db_conn, Target(id=0, telegram_id=111, steam_id="111", name="A", active=True))
        await add_target(db_conn, Target(id=0, telegram_id=222, steam_id="111", name="B", active=True))
        await add_target(db_conn, Target(id=0, telegram_id=222, steam_id="222", name="C", active=True))

        changed = await deactivate_targets_by_steam_id(db_conn, "111")
        assert changed == 2

        user_one_targets = await get_targets(db_conn, 111)
        user_two_targets = await get_targets(db_conn, 222)
        assert user_one_targets[0].active is False
        assert user_two_targets[0].active is False
        assert user_two_targets[1].active is True


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
            last_observed_game_name="Counter-Strike 2",
            last_observed_game_time=1705319000,
        )
        await save_target_state(db_conn, state)

        fetched = await get_target_state(db_conn, added.id)
        assert fetched is not None
        assert fetched.persona_state == 1
        assert fetched.persona_name == "Player1"
        assert fetched.playtime_forever == 42
        assert fetched.last_logoff == 1705319400
        assert fetched.last_observed_game_name == "Counter-Strike 2"
        assert fetched.last_observed_game_time == 1705319000

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


@pytest.mark.asyncio
class TestLegacyPlaytimeMigration:
    async def test_get_target_state_normalizes_legacy_minute_based_rows_safely(self, tmp_path):
        import aiosqlite
        import json

        db_path = tmp_path / "legacy.db"
        async with aiosqlite.connect(db_path) as conn:
            await conn.executescript(
                """
                CREATE TABLE users (
                    telegram_id INTEGER PRIMARY KEY,
                    steam_api_key TEXT NOT NULL
                );
                CREATE TABLE targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    steam_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL DEFAULT 30,
                    active INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(telegram_id, steam_id)
                );
                CREATE TABLE target_states (
                    target_id INTEGER PRIMARY KEY,
                    persona_state INTEGER,
                    persona_name TEXT,
                    game_id TEXT,
                    game_name TEXT,
                    playtime_forever INTEGER,
                    last_logoff INTEGER,
                    last_checked INTEGER,
                    last_match_id TEXT,
                    last_match_time INTEGER,
                    game_playtimes TEXT,
                    visibility_state INTEGER,
                    game_start_time INTEGER,
                    last_session_update INTEGER,
                    daily_playtime_snapshot TEXT,
                    FOREIGN KEY (target_id) REFERENCES targets(id)
                );
                """
            )
            await conn.execute(
                "INSERT INTO users (telegram_id, steam_api_key) VALUES (?, ?)",
                (111, "key"),
            )
            await conn.execute(
                "INSERT INTO targets (id, telegram_id, steam_id, name, interval_seconds, active) VALUES (?, ?, ?, ?, ?, ?)",
                (1, 111, "76561198000000001", "Legacy", 30, 1),
            )
            await conn.execute(
                "INSERT INTO target_states (target_id, persona_state, persona_name, game_id, game_name, playtime_forever, last_logoff, last_checked, game_playtimes, visibility_state, daily_playtime_snapshot) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    1,
                    1,
                    "Legacy",
                    "730",
                    "Counter-Strike 2",
                    42,
                    None,
                    1705320000,
                    json.dumps({"730": 30, "570": 12}),
                    3,
                    json.dumps({"730": 10, "570": 2}),
                ),
            )
            await conn.commit()

        async with aiosqlite.connect(db_path) as conn:
            await init_db(conn)

            async with conn.execute(
                "SELECT playtime_forever, game_playtimes, daily_playtime_snapshot, playtime_unit_version FROM target_states WHERE target_id = 1"
            ) as cur:
                raw = await cur.fetchone()
            assert raw == (
                42,
                json.dumps({"730": 30, "570": 12}),
                json.dumps({"730": 10, "570": 2}),
                None,
            )

            state = await get_target_state(conn, 1)
            assert state is not None
            assert state.playtime_forever == 42 * 60
            assert state.playtime_unit_version == 2
            assert json.loads(state.game_playtimes) == {"730": 30 * 60, "570": 12 * 60}
            assert json.loads(state.daily_playtime_snapshot) == {"730": 10 * 60, "570": 2 * 60}

            await save_target_state(conn, state)
            async with conn.execute(
                "SELECT playtime_forever, game_playtimes, daily_playtime_snapshot, playtime_unit_version FROM target_states WHERE target_id = 1"
            ) as cur:
                persisted = await cur.fetchone()
            assert persisted == (
                42 * 60,
                json.dumps({"730": 30 * 60, "570": 12 * 60}),
                json.dumps({"730": 10 * 60, "570": 2 * 60}),
                2,
            )

    async def test_malformed_legacy_playtime_map_stays_unmigrated(self, tmp_path):
        import aiosqlite
        import json

        db_path = tmp_path / "legacy-malformed.db"
        async with aiosqlite.connect(db_path) as conn:
            await conn.executescript(
                """
                CREATE TABLE users (
                    telegram_id INTEGER PRIMARY KEY,
                    steam_api_key TEXT NOT NULL
                );
                CREATE TABLE targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    steam_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL DEFAULT 30,
                    active INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(telegram_id, steam_id)
                );
                CREATE TABLE target_states (
                    target_id INTEGER PRIMARY KEY,
                    persona_state INTEGER,
                    persona_name TEXT,
                    game_id TEXT,
                    game_name TEXT,
                    playtime_forever INTEGER,
                    last_logoff INTEGER,
                    last_checked INTEGER,
                    last_match_id TEXT,
                    last_match_time INTEGER,
                    game_playtimes TEXT,
                    visibility_state INTEGER,
                    game_start_time INTEGER,
                    last_session_update INTEGER,
                    daily_playtime_snapshot TEXT,
                    FOREIGN KEY (target_id) REFERENCES targets(id)
                );
                """
            )
            await conn.execute("INSERT INTO users (telegram_id, steam_api_key) VALUES (?, ?)", (111, "key"))
            await conn.execute(
                "INSERT INTO targets (id, telegram_id, steam_id, name, interval_seconds, active) VALUES (?, ?, ?, ?, ?, ?)",
                (1, 111, "76561198000000001", "Legacy", 30, 1),
            )
            await conn.execute(
                "INSERT INTO target_states (target_id, persona_state, persona_name, game_id, game_name, playtime_forever, last_logoff, last_checked, game_playtimes, visibility_state, daily_playtime_snapshot) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    1,
                    1,
                    "Legacy",
                    "730",
                    "Counter-Strike 2",
                    42,
                    None,
                    1705320000,
                    json.dumps({"730": 30, "570": None}),
                    3,
                    json.dumps({"730": 10, "570": 2}),
                ),
            )
            await conn.commit()

        async with aiosqlite.connect(db_path) as conn:
            await init_db(conn)
            state = await get_target_state(conn, 1)
            assert state is not None
            assert state.playtime_forever == 42
            assert state.playtime_unit_version == 1
            assert state.game_playtimes is not None
            assert state.daily_playtime_snapshot is not None
            assert json.loads(state.game_playtimes) == {"730": 30, "570": None}
            assert json.loads(state.daily_playtime_snapshot) == {"730": 10, "570": 2}


@pytest.mark.asyncio
class TestRemoveTargetCascade:
    async def test_remove_target_with_children(self, db_conn):
        """With foreign_keys=ON, removing a target that has state/settings/
        activity rows must succeed (no FK violation) and clean up children.
        A bare DELETE on targets would raise IntegrityError here."""
        from src.db import save_activity, save_target_settings, get_activity_log

        await save_user(db_conn, User(telegram_id=111, steam_api_key="key"))
        added = await add_target(
            db_conn, Target(id=0, telegram_id=111, steam_id="76561198000000009", name="P")
        )
        await save_target_state(
            db_conn, TargetState(target_id=added.id, persona_state=1, playtime_forever=5)
        )
        await save_target_settings(db_conn, added.id, {"alert_online": False})
        await save_activity(db_conn, added.id, "online")

        removed = await remove_target(db_conn, 111, "76561198000000009")
        assert removed is True
        assert await get_target_state(db_conn, added.id) is None
        assert await get_activity_log(db_conn, added.id) == []
        assert len(await get_targets(db_conn, 111)) == 0

    async def test_remove_nonexistent_returns_false(self, db_conn):
        await save_user(db_conn, User(telegram_id=111, steam_api_key="key"))
        assert await remove_target(db_conn, 111, "76561198000000404") is False
