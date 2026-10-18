"""Je Raum mit Klimagerät: heizt die Klima gerade mit (Zuheizen aktiv)?"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
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
    async_add_entities(
        HeizplanAuxActive(manager, rid)
        for rid, room in manager.rooms.items()
        if room.get("aux_climate") and room_kind(room) == KIND_CLIMATE
    )


class HeizplanAuxActive(HeizplanEntity, BinarySensorEntity):
    _attr_icon = "mdi:air-conditioner"
    _attr_device_class = BinarySensorDeviceClass.HEAT

    def __init__(self, manager: HeizplanManager, room_id: str) -> None:
        super().__init__(manager)
        self.room_id = room_id
        self._attr_unique_id = f"{manager.entry.entry_id}_{room_id}_zuheizen_aktiv"
        self._attr_name = f"Zuheizen {manager.rooms[room_id].get('name', room_id)}"

    def _aux(self) -> dict[str, Any] | None:
        if self.room_id not in self.manager.rooms:
            return None
        return self.manager.room_status(self.room_id).get("aux_heating")

    @property
    def available(self) -> bool:
        return self._aux() is not None

    @property
    def is_on(self) -> bool | None:
        aux = self._aux()
        return None if aux is None else aux["active"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        aux = self._aux() or {}
        return {
            "klima": aux.get("climate"),
            "erlaubt": aux.get("enabled"),
            "bedarf": aux.get("demand"),
            "bedarf_seit": aux.get("demand_since"),
            "aktiv_seit": aux.get("since"),
            "pausiert": aux.get("paused"),
        }
