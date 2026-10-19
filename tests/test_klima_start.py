"""Klima sicher einschalten: EIN zuerst, Bestätigung abwarten, dann Modus und Soll."""
from __future__ import annotations

import asyncio

import pytest
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.heizplan import klima_start

ENT = "climate.klima_kuche"


@pytest.fixture
def order(hass):
    seq: list[tuple[str, dict]] = []

    def mock(domain, service):
        async def handler(call):
            seq.append((f"{domain}.{service}", dict(call.data)))
        hass.services.async_register(domain, service, handler)

    for d, s in (("climate", "turn_on"), ("climate", "set_hvac_mode"), ("climate", "set_temperature"),
                 ("knx", "read"), ("homeassistant", "update_entity"), ("switch", "turn_on")):
        mock(d, s)
    return seq


async def _call(hass):
    async def call(domain, service, data):
        await hass.services.async_call(domain, service, data, blocking=True)
        return True
    return call


def _tg(ga, payload, kind="GroupValueWrite"):
    return {"destination": ga, "direction": "Incoming", "telegramtype": kind, "payload": payload}


async def test_ohne_knx_reihenfolge_ein_modus_soll(hass, order):
    hass.states.async_set(ENT, "off", {"supported_features": 393})
    ok = await klima_start.async_start_climate(hass, ENT, "heat", 23, await _call(hass))
    assert ok
    assert [s for s, _ in order] == ["climate.turn_on", "climate.set_hvac_mode", "climate.set_temperature"]


async def test_knx_wartet_auf_bestaetigung(hass, order, monkeypatch):
    hass.states.async_set(ENT, "off", {"supported_features": 393})
    monkeypatch.setattr(klima_start, "knx_state_addresses", lambda h, e: ("3/0/7", "3/0/4"))
    monkeypatch.setattr(klima_start, "WAIT_SPONTANEOUS", 0.5)
    monkeypatch.setattr(klima_start, "WAIT_READ", 0.5)

    task = hass.async_create_task(klima_start.async_start_climate(hass, ENT, "heat", 23, await _call(hass)))
    await asyncio.sleep(0.05)
    assert [s for s, _ in order] == ["climate.turn_on"]          # nichts weiter vor der Bestätigung
    async_dispatcher_send(hass, "knx_telegram", None, _tg("3/0/7", 0))   # alter Zustand: zählt nicht
    await asyncio.sleep(0.05)
    assert [s for s, _ in order] == ["climate.turn_on"]
    async_dispatcher_send(hass, "knx_telegram", None, _tg("3/0/7", 1))
    await asyncio.sleep(0.05)
    assert [s for s, _ in order][-1] == "climate.set_hvac_mode"
    async_dispatcher_send(hass, "knx_telegram", None, _tg("3/0/4", (1,), "GroupValueResponse"))
    assert await task
    assert [s for s, _ in order] == ["climate.turn_on", "climate.set_hvac_mode", "climate.set_temperature"]


async def test_knx_liest_status_und_wiederholt(hass, order, monkeypatch):
    hass.states.async_set(ENT, "off", {"supported_features": 393})
    monkeypatch.setattr(klima_start, "knx_state_addresses", lambda h, e: ("3/0/7", None))
    monkeypatch.setattr(klima_start, "WAIT_SPONTANEOUS", 0.05)
    monkeypatch.setattr(klima_start, "WAIT_READ", 0.05)
    ok = await klima_start.async_start_climate(hass, ENT, "heat", 23, await _call(hass))
    assert not ok
    names = [s for s, _ in order]
    assert names == ["climate.turn_on", "knx.read"] * 3 + ["homeassistant.update_entity"]
    assert order[1][1]["address"] == ["3/0/7"] or order[1][1]["address"] == "3/0/7"


async def test_luefter_auto_zum_schluss(hass, order):
    hass.states.async_set(ENT, "off", {"supported_features": 393})
    ok = await klima_start.async_start_climate(hass, ENT, "heat", 23, await _call(hass),
                                               fan_auto_switch="switch.lufter_auto_kuche")
    assert ok
    assert [s for s, _ in order] == ["climate.turn_on", "climate.set_hvac_mode", "climate.set_temperature",
                                     "switch.turn_on"]
    assert order[-1][1]["entity_id"] == "switch.lufter_auto_kuche"
