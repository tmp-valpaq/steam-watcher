import aiosqlite
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "steam_watcher.db")


async def get_db():
    return await aiosqlite.connect(DB_PATH)


async def init_db():
    db = await get_db()
    with open(os.path.join(os.path.dirname(__file__), "schema.sql")) as f:
        await db.executescript(f.read())
    await db.commit()
    await db.close()


# === User ops ===

async def register_user(tg_id: int, api_key: str):
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO users (tg_id, steam_api_key) VALUES (?, ?)",
        (tg_id, api_key),
    )
    await db.commit()
    await db.close()


async def get_user(tg_id: int):
    db = await get_db()
    cur = await db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
    row = await cur.fetchone()
    await db.close()
    if row:
        return {"tg_id": row[0], "steam_api_key": row[1]}
    return None


# === Target ops ===

async def add_target(tg_id: int, steam_id: str, name: str, interval: int = 30):
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO targets (tg_id, steam_id, name, watch_interval) VALUES (?, ?, ?, ?)",
            (tg_id, steam_id, name, interval),
        )
        target_id = cur.lastrowid
        await db.execute(
            "INSERT OR IGNORE INTO target_state (target_id) VALUES (?)", (target_id,)
        )
        await db.commit()
        await db.close()
        return target_id
    except aiosqlite.IntegrityError:
        await db.close()
        return None


async def remove_target(tg_id: int, steam_id: str):
    db = await get_db()
    await db.execute(
        "DELETE FROM targets WHERE tg_id = ? AND steam_id = ?", (tg_id, steam_id)
    )
    await db.commit()
    await db.close()


async def list_targets(tg_id: int):
    db = await get_db()
    cur = await db.execute(
        "SELECT t.id, t.steam_id, t.name, t.watch_interval, t.is_active, "
        "ts.persona_state, ts.current_game, ts.playtime_map "
        "FROM targets t LEFT JOIN target_state ts ON t.id = ts.target_id "
        "WHERE t.tg_id = ?",
        (tg_id,),
    )
    rows = await cur.fetchall()
    await db.close()
    return [
        {
            "id": r[0],
            "steam_id": r[1],
            "name": r[2],
            "interval": r[3],
            "is_active": bool(r[4]),
            "persona_state": r[5],
            "current_game": r[6],
            "playtime_map": r[7],
        }
        for r in rows
    ]


async def get_all_active_targets():
    """Get all active targets grouped by user (for background polling)."""
    db = await get_db()
    cur = await db.execute(
        "SELECT t.id, t.tg_id, t.steam_id, t.name, t.watch_interval, u.steam_api_key, "
        "ts.persona_state, ts.current_game, ts.playtime_map "
        "FROM targets t "
        "JOIN users u ON t.tg_id = u.tg_id "
        "LEFT JOIN target_state ts ON t.id = ts.target_id "
        "WHERE t.is_active = 1"
    )
    rows = await cur.fetchall()
    await db.close()
    return [
        {
            "id": r[0],
            "tg_id": r[1],
            "steam_id": r[2],
            "name": r[3],
            "interval": r[4],
            "api_key": r[5],
            "persona_state": r[6],
            "current_game": r[7],
            "playtime_map": r[8],
        }
        for r in rows
    ]


async def update_state(
    target_id: int,
    persona_state: int,
    current_game: str | None,
    playtime_map: dict,
):
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO target_state (target_id, persona_state, current_game, playtime_map) "
        "VALUES (?, ?, ?, ?)",
        (target_id, persona_state, current_game, json_dumps(playtime_map)),
    )
    await db.commit()
    await db.close()


async def toggle_target(tg_id: int, steam_id: str, active: bool):
    db = await get_db()
    await db.execute(
        "UPDATE targets SET is_active = ? WHERE tg_id = ? AND steam_id = ?",
        (int(active), tg_id, steam_id),
    )
    await db.commit()
    await db.close()


def json_dumps(obj):
    import json

    return json.dumps(obj)
