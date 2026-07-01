import datetime
import json
import logging
import os
import time
from datetime import timezone as _stdlib_timezone
from typing import Optional, List

import aiosqlite

from .models import User, Target, TargetState, UserSettings, SteamProfileBlacklistEntry

logger = logging.getLogger(__name__)


async def init_db(db: aiosqlite.Connection) -> None:
    """Initialize the database schema from schema.sql and add missing columns."""
    # Pragmatic durability/concurrency settings for the single shared connection
    # used by both the watcher and the bot. WAL allows concurrent reads while a
    # write is in progress; busy_timeout makes a brief lock wait instead of
    # erroring; foreign_keys enforces the FK constraints declared in schema.sql
    # (SQLite ignores them unless explicitly enabled). For :memory: databases
    # (used in tests) journal_mode=WAL is a no-op and returns 'memory' without
    # erroring, so this is safe everywhere.
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")
    await db.execute("PRAGMA foreign_keys=ON")

    schema_path = os.path.join(os.path.dirname(__file__), "..", "schema.sql")
    async with db.cursor() as cur:
        with open(schema_path, "r") as f:
            await cur.executescript(f.read())

    # Migrate: add columns that may be missing from older DBs.
    # playtime_unit_version is handled separately: on existing DBs we add it as a
    # nullable column so legacy/versionless rows are not silently stamped as v2.
    _TARGET_STATE_COLUMNS = [
        ("last_match_id", "TEXT"),
        ("last_match_time", "INTEGER"),
        ("game_playtimes", "TEXT"),
        ("visibility_state", "INTEGER DEFAULT 3"),
        ("game_start_time", "INTEGER"),
        ("last_session_update", "INTEGER"),
        ("daily_playtime_snapshot", "TEXT"),
        ("last_observed_game_name", "TEXT"),
        ("last_observed_game_time", "INTEGER"),
    ]
    async with db.cursor() as cur:
        # Get existing columns
        await cur.execute("PRAGMA table_info(target_states)")
        existing = {row[1] for row in await cur.fetchall()}
        for col_name, col_type in _TARGET_STATE_COLUMNS:
            if col_name not in existing:
                await cur.execute(
                    f"ALTER TABLE target_states ADD COLUMN {col_name} {col_type}"
                )
        if "playtime_unit_version" not in existing:
            await cur.execute(
                "ALTER TABLE target_states ADD COLUMN playtime_unit_version INTEGER"
            )
        await cur.execute(
            """
            UPDATE target_states
            SET last_observed_game_name = COALESCE(last_observed_game_name, (
                    SELECT a.game_name
                    FROM activity_log a
                    WHERE a.target_id = target_states.target_id
                      AND a.game_name IS NOT NULL
                    ORDER BY a.detected_at DESC
                    LIMIT 1
                )),
                last_observed_game_time = COALESCE(last_observed_game_time, (
                    SELECT a.detected_at
                    FROM activity_log a
                    WHERE a.target_id = target_states.target_id
                      AND a.game_name IS NOT NULL
                    ORDER BY a.detected_at DESC
                    LIMIT 1
                ))
            WHERE last_observed_game_name IS NULL OR last_observed_game_time IS NULL
            """
        )

        # Migrate user_settings: add timezone column if missing
        await cur.execute("PRAGMA table_info(user_settings)")
        existing_us = {row[1] for row in await cur.fetchall()}
        if "timezone" not in existing_us:
            await cur.execute(
                "ALTER TABLE user_settings ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC'"
            )
        await cur.execute(
            "CREATE TABLE IF NOT EXISTS steam_profile_blacklist ("
            "steam_id TEXT PRIMARY KEY, "
            "reason TEXT, "
            "created_at INTEGER NOT NULL, "
            "created_by INTEGER"
            ")"
        )
    await db.commit()


