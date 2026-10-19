"""Schalter fürs Zuheizen mit Klima: je Raum erlauben/sperren, global 'von Hand eingeschaltete Klima übernehmen'."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import HeizplanEntity
from .manager import HeizplanManager, room_kind
from .plan import KIND_CLIMATE


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager: HeizplanManager = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = [HeizplanAuxTakeover(manager)]
    for rid, room in manager.rooms.items():
        if room.get("aux_climate") and room_kind(room) == KIND_CLIMATE:
            entities += [HeizplanAuxRoomSwitch(manager, rid), HeizplanBoostSwitch(manager, rid)]
    async_add_entities(entities)


class HeizplanAuxTakeover(HeizplanEntity, SwitchEntity):
    _attr_icon = "mdi:hand-back-right-outline"
    _attr_name = "Zuheizen Klima übernehmen"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, manager: HeizplanManager) -> None:
        super().__init__(manager)
        self._attr_unique_id = f"{manager.entry.entry_id}_zuheizen_takeover"

    @property
    def is_on(self) -> bool:
        return self.manager.aux_settings["takeover"]

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.manager.async_set_aux_settings({"takeover": True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.manager.async_set_aux_settings({"takeover": False})


class HeizplanAuxRoomSwitch(HeizplanEntity, SwitchEntity):
    _attr_icon = "mdi:air-conditioner"

    def __init__(self, manager: HeizplanManager, room_id: str) -> None:
        super().__init__(manager)
        self.room_id = room_id
        self._attr_unique_id = f"{manager.entry.entry_id}_{room_id}_zuheizen"
        self._attr_name = f"Zuheizen {manager.rooms[room_id].get('name', room_id)}"

    @property
    def available(self) -> bool:
        return self.room_id in self.manager.rooms

    @property
    def is_on(self) -> bool | None:
        room = self.manager.rooms.get(self.room_id)
        return None if room is None else bool(room.get("aux_enabled", True))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        room = self.manager.rooms.get(self.room_id) or {}
        return {"klima": room.get("aux_climate"), "luefterstufe": room.get("aux_fan_mode"),
                "luefter_schalter": room.get("aux_fan_switch")}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.manager.async_set_aux_enabled(self.room_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.manager.async_set_aux_enabled(self.room_id, False)


class HeizplanBoostSwitch(HeizplanEntity, SwitchEntity):
    """Boost mit Wärmepumpe: an = Klima heizt mit, bis der Soll erreicht ist; geht dann selbst aus."""

    _attr_icon = "mdi:rocket-launch-outline"

    def __init__(self, manager: HeizplanManager, room_id: str) -> None:
        super().__init__(manager)
        self.room_id = room_id
        self._attr_unique_id = f"{manager.entry.entry_id}_{room_id}_boost"
        self._attr_name = f"Boost mit Wärmepumpe {manager.rooms[room_id].get('name', room_id)}"

    @property
    def available(self) -> bool:
        return self.room_id in self.manager.rooms

    @property
    def is_on(self) -> bool:
        return bool((self.manager.aux_state.get(self.room_id) or {}).get("boost"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = self.manager.aux_state.get(self.room_id) or {}
        return {"seit": st.get("boost_since"), "spaetestens_bis": self.manager._boost_until(st),
                "klima_heizt": bool(st.get("active"))}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.manager.async_set_boost(self.room_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.manager.async_set_boost(self.room_id, False)
