"""Tests for src.watcher: alert generation logic."""

import asyncio
from datetime import datetime, timezone

import pytest
from src.models import Target, TargetState, Alert
from src.watcher import generate_alerts


def _make_target(name="TestTarget", steam_id="76561198000000001"):
    return Target(
        id=1, telegram_id=12345, steam_id=steam_id,
        name=name, interval_seconds=300, active=True,
    )


def _make_state(
    target_id=1,
    persona_state=0,
    persona_name="TestUser",
    game_id=None,
    game_name=None,
    playtime_forever=0,
    last_logoff=None,
    last_checked=1705320000,
):
    return TargetState(
        target_id=target_id,
        persona_state=persona_state,
        persona_name=persona_name,
        game_id=game_id,
        game_name=game_name,
        playtime_forever=playtime_forever,
        last_logoff=last_logoff,
        last_checked=last_checked,
    )


class TestGenerateAlertsFirstCheck:
    def test_first_check_offline(self):
        target = _make_target()
        current = _make_state(persona_state=0)
        alerts = generate_alerts(target, None, current, False)
        assert len(alerts) == 1
        assert "первая проверка" in alerts[0].message

    def test_first_check_online_playing(self):
        target = _make_target()
        current = _make_state(
            persona_state=1,
            game_name="Counter-Strike 2",
        )
        alerts = generate_alerts(target, None, current, False)
        assert len(alerts) == 1
        assert "Counter-Strike 2" in alerts[0].message


class TestGenerateAlertsStateChange:
    def test_came_online(self):
        target = _make_target()
        previous = _make_state(persona_state=0)
        current = _make_state(persona_state=1)
        alerts = generate_alerts(target, previous, current, False)
        messages = [a.message for a in alerts]
        assert any("онлайн" in m for m in messages)

    def test_went_offline(self):
        target = _make_target()
        previous = _make_state(persona_state=1)
        current = _make_state(persona_state=0, last_logoff=1705320000)
        alerts = generate_alerts(target, previous, current, False)
        messages = [a.message for a in alerts]
        assert any("оффлайн" in m for m in messages)

    def test_no_change_no_alert(self):
        target = _make_target()
        previous = _make_state(persona_state=1)
        current = _make_state(persona_state=1)
        alerts = generate_alerts(target, previous, current, False)
        assert len(alerts) == 0


class TestGenerateAlertsGameChange:
    def test_started_playing(self):
        target = _make_target()
        previous = _make_state(persona_state=1, game_name=None)
        current = _make_state(persona_state=1, game_name="Dota 2")
        alerts = generate_alerts(target, previous, current, False)
        messages = [a.message for a in alerts]
        assert any("начал играть" in m and "Dota 2" in m for m in messages)

    def test_stopped_playing(self):
        target = _make_target()
        previous = _make_state(persona_state=1, game_name="Dota 2")
        current = _make_state(persona_state=1, game_name=None)
        alerts = generate_alerts(target, previous, current, False)
        messages = [a.message for a in alerts]
        assert any("перестал играть" in m and "Dota 2" in m for m in messages)

    def test_switched_game(self):
        target = _make_target()
        previous = _make_state(persona_state=1, game_name="Game A")
        current = _make_state(persona_state=1, game_name="Game B")
        alerts = generate_alerts(target, previous, current, False)
        messages = [a.message for a in alerts]
        assert any("Game A" in m for m in messages)
        assert any("Game B" in m for m in messages)


class TestGenerateAlertsInvisible:
    def test_invisible_detected(self):
        target = _make_target()
        previous = _make_state(persona_state=0, playtime_forever=50 * 60)
        current = _make_state(persona_state=0, playtime_forever=80 * 60)
        alerts = generate_alerts(target, previous, current, True)
        messages = [a.message for a in alerts]
        assert any("НЕВИДИМКА" in m for m in messages)
        assert any("50" in m and "80" in m for m in messages)


