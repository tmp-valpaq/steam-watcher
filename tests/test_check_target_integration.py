"""Integration tests exercising the real _check_target / poll path.

These cover the interrelated playtime-accumulation bugs:
  - playtime must accumulate across consecutive polls for an active target
  - the daily summary must be non-empty after accumulated play
  - a user with a target but no user_settings row must still be due a summary

The stored unit for game_playtimes is SECONDS; readers convert to minutes
for display.
"""

import datetime as _dtmod
import json
from unittest.mock import AsyncMock, MagicMock

import pytest


class _FixedDateTime(_dtmod.datetime):
    """datetime subclass whose .now() returns a fixed instant (for due tests)."""

    @classmethod
    def now(cls, tz=None):
        # 23:30 — comfortably past the default 21:00 summary time in any tz
        # offset we test with (UTC), making summaries deterministically due.
        return _dtmod.datetime(2026, 6, 4, 23, 30, tzinfo=tz)

from src import db
from src.models import Target, User, SteamProfile, UserSettings
from src.watcher import Watcher


def _profile(
    steam_id="76561198000000001",
    persona_name="TestUser",
    persona_state=1,
    game_id=None,
    game_name=None,
    last_logoff=None,
):
    return SteamProfile(
        steam_id=steam_id,
        persona_name=persona_name,
        persona_state=persona_state,
        game_id=game_id,
        game_name=game_name,
        last_logoff=last_logoff,
        visibility_state=3,
    )


def _make_watcher(db_conn, sent):
    """Build a Watcher with a stub SteamClient and a capturing send_alert."""
    steam = MagicMock()
    steam.get_player_summaries = AsyncMock(return_value=None)
    steam.get_player_summaries_batch = AsyncMock(return_value={})

    async def _send(telegram_id, message):
        sent.append((telegram_id, message))

    return Watcher(db_conn=db_conn, steam_client=steam, send_alert=_send)


async def _setup_target(db_conn, telegram_id=555, steam_id="76561198000000001"):
    await db.save_user(db_conn, User(telegram_id=telegram_id, steam_api_key="k"))
    target = Target(
        id=0, telegram_id=telegram_id, steam_id=steam_id,
        name="Victim", interval_seconds=30, active=True,
    )
    return await db.add_target(db_conn, target)


@pytest.mark.asyncio
class TestPlaytimeAccumulation:
    async def test_playtime_accumulates_across_consecutive_polls(self, db_conn, monkeypatch):
        target = await _setup_target(db_conn)
        sent = []
        watcher = _make_watcher(db_conn, sent)

        # Control wall-clock so elapsed between polls is deterministic.
        clock = {"now": 1_700_000_000}
        monkeypatch.setattr("src.watcher.time.time", lambda: clock["now"])

        prof = _profile(game_id="730", game_name="Counter-Strike 2", persona_state=1)

        # First poll: establishes baseline, no previous state -> elapsed 0.
        await watcher._check_target("k", target, prof)
        state = await db.get_target_state(db_conn, target.id)
        pts = json.loads(state.game_playtimes)
        assert pts.get("730", 0) == 0  # no elapsed yet

        # Second poll 30s later -> +30s.
        clock["now"] += 30
        await watcher._check_target("k", target, prof)
        state = await db.get_target_state(db_conn, target.id)
        pts = json.loads(state.game_playtimes)
        assert pts["730"] == 30

        # Third poll another 30s later -> +30s = 60s total.
        clock["now"] += 30
        await watcher._check_target("k", target, prof)
        state = await db.get_target_state(db_conn, target.id)
        pts = json.loads(state.game_playtimes)
        assert pts["730"] == 60
        assert state.playtime_forever == 60

    async def test_increment_capped_on_long_downtime(self, db_conn, monkeypatch):
        target = await _setup_target(db_conn)
        sent = []
        watcher = _make_watcher(db_conn, sent)

        clock = {"now": 1_700_000_000}
        monkeypatch.setattr("src.watcher.time.time", lambda: clock["now"])

        prof = _profile(game_id="730", game_name="Counter-Strike 2", persona_state=1)
        await watcher._check_target("k", target, prof)

        # Simulate the bot being down for an hour; elapsed must be capped to
        # interval_seconds * 3 (= 90s) so playtime doesn't explode.
        clock["now"] += 3600
        await watcher._check_target("k", target, prof)
        state = await db.get_target_state(db_conn, target.id)
        pts = json.loads(state.game_playtimes)
        assert pts["730"] == target.interval_seconds * 3

    async def test_playtime_accumulates_while_offline_invisible(self, db_conn, monkeypatch):
        """Steam may expose a game while persona_state == 0 (invisible).

        Playtime must still accumulate so detect_invisible() can fire.
        """
        target = await _setup_target(db_conn)
        sent = []
        watcher = _make_watcher(db_conn, sent)

        clock = {"now": 1_700_000_000}
        monkeypatch.setattr("src.watcher.time.time", lambda: clock["now"])

        # Offline (persona_state == 0) but Steam exposes a game id.
        prof = _profile(game_id="570", game_name="Dota 2", persona_state=0)
        await watcher._check_target("k", target, prof)

        clock["now"] += 30
        await watcher._check_target("k", target, prof)

        state = await db.get_target_state(db_conn, target.id)
        pts = json.loads(state.game_playtimes)
        assert pts["570"] == 30  # accumulated despite being "offline"

        # The 3rd poll should now produce an invisible alert (playtime grew
        # while offline).
        clock["now"] += 30
        sent.clear()
        await watcher._check_target("k", target, prof)
        assert any("НЕВИДИМКА" in m for _, m in sent)


