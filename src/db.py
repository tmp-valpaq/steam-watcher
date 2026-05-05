import logging
from typing import Optional, List

import aiosqlite

from .models import User, Target, TargetState

logger = logging.getLogger(__name__)


async def init_db(db: aiosqlite.Connection) -> None:
    """Initialize the database schema from schema.sql."""
    import os
    schema_path = os.path.join(os.path.dirname(__file__), "..", "schema.sql")
    async with db.cursor() as cur:
        with open(schema_path, "r") as f:
            await cur.executescript(f.read())
    await db.commit()


# ── User CRUD ──────────────────────────────────────────────────────────

async def get_user(db: aiosqlite.Connection, telegram_id: int) -> Optional[User]:
    async with db.execute(
        "SELECT telegram_id, steam_api_key FROM users WHERE telegram_id = ?",
        (telegram_id,),
    ) as cur:
        row = await cur.fetchone()
        if row is None:
            return None
        return User(telegram_id=row[0], steam_api_key=row[1])


async def save_user(db: aiosqlite.Connection, user: User) -> None:
    await db.execute(
        "INSERT INTO users (telegram_id, steam_api_key) VALUES (?, ?) "
        "ON CONFLICT(telegram_id) DO UPDATE SET steam_api_key = excluded.steam_api_key",
        (user.telegram_id, user.steam_api_key),
    )
    await db.commit()


# ── Target CRUD ────────────────────────────────────────────────────────

async def add_target(db: aiosqlite.Connection, target: Target) -> Target:
    cursor = await db.execute(
        "INSERT INTO targets (telegram_id, steam_id, name, interval_seconds, active) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            target.telegram_id,
            target.steam_id,
            target.name,
            target.interval_seconds,
            1 if target.active else 0,
        ),
    )
    target.id = cursor.lastrowid
    await db.commit()
    return target


async def remove_target(db: aiosqlite.Connection, telegram_id: int, steam_id: str) -> bool:
    cursor = await db.execute(
        "DELETE FROM targets WHERE telegram_id = ? AND steam_id = ?",
        (telegram_id, steam_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_targets(db: aiosqlite.Connection, telegram_id: int) -> List[Target]:
    async with db.execute(
        "SELECT id, telegram_id, steam_id, name, interval_seconds, active "
        "FROM targets WHERE telegram_id = ?",
        (telegram_id,),
    ) as cur:
        rows = await cur.fetchall()
        return [
            Target(
                id=r[0],
                telegram_id=r[1],
                steam_id=r[2],
                name=r[3],
                interval_seconds=r[4],
                active=bool(r[5]),
            )
            for r in rows
        ]


async def get_active_targets(db: aiosqlite.Connection) -> List[Target]:
    async with db.execute(
        "SELECT id, telegram_id, steam_id, name, interval_seconds, active "
        "FROM targets WHERE active = 1"
    ) as cur:
        rows = await cur.fetchall()
        return [
            Target(
                id=r[0],
                telegram_id=r[1],
                steam_id=r[2],
                name=r[3],
                interval_seconds=r[4],
                active=bool(r[5]),
            )
            for r in rows
        ]


async def set_target_active(
    db: aiosqlite.Connection, telegram_id: int, steam_id: str, active: bool
) -> bool:
    cursor = await db.execute(
        "UPDATE targets SET active = ? WHERE telegram_id = ? AND steam_id = ?",
        (1 if active else 0, telegram_id, steam_id),
    )
    await db.commit()
    return cursor.rowcount > 0


# ── TargetState CRUD ───────────────────────────────────────────────────

async def get_target_state(db: aiosqlite.Connection, target_id: int) -> Optional[TargetState]:
    async with db.execute(
        "SELECT target_id, persona_state, persona_name, game_id, game_name, "
        "playtime_forever, last_logoff, last_checked "
        "FROM target_states WHERE target_id = ?",
        (target_id,),
    ) as cur:
        row = await cur.fetchone()
        if row is None:
            return None
        return TargetState(
            target_id=row[0],
            persona_state=row[1],
            persona_name=row[2],
            game_id=row[3],
            game_name=row[4],
            playtime_forever=row[5],
            last_logoff=row[6],
            last_checked=row[7],
        )


async def save_target_state(db: aiosqlite.Connection, state: TargetState) -> None:
    await db.execute(
        "INSERT INTO target_states "
        "(target_id, persona_state, persona_name, game_id, game_name, "
        "playtime_forever, last_logoff, last_checked) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(target_id) DO UPDATE SET "
        "persona_state = excluded.persona_state, "
        "persona_name = excluded.persona_name, "
        "game_id = excluded.game_id, "
        "game_name = excluded.game_name, "
        "playtime_forever = excluded.playtime_forever, "
        "last_logoff = excluded.last_logoff, "
        "last_checked = excluded.last_checked",
        (
            state.target_id,
            state.persona_state,
            state.persona_name,
            state.game_id,
            state.game_name,
            state.playtime_forever,
            state.last_logoff,
            state.last_checked,
        ),
    )
    await db.commit()