class TestGenerateAlertsNameChange:
    def test_persona_name_change(self):
        target = _make_target()
        previous = _make_state(persona_name="OldName")
        current = _make_state(persona_name="NewName")
        alerts = generate_alerts(target, previous, current, False)
        messages = [a.message for a in alerts]
        assert any("OldName" in m and "NewName" in m for m in messages)

    def test_no_alert_same_name(self):
        target = _make_target()
        previous = _make_state(persona_name="SameName")
        current = _make_state(persona_name="SameName")
        alerts = generate_alerts(target, previous, current, False)
        assert len(alerts) == 0


class TestGenerateAlertsMultiple:
    def test_came_online_and_started_playing(self):
        target = _make_target()
        previous = _make_state(persona_state=0, game_name=None)
        current = _make_state(persona_state=1, game_name="CS2")
        alerts = generate_alerts(target, previous, current, False)
        messages = [a.message for a in alerts]
        assert any("онлайн" in m for m in messages)
        assert any("CS2" in m for m in messages)


def _event_type_of(alerts, substring):
    """Return the event_type of the single alert whose message contains substring."""
    matching = [a for a in alerts if substring in a.message]
    assert len(matching) == 1, f"expected exactly one alert with {substring!r}"
    return matching[0].event_type


class TestGenerateAlertsEventType:
    def test_first_check_event_type(self):
        target = _make_target()
        current = _make_state(persona_state=0)
        alerts = generate_alerts(target, None, current, False)
        assert alerts[0].event_type == "first_check"

    def test_online_event_type(self):
        target = _make_target()
        previous = _make_state(persona_state=0)
        current = _make_state(persona_state=1)
        alerts = generate_alerts(target, previous, current, False)
        assert _event_type_of(alerts, "зашёл онлайн") == "online"

    def test_offline_event_type(self):
        target = _make_target()
        previous = _make_state(persona_state=1)
        current = _make_state(persona_state=0, last_logoff=1705320000)
        alerts = generate_alerts(target, previous, current, False)
        assert _event_type_of(alerts, "вышел оффлайн") == "offline"

    def test_game_start_event_type_and_metadata(self):
        target = _make_target()
        previous = _make_state(persona_state=1, game_name=None)
        current = _make_state(persona_state=1, game_name="Dota 2")
        alerts = generate_alerts(target, previous, current, False)
        start = [a for a in alerts if "начал играть" in a.message][0]
        assert start.event_type == "game_start"
        assert start.game_name == "Dota 2"
        assert start.is_dota is True

    def test_game_start_non_dota_is_dota_false(self):
        target = _make_target()
        previous = _make_state(persona_state=1, game_name=None)
        current = _make_state(persona_state=1, game_name="Counter-Strike 2")
        alerts = generate_alerts(target, previous, current, False)
        start = [a for a in alerts if "начал играть" in a.message][0]
        assert start.event_type == "game_start"
        assert start.is_dota is False

    def test_game_stop_event_type(self):
        target = _make_target()
        previous = _make_state(persona_state=1, game_name="Dota 2")
        current = _make_state(persona_state=1, game_name=None)
        alerts = generate_alerts(target, previous, current, False)
        stop = [a for a in alerts if "перестал играть" in a.message][0]
        assert stop.event_type == "game_stop"
        assert stop.game_name == "Dota 2"

    def test_switch_game_event_types(self):
        target = _make_target()
        previous = _make_state(persona_state=1, game_name="Game A")
        current = _make_state(persona_state=1, game_name="Game B")
        alerts = generate_alerts(target, previous, current, False)
        assert _event_type_of(alerts, "Game A") == "game_stop"
        assert _event_type_of(alerts, "Game B") == "game_start"

    def test_name_change_event_type(self):
        target = _make_target()
        previous = _make_state(persona_name="OldName")
        current = _make_state(persona_name="NewName")
        alerts = generate_alerts(target, previous, current, False)
        assert _event_type_of(alerts, "сменил ник") == "name_change"

    def test_invisible_event_type(self):
        target = _make_target()
        previous = _make_state(persona_state=0, playtime_forever=50)
        current = _make_state(persona_state=0, playtime_forever=80)
        alerts = generate_alerts(target, previous, current, True)
        assert _event_type_of(alerts, "НЕВИДИМКА") == "invisible"

    def test_nickname_containing_event_text_not_misclassified(self):
        # Regression for ARCH#8: a nickname that literally contains "начал играть"
        # must NOT cause the name-change alert to be routed as a game_start.
        target = _make_target()
        previous = _make_state(persona_name="old")
        current = _make_state(persona_name="начал играть в Dota")
        alerts = generate_alerts(target, previous, current, False)
        assert _event_type_of(alerts, "сменил ник") == "name_change"


