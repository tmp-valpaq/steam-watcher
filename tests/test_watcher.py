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
