"""Heizplan – Wochenpläne für Heizungen mit echter Solltemperatur."""

from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.config_validation as cv

from .const import CARD_URL, DOMAIN, MODES, VERSION
from .manager import HeizplanManager, room_id_from_entity
from .plan import PlanError
from .websocket import async_register_websocket

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.SELECT, Platform.NUMBER]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def get_manager(hass: HomeAssistant) -> HeizplanManager:
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        raise HomeAssistantError("Heizplan ist nicht eingerichtet")
    return next(iter(entries.values()))


def _resolve_room(manager: HeizplanManager, value: str) -> str:
    if value in manager.rooms:
        return value
    rid = room_id_from_entity(value)
    if rid in manager.rooms:
        return rid
    for room_id, room in manager.rooms.items():
        if room["climate"] == value or room.get("name", "").lower() == value.lower():
            return room_id
    raise ServiceValidationError(f"Unbekannter Raum: {value}")


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    path = Path(__file__).parent / "www" / "heizplan-card.js"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(path), False)]
    )
    add_extra_js_url(hass, f"{CARD_URL}?v={VERSION}")
    async_register_websocket(hass)
    _register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    manager = HeizplanManager(hass, entry)
    await manager.async_load()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await manager.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    manager: HeizplanManager = hass.data[DOMAIN][entry.entry_id]
    manager.async_stop()
    await manager.async_flush()
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return ok


def _register_services(hass: HomeAssistant) -> None:
    async def set_mode(call: ServiceCall) -> None:
        await get_manager(hass).async_set_mode(call.data["mode"])

    async def set_override(call: ServiceCall) -> None:
        manager = get_manager(hass)
        room_id = _resolve_room(manager, call.data["room"])
        await manager.async_set_override(
            room_id, call.data["temperature"], call.data.get("minutes", 120)
        )

    async def clear_override(call: ServiceCall) -> None:
        manager = get_manager(hass)
        await manager.async_clear_override(_resolve_room(manager, call.data["room"]))

    async def import_scheduler(call: ServiceCall) -> dict:
        try:
            return await get_manager(hass).async_import_scheduler(
                call.data["entities"], call.data.get("create_rooms", True)
            )
        except PlanError as err:
            raise ServiceValidationError(str(err)) from err

    hass.services.async_register(
        DOMAIN, "set_mode", set_mode, schema=vol.Schema({vol.Required("mode"): vol.In(MODES)})
    )
    hass.services.async_register(
        DOMAIN,
        "set_override",
        set_override,
        schema=vol.Schema(
            {
                vol.Required("room"): cv.string,
                vol.Required("temperature"): vol.All(vol.Coerce(float), vol.Range(5, 30)),
                vol.Optional("minutes", default=120): vol.All(vol.Coerce(int), vol.Range(1, 7 * 24 * 60)),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN, "clear_override", clear_override, schema=vol.Schema({vol.Required("room"): cv.string})
    )
    hass.services.async_register(
        DOMAIN,
        "import_scheduler",
        import_scheduler,
        schema=vol.Schema(
            {
                vol.Required("entities"): cv.entity_ids,
                vol.Optional("create_rooms", default=True): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
