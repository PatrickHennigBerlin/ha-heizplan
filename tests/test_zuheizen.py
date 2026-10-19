"""Tests für das Zuheizen mit Klimagerät."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.heizplan.const import DOMAIN
from custom_components.heizplan.zuheizen import (
    AuxError,
    DEFAULT_AUX_SETTINGS,
    can_start,
    climate_setpoint,
    conditions,
    normalize_aux_settings,
    outside_ok,
)

S = DEFAULT_AUX_SETTINGS


# ------------------------------------------------------------------ reine Logik
def test_thresholds_and_hysteresis():
    c = lambda cur: conditions(enabled=True, target=22, current=cur, outside_is_ok=True, settings=S)
    assert c(20.5) == (True, False)    # 1,5 K unter Soll -> ein
    assert c(20.6) == (False, False)   # dazwischen: Zustand bleibt
    assert c(21.4) == (False, False)
    assert c(21.5) == (False, True)    # Soll − 0,5 -> aus
    assert c(None) == (False, False)   # kein Ist: nichts anfangen, nicht blind beenden


def test_conditions_inactive():
    for kw in ({"enabled": False}, {"target": None}, {"outside_is_ok": False}):
        args = {"enabled": True, "target": 22, "current": 18, "outside_is_ok": True, "settings": S, **kw}
        assert conditions(**args) == (False, True)


def test_outside():
    assert outside_ok(False, None, 10) is True
    assert outside_ok(True, "9.5", 10) is True
    assert outside_ok(True, "10", 10) is False
    assert outside_ok(True, "unavailable", 10) is False
    assert outside_ok(True, None, 10) is False


def test_setpoint_and_start():
    attrs = {"min_temp": 19, "max_temp": 30, "target_temp_step": 0.5}
    assert climate_setpoint(17, attrs) == 19
    assert climate_setpoint(21.3, attrs) == 21.5
    assert climate_setpoint(35, attrs) == 30
    assert climate_setpoint(21.3, {"target_temp_step": 1}) == 21
    assert can_start("off", False) and not can_start("cool", False)
    assert can_start("cool", True) and can_start("heat", True)
    assert not can_start("unavailable", True)


def test_normalize():
    assert normalize_aux_settings({})["on_delta"] == 1.5
    with pytest.raises(AuxError):
        normalize_aux_settings({"on_delta": 1, "off_delta": 1})
    with pytest.raises(AuxError):
        normalize_aux_settings({"wait": "x"})


# ------------------------------------------------------------------ mit HA
SCHEDULE = {
    "weekdays": ["daily"],
    "timeslots": ["00:00:00 - 06:00:00", "06:00:00 - 22:00:00", "22:00:00 - 00:00:00"],
    "entities": ["climate.lounge"],
    "actions": [
        {"service": "climate.set_temperature", "data": {"temperature": 17}},
        {"service": "climate.set_temperature", "data": {"temperature": 22}},
        {"service": "climate.set_temperature", "data": {"temperature": 16}},
    ],
}
KLIMA_ATTRS = {"hvac_modes": ["cool", "dry", "fan_only", "heat", "off"], "min_temp": 19, "max_temp": 30,
               "target_temp_step": 0.5, "fan_modes": ["auto", "1", "2"], "temperature": 24,
               "friendly_name": "Klima Wohnzimmer"}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


class Env:
    def __init__(self, hass, entry, clock, calls):
        self.hass, self.entry, self.clock, self.calls = hass, entry, clock, calls

    @property
    def manager(self):
        return self.hass.data[DOMAIN][self.entry.entry_id]

    def ist(self, value):
        attrs = dict(self.hass.states.get("climate.lounge").attributes)
        attrs["current_temperature"] = value
        self.hass.states.async_set("climate.lounge", "heat", attrs)

    def klima(self, state, **attrs):
        self.hass.states.async_set("climate.klima_wz", state, {**KLIMA_ATTRS, **attrs})

    async def at(self, minutes):
        self.clock["now"] = self.clock["t0"] + timedelta(minutes=minutes)
        await self.manager.async_apply()
        await self.hass.async_block_till_done()

    def klima_calls(self, kind):
        if kind in ("heat_on", "off"):   # Modus-Aufrufe getrennt: Einschalten (heat) bzw. Ausschalten (off)
            mode = "heat" if kind == "heat_on" else "off"
            return [c for c in self.klima_calls("set_hvac_mode") if c.data.get("hvac_mode") == mode]
        return [c for c in self.calls[kind] if c.data.get("entity_id") == "climate.klima_wz"]


async def _setup(hass: HomeAssistant, clock: dict, outside: str | None = None, takeover: bool = True,
                 fan: dict | None = None) -> Env:
    await async_setup_component(hass, "http", {})
    await async_setup_component(hass, "websocket_api", {})
    hass.config.components.add("frontend")
    hass.states.async_set("climate.lounge", "heat", {
        "current_temperature": 21.8, "temperature": 22, "min_temp": 16, "max_temp": 28,
        "preset_mode": "comfort", "preset_modes": ["comfort", "standby"], "friendly_name": "Wohnzimmer"})
    hass.states.async_set("climate.klima_wz", "off", KLIMA_ATTRS)
    hass.states.async_set("switch.heizperiode", "on")
    hass.states.async_set("switch.schedule_wz", "on", SCHEDULE)
    hass.states.async_set("switch.lufter_auto_wz", "off")
    calls = {
        "set_temperature": async_mock_service(hass, "climate", "set_temperature"),
        "set_hvac_mode": async_mock_service(hass, "climate", "set_hvac_mode"),
        "set_fan_mode": async_mock_service(hass, "climate", "set_fan_mode"),
        "set_preset_mode": async_mock_service(hass, "climate", "set_preset_mode"),
    }
    options = {"season_entity": "switch.heizperiode", "keep_comfort": True, "comfort_preset": "comfort"}
    if outside:
        options["aux_outside_entity"] = outside
    entry = MockConfigEntry(domain=DOMAIN, title="Heizplan", unique_id=DOMAIN, options=options)
    entry.add_to_hass(hass)
    with patch("custom_components.heizplan.add_extra_js_url"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    # erst nach dem Einrichten: die Integration lädt die echte switch-Plattform
    calls["switch_on"] = async_mock_service(hass, "switch", "turn_on")
    await hass.services.async_call(DOMAIN, "import_scheduler", {"entities": ["switch.schedule_wz"]},
                                   blocking=True, return_response=True)
    await hass.async_block_till_done()
    room = {"room": "lounge", "aux_climate": "climate.klima_wz", **(fan if fan is not None else {"aux_fan_switch": "switch.lufter_auto_wz"})}
    await hass.services.async_call(DOMAIN, "update_room", room, blocking=True)
    await hass.async_block_till_done()   # Neuladen wegen neuer Entitäten
    await hass.async_block_till_done()
    if not takeover:
        await hass.services.async_call(DOMAIN, "set_aux", {"takeover": False}, blocking=True)
    return Env(hass, entry, clock, calls)


@pytest.fixture
def clock():
    t0 = dt_util.as_local(datetime(2026, 10, 12, 12, 0)).replace(tzinfo=dt_util.get_default_time_zone())
    c = {"t0": t0, "now": t0}
    with patch("custom_components.heizplan.manager.dt_util.now", side_effect=lambda: c["now"]):
        yield c


async def test_on_after_wait_off_with_hysteresis(hass, clock):
    env = await _setup(hass, clock)
    # Entitäten da
    assert hass.states.get("switch.heizplan_zuheizen_heizung_wohnzimmer") is not None or \
        hass.states.get("switch.heizplan_zuheizen_wohnzimmer") is not None
    assert hass.states.get("number.heizplan_zuheizen_abweichung").state == "1.5"
    assert hass.states.get("number.heizplan_zuheizen_aussengrenze") is not None

    env.ist(20.4)   # 1,6 K unter Soll 22
    await env.at(0)
    assert not env.klima_calls("set_temperature")
    await env.at(5)
    assert not env.klima_calls("set_temperature")
    # Unterbrechung setzt die Wartezeit zurück
    env.ist(21.0)
    await env.at(6)
    env.ist(20.4)
    await env.at(7)
    await env.at(16)
    assert not env.klima_calls("set_temperature")
    await env.at(17)
    on = env.klima_calls("set_temperature")
    assert on and on[-1].data == {"entity_id": "climate.klima_wz", "temperature": 22.0} and env.klima_calls("heat_on")
    assert env.calls["switch_on"][-1].data["entity_id"] == "switch.lufter_auto_wz"
    assert env.manager.aux_state["lounge"]["active"] is True
    bs = [s for s in hass.states.async_all("binary_sensor") if s.entity_id.startswith("binary_sensor.heizplan_zuheizen")]
    assert bs and bs[0].state == "on"

    env.klima("heat", temperature=22)
    env.ist(21.2)      # zwischen den Schwellen -> bleibt an
    await env.at(30)
    assert not env.klima_calls("off")
    env.ist(21.5)      # Soll − 0,5 -> aus
    await env.at(31)
    off = env.klima_calls("off")
    assert off and off[-1].data == {"entity_id": "climate.klima_wz", "hvac_mode": "off"}
    assert env.manager.aux_state["lounge"]["active"] is False


async def test_setpoint_follows_and_away(hass, clock):
    env = await _setup(hass, clock)
    env.ist(20.0)
    await env.at(0)
    await env.at(10)
    env.klima("heat", temperature=22)
    n = len(env.klima_calls("set_temperature"))
    await env.manager.async_set_override("lounge", 23.2, 60)
    await hass.async_block_till_done()
    last = env.klima_calls("set_temperature")[-1].data
    assert last == {"entity_id": "climate.klima_wz", "temperature": 23.0}   # auf 0,5 gerundet
    assert len(env.klima_calls("set_temperature")) == n + 1
    # Abwesend 17 °C -> Klima-Soll mindestens min_temp 19; Ist 20 ≥ 17−0,5 -> aus
    await env.manager.async_clear_override("lounge")
    await env.manager.async_set_mode("away")
    await hass.async_block_till_done()
    assert env.klima_calls("off")[-1].data["hvac_mode"] == "off"


async def test_outside_sensor(hass, clock):
    env = await _setup(hass, clock, outside="sensor.aussen")
    env.ist(19.0)
    hass.states.async_set("sensor.aussen", "12")
    await env.at(0)
    await env.at(15)
    assert not env.klima_calls("set_temperature")   # außen zu warm
    hass.states.async_set("sensor.aussen", "unavailable")
    await env.at(30)
    await env.at(45)
    assert not env.klima_calls("set_temperature")   # Fühler weg -> nicht zuheizen
    hass.states.async_set("sensor.aussen", "4.5")
    await env.at(50)
    await env.at(60)
    assert env.klima_calls("set_temperature")
    env.klima("heat", temperature=22)
    hass.states.async_set("sensor.aussen", "11")     # wärmer als Grenze -> aus
    await env.at(61)
    assert env.klima_calls("off")[-1].data["hvac_mode"] == "off"


async def test_takeover_off_and_on(hass, clock):
    env = await _setup(hass, clock, takeover=False)
    env.ist(19.0)
    env.klima("cool")
    await env.at(0)
    await env.at(20)
    assert not env.klima_calls("set_temperature")   # läuft (Kühlen) -> ohne Übernehmen nicht anfassen
    env.klima("off")
    await env.at(21)
    assert env.klima_calls("set_temperature")       # aus -> einschalten
    assert env.manager.aux_state["lounge"]["owned"] is True

    await env.manager.async_set_aux_settings({"takeover": True})
    env.klima("heat")
    env.ist(22)
    await env.at(30)
    assert env.klima_calls("off")[-1].data["hvac_mode"] == "off"
    # Übernehmen an: auch eine kühlende Klima auf Heizen stellen
    env.klima("cool")
    env.ist(19)
    await env.at(31)
    await env.at(41)
    assert env.klima_calls("heat_on")


async def test_manual_off_pauses_until_demand_ends(hass, clock):
    env = await _setup(hass, clock)
    env.ist(19.0)
    await env.at(0)
    await env.at(10)
    n = len(env.klima_calls("set_temperature"))
    env.klima("off")         # jemand schaltet die Klima von Hand aus
    await env.at(11)
    await env.at(40)
    assert len(env.klima_calls("set_temperature")) == n      # pausiert
    assert env.manager.room_status("lounge")["aux_heating"]["paused"] is True
    env.ist(22)              # warm -> Pause endet
    await env.at(41)
    env.ist(19)
    await env.at(42)
    await env.at(52)
    assert len(env.klima_calls("set_temperature")) == n + 1


async def test_fan_mode_and_room_switch(hass, clock):
    env = await _setup(hass, clock, fan={"aux_fan_mode": "auto"})
    env.ist(19.0)
    await env.at(0)
    await env.at(10)
    assert env.calls["set_fan_mode"][-1].data == {"entity_id": "climate.klima_wz", "fan_mode": "auto"}
    assert not env.calls["switch_on"]
    # Raumschalter aus -> Klima aus
    env.klima("heat")
    sw = [s.entity_id for s in hass.states.async_all("switch") if s.entity_id.startswith("switch.heizplan_zuheizen")
          and "ubernehmen" not in s.entity_id]
    assert len(sw) == 1
    await hass.services.async_call("switch", "turn_off", {"entity_id": sw[0]}, blocking=True)
    await hass.async_block_till_done()
    assert env.klima_calls("off")[-1].data["hvac_mode"] == "off"
    assert env.manager.rooms["lounge"]["aux_enabled"] is False


async def test_restart_keeps_running(hass, clock):
    env = await _setup(hass, clock)
    env.ist(19.0)
    await env.at(0)
    await env.at(10)
    env.klima("heat", temperature=22)
    await env.manager.async_flush()
    # Neustart (Neuladen); Ist inzwischen zwischen den Schwellen
    env.ist(21.0)
    assert await hass.config_entries.async_reload(env.entry.entry_id)
    await hass.async_block_till_done()
    await env.at(12)
    assert not env.klima_calls("off")
    assert env.manager.aux_state["lounge"]["active"] is True
    # Klima nach Neustart doch aus: nur zurücksetzen (keine Pause), Wartezeit beginnt neu
    env.klima("off")
    env.ist(19.0)
    assert await hass.config_entries.async_reload(env.entry.entry_id)
    await hass.async_block_till_done()
    n = len(env.klima_calls("set_temperature"))
    await env.at(13)
    assert env.manager.aux_state["lounge"]["active"] is False
    assert env.manager.room_status("lounge")["aux_heating"]["paused"] is False
    await env.at(23)
    assert len(env.klima_calls("set_temperature")) == n + 1


# ------------------------------------------------------------------ Boost mit Wärmepumpe
def test_boost_conditions():
    from custom_components.heizplan.zuheizen import boost_conditions
    assert boost_conditions(target=22, current=21.9) == (True, False)
    assert boost_conditions(target=22, current=22) == (False, True)
    assert boost_conditions(target=None, current=18) == (False, True)
    assert boost_conditions(target=22, current=None) == (False, False)


async def test_boost_runs_until_setpoint(hass, clock):
    env = await _setup(hass, clock)
    env.ist(21.5)                      # nur 0,5 K zu kalt: normales Zuheizen würde nicht starten
    await env.at(0)
    assert not env.klima_calls("set_temperature")
    sw = "switch.heizplan_boost_mit_klima_wohnzimmer"
    assert hass.states.get(sw).state == "off"
    # switch.turn_on ist im Test gemockt (Lüfter-Schalter) -> Entität direkt schalten
    ent = next(e for e in hass.data["entity_components"]["switch"].entities if e.entity_id == sw)
    await ent.async_turn_on()
    await hass.async_block_till_done()
    on = env.klima_calls("set_temperature")
    assert on and on[-1].data == {"entity_id": "climate.klima_wz", "temperature": 22.0} and env.klima_calls("heat_on")
    assert hass.states.get(sw).state == "on"
    env.klima("heat", temperature=22)
    env.ist(21.8)
    await env.at(10)
    assert not env.klima_calls("off")
    env.ist(22.0)                      # Soll erreicht -> Klima aus, Boost aus
    await env.at(11)
    off = env.klima_calls("off")
    assert off and off[-1].data == {"entity_id": "climate.klima_wz", "hvac_mode": "off"}
    assert hass.states.get(sw).state == "off"


async def test_boost_cancel_and_max_duration(hass, clock):
    env = await _setup(hass, clock)
    env.ist(20.0)
    await hass.services.async_call("heizplan", "boost", {"room": "lounge"}, blocking=True)
    await hass.async_block_till_done()
    assert env.klima_calls("set_temperature")
    env.klima("heat", temperature=22)
    # abbrechen: Klima aus, obwohl noch kalt (normales Zuheizen läuft erst nach der Wartezeit)
    env.ist(21.0)
    await hass.services.async_call("heizplan", "boost", {"room": "lounge", "on": False}, blocking=True)
    await hass.async_block_till_done()
    assert env.klima_calls("off")[-1].data["hvac_mode"] == "off"
    # Höchstdauer
    env.klima("off")
    await hass.services.async_call("heizplan", "set_aux", {"boost_max": 30}, blocking=True)
    await hass.services.async_call("heizplan", "boost", {"room": "lounge"}, blocking=True)
    await hass.async_block_till_done()
    env.klima("heat", temperature=22)
    env.ist(21.9)
    n = len(env.klima_calls("off"))
    await env.at(31)
    assert len(env.klima_calls("off")) == n + 1
    assert env.manager.aux_state["lounge"]["boost"] is False



async def test_boost_survives_unavailable_season(hass, clock):
    env = await _setup(hass, clock)
    env.ist(20.0)
    await hass.services.async_call("heizplan", "boost", {"room": "lounge"}, blocking=True)
    await hass.async_block_till_done()
    env.klima("heat", temperature=22)
    hass.states.async_set("switch.heizperiode", "unavailable")   # z. B. Neustart
    await env.at(1)
    assert env.manager.aux_state["lounge"]["boost"] is True
    hass.states.async_set("switch.heizperiode", "on")
    await env.at(2)
    assert env.manager.aux_state["lounge"]["boost"] is True