@pytest.mark.asyncio
class TestDailySummaryAfterPlay:
    async def test_summary_non_empty_after_accumulated_play(self, db_conn, monkeypatch):
        target = await _setup_target(db_conn, telegram_id=777)
        sent = []
        watcher = _make_watcher(db_conn, sent)

        # Freeze the snapshot-day boundary to a fixed mid-day instant and base
        # the synthetic poll clock on that same instant's epoch, so day stays
        # stable across polls (production shares one clock for both).
        fixed = _dtmod.datetime(2026, 6, 4, 12, 0, tzinfo=_dtmod.timezone.utc)

        class _MidDayDateTime(_dtmod.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

            @classmethod
            def fromtimestamp(cls, ts, tz=None):
                return _dtmod.datetime.fromtimestamp(ts, tz)

        monkeypatch.setattr("src.watcher.datetime", _MidDayDateTime)
        clock = {"now": int(fixed.timestamp())}
        monkeypatch.setattr("src.watcher.time.time", lambda: clock["now"])

        prof = _profile(game_id="730", game_name="Counter-Strike 2", persona_state=1)
        # Several polls accumulate ~1h+ of playtime (capped per step at 90s).
        await watcher._check_target("k", target, prof)
        for _ in range(60):
            clock["now"] += 90
            await watcher._check_target("k", target, prof)

        state = await db.get_target_state(db_conn, target.id)
        pts = json.loads(state.game_playtimes)
        assert pts["730"] >= 60 * 90  # plenty of seconds accumulated

        # Force the summary to be due: enable, time in the past, not sent today.
        await db.save_user_settings(db_conn, UserSettings(
            telegram_id=777,
            daily_summary_enabled=True,
            daily_summary_time="00:00",
            last_summary_date="",
            timezone="UTC",
        ))

        await watcher._send_daily_summaries()

        summaries = [m for tid, m in sent if tid == 777 and "Ежедневная сводка" in m]
        assert summaries, "expected a non-empty daily summary"
        body = summaries[0]
        assert "TestUser" in body
        assert "Counter-Strike 2" in body
        # ~90 minutes accumulated (60 polls * 90s = 5400s) -> "1ч 30мин".
        assert "Всего: 1ч 30мин" in body
        # Snapshot must have been marked as sent for the user.
        settings = await db.get_user_settings(db_conn, 777)
        assert settings.last_summary_date != ""


@pytest.mark.asyncio
class TestSummaryWithoutSettingsRow:
    async def test_user_with_target_but_no_settings_row_is_due(self, db_conn, monkeypatch):
        """A user who added a target but never opened settings must be due.

        add_target now ensures a default row, and get_users_due_summary also
        synthesises defaults for any legacy target without a row.
        """
        # Freeze "now" to 23:30 so the default 21:00 summary time is in the past.
        monkeypatch.setattr(_dtmod, "datetime", _FixedDateTime)

        telegram_id = 999
        await db.save_user(db_conn, User(telegram_id=telegram_id, steam_api_key="k"))
        # Insert a target WITHOUT going through add_target's ensure step, to
        # simulate a legacy row created before the fix.
        await db_conn.execute(
            "INSERT INTO targets (telegram_id, steam_id, name, interval_seconds, active) "
            "VALUES (?, ?, ?, ?, ?)",
            (telegram_id, "76561198000000009", "Legacy", 30, 1),
        )
        await db_conn.commit()

        # Sanity: no user_settings row exists for this user.
        async with db_conn.execute(
            "SELECT COUNT(*) FROM user_settings WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            assert (await cur.fetchone())[0] == 0

        due = await db.get_users_due_summary(db_conn)
        due_ids = [t[0] for t in due]
        assert telegram_id in due_ids
        # Synthesised entry carries the schema defaults.
        entry = next(t for t in due if t[0] == telegram_id)
        assert entry[1] == "21:00"  # daily_summary_time default
        assert entry[2] == "UTC"    # timezone default

    async def test_add_target_creates_default_settings_row(self, db_conn):
        target = await _setup_target(db_conn, telegram_id=1001)
        async with db_conn.execute(
            "SELECT daily_summary_enabled, daily_summary_time, timezone "
            "FROM user_settings WHERE telegram_id = ?", (1001,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 1  # daily_summary_enabled default on
        assert row[1] == "21:00"
        assert row[2] == "UTC"

    async def test_mark_summary_sent_creates_row_when_missing(self, db_conn):
        # No settings row yet; mark_summary_sent should upsert one.
        await db.mark_summary_sent(db_conn, 2002, "2026-06-04")
        settings = await db.get_user_settings(db_conn, 2002)
        assert settings.last_summary_date == "2026-06-04"
