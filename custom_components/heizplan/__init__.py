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
from .season import BASES, SEASON_MODES
from .websocket import async_register_websocket

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.SELECT, Platform.NUMBER, Platform.SWITCH, Platform.BINARY_SENSOR]

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


def _lovelace_resources(hass: HomeAssistant):
    """Ressourcen-Sammlung im Storage-Modus, sonst None."""
    try:
        from homeassistant.components.lovelace.const import LOVELACE_DATA
    except ImportError:  # sehr alte HA-Version
        return None
    data = hass.data.get(LOVELACE_DATA)
    resources = getattr(data, "resources", None)
    if resources is None or getattr(data, "resource_mode", "storage") != "storage":
        return None
    if not hasattr(resources, "async_create_item"):
        return None
    return resources


async def _async_register_resource(hass: HomeAssistant, card_url: str) -> bool:
    """Karte als Lovelace-Ressource eintragen bzw. auf die aktuelle Version setzen."""
    resources = _lovelace_resources(hass)
    if resources is None:
        return False
    try:
        await resources.async_get_info()  # stellt sicher, dass die Sammlung geladen ist
        ours = [
            item for item in resources.async_items()
            if str(item.get("url", "")).split("?", 1)[0] == CARD_URL
        ]
        if not ours:
            await resources.async_create_item({"res_type": "module", "url": card_url})
        else:
            if ours[0].get("url") != card_url:
                await resources.async_update_item(ours[0]["id"], {"res_type": "module", "url": card_url})
            for extra in ours[1:]:
                await resources.async_delete_item(extra["id"])
    except Exception as err:  # noqa: BLE001 – im Zweifel der alte Weg
        _LOGGER.warning("Heizplan: Karte nicht als Dashboard-Ressource eintragbar (%s)", err)
        return False
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Integration gelöscht: Dashboard-Ressource wieder entfernen."""
    resources = _lovelace_resources(hass)
    if resources is None:
        return
    try:
        await resources.async_get_info()
        for item in list(resources.async_items()):
            if str(item.get("url", "")).split("?", 1)[0] == CARD_URL:
                await resources.async_delete_item(item["id"])
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Heizplan: Dashboard-Ressource nicht entfernbar (%s)", err)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    path = Path(__file__).parent / "www" / "heizplan-card.js"
    # Mit Cache-Headern ausliefern: die URL trägt die Version, ein Update lädt also neu
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(path), True)]
    )
    card_url = f"{CARD_URL}?v={VERSION}"
    # Als Dashboard-Ressource eintragen: dann lädt das Frontend die Karte zusammen mit
    # den anderen Karten, bevor das Dashboard aufgebaut wird (sonst kurz „Konfigurationsfehler“).
    if not await _async_register_resource(hass, card_url):
        add_extra_js_url(hass, card_url)
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
        value = call.data["on"] if "on" in call.data else call.data.get("temperature")
        if value is None:
            raise ServiceValidationError("temperature oder on angeben")
        await manager.async_set_override(room_id, value, call.data.get("minutes", 120))

    async def clear_override(call: ServiceCall) -> None:
        manager = get_manager(hass)
        await manager.async_clear_override(_resolve_room(manager, call.data["room"]))

    async def update_room(call: ServiceCall) -> None:
        manager = get_manager(hass)
        try:
            await manager.async_update_room(
                _resolve_room(manager, call.data["room"]),
                call.data.get("name"), call.data.get("valve"),
                call.data.get("follow_season"), call.data.get("months"),
                aux_climate=call.data.get("aux_climate"),
                aux_fan_mode=call.data.get("aux_fan_mode"),
                aux_fan_switch=call.data.get("aux_fan_switch"),
                aux_enabled=call.data.get("aux_enabled"),
            )
        except PlanError as err:
            raise ServiceValidationError(str(err)) from err

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
                vol.Exclusive("temperature", "value"): vol.All(vol.Coerce(float), vol.Range(5, 30)),
                vol.Exclusive("on", "value"): cv.boolean,
                vol.Optional("minutes", default=120): vol.All(vol.Coerce(int), vol.Range(1, 7 * 24 * 60)),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN, "clear_override", clear_override, schema=vol.Schema({vol.Required("room"): cv.string})
    )
    hass.services.async_register(
        DOMAIN,
        "update_room",
        update_room,
        schema=vol.Schema(
            {
                vol.Required("room"): cv.string,
                vol.Optional("name"): cv.string,
                vol.Optional("valve"): cv.string,
                vol.Optional("follow_season"): cv.boolean,
                vol.Optional("months"): [vol.All(vol.Coerce(int), vol.Range(1, 12))],
                # Zuheizen mit Klima ("" = entfernen)
                vol.Optional("aux_climate"): vol.Any(cv.entity_id, ""),
                vol.Optional("aux_fan_mode"): cv.string,
                vol.Optional("aux_fan_switch"): vol.Any(cv.entity_id, ""),
                vol.Optional("aux_enabled"): cv.boolean,
            }
        ),
    )

    async def boost(call: ServiceCall) -> None:
        manager = get_manager(hass)
        try:
            await manager.async_set_boost(_resolve_room(manager, call.data["room"]), call.data.get("on", True))
        except PlanError as err:
            raise ServiceValidationError(str(err)) from err

    hass.services.async_register(
        DOMAIN, "boost", boost,
        schema=vol.Schema({vol.Required("room"): cv.string, vol.Optional("on", default=True): cv.boolean}),
    )

    async def set_aux(call: ServiceCall) -> None:
        try:
            await get_manager(hass).async_set_aux_settings(dict(call.data))
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    hass.services.async_register(
        DOMAIN,
        "set_aux",
        set_aux,
        schema=vol.Schema(
            {
                vol.Optional("on_delta"): vol.Coerce(float),
                vol.Optional("off_delta"): vol.Coerce(float),
                vol.Optional("wait"): vol.Coerce(int),
                vol.Optional("outside_limit"): vol.Coerce(float),
                vol.Optional("takeover"): cv.boolean,
                vol.Optional("boost_max"): vol.Coerce(int),
            }
        ),
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

    async def set_season_auto(call: ServiceCall) -> None:
        cfg = {k: v for k, v in call.data.items()}
        try:
            await get_manager(hass).async_set_season_auto(cfg)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    hass.services.async_register(
        DOMAIN,
        "set_season_auto",
        set_season_auto,
        schema=vol.Schema(
            {
                vol.Optional("mode"): vol.In(SEASON_MODES),
                vol.Optional("on_date"): cv.string,
                vol.Optional("off_date"): cv.string,
                vol.Optional("weather_entity"): cv.entity_id,
                vol.Optional("on_below"): vol.Coerce(float),
                vol.Optional("off_above"): vol.Coerce(float),
                vol.Optional("days"): vol.All(vol.Coerce(int), vol.Range(1, 10)),
                vol.Optional("basis"): vol.In(BASES),
            }
        ),
    )
