"""Tests for src.watcher: alert generation logic."""

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
        previous = _make_state(persona_state=0, playtime_forever=50)
        current = _make_state(persona_state=0, playtime_forever=80)
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
