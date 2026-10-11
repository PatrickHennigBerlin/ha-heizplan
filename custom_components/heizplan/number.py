"""Abwesend-Temperatur."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import HeizplanEntity
from .manager import HeizplanManager


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager: HeizplanManager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HeizplanAwayTemp(manager)])


class HeizplanAwayTemp(HeizplanEntity, NumberEntity):
    _attr_icon = "mdi:home-export-outline"
    _attr_name = "Abwesend-Temperatur"
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = 10
    _attr_native_max_value = 24
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX

    def __init__(self, manager: HeizplanManager) -> None:
        super().__init__(manager)
        self._attr_unique_id = f"{manager.entry.entry_id}_abwesend_temp"

    @property
    def native_value(self) -> float:
        return self.manager.away_temp

    async def async_set_native_value(self, value: float) -> None:
        await self.manager.async_set_away_temp(value)
