"""Globaler Modus: Wochenplan / Abwesend / Aus."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MODE_AWAY, MODE_OFF, MODE_PLAN
from .entity import HeizplanEntity
from .manager import HeizplanManager

OPTIONS = {MODE_PLAN: "Wochenplan", MODE_AWAY: "Abwesend", MODE_OFF: "Aus"}
REVERSE = {v: k for k, v in OPTIONS.items()}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager: HeizplanManager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HeizplanModeSelect(manager)])


class HeizplanModeSelect(HeizplanEntity, SelectEntity):
    _attr_icon = "mdi:home-thermometer"
    _attr_name = "Modus"
    _attr_options = list(OPTIONS.values())

    def __init__(self, manager: HeizplanManager) -> None:
        super().__init__(manager)
        self._attr_unique_id = f"{manager.entry.entry_id}_modus"

    @property
    def current_option(self) -> str:
        return OPTIONS[self.manager.mode]

    async def async_select_option(self, option: str) -> None:
        await self.manager.async_set_mode(REVERSE[option])