# ── Dota enrichment + blocked-bot handling (Watcher methods) ────────────────

from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from src import db
from src.models import MatchInfo, SteamProfile, TargetState, User, UserSettings
from src.watcher import Watcher


def _make_watcher(db_conn, send_alert, match_tracker=None):
    steam = MagicMock()
    steam.get_player_summaries = AsyncMock(return_value=None)
    steam.get_player_summaries_batch = AsyncMock(return_value={})
    return Watcher(
        db_conn=db_conn,
        steam_client=steam,
        send_alert=send_alert,
        match_tracker=match_tracker,
    )


def _forbidden_error():
    return TelegramForbiddenError(
        method=SendMessage(chat_id=1, text="x"),
        message="Forbidden: bot was blocked by the user",
    )


@pytest.mark.asyncio
async def test_run_loop_keeps_10_second_idle_sleep(monkeypatch):
    watcher = _make_watcher(MagicMock(), AsyncMock(), match_tracker=None)
    watcher._running = True
    watcher._last_cleanup_date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    async def fake_poll_all():
        watcher._running = False

    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    watcher._poll_all = fake_poll_all
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await watcher._run_loop()

    assert sleep_calls == [10]


@pytest.mark.asyncio
class TestEnrichDotaAlert:
    async def test_enrich_appends_mmr_and_rank(self):
        target = _make_target(steam_id="76561198000000001")
        match_tracker = MagicMock()
        # rank_tier 11 -> Herald 1; mmr 3500
        match_tracker.get_player_rank = AsyncMock(
            return_value={"mmr": 3500, "rank_tier": 11}
        )
        watcher = _make_watcher(MagicMock(), AsyncMock(), match_tracker)

        enriched = await watcher._enrich_dota_alert(target, "base")
        assert enriched is not None
        assert "base" in enriched
        assert "MMR: 3500" in enriched
        assert "Ранг: Herald 1" in enriched
        match_tracker.get_player_rank.assert_awaited_once_with(target.steam_id)

    async def test_enrich_immortal_rank(self):
        target = _make_target(steam_id="76561198000000001")
        match_tracker = MagicMock()
        match_tracker.get_player_rank = AsyncMock(
            return_value={"mmr": None, "rank_tier": 80}
        )
        watcher = _make_watcher(MagicMock(), AsyncMock(), match_tracker)

        enriched = await watcher._enrich_dota_alert(target, "base")
        assert enriched is not None
        assert "Ранг: Immortal (rank 0)" in enriched
        assert "MMR:" not in enriched

    async def test_enrich_returns_none_without_tracker(self):
        target = _make_target()
        watcher = _make_watcher(MagicMock(), AsyncMock(), match_tracker=None)
        assert await watcher._enrich_dota_alert(target, "base") is None

    async def test_enrich_returns_none_when_no_rank(self):
        target = _make_target(steam_id="76561198000000001")
        match_tracker = MagicMock()
        match_tracker.get_player_rank = AsyncMock(
            return_value={"mmr": None, "rank_tier": None}
        )
        watcher = _make_watcher(MagicMock(), AsyncMock(), match_tracker)
        assert await watcher._enrich_dota_alert(target, "base") is None


