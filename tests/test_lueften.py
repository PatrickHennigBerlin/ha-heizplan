"""Lüften: Heizung aus, Ausgänge an, Ablauf, Fremdschaltung, verknüpfter An/Aus-Raum."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.heizplan.const import DOMAIN, SOURCE_VENT


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


THERMO = {
    "current_temperature": 20.0,
    "temperature": 20,
    "min_temp": 5,
    "max_temp": 28,
    "preset_mode": "comfort",
    "preset_modes": ["comfort", "standby", "economy", "building_protection"],
    "friendly_name": "Heizung Bad",
}


async def _setup(hass: HomeAssistant):
    await async_setup_component(hass, "http", {})
    await async_setup_component(hass, "websocket_api", {})
    hass.config.components.add("frontend")
    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {
        "fenster_bad": {"name": "Fenster Bad"},
        "klima_fenster_bad": {"name": "Klima Fenster Bad"},
        "fbh": {"name": "Fußbodenheizung"},
    }})
    hass.states.async_set("climate.bad", "heat", THERMO)
    hass.states.async_set("switch.heizperiode", "on")
    set_temp = async_mock_service(hass, "climate", "set_temperature")
    set_preset = async_mock_service(hass, "climate", "set_preset_mode")
    entry = MockConfigEntry(
        domain=DOMAIN, title="Heizplan", unique_id=DOMAIN,
        options={"season_entity": "switch.heizperiode", "keep_comfort": True, "comfort_preset": "comfort"},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.heizplan.add_extra_js_url"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    manager = hass.data[DOMAIN][entry.entry_id]
    return entry, manager, set_temp, set_preset


async def _rooms(hass: HomeAssistant, manager):
    await manager.async_add_room("climate.bad", "Bad")
    await hass.async_block_till_done()
    manager = next(iter(hass.data[DOMAIN].values()))
    await manager.async_add_room("input_boolean.fbh", "Fußbodenheizung")
    await hass.async_block_till_done()
    manager = next(iter(hass.data[DOMAIN].values()))
    # FBH ganztägig an
    await manager.async_set_plan("fbh", {d: [{"start": "00:00", "on": True}]
                                         for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]})
    await manager.async_update_room("bad", vent_outputs=["input_boolean.fenster_bad", "input_boolean.klima_fenster_bad"])
    await hass.async_block_till_done()
    manager = next(iter(hass.data[DOMAIN].values()))
    await manager.async_update_room("fbh", vent_with="bad")
    await hass.async_block_till_done()
    return next(iter(hass.data[DOMAIN].values()))


def _on(hass, ent):
    return hass.states.get(ent).state == "on"


async def test_vent_switch_outputs_and_expiry(hass: HomeAssistant):
    clock = Clock(dt_util.as_local(datetime(2026, 11, 9, 10, 0)).replace(tzinfo=dt_util.get_default_time_zone()))
    with patch("custom_components.heizplan.manager.dt_util.now", side_effect=clock):
        entry, manager, set_temp, set_preset = await _setup(hass)
        manager = await _rooms(hass, manager)
        await manager.async_apply(force=True)
        await hass.async_block_till_done()
        assert _on(hass, "input_boolean.fbh")
        assert not _on(hass, "input_boolean.fenster_bad")

        sw = "switch.heizplan_luften_bad"
        assert hass.states.get(sw) is not None, [s.entity_id for s in hass.states.async_all("switch")]
        await hass.services.async_call("switch", "turn_on", {"entity_id": sw}, blocking=True)
        await hass.async_block_till_done()

        # Ausgänge an, FBH (lüftet mit Bad) aus, Bad-Status = Lüften, Schalter an mit Endzeit
        assert _on(hass, "input_boolean.fenster_bad")
        assert _on(hass, "input_boolean.klima_fenster_bad")
        assert not _on(hass, "input_boolean.fbh")
        st = manager.room_status("bad")
        assert st["source"] == SOURCE_VENT and st["target"] is None
        assert st["vent"]["active"] and st["vent"]["own"]
        assert manager.room_status("fbh")["source"] == SOURCE_VENT
        assert hass.states.get(sw).state == "on"
        assert hass.states.get(sw).attributes["bis"].startswith("2026-11-09T11:00")

        # Regler meldet Frostschutz (Fensterkontakt): keine Komfort-Korrektur, kein Sollwert
        hass.states.async_set("climate.bad", "heat", {**THERMO, "preset_mode": "building_protection"})
        n_temp, n_preset = len(set_temp), len(set_preset)
        clock.now += timedelta(minutes=10)
        await manager.async_apply(force=True)
        await hass.async_block_till_done()
        assert len(set_temp) == n_temp and len(set_preset) == n_preset

        # nach 60 min: Ende, Ausgänge aus, FBH wieder an, Sollwert + Komfort neu
        clock.now += timedelta(minutes=51)
        await manager.async_apply()
        await hass.async_block_till_done()
        assert not _on(hass, "input_boolean.fenster_bad")
        assert not _on(hass, "input_boolean.klima_fenster_bad")
        assert _on(hass, "input_boolean.fbh")
        assert manager.room_status("bad")["source"] != SOURCE_VENT
        assert set_preset[-1].data["preset_mode"] == "comfort"
        assert len(set_temp) > n_temp
        assert hass.states.get(sw).state == "off"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_vent_external_output_and_stop(hass: HomeAssistant):
    clock = Clock(dt_util.as_local(datetime(2026, 11, 9, 10, 0)).replace(tzinfo=dt_util.get_default_time_zone()))
    with patch("custom_components.heizplan.manager.dt_util.now", side_effect=clock):
        entry, manager, set_temp, set_preset = await _setup(hass)
        manager = await _rooms(hass, manager)
        await manager.async_apply(force=True)
        await hass.async_block_till_done()

        # Pad schaltet den Fensterkontakt (Ausgang) von außen ein -> Lüften startet, zweiter Ausgang folgt
        await hass.services.async_call("input_boolean", "turn_on", {"entity_id": "input_boolean.fenster_bad"},
                                       blocking=True)
        await hass.async_block_till_done()
        assert manager.room_status("bad")["vent"]["active"]
        assert _on(hass, "input_boolean.klima_fenster_bad")
        assert not _on(hass, "input_boolean.fbh")

        # erneut drücken (Ausgang aus) -> Lüften endet sofort
        clock.now += timedelta(minutes=5)
        await hass.services.async_call("input_boolean", "turn_off", {"entity_id": "input_boolean.fenster_bad"},
                                       blocking=True)
        await hass.async_block_till_done()
        assert not manager.room_status("bad")["vent"]["active"]
        assert not _on(hass, "input_boolean.klima_fenster_bad")
        assert _on(hass, "input_boolean.fbh")

        # Dienste
        await hass.services.async_call(DOMAIN, "start_ventilation", {"room": "bad", "minutes": 15}, blocking=True)
        await hass.async_block_till_done()
        assert manager.room_status("bad")["vent"]["until"].startswith("2026-11-09T10:20")
        await hass.services.async_call(DOMAIN, "stop_ventilation", {"room": "bad"}, blocking=True)
        await hass.async_block_till_done()
        assert not _on(hass, "input_boolean.fenster_bad")
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_vent_survives_reload_and_resends(hass: HomeAssistant):
    clock = Clock(dt_util.as_local(datetime(2026, 11, 9, 10, 0)).replace(tzinfo=dt_util.get_default_time_zone()))
    with patch("custom_components.heizplan.manager.dt_util.now", side_effect=clock):
        entry, manager, set_temp, set_preset = await _setup(hass)
        manager = await _rooms(hass, manager)
        await manager.async_set_vent("bad", True)
        await hass.async_block_till_done()
        await manager.async_flush()

        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        manager = next(iter(hass.data[DOMAIN].values()))
        assert manager.room_status("bad")["vent"]["active"]

        # Ausgang von Hand auf aus gesetzt, ohne Ereignis-Auswertung (z. B. Gerät neu gestartet):
        # spätestens beim Nachsenden wieder an
        hass.states.async_set("input_boolean.klima_fenster_bad", "off")
        clock.now += timedelta(minutes=16)
        await manager.async_apply()
        await hass.async_block_till_done()
        assert _on(hass, "input_boolean.klima_fenster_bad")
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_vent_stops_boost(hass: HomeAssistant):
    clock = Clock(dt_util.as_local(datetime(2026, 11, 9, 10, 0)).replace(tzinfo=dt_util.get_default_time_zone()))
    with patch("custom_components.heizplan.manager.dt_util.now", side_effect=clock):
        entry, manager, set_temp, set_preset = await _setup(hass)
        hass.states.async_set("climate.klima_bad", "off", {"hvac_modes": ["off", "heat"], "min_temp": 16,
                                                          "max_temp": 30, "target_temp_step": 0.5})
        manager = await _rooms(hass, manager)
        await manager.async_update_room("bad", aux_climate="climate.klima_bad")
        await hass.async_block_till_done()
        manager = next(iter(hass.data[DOMAIN].values()))
        manager.aux_state["bad"] = {"active": False, "owned": False, "since": None,
                                    "boost": True, "boost_since": clock.now.isoformat()}
        await manager.async_set_vent("bad", True)
        await hass.async_block_till_done()
        assert not manager.aux_state["bad"]["boost"]
        assert manager.room_status("bad")["aux_heating"]["demand"] is False
    assert await hass.config_entries.async_unload(entry.entry_id)
