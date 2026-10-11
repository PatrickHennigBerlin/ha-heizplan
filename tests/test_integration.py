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

    assert await hass.config_entries.async_unload(entry.entry_id)
