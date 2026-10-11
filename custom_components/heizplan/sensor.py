"""Je Raum ein Sensor mit der echten Solltemperatur aus dem Heizplan."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import HeizplanEntity
from .manager import HeizplanManager

SOURCE_TEXT = {
    "plan": "Wochenplan",
    "away": "Abwesend",
    "override": "Manuell",
    "inactive": "Inaktiv",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager: HeizplanManager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(HeizplanRoomSensor(manager, rid) for rid in manager.rooms)


class HeizplanRoomSensor(HeizplanEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_icon = "mdi:radiator"

    def __init__(self, manager: HeizplanManager, room_id: str) -> None:
        super().__init__(manager)
        self.room_id = room_id
        self._attr_unique_id = f"{manager.entry.entry_id}_{room_id}_soll"
        self._attr_name = f"{manager.rooms[room_id].get('name', room_id)} Soll"

    @property
    def available(self) -> bool:
        return self.room_id in self.manager.rooms

    @property
    def native_value(self) -> float | None:
        if self.room_id not in self.manager.rooms:
            return None
        return self.manager.room_status(self.room_id)["target"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.room_id not in self.manager.rooms:
            return {}
        status = self.manager.room_status(self.room_id)
        return {
            "raum": status["name"],
            "heizung": status["climate"],
            "quelle": SOURCE_TEXT.get(status["source"], status["source"]),
            "planwert": status["plan_temp"],
            "block": f"{status['block_start']}–{status['block_end']}",
            "naechster_wechsel": status["next_change"],
            "naechster_wert": status["next_temp"],
            "ist": status["current_temp"],
            "ventil": status["valve"],
            "manuell_bis": status["override"]["until"] if status["override"] else None,
        }