@pytest.mark.asyncio
class TestPollMatchesAlertLinks:
    async def test_check_target_preserves_last_sent_match_state(self, db_conn, monkeypatch):
        telegram_id = 443
        await db.save_user(db_conn, User(telegram_id=telegram_id, steam_api_key="k"))
        target = await db.add_target(
            db_conn,
            Target(
                id=0,
                telegram_id=telegram_id,
                steam_id="76561198000000443",
                name="debustie",
                interval_seconds=30,
                active=True,
            ),
        )
        await db.save_target_state(
            db_conn,
            TargetState(
                target_id=target.id,
                persona_state=0,
                persona_name="debustie",
                last_checked=1_700_000_000,
                last_match_id="8839259291",
                last_match_time=1_700_000_010,
            ),
        )

        monkeypatch.setattr("src.watcher.time.time", lambda: 1_700_000_030)
        watcher = _make_watcher(db_conn, AsyncMock(), match_tracker=None)
        profile = SteamProfile(
            steam_id=target.steam_id,
            persona_name="debustie",
            persona_state=0,
            game_id="570",
            game_name="Dota 2",
            last_logoff=None,
            visibility_state=3,
        )

        await watcher._check_target("k", target, profile)

        state = await db.get_target_state(db_conn, target.id)
        assert state is not None
        assert state.last_match_id == "8839259291"
        assert state.last_match_time == 1_700_000_010

    async def test_dotabuff_hidden_activity_alert_includes_clickable_match_link(self, db_conn):
        telegram_id = 444
        await db.save_user(db_conn, User(telegram_id=telegram_id, steam_api_key="k"))
        target = await db.add_target(
            db_conn,
            Target(
                id=0,
                telegram_id=telegram_id,
                steam_id="76561198000000999",
                name="debustie",
                interval_seconds=30,
                active=True,
            ),
        )
        await db.save_target_state(
            db_conn,
            TargetState(
                target_id=target.id,
                persona_state=0,
                persona_name="debustie",
                last_checked=1_700_000_000,
            ),
        )

        sent = []

        async def _send(telegram_id, message):
            sent.append((telegram_id, message))

        match_tracker = MagicMock()
        match_tracker.get_last_match = AsyncMock(
            return_value=MatchInfo(
                match_id="8839259291",
                steam_id=target.steam_id,
                game="dota2",
                duration=1800,
                start_time=1_699_999_000,
                hero_name="Invoker",
                source="dotabuff",
            )
        )
        match_tracker.get_recent_matches = AsyncMock(return_value=[])
        watcher = _make_watcher(db_conn, _send, match_tracker)

        await watcher._poll_matches()

        assert len(sent) == 1
        assert sent[0][0] == telegram_id
        assert "матч 8839259291" in sent[0][1]
        assert "https://www.dotabuff.com/matches/8839259291" in sent[0][1]

    async def test_poll_matches_skips_already_logged_match_id(self, db_conn):
        telegram_id = 445
        await db.save_user(db_conn, User(telegram_id=telegram_id, steam_api_key="k"))
        target = await db.add_target(
            db_conn,
            Target(
                id=0,
                telegram_id=telegram_id,
                steam_id="76561198000000445",
                name="debustie",
                interval_seconds=30,
                active=True,
            ),
        )
        await db.save_target_state(
            db_conn,
            TargetState(
                target_id=target.id,
                persona_state=0,
                persona_name="debustie",
                last_checked=1_700_000_000,
            ),
        )
        await db.save_activity(
            db_conn,
            target.id,
            "match",
            game_name="Dota 2",
            match_id="8839103634",
            detected_at=1_700_000_005,
        )

        sent = []

        async def _send(telegram_id, message):
            sent.append((telegram_id, message))

        match_tracker = MagicMock()
        match_tracker.get_last_match = AsyncMock(
            return_value=MatchInfo(
                match_id="8839103634",
                steam_id=target.steam_id,
                game="dota2",
                duration=1800,
                start_time=1_699_999_000,
                hero_name="Invoker",
                source="dotabuff",
            )
        )
        match_tracker.get_recent_matches = AsyncMock(return_value=[])
        watcher = _make_watcher(db_conn, _send, match_tracker)

        await watcher._poll_matches()

        assert sent == []
        activity = await db.get_activity_log(db_conn, target.id, limit=10, event_types=["match"])
        assert len(activity) == 1
        state = await db.get_target_state(db_conn, target.id)
        assert state is not None
        assert state.last_match_id == "8839103634"


