"""Konstanten für Heizplan."""

DOMAIN = "heizplan"
VERSION = "0.5.9"

STORAGE_KEY = "heizplan"
STORAGE_VERSION = 1

CARD_URL = "/heizplan_static/heizplan-card.js"

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_NAMES_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

MODE_PLAN = "plan"
MODE_AWAY = "away"
MODE_OFF = "off"
MODES = [MODE_PLAN, MODE_AWAY, MODE_OFF]

SOURCE_PLAN = "plan"
SOURCE_AWAY = "away"
SOURCE_OVERRIDE = "override"
SOURCE_INACTIVE = "inactive"

DEFAULT_AWAY_TEMP = 17.0
DEFAULT_TEMP = 20.0

# Optionen (config entry options)
CONF_SEASON_ENTITY = "season_entity"          # z. B. switch.heizperiode – aus = Heizplan schreibt nichts
CONF_KEEP_COMFORT = "keep_comfort"            # Regler beim Schreiben auf preset 'comfort' halten
CONF_COMFORT_PRESET = "comfort_preset"
CONF_AUX_OUTSIDE = "aux_outside_entity"      # Außenfühler fürs Zuheizen mit Klima (optional)

DEFAULT_COMFORT_PRESET = "comfort"

SIGNAL_UPDATED = "heizplan_updated"
EVENT_UPDATED = "heizplan_updated"