def _loads_playtime_map(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    converted = {}
    for key, value in data.items():
        try:
            converted[str(key)] = int(value)
        except (TypeError, ValueError):
            return None
    return converted


def _dumps_playtime_map(data: Optional[dict], fallback_raw: Optional[str]) -> Optional[str]:
    if data is None:
        return fallback_raw
    return json.dumps(data)


def _normalize_legacy_playtime_payload(
    playtime_forever: Optional[int],
    game_playtimes: Optional[str],
    daily_snapshot: Optional[str],
    playtime_unit_version: Optional[int],
) -> tuple[int, Optional[str], Optional[str], int]:
    version = playtime_unit_version if playtime_unit_version is not None else 1
    normalized_playtime = playtime_forever or 0
    if version >= 2:
        return normalized_playtime, game_playtimes, daily_snapshot, version

    current_pts = _loads_playtime_map(game_playtimes)
    snapshot_pts = _loads_playtime_map(daily_snapshot)
    if (game_playtimes and current_pts is None) or (daily_snapshot and snapshot_pts is None):
        # Refuse partial/ambiguous migration. Keep the row legacy-marked so a
        # later explicit cleanup can fix the malformed payload without data loss.
        return normalized_playtime, game_playtimes, daily_snapshot, version

    normalized_playtime *= 60
    normalized_game_pts = _dumps_playtime_map(
        {appid: secs * 60 for appid, secs in current_pts.items()} if current_pts is not None else None,
        game_playtimes,
    )
    normalized_snapshot = _dumps_playtime_map(
        {appid: secs * 60 for appid, secs in snapshot_pts.items()} if snapshot_pts is not None else None,
        daily_snapshot,
    )
    return normalized_playtime, normalized_game_pts, normalized_snapshot, 2


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
    # Ensure a default user_settings row exists so this user is eligible for
    # daily summaries (which default to on) even if they never open settings.
    await ensure_user_settings(db, target.telegram_id)
    return target


async def remove_target(db: aiosqlite.Connection, telegram_id: int, steam_id: str) -> bool:
    """Remove a target together with its dependent rows.

    foreign_keys=ON is enabled (see init_db) but the schema FKs have no
    ON DELETE CASCADE, so a bare DELETE on targets would fail once child rows
    exist (state/settings/activity after the first poll). Delete children
    explicitly — this also prevents orphan rows that used to accumulate.
    """
    async with db.execute(
        "SELECT id FROM targets WHERE telegram_id = ? AND steam_id = ?",
        (telegram_id, steam_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return False
    target_id = row[0]
    await db.execute("DELETE FROM target_states WHERE target_id = ?", (target_id,))
    await db.execute("DELETE FROM target_settings WHERE target_id = ?", (target_id,))
    await db.execute("DELETE FROM activity_log WHERE target_id = ?", (target_id,))
    await db.execute("DELETE FROM targets WHERE id = ?", (target_id,))
    await db.commit()
    return True


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


async def get_target_by_id(db: aiosqlite.Connection, target_id: int, telegram_id: int) -> Optional[Target]:
    """Get a single target by ID, verifying ownership via telegram_id."""
    async with db.execute(
        "SELECT id, telegram_id, steam_id, name, interval_seconds, active "
        "FROM targets WHERE id = ? AND telegram_id = ?",
        (target_id, telegram_id),
    ) as cur:
        row = await cur.fetchone()
        if row is None:
            return None
        return Target(
            id=row[0], telegram_id=row[1], steam_id=row[2],
            name=row[3], interval_seconds=row[4], active=bool(row[5]),
        )


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


async def set_all_targets_active(
    db: aiosqlite.Connection, telegram_id: int, active: bool
) -> int:
    cursor = await db.execute(
        "UPDATE targets SET active = ? WHERE telegram_id = ?",
        (1 if active else 0, telegram_id),
    )
    await db.commit()
    return cursor.rowcount


async def rename_target(
    db: aiosqlite.Connection, telegram_id: int, steam_id: str, new_name: str
) -> bool:
    cursor = await db.execute(
        "UPDATE targets SET name = ? WHERE telegram_id = ? AND steam_id = ?",
        (new_name, telegram_id, steam_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def set_target_interval(
    db: aiosqlite.Connection,
    telegram_id: int,
    steam_id: str,
    interval_seconds: int,
) -> bool:
    cursor = await db.execute(
        "UPDATE targets SET interval_seconds = ? WHERE telegram_id = ? AND steam_id = ?",
        (interval_seconds, telegram_id, steam_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def is_steam_profile_blacklisted(db: aiosqlite.Connection, steam_id: str) -> bool:
    async with db.execute(
        "SELECT 1 FROM steam_profile_blacklist WHERE steam_id = ?",
        (steam_id,),
    ) as cur:
        return await cur.fetchone() is not None


async def get_steam_profile_blacklist_entry(
    db: aiosqlite.Connection,
    steam_id: str,
) -> Optional[SteamProfileBlacklistEntry]:
    async with db.execute(
        "SELECT steam_id, reason, created_at, created_by "
        "FROM steam_profile_blacklist WHERE steam_id = ?",
        (steam_id,),
    ) as cur:
        row = await cur.fetchone()
        if row is None:
            return None
        return SteamProfileBlacklistEntry(
            steam_id=row[0],
            reason=row[1],
            created_at=row[2],
            created_by=row[3],
        )


async def add_steam_profile_blacklist_entry(
    db: aiosqlite.Connection,
    entry: SteamProfileBlacklistEntry,
) -> None:
    await db.execute(
        "INSERT INTO steam_profile_blacklist (steam_id, reason, created_at, created_by) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(steam_id) DO UPDATE SET "
        "reason = excluded.reason, "
        "created_at = excluded.created_at, "
        "created_by = excluded.created_by",
        (entry.steam_id, entry.reason, entry.created_at, entry.created_by),
    )
    await db.commit()


async def remove_steam_profile_blacklist_entry(
    db: aiosqlite.Connection,
    steam_id: str,
) -> bool:
    cursor = await db.execute(
        "DELETE FROM steam_profile_blacklist WHERE steam_id = ?",
        (steam_id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def deactivate_targets_by_steam_id(
    db: aiosqlite.Connection,
    steam_id: str,
) -> int:
    cursor = await db.execute(
        "UPDATE targets SET active = 0 WHERE steam_id = ? AND active != 0",
        (steam_id,),
    )
    await db.commit()
    return cursor.rowcount


# ── TargetState CRUD ───────────────────────────────────────────────────

async def get_target_state(db: aiosqlite.Connection, target_id: int) -> Optional[TargetState]:
    async with db.execute(
        "SELECT target_id, persona_state, persona_name, game_id, game_name, "
        "playtime_forever, last_logoff, last_checked, last_match_id, last_match_time, "
        "game_playtimes, visibility_state, game_start_time, last_session_update, "
        "daily_playtime_snapshot, last_observed_game_name, last_observed_game_time, "
        "playtime_unit_version "
        "FROM target_states WHERE target_id = ?",
        (target_id,),
    ) as cur:
        row = await cur.fetchone()
        if row is None:
            return None
        playtime_forever, game_playtimes, daily_snapshot, version = _normalize_legacy_playtime_payload(
            row[5], row[10], row[14], row[17]
        )
        return TargetState(
            target_id=row[0],
            persona_state=row[1],
            persona_name=row[2],
            game_id=row[3],
            game_name=row[4],
            playtime_forever=playtime_forever,
            last_logoff=row[6],
            last_checked=row[7],
            last_match_id=row[8],
            last_match_time=row[9],
            game_playtimes=game_playtimes,
            visibility_state=row[11] if row[11] is not None else 3,
            game_start_time=row[12],
            last_session_update=row[13],
            daily_playtime_snapshot=daily_snapshot,
            last_observed_game_name=row[15],
            last_observed_game_time=row[16],
            playtime_unit_version=version,
        )


async def save_target_state(db: aiosqlite.Connection, state: TargetState) -> None:
    await db.execute(
        "INSERT INTO target_states "
        "(target_id, persona_state, persona_name, game_id, game_name, "
        "playtime_forever, last_logoff, last_checked, last_match_id, last_match_time, "
        "game_playtimes, visibility_state, game_start_time, last_session_update, "
        "daily_playtime_snapshot, last_observed_game_name, last_observed_game_time, playtime_unit_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(target_id) DO UPDATE SET "
        "persona_state = excluded.persona_state, "
        "persona_name = excluded.persona_name, "
        "game_id = excluded.game_id, "
        "game_name = excluded.game_name, "
        "playtime_forever = excluded.playtime_forever, "
        "last_logoff = excluded.last_logoff, "
        "last_checked = excluded.last_checked, "
        "last_match_id = excluded.last_match_id, "
        "last_match_time = excluded.last_match_time, "
        "game_playtimes = excluded.game_playtimes, "
        "visibility_state = excluded.visibility_state, "
        "game_start_time = excluded.game_start_time, "
        "last_session_update = excluded.last_session_update, "
        "daily_playtime_snapshot = excluded.daily_playtime_snapshot, "
        "last_observed_game_name = excluded.last_observed_game_name, "
        "last_observed_game_time = excluded.last_observed_game_time, "
        "playtime_unit_version = excluded.playtime_unit_version",
        (
            state.target_id,
            state.persona_state,
            state.persona_name,
            state.game_id,
            state.game_name,
            state.playtime_forever,
            state.last_logoff,
            state.last_checked,
            state.last_match_id,
            state.last_match_time,
            state.game_playtimes,
            state.visibility_state,
            state.game_start_time,
            state.last_session_update,
            state.daily_playtime_snapshot,
            state.last_observed_game_name,
            state.last_observed_game_time,
            state.playtime_unit_version,
        ),
    )
    await db.commit()


# ── UserSettings CRUD ──────────────────────────────────────────────────

async def get_user_settings(db: aiosqlite.Connection, telegram_id: int) -> UserSettings:
    """Get user settings, returning defaults if not found."""
    async with db.execute(
        "SELECT telegram_id, daily_summary_enabled, daily_summary_time, "
        "session_updates_enabled, session_update_interval, "
        "privacy_alerts_enabled, last_summary_date, timezone "
        "FROM user_settings WHERE telegram_id = ?",
        (telegram_id,),
    ) as cur:
        row = await cur.fetchone()
        if row is None:
            return UserSettings(telegram_id=telegram_id)
        return UserSettings(
            telegram_id=row[0],
            daily_summary_enabled=bool(row[1]),
            daily_summary_time=row[2],
            session_updates_enabled=bool(row[3]),
            session_update_interval=row[4],
            privacy_alerts_enabled=bool(row[5]),
            last_summary_date=row[6] or "",
            timezone=row[7] or "UTC",
        )


async def ensure_user_settings(db: aiosqlite.Connection, telegram_id: int) -> None:
    """Ensure a user_settings row exists for this user, using schema defaults.

    Uses INSERT OR IGNORE so existing rows (with user customisations) are
    untouched. This guarantees default-on features like the daily summary
    apply to users who never opened the settings menu.
    """
    await db.execute(
        "INSERT OR IGNORE INTO user_settings (telegram_id) VALUES (?)",
        (telegram_id,),
    )
    await db.commit()


async def save_user_settings(db: aiosqlite.Connection, settings: UserSettings) -> None:
    """UPSERT user settings."""
    await db.execute(
        "INSERT INTO user_settings "
        "(telegram_id, daily_summary_enabled, daily_summary_time, "
        "session_updates_enabled, session_update_interval, "
        "privacy_alerts_enabled, last_summary_date, timezone) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(telegram_id) DO UPDATE SET "
        "daily_summary_enabled = excluded.daily_summary_enabled, "
        "daily_summary_time = excluded.daily_summary_time, "
        "session_updates_enabled = excluded.session_updates_enabled, "
        "session_update_interval = excluded.session_update_interval, "
        "privacy_alerts_enabled = excluded.privacy_alerts_enabled, "
        "last_summary_date = excluded.last_summary_date, "
        "timezone = excluded.timezone",
        (
            settings.telegram_id,
            1 if settings.daily_summary_enabled else 0,
            settings.daily_summary_time,
            1 if settings.session_updates_enabled else 0,
            settings.session_update_interval,
            1 if settings.privacy_alerts_enabled else 0,
            settings.last_summary_date,
            settings.timezone,
        ),
    )
    await db.commit()


async def get_users_due_summary(db: aiosqlite.Connection) -> List[tuple]:
    """Get users whose daily summary is due, evaluated in each user's timezone.
    Returns list of (telegram_id, daily_summary_time, timezone) tuples.

    Users who have targets but no user_settings row are synthesised with
    schema defaults (daily_summary_enabled on) so they receive summaries too.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    dt_cls = datetime.datetime if hasattr(datetime, "datetime") else datetime
    now_utc = dt_cls.now(tz=_stdlib_timezone.utc)
    # Union of users with a settings row (summary enabled) and users who own at
    # least one target but have no settings row yet (treated as defaults-on).
    # COALESCE supplies schema defaults for the no-row case.
    async with db.execute(
        "SELECT t.telegram_id, "
        "COALESCE(us.daily_summary_time, '21:00'), "
        "COALESCE(us.timezone, 'UTC'), "
        "COALESCE(us.last_summary_date, '') "
        "FROM (SELECT DISTINCT telegram_id FROM targets) AS t "
        "LEFT JOIN user_settings AS us ON us.telegram_id = t.telegram_id "
        "WHERE COALESCE(us.daily_summary_enabled, 1) = 1 "
        "UNION "
        "SELECT us.telegram_id, us.daily_summary_time, us.timezone, "
        "COALESCE(us.last_summary_date, '') "
        "FROM user_settings AS us "
        "WHERE us.daily_summary_enabled = 1"
    ) as cur:
        rows = await cur.fetchall()

    due: List[tuple] = []
    for telegram_id, summary_time, tz_name, last_summary_date in rows:
        try:
            tz = ZoneInfo(tz_name or "UTC")
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")
        now_local = now_utc.astimezone(tz)
        current_time = now_local.strftime("%H:%M")
        today = now_local.strftime("%Y-%m-%d")
        if summary_time <= current_time and (last_summary_date or "") != today:
            due.append((telegram_id, summary_time, tz_name or "UTC"))
    return due


async def mark_summary_sent(db: aiosqlite.Connection, telegram_id: int, date_str: str) -> None:
    """Mark that a daily summary has been sent for a user on a given date.

    Uses an UPSERT so it also works for users whose summary was synthesised
    from defaults and who don't yet have a user_settings row.
    """
    await db.execute(
        "INSERT INTO user_settings (telegram_id, last_summary_date) VALUES (?, ?) "
        "ON CONFLICT(telegram_id) DO UPDATE SET last_summary_date = excluded.last_summary_date",
        (telegram_id, date_str),
    )
    await db.commit()


# ── TargetSettings CRUD ──────────────────────────────────────────

_TARGET_SETTINGS_COLUMNS = [
    "alert_online", "alert_game_start", "alert_invisible",
    "alert_privacy", "alert_mmr", "alert_session", "alert_name_change",
]

_TARGET_SETTINGS_DEFAULTS = {
    "alert_online": True,
    "alert_game_start": True,
    "alert_invisible": True,
    "alert_privacy": True,
    "alert_mmr": True,
    "alert_session": True,
    "alert_name_change": True,
}


async def get_target_settings(db: aiosqlite.Connection, target_id: int) -> dict:
    """Get target-specific alert settings, returning defaults if not found."""
    async with db.execute(
        "SELECT target_id, alert_online, alert_game_start, alert_invisible, "
        "alert_privacy, alert_mmr, alert_session, alert_name_change "
        "FROM target_settings WHERE target_id = ?",
        (target_id,),
    ) as cur:
        row = await cur.fetchone()
        if row is None:
            return dict(_TARGET_SETTINGS_DEFAULTS)
        return {
            "alert_online": bool(row[1]),
            "alert_game_start": bool(row[2]),
            "alert_invisible": bool(row[3]),
            "alert_privacy": bool(row[4]),
            "alert_mmr": bool(row[5]),
            "alert_session": bool(row[6]),
            "alert_name_change": bool(row[7]),
        }


# ── ActivityLog ──────────────────────────────────────────────────────

async def save_activity(
    db: aiosqlite.Connection,
    target_id: int,
    event_type: str,
    *,
    game_name: str = None,
    match_id: str = None,
    hero_name: str = None,
    duration_seconds: int = None,
    details: str = None,
    detected_at: int = None,
) -> None:
    """Log a detected activity event. detected_at defaults to now."""
    if detected_at is None:
        detected_at = int(time.time())
    await db.execute(
        "INSERT INTO activity_log "
        "(target_id, event_type, game_name, match_id, hero_name, duration_seconds, details, detected_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (target_id, event_type, game_name, match_id, hero_name, duration_seconds, details, detected_at),
    )
    await db.commit()


async def has_activity(
    db: aiosqlite.Connection,
    target_id: int,
    event_type: str,
    *,
    match_id: Optional[str] = None,
) -> bool:
    """True if an activity row already exists for this target/event identity."""
    query = "SELECT 1 FROM activity_log WHERE target_id = ? AND event_type = ?"
    params: list = [target_id, event_type]
    if match_id is None:
        query += " AND match_id IS NULL"
    else:
        query += " AND match_id = ?"
        params.append(match_id)
    query += " LIMIT 1"
    async with db.execute(query, params) as cur:
        return await cur.fetchone() is not None


async def save_activity_batch(
    db: aiosqlite.Connection,
    entries: list,
) -> None:
    """Log multiple activity events in a single transaction.
    entries: list of dicts with keys target_id, event_type, and optional
              game_name, match_id, hero_name, duration_seconds, details, detected_at.
    """
    if not entries:
        return
    now = int(time.time())
    rows = []
    for e in entries:
        detected_at = e.get("detected_at") or now
        rows.append((
            e["target_id"], e["event_type"],
            e.get("game_name"), e.get("match_id"),
            e.get("hero_name"), e.get("duration_seconds"),
            e.get("details"), detected_at,
        ))
    await db.executemany(
        "INSERT INTO activity_log "
        "(target_id, event_type, game_name, match_id, hero_name, duration_seconds, details, detected_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    await db.commit()


async def cleanup_activity_log(
    db: aiosqlite.Connection,
    retention_days: int = 180,
) -> int:
    """Delete activity_log entries older than retention_days. Returns count deleted."""
    cutoff = int(time.time()) - (retention_days * 86400)
    cursor = await db.execute(
        "DELETE FROM activity_log WHERE detected_at < ?", (cutoff,)
    )
    await db.commit()
    return cursor.rowcount


async def get_activity_log(
    db: aiosqlite.Connection,
    target_id: int,
    limit: int = 20,
    event_types: list = None,
) -> list:
    """Get recent activity log entries, newest first.
    Returns list of dicts with keys matching the columns."""
    query = (
        "SELECT id, event_type, game_name, match_id, hero_name, "
        "duration_seconds, details, detected_at "
        "FROM activity_log WHERE target_id = ?"
    )
    params: list = [target_id]
    if event_types:
        placeholders = ",".join("?" for _ in event_types)
        query += f" AND event_type IN ({placeholders})"
        params.extend(event_types)
    query += " ORDER BY detected_at DESC LIMIT ?"
    params.append(limit)

    cols = ["id", "event_type", "game_name", "match_id", "hero_name",
            "duration_seconds", "details", "detected_at"]
    rows = []
    async with db.execute(query, params) as cur:
        async for row in cur:
            rows.append(dict(zip(cols, row)))
    return rows


# ── TargetSettings CRUD ──────────────────────────────────────────

async def save_target_settings(db: aiosqlite.Connection, target_id: int, settings: dict) -> None:
    """UPSERT target-specific alert settings."""
    await db.execute(
        "INSERT INTO target_settings "
        "(target_id, alert_online, alert_game_start, alert_invisible, "
        "alert_privacy, alert_mmr, alert_session, alert_name_change) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(target_id) DO UPDATE SET "
        "alert_online = excluded.alert_online, "
        "alert_game_start = excluded.alert_game_start, "
        "alert_invisible = excluded.alert_invisible, "
        "alert_privacy = excluded.alert_privacy, "
        "alert_mmr = excluded.alert_mmr, "
        "alert_session = excluded.alert_session, "
        "alert_name_change = excluded.alert_name_change",
        (
            target_id,
            1 if settings.get("alert_online", True) else 0,
            1 if settings.get("alert_game_start", True) else 0,
            1 if settings.get("alert_invisible", True) else 0,
            1 if settings.get("alert_privacy", True) else 0,
            1 if settings.get("alert_mmr", True) else 0,
            1 if settings.get("alert_session", True) else 0,
            1 if settings.get("alert_name_change", True) else 0,
        ),
    )
    await db.commit()
