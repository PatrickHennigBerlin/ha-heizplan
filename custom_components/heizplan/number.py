"""Abwesend-Temperatur und Einstellungen fürs Zuheizen mit Klima."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import HeizplanEntity
from .manager import HeizplanManager


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager: HeizplanManager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        HeizplanAwayTemp(manager),
        HeizplanAuxNumber(manager, "on_delta", "Zuheizen Abweichung", "mdi:thermometer-chevron-down",
                          0.5, 5, 0.5, UnitOfTemperature.KELVIN),
        HeizplanAuxNumber(manager, "off_delta", "Zuheizen Abschaltabstand", "mdi:thermometer-chevron-up",
                          0, 3, 0.1, UnitOfTemperature.KELVIN),
        HeizplanAuxNumber(manager, "wait", "Zuheizen Wartezeit", "mdi:timer-sand",
                          0, 60, 1, UnitOfTime.MINUTES),
        HeizplanAuxNumber(manager, "outside_limit", "Zuheizen Außengrenze", "mdi:home-thermometer-outline",
                          -10, 20, 0.5, UnitOfTemperature.CELSIUS),
        HeizplanAuxNumber(manager, "boost_max", "Boost mit Klima Höchstdauer", "mdi:timer-outline",
                          15, 720, 15, UnitOfTime.MINUTES),
    ])


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


class HeizplanAuxNumber(HeizplanEntity, NumberEntity):
    """Eine Einstellung fürs Zuheizen mit Klima (gilt für alle Räume)."""

    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, manager: HeizplanManager, key: str, name: str, icon: str,
                 low: float, high: float, step: float, unit: str) -> None:
        super().__init__(manager)
        self._key = key
        self._attr_unique_id = f"{manager.entry.entry_id}_zuheizen_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_native_min_value = low
        self._attr_native_max_value = high
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit

    @property
    def native_value(self) -> float:
        return self.manager.aux_settings[self._key]

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.manager.async_set_aux_settings({self._key: value})
        except ValueError as err:
            from homeassistant.exceptions import ServiceValidationError
            raise ServiceValidationError(str(err)) from err
