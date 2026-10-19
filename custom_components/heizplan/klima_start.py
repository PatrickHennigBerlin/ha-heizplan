"""Klimagerät sicher einschalten: zuerst EIN, auf die Bestätigung des Geräts warten, dann Modus und Soll.

Hintergrund (KNX / Intesis ME-AC-KNX): Telegramme, die vor oder während des Einschaltens kommen, verwirft das
Gateway leicht (Neustart, Kommunikationsaussetzer zur Inneneinheit). HA's KNX-Klima sendet bei set_hvac_mode
zuerst den Modus und erst danach EIN. Deshalb hier:

1. climate.turn_on  (nur das EIN-Telegramm)
2. warten, bis das Gerät auf seiner Status-Adresse EIN meldet (eingehendes Telegramm); kommt nichts, die
   Status-Adresse lesen; bleibt die Bestätigung aus, EIN wiederholen (höchstens ATTEMPTS-mal)
3. climate.set_hvac_mode  (Modus) – ebenso bestätigen lassen, wenn die Status-Adresse bekannt ist
4. climate.set_temperature (Soll)
5. Lüfter auf Auto (Schalter „Lüfter Auto“, falls angegeben) – ebenso bestätigt über dessen Status-Adresse

Die Status-Adressen kommen aus der KNX-Klima-Entität selbst. Ist das Gerät keine KNX-Klima (oder die Adressen
sind nicht zu ermitteln), wird ohne Bestätigung in derselben Reihenfolge geschaltet.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

_LOGGER = logging.getLogger(__name__)

SIGNAL_KNX_TELEGRAM = "knx_telegram"   # homeassistant.components.knx.const.SIGNAL_KNX_TELEGRAM
FEATURE_TURN_ON = 128                  # ClimateEntityFeature.TURN_ON
ATTEMPTS = 3
WAIT_SPONTANEOUS = 15.0                # s: das Gerät meldet seinen Zustand meist von selbst (Test Küche: 11 s)
WAIT_READ = 8.0                        # s: Antwort auf eine Leseanfrage
# DPT 20.105 (HVAC-Controller-Modus) – Werte wie beim Intesis-Gateway (Status 0x01 = Heizen)
CONTROLLER_CODES = {"heat": 1, "cool": 3, "fan_only": 9, "dry": 14}

Call = Callable[[str, str, dict[str, Any]], Awaitable[bool]]


def knx_state_addresses(hass: HomeAssistant, entity_id: str) -> tuple[str | None, str | None]:
    """(EIN-Status-GA, Modus-Status-GA) einer KNX-Klima, sonst (None, None)."""
    try:
        comp = hass.data.get("entity_components", {}).get("climate")
        entity = comp.get_entity(entity_id) if comp is not None else None
        if entity is None or getattr(entity, "platform", None) is None or entity.platform.platform_name != "knx":
            return None, None
        device = entity._device  # noqa: SLF001 – xknx-Gerät der KNX-Klima
        on_ga = getattr(getattr(device, "on", None), "group_address_state", None)
        mode = getattr(device, "mode", None)
        mode_ga = getattr(getattr(mode, "remote_value_controller_mode", None), "group_address_state", None)
        return (str(on_ga) if on_ga else None), (str(mode_ga) if mode_ga else None)
    except Exception:  # noqa: BLE001 – Interna können sich ändern: dann ohne Bestätigung
        _LOGGER.debug("Klima-Start: Status-Adressen von %s nicht ermittelbar", entity_id, exc_info=True)
        return None, None


def knx_switch_state_address(hass: HomeAssistant, entity_id: str) -> str | None:
    """Status-GA eines KNX-Schalters, sonst None."""
    try:
        comp = hass.data.get("entity_components", {}).get(entity_id.split(".", 1)[0])
        entity = comp.get_entity(entity_id) if comp is not None else None
        if entity is None or getattr(entity, "platform", None) is None or entity.platform.platform_name != "knx":
            return None
        ga = getattr(getattr(entity._device, "switch", None), "group_address_state", None)  # noqa: SLF001
        return str(ga) if ga else None
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Klima-Start: Status-Adresse von %s nicht ermittelbar", entity_id, exc_info=True)
        return None


def _payload_int(payload: Any) -> int | None:
    if isinstance(payload, bool):
        return int(payload)
    if isinstance(payload, int):
        return payload
    if isinstance(payload, (tuple, list)) and len(payload) == 1 and isinstance(payload[0], int):
        return payload[0]
    return None


async def _confirm(hass: HomeAssistant, ga: str, expected: int, send: Callable[[], Awaitable[bool]],
                   call: Call, label: str) -> bool:
    """Senden und auf eingehendes Telegramm ga == expected warten; ggf. lesen und wiederholen."""
    for attempt in range(1, ATTEMPTS + 1):
        got: asyncio.Event = asyncio.Event()
        seen: list[int | None] = []

        @callback
        def _telegram(*args: Any) -> None:
            tg = args[-1] if args else None
            if not isinstance(tg, dict) or tg.get("destination") != ga or tg.get("direction") != "Incoming":
                return
            if tg.get("telegramtype") not in ("GroupValueWrite", "GroupValueResponse"):
                return
            value = _payload_int(tg.get("payload"))
            seen.append(value)
            if value == expected:
                got.set()

        unsub = async_dispatcher_connect(hass, SIGNAL_KNX_TELEGRAM, _telegram)
        try:
            if not await send():
                return False
            try:
                await asyncio.wait_for(got.wait(), WAIT_SPONTANEOUS)
            except TimeoutError:
                await call("knx", "read", {"address": ga})
                try:
                    await asyncio.wait_for(got.wait(), WAIT_READ)
                except TimeoutError:
                    pass
        finally:
            unsub()
        if got.is_set():
            if attempt > 1:
                _LOGGER.info("Klima-Start: %s im %s. Versuch bestätigt", label, attempt)
            return True
        _LOGGER.warning("Klima-Start: %s nicht bestätigt (Versuch %s/%s, gemeldet: %s)",
                        label, attempt, ATTEMPTS, seen or "nichts")
    return False


async def async_start_climate(hass: HomeAssistant, entity_id: str, hvac_mode: str, temperature: float,
                              call: Call, fan_auto_switch: str | None = None) -> bool:
    """Klima einschalten: EIN (bestätigt) → Modus (bestätigt) → Soll → Lüfter Auto. True, wenn Ein/Modus/Soll durchgingen."""
    on_ga, mode_ga = knx_state_addresses(hass, entity_id)
    data = {ATTR_ENTITY_ID: entity_id}

    state = hass.states.get(entity_id)
    features = int(state.attributes.get("supported_features", 0)) if state is not None else 0

    async def _on() -> bool:
        return await call("climate", "turn_on", data)

    async def _mode() -> bool:
        return await call("climate", "set_hvac_mode", {**data, "hvac_mode": hvac_mode})

    if on_ga:
        if not await _confirm(hass, on_ga, 1, _on, call, f"{entity_id} EIN"):
            _LOGGER.error("Klima-Start: %s meldet nach %s Versuchen kein EIN – abgebrochen", entity_id, ATTEMPTS)
            await call("homeassistant", "update_entity", data)   # HA-Anzeige wieder auf den Gerätezustand
            return False
    elif features & FEATURE_TURN_ON:
        if not await _on():
            return False

    code = CONTROLLER_CODES.get(hvac_mode)
    if on_ga and mode_ga and code is not None:
        if not await _confirm(hass, mode_ga, code, _mode, call, f"{entity_id} Modus {hvac_mode}"):
            _LOGGER.error("Klima-Start: %s bestätigt Modus %s nicht – abgebrochen", entity_id, hvac_mode)
            await call("homeassistant", "update_entity", data)
            return False
    elif not await _mode():
        return False

    if not await call("climate", "set_temperature", {**data, "temperature": temperature}):
        return False

    if fan_auto_switch:
        fan_data = {ATTR_ENTITY_ID: fan_auto_switch}

        async def _fan() -> bool:
            return await call(fan_auto_switch.split(".", 1)[0], "turn_on", fan_data)

        fan_ga = knx_switch_state_address(hass, fan_auto_switch)
        if fan_ga:
            if not await _confirm(hass, fan_ga, 1, _fan, call, f"{fan_auto_switch} Lüfter Auto"):
                _LOGGER.warning("Klima-Start: %s – Lüfter Auto nicht bestätigt", entity_id)
        else:
            await _fan()
    return True