@pytest.mark.asyncio
class TestBlockedBotDeactivates:
    async def test_forbidden_error_deactivates_all_targets_for_user(self, db_conn):
        telegram_id = 999
        steam_id_a = "76561198000000777"
        steam_id_b = "76561198000000888"
        await db.save_user(db_conn, User(telegram_id=telegram_id, steam_api_key="k"))
        target = await db.add_target(
            db_conn,
            Target(
                id=0, telegram_id=telegram_id, steam_id=steam_id_a,
                name="Blocked", interval_seconds=30, active=True,
            ),
        )
        await db.add_target(
            db_conn,
            Target(
                id=0, telegram_id=telegram_id, steam_id=steam_id_b,
                name="BlockedToo", interval_seconds=30, active=True,
            ),
        )

        async def _raise(telegram_id, message):
            raise _forbidden_error()

        watcher = _make_watcher(db_conn, _raise)

        ok = await watcher._send_to_target(target, "hello")
        assert ok is False
        assert target.active is False

        active = await db.get_active_targets(db_conn)
        assert all(t.telegram_id != telegram_id for t in active)
        stored = await db.get_targets(db_conn, telegram_id)
        assert len(stored) == 2
        assert all(t.active is False for t in stored)

    async def test_daily_summary_marks_user_sent_when_bot_blocked(self, db_conn, monkeypatch):
        import datetime as _dtmod

        telegram_id = 1234
        await db.save_user(db_conn, User(telegram_id=telegram_id, steam_api_key="k"))
        target = await db.add_target(
            db_conn,
            Target(
                id=0, telegram_id=telegram_id, steam_id="76561198000000444",
                name="SummaryBlocked", interval_seconds=30, active=True,
            ),
        )
        await db.save_target_state(
            db_conn,
            TargetState(
                target_id=target.id,
                persona_state=0,
                persona_name="SummaryBlocked",
                playtime_forever=3600,
                last_checked=int(_dtmod.datetime(2026, 6, 4, 10, 0, tzinfo=_dtmod.timezone.utc).timestamp()),
                game_playtimes='{"730": 3600}',
                daily_playtime_snapshot='{"730": 0}',
                playtime_unit_version=2,
            ),
        )
        await db.save_user_settings(
            db_conn,
            UserSettings(
                telegram_id=telegram_id,
                daily_summary_enabled=True,
                daily_summary_time="00:00",
                last_summary_date="",
                timezone="UTC",
            ),
        )

        fixed = _dtmod.datetime(2026, 6, 4, 23, 30, tzinfo=_dtmod.timezone.utc)

        class _FixedDateTime(_dtmod.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

        monkeypatch.setattr("src.db.datetime", _FixedDateTime)
        monkeypatch.setattr("src.watcher.datetime", _FixedDateTime)

        async def _raise(telegram_id, message):
            raise _forbidden_error()

        watcher = _make_watcher(db_conn, _raise)
        await watcher._send_daily_summaries()

        settings = await db.get_user_settings(db_conn, telegram_id)
        assert settings.last_summary_date == "2026-06-04"
        stored = await db.get_targets(db_conn, telegram_id)
        assert all(t.active is False for t in stored)

    async def test_poll_all_skips_remaining_targets_for_user_after_block(self, db_conn):
        telegram_id = 777
        await db.save_user(db_conn, User(telegram_id=telegram_id, steam_api_key="k"))
        target_a = await db.add_target(
            db_conn,
            Target(id=0, telegram_id=telegram_id, steam_id="76561198000000111", name="A", interval_seconds=30, active=True),
        )
        target_b = await db.add_target(
            db_conn,
            Target(id=0, telegram_id=telegram_id, steam_id="76561198000000222", name="B", interval_seconds=30, active=True),
        )

        async def _send_ok(telegram_id, message):
            return None

        watcher = _make_watcher(db_conn, _send_ok)
        watcher._steam.get_player_summaries_batch = AsyncMock(
            return_value={
                target_a.steam_id: object(),
                target_b.steam_id: object(),
            }
        )

        seen = []

        async def _fake_check_target(api_key, target, profile=None):
            seen.append(target.steam_id)
            if target.steam_id == target_a.steam_id:
                await watcher._deactivate_blocked_user(telegram_id)

        watcher._check_target = AsyncMock(side_effect=_fake_check_target)

        await watcher._poll_all()

        assert seen == [target_a.steam_id]
        stored = await db.get_targets(db_conn, telegram_id)
        assert all(t.active is False for t in stored)
