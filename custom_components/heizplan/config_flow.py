"""Einrichtung über die Oberfläche."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_COMFORT_PRESET,
    CONF_KEEP_COMFORT,
    CONF_SEASON_ENTITY,
    DEFAULT_COMFORT_PRESET,
    DOMAIN,
)


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    season = defaults.get(CONF_SEASON_ENTITY)
    return vol.Schema(
        {
            vol.Optional(
                CONF_SEASON_ENTITY,
                description={"suggested_value": season} if season else None,
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["switch", "input_boolean", "binary_sensor"])
            ),
            vol.Required(
                CONF_KEEP_COMFORT, default=defaults.get(CONF_KEEP_COMFORT, True)
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_COMFORT_PRESET,
                default=defaults.get(CONF_COMFORT_PRESET, DEFAULT_COMFORT_PRESET),
            ): selector.TextSelector(),
        }
    )


class HeizplanConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Heizplan", data={}, options=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HeizplanOptionsFlow()


class HeizplanOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init", data_schema=_schema(dict(self.config_entry.options))
        )
