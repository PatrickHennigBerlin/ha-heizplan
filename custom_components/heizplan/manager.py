"""Zentrale Logik: Daten halten, Solltemperaturen berechnen und schreiben."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, STATE_ON
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_COMFORT_PRESET,
    CONF_KEEP_COMFORT,
    CONF_SEASON_ENTITY,
    DEFAULT_AWAY_TEMP,
    DEFAULT_COMFORT_PRESET,
    CORRECTED_PRESETS,
    DEFAULT_TEMP,
    MODE_AWAY,
    MODE_OFF,
    MODE_PLAN,
    MODES,
    SIGNAL_UPDATED,
    SOURCE_AWAY,
    SOURCE_INACTIVE,
    SOURCE_OVERRIDE,
    SOURCE_PLAN,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .plan import (
    DAYS,
    PlanError,
    block_at,
    blocks_from_scheduler,
    default_plan,
    next_change,
    normalize_day,
    normalize_plan,
)

_LOGGER = logging.getLogger(__name__)


def room_id_from_entity(entity_id: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", entity_id.split(".", 1)[-1].lower())


class HeizplanManager:
    """Hält Räume, Pläne, Vorlagen, Overrides und schreibt Sollwerte."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")
        self.rooms: dict[str, dict[str, Any]] = {}
        self.templates: dict[str, dict[str, Any]] = {}
        self.overrides: dict[str, dict[str, Any]] = {}
        self.mode: str = MODE_PLAN
        self.away_temp: float = DEFAULT_AWAY_TEMP
        self._last_written: dict[str, float] = {}
        self._unsubs: list[CALLBACK_TYPE] = []

    # ------------------------------------------------------------------ Optionen
    @property
    def season_entity(self) -> str | None:
        return self.entry.options.get(CONF_SEASON_ENTITY) or None

    @property
    def keep_comfort(self) -> bool:
        return bool(self.entry.options.get(CONF_KEEP_COMFORT, True))

    @property
    def comfort_preset(self) -> str:
        return self.entry.options.get(CONF_COMFORT_PRESET) or DEFAULT_COMFORT_PRESET

    def season_active(self) -> bool:
        ent = self.season_entity
        if not ent:
            return True
        state = self.hass.states.get(ent)
        return state is not None and state.state == STATE_ON

    # ------------------------------------------------------------------ Speicher
    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.rooms = data.get("rooms", {})
        self.templates = data.get("templates", {})
        self.overrides = data.get("overrides", {})
        self.mode = data.get("mode", MODE_PLAN)
        if self.mode not in MODES:
            self.mode = MODE_PLAN
        self.away_temp = float(data.get("away_temp", DEFAULT_AWAY_TEMP))

    def as_dict(self) -> dict[str, Any]:
        return {
            "rooms": self.rooms,
            "templates": self.templates,
            "overrides": self.overrides,
            "mode": self.mode,
            "away_temp": self.away_temp,
        }

    @callback
    def _schedule_save(self) -> None:
        self._store.async_delay_save(self.as_dict, 1)

    # ------------------------------------------------------------------ Start/Stop
    async def async_start(self) -> None:
        self._unsubs.append(
            async_track_time_change(self.hass, self._tick, second=5)
        )
        if self.season_entity:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, [self.season_entity], self._season_changed
                )
            )
        await self.async_apply(force=True)

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    async def _tick(self, now: datetime) -> None:
        await self.async_apply()

    async def _season_changed(self, event: Event) -> None:
        self._last_written.clear()
        await self.async_apply(force=True)

    # ------------------------------------------------------------------ Status
    def _override_active(self, room_id: str, now: datetime) -> dict[str, Any] | None:
        ovr = self.overrides.get(room_id)
        if not ovr:
            return None
        until = dt_util.parse_datetime(ovr.get("until", ""))
        if until is None or until <= now:
            return None
        return ovr

    def room_status(self, room_id: str, now: datetime | None = None) -> dict[str, Any]:
        now = now or dt_util.now()
        room = self.rooms[room_id]
        plan = room["plan"]
        plan_temp, block_start, block_end = block_at(plan, now)
        nxt = next_change(plan, now)
        climate = self.hass.states.get(room["climate"])
        status: dict[str, Any] = {
            "room_id": room_id,
            "name": room.get("name", room_id),
            "climate": room["climate"],
            "plan_temp": plan_temp,
            "block_start": f"{block_start // 60:02d}:{block_start % 60:02d}",
            "block_end": f"{block_end // 60:02d}:{block_end % 60:02d}",
            "next_change": nxt[0].isoformat() if nxt else None,
            "next_temp": nxt[1] if nxt else None,
            "current_temp": climate.attributes.get("current_temperature") if climate else None,
            "climate_preset": climate.attributes.get("preset_mode") if climate else None,
            "valve": None,
            "override": None,
        }
        valve = room.get("valve")
        if valve:
            vstate = self.hass.states.get(valve)
            if vstate is not None:
                try:
                    status["valve"] = float(vstate.state)
                except ValueError:
                    status["valve"] = None
        ovr = self._override_active(room_id, now)
        if not self.season_active() or self.mode == MODE_OFF:
            status["target"] = None
            status["source"] = SOURCE_INACTIVE
        elif ovr:
            status["target"] = float(ovr["temp"])
            status["source"] = SOURCE_OVERRIDE
            status["override"] = {"temp": float(ovr["temp"]), "until": ovr["until"]}
        elif self.mode == MODE_AWAY:
            status["target"] = self.away_temp
            status["source"] = SOURCE_AWAY
        else:
            status["target"] = plan_temp
            status["source"] = SOURCE_PLAN
        status["written"] = self._last_written.get(room_id)
        return status

    def full_state(self) -> dict[str, Any]:
        now = dt_util.now()
        return {
            "mode": self.mode,
            "away_temp": self.away_temp,
            "season_entity": self.season_entity,
            "season_active": self.season_active(),
            "rooms": {
                rid: {**room, "status": self.room_status(rid, now)}
                for rid, room in self.rooms.items()
            },
            "templates": self.templates,
            "now": now.isoformat(),
        }

    # ------------------------------------------------------------------ Schreiben
    async def async_apply(self, force: bool = False) -> None:
        now = dt_util.now()
        changed = False
        # abgelaufene Overrides entfernen
        for room_id in list(self.overrides):
            if self._override_active(room_id, now) is None:
                self.overrides.pop(room_id, None)
                changed = True
        for room_id in list(self.rooms):
            try:
                status = self.room_status(room_id, now)
            except (KeyError, PlanError) as err:
                _LOGGER.error("Heizplan: Raum %s fehlerhaft: %s", room_id, err)
                continue
            target = status["target"]
            if target is None:
                continue
            if not force and self._last_written.get(room_id) == target:
                continue
            if await self._write(room_id, target):
                self._last_written[room_id] = target
                changed = True
        if changed:
            self._schedule_save()
        async_dispatcher_send(self.hass, SIGNAL_UPDATED)

    async def _write(self, room_id: str, target: float) -> bool:
        entity_id = self.rooms[room_id]["climate"]
        state = self.hass.states.get(entity_id)
        if state is None or state.state == "unavailable":
            _LOGGER.warning("Heizplan: %s nicht verfügbar", entity_id)
            return False
        try:
            if self.keep_comfort:
                presets = state.attributes.get("preset_modes") or []
                preset = state.attributes.get("preset_mode")
                # Nur Absenk-Betriebsarten (Standby/Nacht) zurück auf Komfort stellen –
                # sie verfälschen den Sollwert. Frostschutz o. Ä. bleiben für Sonderfälle unangetastet.
                if (
                    self.comfort_preset in presets
                    and preset in CORRECTED_PRESETS
                    and preset != self.comfort_preset
                ):
                    await self.hass.services.async_call(
                        "climate",
                        "set_preset_mode",
                        {ATTR_ENTITY_ID: entity_id, "preset_mode": self.comfort_preset},
                        blocking=True,
                    )
            low = state.attributes.get("min_temp")
            high = state.attributes.get("max_temp")
            value = target
            if low is not None:
                value = max(value, float(low))
            if high is not None:
                value = min(value, float(high))
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {ATTR_ENTITY_ID: entity_id, "temperature": value},
                blocking=True,
            )
            _LOGGER.debug("Heizplan: %s -> %s °C", entity_id, value)
            return True
        except Exception as err:  # noqa: BLE001 – Fehler eines Raums soll andere nicht stoppen
            _LOGGER.error("Heizplan: Schreiben auf %s fehlgeschlagen: %s", entity_id, err)
            return False

    # ------------------------------------------------------------------ Änderungen
    async def async_flush(self) -> None:
        await self._store.async_save(self.as_dict())

    async def _changed(self, rooms_changed: bool = False) -> None:
        self._schedule_save()
        await self.async_apply()
        if rooms_changed:
            await self.async_flush()
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self.entry.entry_id)
            )

    async def async_add_room(self, climate: str, name: str | None = None,
                             valve: str | None = None) -> str:
        room_id = room_id_from_entity(climate)
        if room_id in self.rooms:
            raise PlanError(f"Raum {room_id} existiert schon")
        state = self.hass.states.get(climate)
        self.rooms[room_id] = {
            "name": name or (state.name if state else room_id),
            "climate": climate,
            "valve": valve,
            "plan": default_plan(DEFAULT_TEMP),
        }
        await self._changed(rooms_changed=True)
        return room_id

    async def async_update_room(self, room_id: str, name: str | None = None,
                                valve: str | None = None) -> None:
        room = self.rooms[room_id]
        if name is not None:
            room["name"] = name
        if valve is not None:
            room["valve"] = valve or None
        await self._changed()

    async def async_remove_room(self, room_id: str) -> None:
        self.rooms.pop(room_id)
        self.overrides.pop(room_id, None)
        self._last_written.pop(room_id, None)
        await self._changed(rooms_changed=True)

    async def async_set_plan(self, room_id: str, plan: dict[str, Any]) -> None:
        self.rooms[room_id]["plan"] = normalize_plan(plan, DEFAULT_TEMP)
        await self._changed()

    async def async_set_day(self, room_id: str, day: str, blocks: list, copy_to: list[str] | None = None) -> None:
        if day not in DAYS:
            raise PlanError(f"Unbekannter Tag {day}")
        norm = normalize_day(blocks, DEFAULT_TEMP)
        plan = self.rooms[room_id]["plan"]
        for target in [day, *(copy_to or [])]:
            if target in DAYS:
                plan[target] = [dict(b) for b in norm]
        await self._changed()

    async def async_set_override(self, room_id: str, temp: float, minutes: int) -> None:
        if room_id not in self.rooms:
            raise PlanError(f"Unbekannter Raum {room_id}")
        until = dt_util.now() + timedelta(minutes=int(minutes))
        self.overrides[room_id] = {"temp": round(float(temp) * 2) / 2, "until": until.isoformat()}
        await self._changed()

    async def async_clear_override(self, room_id: str) -> None:
        self.overrides.pop(room_id, None)
        self._last_written.pop(room_id, None)
        await self._changed()

    async def async_set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise PlanError(f"Unbekannter Modus {mode}")
        self.mode = mode
        await self._changed()

    async def async_set_away_temp(self, temp: float) -> None:
        self.away_temp = round(float(temp) * 2) / 2
        await self._changed()

    async def async_save_template(self, template_id: str | None, name: str, plan: dict[str, Any]) -> str:
        tid = template_id or re.sub(r"[^a-z0-9_]", "_", name.lower()) or "vorlage"
        base, n = tid, 2
        while template_id is None and tid in self.templates:
            tid, n = f"{base}_{n}", n + 1
        self.templates[tid] = {"name": name, "plan": normalize_plan(plan, DEFAULT_TEMP)}
        self._schedule_save()
        async_dispatcher_send(self.hass, SIGNAL_UPDATED)
        return tid

    async def async_delete_template(self, template_id: str) -> None:
        self.templates.pop(template_id, None)
        self._schedule_save()
        async_dispatcher_send(self.hass, SIGNAL_UPDATED)

    async def async_apply_template(self, template_id: str, room_ids: list[str]) -> None:
        plan = self.templates[template_id]["plan"]
        for room_id in room_ids:
            if room_id in self.rooms:
                self.rooms[room_id]["plan"] = normalize_plan(plan, DEFAULT_TEMP)
        await self._changed()

    async def async_import_scheduler(self, schedule_entities: list[str], create_rooms: bool = True) -> dict[str, Any]:
        """Übernimmt switch.schedule_* (Scheduler-Component) in Raumpläne."""
        result: dict[str, Any] = {"imported": [], "skipped": []}
        per_climate: dict[str, dict[str, list]] = {}
        for ent in schedule_entities:
            state = self.hass.states.get(ent)
            if state is None:
                result["skipped"].append({"entity": ent, "reason": "nicht gefunden"})
                continue
            days, targets, blocks = blocks_from_scheduler(dict(state.attributes))
            if not blocks or not days:
                result["skipped"].append({"entity": ent, "reason": "keine Temperatur-Blöcke"})
                continue
            for climate in targets:
                if not climate.startswith("climate."):
                    continue
                plan = per_climate.setdefault(climate, {})
                for day in days:
                    plan[day] = blocks
                result["imported"].append({"entity": ent, "climate": climate, "days": days})
        rooms_changed = False
        for climate, partial in per_climate.items():
            room_id = room_id_from_entity(climate)
            if room_id not in self.rooms:
                if not create_rooms:
                    continue
                state = self.hass.states.get(climate)
                self.rooms[room_id] = {
                    "name": state.name if state else room_id,
                    "climate": climate,
                    "valve": None,
                    "plan": default_plan(DEFAULT_TEMP),
                }
                rooms_changed = True
            plan = self.rooms[room_id]["plan"]
            for day, blocks in partial.items():
                plan[day] = normalize_day(blocks, DEFAULT_TEMP)
        await self._changed(rooms_changed=rooms_changed)
        return result
