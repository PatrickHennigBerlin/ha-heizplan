"""Integrationstest mit echter HA-Testinstanz."""

from datetime import datetime
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.heizplan.const import DOMAIN

SCHEDULE_ATTRS = {
    "weekdays": ["daily"],
    "timeslots": ["00:00:00 - 06:00:00", "06:00:00 - 22:00:00", "22:00:00 - 00:00:00"],
    "entities": ["climate.lounge"],
    "actions": [
        {"service": "climate.set_temperature", "data": {"temperature": 17}},
        {"service": "climate.set_temperature", "data": {"temperature": 22}},
        {"service": "climate.set_temperature", "data": {"temperature": 16}},
    ],
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


async def _setup(hass: HomeAssistant, options=None):
    await async_setup_component(hass, "http", {})
    await async_setup_component(hass, "websocket_api", {})
    hass.config.components.add("frontend")  # echtes Frontend-Paket fehlt im Test
    hass.states.async_set(
        "climate.lounge",
        "heat",
        {
            "current_temperature": 20.6,
            "temperature": 0,
            "min_temp": 16,
            "max_temp": 28,
            "preset_mode": "standby",
            "preset_modes": ["comfort", "standby", "economy", "building_protection"],
            "friendly_name": "Heizung Wohnzimmer",
        },
    )
    hass.states.async_set("switch.schedule_test", "on", SCHEDULE_ATTRS)
    hass.states.async_set("switch.heizperiode", "on")
    set_temp = async_mock_service(hass, "climate", "set_temperature")
    set_preset = async_mock_service(hass, "climate", "set_preset_mode")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Heizplan",
        unique_id=DOMAIN,
        options=options or {"season_entity": "switch.heizperiode", "keep_comfort": True, "comfort_preset": "comfort"},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.heizplan.add_extra_js_url"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, set_temp, set_preset


async def test_import_write_override_away(hass: HomeAssistant, hass_ws_client):
    fixed = dt_util.as_local(datetime(2026, 10, 12, 12, 0)).replace(tzinfo=dt_util.get_default_time_zone())
    with patch("custom_components.heizplan.manager.dt_util.now", return_value=fixed):
        entry, set_temp, set_preset = await _setup(hass)

        res = await hass.services.async_call(
            DOMAIN, "import_scheduler", {"entities": ["switch.schedule_test"]},
            blocking=True, return_response=True,
        )
        assert res["imported"][0]["climate"] == "climate.lounge"
        await hass.async_block_till_done()
        await hass.async_block_till_done()

        # Raum angelegt, Plan 12:00 -> 22 °C geschrieben, vorher Komfort gesetzt
        assert set_preset and set_preset[-1].data["preset_mode"] == "comfort"
        assert set_temp[-1].data == {"entity_id": "climate.lounge", "temperature": 22.0}

        sensor = hass.states.get("sensor.heizplan_heizung_wohnzimmer_soll")
        assert sensor is not None, [s.entity_id for s in hass.states.async_all("sensor")]
        assert float(sensor.state) == 22.0
        assert sensor.attributes["quelle"] == "Wochenplan"

        client = await hass_ws_client(hass)
        await client.send_json({"id": 1, "type": "heizplan/get"})
        msg = await client.receive_json()
        assert msg["success"]
        room = msg["result"]["rooms"]["lounge"]
        assert room["plan"]["mon"][1] == {"start": "06:00", "temp": 22.0}

        # Override
        await client.send_json({"id": 2, "type": "heizplan/set_override", "room_id": "lounge", "temp": 24, "minutes": 60})
        msg = await client.receive_json()
        assert msg["success"], msg
        assert msg["result"]["rooms"]["lounge"]["status"]["source"] == "override"
        assert set_temp[-1].data["temperature"] == 24.0

        # Abwesend hat Vorrang? -> Override bleibt Vorrang, nach clear gilt Abwesend
        await client.send_json({"id": 3, "type": "heizplan/set_mode", "mode": "away"})
        await client.receive_json()
        await client.send_json({"id": 4, "type": "heizplan/clear_override", "room_id": "lounge"})
        msg = await client.receive_json()
        assert msg["result"]["rooms"]["lounge"]["status"]["target"] == 17.0
        assert set_temp[-1].data["temperature"] == 17.0

        # Tag bearbeiten + kopieren
        await client.send_json({"id": 5, "type": "heizplan/set_mode", "mode": "plan"})
        await client.receive_json()
        await client.send_json({
            "id": 6, "type": "heizplan/set_day", "room_id": "lounge", "day": "mon",
            "blocks": [{"start": "00:00", "temp": 18}, {"start": "11:00", "temp": 21.5}],
            "copy_to": ["tue"],
        })
        msg = await client.receive_json()
        assert msg["success"], msg
        assert msg["result"]["rooms"]["lounge"]["plan"]["tue"] == [
            {"start": "00:00", "temp": 18.0}, {"start": "11:00", "temp": 21.5}]
        assert set_temp[-1].data["temperature"] == 21.5

        # Heizperiode aus -> nichts mehr schreiben
        count = len(set_temp)
        hass.states.async_set("switch.heizperiode", "off")
        await hass.async_block_till_done()
        await client.send_json({"id": 7, "type": "heizplan/set_override", "room_id": "lounge", "temp": 25, "minutes": 60})
        msg = await client.receive_json()
        assert msg["result"]["rooms"]["lounge"]["status"]["source"] == "inactive"
        assert len(set_temp) == count

        # Vorlage
        await client.send_json({"id": 8, "type": "heizplan/save_template", "name": "Winter", "plan": room["plan"]})
        msg = await client.receive_json()
        assert msg["result"]["template_id"] == "winter"

        # Heizperiode wieder an, Regler steht auf Frostschutz -> einmalig auf Komfort
        frost = {**hass.states.get("climate.lounge").attributes, "preset_mode": "building_protection"}
        hass.states.async_set("climate.lounge", "heat", frost)
        presets_before = len(set_preset)
        hass.states.async_set("switch.heizperiode", "on")
        await hass.async_block_till_done()
        assert len(set_preset) == presets_before + 1
        assert set_preset[-1].data["preset_mode"] == "comfort"

        # Danach (Sonderfall) Frostschutz -> bei normalem Schreiben NICHT anfassen
        hass.states.async_set("climate.lounge", "heat", frost)
        await hass.async_block_till_done()
        presets_before = len(set_preset)
        await client.send_json({"id": 9, "type": "heizplan/clear_override", "room_id": "lounge"})
        await client.receive_json()
        assert len(set_preset) == presets_before
        assert set_temp[-1].data["temperature"] == 21.5

    assert await hass.config_entries.async_unload(entry.entry_id)


FBH_ATTRS = {
    "weekdays": ["daily"],
    "timeslots": ["00:00:00 - 06:00:00", "06:00:00 - 09:00:00", "09:00:00 - 00:00:00"],
    "entities": ["switch.fussbodenheizung"],
    "actions": [{"service": "switch.turn_off"}, {"service": "switch.turn_on"}, {"service": "switch.turn_off"}],
}


async def test_floor_heating_switch_room(hass: HomeAssistant, hass_ws_client):
    fixed = dt_util.as_local(datetime(2026, 10, 12, 7, 0)).replace(tzinfo=dt_util.get_default_time_zone())
    with patch("custom_components.heizplan.manager.dt_util.now", return_value=fixed):
        entry, set_temp, set_preset = await _setup(hass)
        hass.states.async_set("switch.fussbodenheizung", "off", {"friendly_name": "Fussbodenheizung"})
        hass.states.async_set("switch.schedule_fbh", "on", FBH_ATTRS)
        turn_on = async_mock_service(hass, "switch", "turn_on")
        turn_off = async_mock_service(hass, "switch", "turn_off")

        await hass.services.async_call(DOMAIN, "import_scheduler",
                                       {"entities": ["switch.schedule_fbh", "switch.schedule_test"]},
                                       blocking=True, return_response=True)
        await hass.async_block_till_done()
        await hass.async_block_till_done()
        # 07:00 -> An
        assert turn_on and turn_on[-1].data["entity_id"] == "switch.fussbodenheizung"

        # unabhängig von der Heizperiode, nur Okt–Apr
        await hass.services.async_call(DOMAIN, "update_room",
                                       {"room": "switch.fussbodenheizung", "follow_season": False,
                                        "months": [10, 11, 12, 1, 2, 3, 4]}, blocking=True)
        await hass.async_block_till_done()
        sensor = hass.states.get("sensor.heizplan_fussbodenheizung_soll")
        assert sensor is not None and sensor.state == "Ein"

        client = await hass_ws_client(hass)
        await client.send_json({"id": 1, "type": "heizplan/set_override", "room_id": "fussbodenheizung",
                                "on": False, "minutes": 60})
        msg = await client.receive_json()
        assert msg["success"], msg
        room = msg["result"]["rooms"]["fussbodenheizung"]
        assert room["kind"] == "switch" and room["status"]["target"] is False
        assert turn_off and turn_off[-1].data["entity_id"] == "switch.fussbodenheizung"

        # Heizperiode aus -> Heizkörper inaktiv, Fußbodenheizung (follow_season False) weiter aktiv
        hass.states.async_set("switch.heizperiode", "off")
        await hass.async_block_till_done()
        await client.send_json({"id": 2, "type": "heizplan/get"})
        msg = await client.receive_json()
        assert msg["result"]["rooms"]["lounge"]["status"]["source"] == "inactive"
        assert msg["result"]["rooms"]["fussbodenheizung"]["status"]["source"] == "override"

        # Tag bearbeiten mit An/Aus-Blöcken
        await client.send_json({"id": 3, "type": "heizplan/set_day", "room_id": "fussbodenheizung", "day": "mon",
                                "blocks": [{"start": "00:00", "on": False}, {"start": "05:30", "on": True}]})
        msg = await client.receive_json()
        assert msg["success"], msg
        assert msg["result"]["rooms"]["fussbodenheizung"]["plan"]["mon"][1] == {"start": "05:30", "on": True}

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_season_auto_weather(hass: HomeAssistant, hass_ws_client):
    """Wetter-Automatik: kalt -> an; Hand-Aus wird bei gleicher Lage nicht überstimmt; warm -> aus."""
    from homeassistant.core import SupportsResponse

    fixed = dt_util.as_local(datetime(2026, 10, 12, 6, 5)).replace(tzinfo=dt_util.get_default_time_zone())
    forecast = {"temps": [(8, 2)] * 7}

    async def get_forecasts(call):
        start = fixed.date()
        return {
            "weather.home": {
                "forecast": [
                    {"datetime": f"{start.replace(day=start.day + i).isoformat()}T12:00:00+00:00",
                     "temperature": hi, "templow": lo}
                    for i, (hi, lo) in enumerate(forecast["temps"])
                ]
            }
        }

    hass.services.async_register("weather", "get_forecasts", get_forecasts, supports_response=SupportsResponse.ONLY)

    with patch("custom_components.heizplan.manager.dt_util.now", return_value=fixed):
        entry, _, _ = await _setup(hass)
        hass.states.async_set("switch.heizperiode", "off")
        turn_on = async_mock_service(hass, "switch", "turn_on")
        turn_off = async_mock_service(hass, "switch", "turn_off")
        manager = hass.data[DOMAIN][entry.entry_id]

        await hass.services.async_call(
            DOMAIN, "set_season_auto", {"mode": "weather", "weather_entity": "weather.home"}, blocking=True
        )
        await hass.async_block_till_done()
        assert len(turn_on) == 1 and turn_on[0].data["entity_id"] == "switch.heizperiode"
        assert manager.season_status["values"] == [5.0] * 6

        # Mock schaltet nicht wirklich -> Zustand selbst setzen; danach Hand-Aus
        hass.states.async_set("switch.heizperiode", "off")
        await manager.async_season_check()
        assert len(turn_on) == 1  # gleiche Lage -> keine neue Schaltung

        forecast["temps"] = [(20, 12)] * 7  # Mittel 16 > 15
        hass.states.async_set("switch.heizperiode", "on")
        await manager.async_season_check()
        assert len(turn_off) == 1

        ws = await hass_ws_client(hass)
        await ws.send_json({"id": 1, "type": "heizplan/get"})
        msg = await ws.receive_json()
        assert msg["result"]["season_auto"]["mode"] == "weather"
        assert msg["result"]["season_status"]["decision"] is False

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_season_auto_dates(hass: HomeAssistant):
    fixed = dt_util.as_local(datetime(2026, 10, 1, 0, 5)).replace(tzinfo=dt_util.get_default_time_zone())
    with patch("custom_components.heizplan.manager.dt_util.now", return_value=fixed):
        entry, _, _ = await _setup(hass)
        hass.states.async_set("switch.heizperiode", "off")
        turn_on = async_mock_service(hass, "switch", "turn_on")
        manager = hass.data[DOMAIN][entry.entry_id]
        await manager.async_set_season_auto({"mode": "dates", "on_date": "01.10.", "off_date": "01.05."})
        assert len(turn_on) == 1
        await manager.async_season_check()   # gleicher Tag -> nicht nochmal
        assert len(turn_on) == 1
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
