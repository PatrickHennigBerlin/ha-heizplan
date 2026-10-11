"""WebSocket-API für die Heizplan-Karte."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, MODES, SIGNAL_UPDATED
from .plan import DAYS, PlanError


def _manager(hass: HomeAssistant):
    entries = hass.data.get(DOMAIN, {})
    return next(iter(entries.values())) if entries else None


def _wrap(handler):
    """Gemeinsame Fehlerbehandlung + Antwort mit vollem Zustand."""

    async def _inner(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
        manager = _manager(hass)
        if manager is None:
            connection.send_error(msg["id"], "not_setup", "Heizplan ist nicht eingerichtet")
            return
        try:
            extra = await handler(hass, manager, msg)
        except (PlanError, KeyError, ValueError) as err:
            connection.send_error(msg["id"], "invalid", str(err))
            return
        result = manager.full_state()
        if extra:
            result.update(extra)
        connection.send_result(msg["id"], result)

    return _inner


@websocket_api.websocket_command({vol.Required("type"): "heizplan/get"})
@websocket_api.async_response
@_wrap
async def ws_get(hass, manager, msg):
    return None


@websocket_api.websocket_command({vol.Required("type"): "heizplan/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    @callback
    def _forward() -> None:
        manager = _manager(hass)
        if manager is not None:
            connection.send_message(websocket_api.event_message(msg["id"], manager.full_state()))

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(hass, SIGNAL_UPDATED, _forward)
    connection.send_result(msg["id"])
    _forward()


@websocket_api.websocket_command(
    {
        vol.Required("type"): "heizplan/set_day",
        vol.Required("room_id"): str,
        vol.Required("day"): vol.In(DAYS),
        vol.Required("blocks"): list,
        vol.Optional("copy_to", default=[]): [vol.In(DAYS)],
    }
)
@websocket_api.async_response
@_wrap
async def ws_set_day(hass, manager, msg):
    await manager.async_set_day(msg["room_id"], msg["day"], msg["blocks"], msg["copy_to"])


@websocket_api.websocket_command(
    {vol.Required("type"): "heizplan/set_plan", vol.Required("room_id"): str, vol.Required("plan"): dict}
)
@websocket_api.async_response
@_wrap
async def ws_set_plan(hass, manager, msg):
    await manager.async_set_plan(msg["room_id"], msg["plan"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "heizplan/set_override",
        vol.Required("room_id"): str,
        vol.Exclusive("temp", "value"): vol.Coerce(float),
        vol.Exclusive("on", "value"): bool,
        vol.Required("minutes"): vol.Coerce(int),
    }
)
@websocket_api.async_response
@_wrap
async def ws_set_override(hass, manager, msg):
    value = msg["on"] if "on" in msg else msg.get("temp")
    if value is None:
        raise ValueError("temp oder on fehlt")
    await manager.async_set_override(msg["room_id"], value, msg["minutes"])


@websocket_api.websocket_command(
    {vol.Required("type"): "heizplan/clear_override", vol.Required("room_id"): str}
)
@websocket_api.async_response
@_wrap
async def ws_clear_override(hass, manager, msg):
    await manager.async_clear_override(msg["room_id"])


@websocket_api.websocket_command(
    {vol.Required("type"): "heizplan/set_mode", vol.Required("mode"): vol.In(MODES)}
)
@websocket_api.async_response
@_wrap
async def ws_set_mode(hass, manager, msg):
    await manager.async_set_mode(msg["mode"])


@websocket_api.websocket_command(
    {vol.Required("type"): "heizplan/set_away_temp", vol.Required("temp"): vol.Coerce(float)}
)
@websocket_api.async_response
@_wrap
async def ws_set_away_temp(hass, manager, msg):
    await manager.async_set_away_temp(msg["temp"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "heizplan/save_template",
        vol.Optional("template_id"): vol.Any(str, None),
        vol.Required("name"): str,
        vol.Required("plan"): dict,
        vol.Optional("kind", default="climate"): vol.In(["climate", "switch"]),
    }
)
@websocket_api.async_response
@_wrap
async def ws_save_template(hass, manager, msg):
    tid = await manager.async_save_template(msg.get("template_id"), msg["name"], msg["plan"], msg["kind"])
    return {"template_id": tid}


@websocket_api.websocket_command(
    {vol.Required("type"): "heizplan/delete_template", vol.Required("template_id"): str}
)
@websocket_api.async_response
@_wrap
async def ws_delete_template(hass, manager, msg):
    await manager.async_delete_template(msg["template_id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "heizplan/apply_template",
        vol.Required("template_id"): str,
        vol.Required("room_ids"): [str],
    }
)
@websocket_api.async_response
@_wrap
async def ws_apply_template(hass, manager, msg):
    await manager.async_apply_template(msg["template_id"], msg["room_ids"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "heizplan/add_room",
        vol.Required("climate"): str,
        vol.Optional("name"): vol.Any(str, None),
        vol.Optional("valve"): vol.Any(str, None),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_wrap
async def ws_add_room(hass, manager, msg):
    rid = await manager.async_add_room(msg["climate"], msg.get("name"), msg.get("valve"))
    return {"room_id": rid}


@websocket_api.websocket_command(
    {
        vol.Required("type"): "heizplan/update_room",
        vol.Required("room_id"): str,
        vol.Optional("name"): vol.Any(str, None),
        vol.Optional("valve"): vol.Any(str, None),
        vol.Optional("follow_season"): vol.Any(bool, None),
        vol.Optional("months"): vol.Any([vol.Coerce(int)], None),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_wrap
async def ws_update_room(hass, manager, msg):
    await manager.async_update_room(
        msg["room_id"], msg.get("name"), msg.get("valve"), msg.get("follow_season"), msg.get("months")
    )


@websocket_api.websocket_command(
    {vol.Required("type"): "heizplan/remove_room", vol.Required("room_id"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
@_wrap
async def ws_remove_room(hass, manager, msg):
    await manager.async_remove_room(msg["room_id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "heizplan/set_season_auto",
        vol.Optional("mode"): str,
        vol.Optional("on_date"): str,
        vol.Optional("off_date"): str,
        vol.Optional("weather_entity"): vol.Any(str, None),
        vol.Optional("on_below"): vol.Coerce(float),
        vol.Optional("off_above"): vol.Coerce(float),
        vol.Optional("days"): vol.Coerce(int),
        vol.Optional("basis"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_wrap
async def ws_set_season_auto(hass, manager, msg):
    cfg = {k: v for k, v in msg.items() if k not in ("type", "id")}
    await manager.async_set_season_auto(cfg)


@websocket_api.websocket_command({vol.Required("type"): "heizplan/season_check"})
@websocket_api.async_response
@_wrap
async def ws_season_check(hass, manager, msg):
    await manager.async_season_check()


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    for handler in (
        ws_get,
        ws_subscribe,
        ws_set_day,
        ws_set_plan,
        ws_set_override,
        ws_clear_override,
        ws_set_mode,
        ws_set_away_temp,
        ws_save_template,
        ws_delete_template,
        ws_apply_template,
        ws_add_room,
        ws_update_room,
        ws_remove_room,
        ws_set_season_auto,
        ws_season_check,
    ):
        websocket_api.async_register_command(hass, handler)
