"""Zentrale Logik: Daten halten, Solltemperaturen berechnen und schreiben."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, EVENT_HOMEASSISTANT_STARTED, STATE_ON
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AUX_OUTSIDE,
    CONF_COMFORT_PRESET,
    CONF_KEEP_COMFORT,
    CONF_SEASON_ENTITY,
    DEFAULT_AWAY_TEMP,
    DEFAULT_COMFORT_PRESET,
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
from .klima_start import async_start_climate
from .season import (
    DEFAULT_SEASON_AUTO,
    SEASON_DATES,
    SEASON_WEATHER,
    date_decision,
    daily_values,
    normalize_season_auto,
    weather_decision,
)
from .zuheizen import (
    DEFAULT_AUX_SETTINGS,
    HEAT,
    RUNNING_OFF,
    can_start,
    climate_setpoint,
    conditions,
    normalize_aux_settings,
    boost_conditions,
    outside_ok,
)
from .plan import (
    DAYS,
    KIND_CLIMATE,
    KIND_SWITCH,
    PlanError,
    block_at,
    blocks_from_scheduler,
    default_plan,
    next_change,
    normalize_day,
    normalize_plan,
)

_LOGGER = logging.getLogger(__name__)
START_RETRY = timedelta(minutes=5)   # nach unbestätigtem Einschalten so lange warten
STARTUP_GRACE = timedelta(minutes=2)   # nach dem Start: KNX-Klimas zeigen „aus“, bis ihr Status gelesen ist


def room_id_from_entity(entity_id: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", entity_id.split(".", 1)[-1].lower())


def kind_for_entity(entity_id: str) -> str:
    return KIND_CLIMATE if entity_id.startswith("climate.") else KIND_SWITCH


def room_kind(room: dict[str, Any]) -> str:
    return room.get("kind") or kind_for_entity(room["climate"])


def fallback_for(kind: str) -> Any:
    return False if kind == KIND_SWITCH else DEFAULT_TEMP


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
        # Räume, deren Regler einmalig (auch aus Frostschutz) auf Komfort gestellt werden sollen
        self._restore_pending: set[str] = set()
        # letzte Komfort-Korrektur je Raum (Bremse, falls ein Regler nicht reagiert)
        self._comfort_tries: dict[str, datetime] = {}
        self._unsubs: list[CALLBACK_TYPE] = []
        # Heizperiode-Automatik (manuell / Datum / Wetter)
        self.season_auto: dict[str, Any] = dict(DEFAULT_SEASON_AUTO)
        self.season_last: dict[str, Any] = {}   # {"decision": bool|None, "date": "YYYY-MM-DD"}
        self.season_status: dict[str, Any] = {}
        self._started_unsub: CALLBACK_TYPE | None = None
        # Zuheizen mit Klima
        self.aux_settings: dict[str, Any] = dict(DEFAULT_AUX_SETTINGS)
        self.aux_state: dict[str, dict[str, Any]] = {}   # je Raum {active, owned, since} (gespeichert)
        self._aux_demand_since: dict[str, datetime] = {}  # Einschaltbedingung gilt seit (nicht gespeichert)
        self._aux_blocked: set[str] = set()   # Klima während des Zuheizens von Hand geändert -> Pause bis Bedarf endet
        self._aux_fresh: set[str] = set()     # nach Neustart noch nicht neu bewertet
        self._aux_starting: set[str] = set()  # Klima wird gerade eingeschaltet (klima_start)
        self._loaded_at: datetime = dt_util.now()
        self._aux_start_failed: dict[str, datetime] = {}  # Einschalten nicht bestätigt -> START_RETRY warten
        self._aux_written: dict[str, float] = {}

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

    @property
    def aux_outside_entity(self) -> str | None:
        return self.entry.options.get(CONF_AUX_OUTSIDE) or None

    def room_active(self, room: dict[str, Any], now: datetime) -> bool:
        """Raum aktiv? Folgt der Heizperiode (Standard) und/oder festen Monaten."""
        months = room.get("months")
        if months and now.month not in months:
            return False
        if room.get("follow_season", True) and not self.season_active():
            return False
        return True

    def season_active(self) -> bool:
        ent = self.season_entity
        if not ent:
            return True
        state = self.hass.states.get(ent)
        return state is not None and state.state == STATE_ON

    # ------------------------------------------------------------------ Speicher
    async def async_load(self) -> None:
        self._loaded_at = dt_util.now()
        data = await self._store.async_load() or {}
        self.rooms = data.get("rooms", {})
        self.templates = data.get("templates", {})
        self.overrides = data.get("overrides", {})
        self.mode = data.get("mode", MODE_PLAN)
        if self.mode not in MODES:
            self.mode = MODE_PLAN
        self.away_temp = float(data.get("away_temp", DEFAULT_AWAY_TEMP))
        try:
            self.season_auto = normalize_season_auto(data.get("season_auto"))
        except ValueError as err:
            _LOGGER.warning("Heizplan: Heizperiode-Automatik zurückgesetzt (%s)", err)
            self.season_auto = dict(DEFAULT_SEASON_AUTO)
        self.season_last = data.get("season_last") or {}
        try:
            self.aux_settings = normalize_aux_settings(data.get("aux_settings"))
        except ValueError as err:
            _LOGGER.warning("Heizplan: Zuheizen-Einstellungen zurückgesetzt (%s)", err)
            self.aux_settings = dict(DEFAULT_AUX_SETTINGS)
        self.aux_state = {
            rid: {"active": bool(st.get("active")), "owned": bool(st.get("owned")), "since": st.get("since"),
                  "boost": bool(st.get("boost")), "boost_since": st.get("boost_since"),
                  "by_boost": bool(st.get("by_boost"))}
            for rid, st in (data.get("aux_state") or {}).items() if rid in self.rooms
        }
        # Nach dem Neustart neu bewerten; Wartezeit beginnt neu, laufendes Zuheizen nicht blind beenden
        self._aux_fresh = {rid for rid, st in self.aux_state.items() if st["active"]}
        # Boost überlebt einen Neustart (gespeichert), Höchstdauer zählt weiter ab boost_since

    def as_dict(self) -> dict[str, Any]:
        return {
            "rooms": self.rooms,
            "templates": self.templates,
            "overrides": self.overrides,
            "mode": self.mode,
            "away_temp": self.away_temp,
            "season_auto": self.season_auto,
            "season_last": self.season_last,
            "aux_settings": self.aux_settings,
            "aux_state": self.aux_state,
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
        # Heizperiode-Automatik: kurz nach Mitternacht (Datum) sowie morgens und abends (Wetter)
        self._unsubs.append(
            async_track_time_change(self.hass, self._season_tick, hour=[0, 6, 18], minute=[5], second=[30])
        )
        if not self.hass.is_running:
            # HA-Start (z. B. nach Stromausfall): Regler einmalig auf Komfort, auch aus Frostschutz
            self._restore_pending = set(self.rooms)
            self._started_unsub = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._season_started
            )
        else:
            self.hass.async_create_task(self.async_season_check())
        await self.async_apply(force=True)

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._started_unsub is not None:
            self._started_unsub()
            self._started_unsub = None

    async def _tick(self, now: datetime) -> None:
        await self.async_apply()

    async def _season_tick(self, now: datetime) -> None:
        await self.async_season_check()

    async def _season_started(self, event: Event) -> None:
        self._started_unsub = None  # Listener ist nach dem Auslösen bereits entfernt
        await self.async_season_check()

    # ------------------------------------------------------------------ Heizperiode-Automatik
    async def async_set_season_auto(self, cfg: dict[str, Any]) -> None:
        self.season_auto = normalize_season_auto({**self.season_auto, **cfg})
        # neue Einstellung: nächste Entscheidung darf wieder schalten
        self.season_last = {}
        self._schedule_save()
        await self.async_season_check()

    async def _fetch_forecast(self, entity_id: str) -> list[dict[str, Any]] | None:
        try:
            resp = await self.hass.services.async_call(
                "weather", "get_forecasts", {"entity_id": entity_id, "type": "daily"},
                blocking=True, return_response=True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Heizplan: Wettervorhersage von %s nicht abrufbar: %s", entity_id, err)
            return None
        return ((resp or {}).get(entity_id) or {}).get("forecast")

    async def _set_season(self, on: bool, reason: str) -> bool:
        ent = self.season_entity
        if not ent:
            return False
        state = self.hass.states.get(ent)
        if state is None or state.state in ("unavailable", "unknown"):
            _LOGGER.warning("Heizplan: %s nicht verfügbar – Heizperiode nicht geschaltet", ent)
            return False
        if (state.state == STATE_ON) == on:
            return False
        domain = ent.split(".", 1)[0]
        if domain not in ("switch", "input_boolean"):
            _LOGGER.warning("Heizplan: %s kann nicht geschaltet werden (nur switch/input_boolean)", ent)
            return False
        await self.hass.services.async_call(
            domain, "turn_on" if on else "turn_off", {ATTR_ENTITY_ID: ent}, blocking=True
        )
        _LOGGER.info("Heizplan: Heizperiode %s (%s)", "an" if on else "aus", reason)
        return True

    async def async_season_check(self) -> None:
        cfg = self.season_auto
        now = dt_util.now()
        today = now.date()
        status: dict[str, Any] = {"checked": now.isoformat(), "mode": cfg["mode"], "values": None,
                                  "decision": None, "switched": False, "text": ""}
        if cfg["mode"] == SEASON_DATES:
            decision = date_decision(today, cfg["on_date"], cfg["off_date"])
            status["decision"] = decision
            if decision is None:
                status["text"] = "Heute kein Schalttag"
            elif self.season_last.get("date") == today.isoformat():
                status["text"] = "Heute schon geschaltet"
            else:
                status["switched"] = await self._set_season(decision, "Datum")
                self.season_last = {"decision": decision, "date": today.isoformat()}
                status["text"] = f"Schalttag: Heizperiode {'an' if decision else 'aus'}"
        elif cfg["mode"] == SEASON_WEATHER:
            forecast = await self._fetch_forecast(cfg["weather_entity"])
            if forecast is None:
                status["text"] = "Vorhersage nicht verfügbar"
            else:
                values = daily_values(forecast, today, cfg["days"], cfg["basis"])
                decision = weather_decision(values, cfg["days"], cfg["on_below"], cfg["off_above"])
                status["values"] = values
                status["decision"] = decision
                if len(values) < cfg["days"]:
                    status["text"] = f"Vorhersage reicht nur für {len(values)} von {cfg['days']} Tagen"
                elif decision is None:
                    status["text"] = "Keine Bedingung erfüllt – bleibt wie es ist"
                elif decision == self.season_last.get("decision"):
                    status["text"] = "Bedingung unverändert – keine Schaltung (Hand-Schaltung bleibt)"
                else:
                    status["switched"] = await self._set_season(decision, "Wettervorhersage")
                    self.season_last = {"decision": decision, "date": today.isoformat()}
                    status["text"] = f"Bedingung erfüllt: Heizperiode {'an' if decision else 'aus'}"
        else:
            status["text"] = "Manuell"
        self.season_status = status
        self._schedule_save()
        async_dispatcher_send(self.hass, SIGNAL_UPDATED)

    async def _season_changed(self, event: Event) -> None:
        self._last_written.clear()
        new = event.data.get("new_state")
        if new is not None and new.state == STATE_ON:
            self._restore_pending = set(self.rooms)
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
        kind = room_kind(room)
        plan_temp, block_start, block_end = block_at(plan, now)
        nxt = next_change(plan, now)
        climate = self.hass.states.get(room["climate"])
        status: dict[str, Any] = {
            "room_id": room_id,
            "kind": kind,
            "is_on": (climate.state == "on") if (climate and kind == KIND_SWITCH) else None,
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
        if not self.room_active(room, now) or self.mode == MODE_OFF:
            status["target"] = None
            status["source"] = SOURCE_INACTIVE
        elif ovr:
            value = bool(ovr["on"]) if kind == KIND_SWITCH else float(ovr["temp"])
            status["target"] = value
            status["source"] = SOURCE_OVERRIDE
            status["override"] = {**ovr}
        elif self.mode == MODE_AWAY:
            status["target"] = False if kind == KIND_SWITCH else self.away_temp
            status["source"] = SOURCE_AWAY
        else:
            status["target"] = plan_temp
            status["source"] = SOURCE_PLAN
        status["written"] = self._last_written.get(room_id)
        status["aux_heating"] = self.aux_status(room_id, status, now)
        return status

    def full_state(self) -> dict[str, Any]:
        now = dt_util.now()
        return {
            "mode": self.mode,
            "away_temp": self.away_temp,
            "season_entity": self.season_entity,
            "season_active": self.season_active(),
            "season_auto": self.season_auto,
            "season_status": self.season_status,
            "aux": {
                "settings": self.aux_settings,
                "outside_entity": self.aux_outside_entity,
                "outside_value": self._outside_value(),
            },
            "rooms": {
                rid: {**room, "kind": room_kind(room), "status": self.room_status(rid, now)}
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
            needs_comfort = self._needs_comfort(room_id, now)
            if (not force and self._last_written.get(room_id) == target
                    and room_id not in self._restore_pending and not needs_comfort):
                continue
            restore = room_id in self._restore_pending or needs_comfort
            if await self._write(room_id, target, restore):
                self._last_written[room_id] = target
                self._restore_pending.discard(room_id)
                changed = True
        if await self._aux_apply(now):
            changed = True
        if changed:
            self._schedule_save()
        async_dispatcher_send(self.hass, SIGNAL_UPDATED)

    def _needs_comfort(self, room_id: str, now: datetime) -> bool:
        """Aktiver Heizkörper-Raum, dessen Regler nicht auf Komfort steht (z. B. nach Busausfall Frostschutz).

        Höchstens alle 5 Minuten ein neuer Versuch je Raum, damit ein nicht reagierender
        Regler den Bus nicht im Minutentakt belastet.
        """
        if not self.keep_comfort or room_kind(self.rooms[room_id]) != KIND_CLIMATE:
            return False
        state = self.hass.states.get(self.rooms[room_id]["climate"])
        if state is None or state.state in ("unavailable", "unknown"):
            return False
        presets = state.attributes.get("preset_modes") or []
        preset = state.attributes.get("preset_mode")
        if self.comfort_preset not in presets or preset in (None, self.comfort_preset):
            return False
        last = self._comfort_tries.get(room_id)
        return last is None or now - last >= timedelta(minutes=5)

    async def _write(self, room_id: str, target: Any, restore_all: bool = False) -> bool:
        entity_id = self.rooms[room_id]["climate"]
        state = self.hass.states.get(entity_id)
        if state is None or state.state == "unavailable":
            _LOGGER.warning("Heizplan: %s nicht verfügbar", entity_id)
            return False
        if room_kind(self.rooms[room_id]) == KIND_SWITCH:
            domain = entity_id.split(".", 1)[0]
            try:
                await self.hass.services.async_call(
                    domain, "turn_on" if target else "turn_off",
                    {ATTR_ENTITY_ID: entity_id}, blocking=True,
                )
                return True
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Heizplan: Schalten von %s fehlgeschlagen: %s", entity_id, err)
                return False
        try:
            if self.keep_comfort:
                presets = state.attributes.get("preset_modes") or []
                preset = state.attributes.get("preset_mode")
                # In der Heizperiode immer Komfort: Standby, Nacht und auch Frostschutz
                # (z. B. nach Busausfall/Reset) werden zurückgestellt (Patrick, 11.10.2026).
                if (
                    self.comfort_preset in presets
                    and preset is not None
                    and preset != self.comfort_preset
                ):
                    self._comfort_tries[room_id] = dt_util.now()
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

    # ------------------------------------------------------------------ Zuheizen mit Klima
    def _outside_value(self) -> float | None:
        ent = self.aux_outside_entity
        if not ent:
            return None
        state = self.hass.states.get(ent)
        try:
            return float(state.state) if state is not None else None
        except ValueError:
            return None

    def _aux_conditions(self, room_id: str, status: dict[str, Any]) -> tuple[bool, bool, bool]:
        """-> (erlaubt, einschalten_gewünscht, ausschalten_nötig)."""
        room = self.rooms[room_id]
        enabled = bool(room.get("aux_climate")) and room.get("aux_enabled", True)
        ok = outside_ok(bool(self.aux_outside_entity), self._outside_value(), self.aux_settings["outside_limit"])
        target = status["target"] if status.get("source") != SOURCE_INACTIVE else None
        want_on, must_off = conditions(enabled=enabled, target=target, current=status.get("current_temp"),
                                       outside_is_ok=ok, settings=self.aux_settings)
        return enabled, want_on, must_off

    def aux_status(self, room_id: str, status: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        room = self.rooms[room_id]
        if not room.get("aux_climate") or room_kind(room) != KIND_CLIMATE:
            return None
        enabled, want_on, _ = self._aux_conditions(room_id, status)
        st = self.aux_state.get(room_id) or {}
        since = self._aux_demand_since.get(room_id)
        return {
            "climate": room["aux_climate"],
            "enabled": enabled,
            "demand": want_on,
            "demand_since": since.isoformat() if since else None,
            "active": bool(st.get("active")),
            "since": st.get("since"),
            "paused": room_id in self._aux_blocked,
            "boost": bool(st.get("boost")),
            "boost_since": st.get("boost_since"),
            "boost_until": self._boost_until(st),
        }

    def _boost_uncertain(self, room: dict[str, Any]) -> bool:
        """True, solange Heizperiode-Schalter oder Raum-Thermostat nicht verfügbar sind (z. B. beim Neustart)."""
        for ent in (self.season_entity if room.get("follow_season", True) else None, room.get("climate")):
            if ent:
                state = self.hass.states.get(ent)
                if state is None or state.state in ("unavailable", "unknown"):
                    return True
        return False

    def _boost_until(self, st: dict[str, Any]) -> str | None:
        start = dt_util.parse_datetime(st["boost_since"]) if st.get("boost") and st.get("boost_since") else None
        return (start + timedelta(minutes=self.aux_settings["boost_max"])).isoformat() if start else None

    async def _aux_call(self, domain: str, service: str, data: dict[str, Any]) -> bool:
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True)
            return True
        except Exception as err:  # noqa: BLE001 – ein Raum soll andere nicht stoppen
            _LOGGER.error("Heizplan: Zuheizen %s.%s %s fehlgeschlagen: %s", domain, service, data, err)
            return False

    async def _aux_apply(self, now: datetime) -> bool:
        """Klimageräte je Raum ein-/ausschalten. True, wenn sich der gespeicherte Zustand änderte."""
        changed = False
        wait = timedelta(minutes=self.aux_settings["wait"])
        takeover = self.aux_settings["takeover"]
        for room_id, room in list(self.rooms.items()):
            ent = room.get("aux_climate")
            if not ent or room_kind(room) != KIND_CLIMATE:
                if self.aux_state.pop(room_id, None) is not None:
                    changed = True
                continue
            try:
                status = self.room_status(room_id, now)
            except (KeyError, PlanError):
                continue
            if room_id in self._aux_starting:
                continue  # Klima wird gerade eingeschaltet (wartet auf Bestätigung)
            st = self.aux_state.setdefault(room_id, {"active": False, "owned": False, "since": None})
            klima = self.hass.states.get(ent)
            hvac = klima.state if klima is not None else None
            if hvac in (None, "unavailable", "unknown") or self._boost_uncertain(room):
                continue  # Klima, Heizperiode oder Thermostat (noch) nicht erreichbar (z. B. Neustart): nichts entscheiden
            if st.get("active") and hvac != HEAT and now - self._loaded_at < STARTUP_GRACE:
                continue  # direkt nach dem Start meldet die Klima ggf. noch „aus“ (Status nicht gelesen): abwarten
            _, want_on, must_off = self._aux_conditions(room_id, status)

            # Boost: von Hand angefordert – heizt ohne Wartezeit/Abweichung/Außengrenze bis Ist ≥ Soll
            boost_ended = False
            if st.get("boost"):
                target = status["target"] if status.get("source") != SOURCE_INACTIVE else None
                b_on, b_off = boost_conditions(target=target, current=status.get("current_temp"))
                b_start = dt_util.parse_datetime(st["boost_since"]) if st.get("boost_since") else now
                if b_off or now - b_start >= timedelta(minutes=self.aux_settings["boost_max"]):
                    st.update(boost=False, boost_since=None)
                    boost_ended = True
                    changed = True
                    _LOGGER.info("Heizplan: Boost %s beendet", ent)
                else:
                    self._aux_blocked.discard(room_id)
                    want_on, must_off = b_on, False
                    if b_on:
                        self._aux_demand_since[room_id] = now - timedelta(minutes=self.aux_settings["wait"])
            if (boost_ended or not st.get("boost")) and st.get("by_boost") and st.get("active") and not want_on:
                must_off = True   # vom Boost gestartet: nach dem Boost aus, außer normales Zuheizen will weiter

            started = dt_util.parse_datetime(st["since"]) if st.get("since") else None
            settled = started is None or now - started >= timedelta(minutes=2)
            if st["active"] and hvac != HEAT and settled:
                # Klima wurde während des Zuheizens von Hand aus- oder umgestellt: Zuheizen beenden.
                # Direkt nach einem Neustart nur zurücksetzen, sonst Pause, bis der Bedarf endet.
                # (In den ersten 2 min nach dem Einschalten nicht, bis die Klima ihren Zustand meldet.)
                if room_id not in self._aux_fresh:
                    self._aux_blocked.add(room_id)
                st.update(active=False, owned=False, since=None, boost=False, boost_since=None, by_boost=False)
                self._aux_written.pop(room_id, None)
                changed = True
            self._aux_fresh.discard(room_id)
            if must_off:
                self._aux_blocked.discard(room_id)

            if want_on:
                self._aux_demand_since.setdefault(room_id, now)
            else:
                self._aux_demand_since.pop(room_id, None)

            if st["active"]:
                if must_off:
                    if hvac == HEAT and (takeover or st["owned"] or st.get("by_boost")):
                        await self._aux_call("climate", "set_hvac_mode", {ATTR_ENTITY_ID: ent, "hvac_mode": RUNNING_OFF})
                        _LOGGER.info("Heizplan: Zuheizen %s aus", ent)
                    st.update(active=False, owned=False, since=None, by_boost=False)
                    self._aux_written.pop(room_id, None)
                    changed = True
                else:
                    # Soll folgt dem Raum-Soll
                    value = climate_setpoint(status["target"], dict(klima.attributes))
                    if self._aux_written.get(room_id) != value:
                        if await self._aux_call("climate", "set_temperature", {ATTR_ENTITY_ID: ent, "temperature": value}):
                            self._aux_written[room_id] = value
                continue

            since = self._aux_demand_since.get(room_id)
            if (want_on and since is not None and now - since >= wait
                    and room_id not in self._aux_blocked and can_start(hvac, takeover or bool(st.get("boost")))):
                failed = self._aux_start_failed.get(room_id)
                if failed is not None and now - failed < START_RETRY:
                    continue
                value = climate_setpoint(status["target"], dict(klima.attributes))
                # Zuerst EIN und auf die Bestätigung des Geräts warten, dann Modus und Soll (klima_start).
                # Läuft im Hintergrund; solange bleibt der Raum hier unangetastet.
                self._aux_starting.add(room_id)
                self.hass.async_create_task(
                    self._aux_start(room_id, ent, value, owned=hvac != HEAT), f"heizplan_klima_start_{room_id}")
        return changed

    async def _aux_start(self, room_id: str, ent: str, value: float, owned: bool) -> None:
        try:
            room = self.rooms.get(room_id) or {}
            ok = await async_start_climate(self.hass, ent, HEAT, value, self._aux_call,
                                           fan_auto_switch=room.get("aux_fan_switch") or None)
            st = self.aux_state.setdefault(room_id, {"active": False, "owned": False, "since": None})
            if ok:
                self._aux_start_failed.pop(room_id, None)
                self._aux_written[room_id] = value
                st.update(active=True, owned=owned, since=dt_util.now().isoformat(), by_boost=bool(st.get("boost")))
                _LOGGER.info("Heizplan: Zuheizen %s an (%s °C)", ent, value)
                klima = self.hass.states.get(ent)
                if room.get("aux_fan_mode") and klima is not None:
                    await self._aux_fan({**room, "aux_fan_switch": None}, klima)   # Lüfter-Schalter schon gesetzt
            else:
                self._aux_start_failed[room_id] = dt_util.now()
                _LOGGER.warning("Heizplan: Zuheizen %s – Klima hat das Einschalten nicht bestätigt, "
                                "neuer Versuch in %s min", ent, int(START_RETRY.total_seconds() // 60))
        finally:
            self._aux_starting.discard(room_id)
            self._schedule_save()
            async_dispatcher_send(self.hass, SIGNAL_UPDATED)

    async def _aux_fan(self, room: dict[str, Any], klima) -> None:
        fan_mode = room.get("aux_fan_mode")
        if fan_mode:
            modes = klima.attributes.get("fan_modes") or []
            if fan_mode in modes:
                await self._aux_call("climate", "set_fan_mode",
                                     {ATTR_ENTITY_ID: room["aux_climate"], "fan_mode": fan_mode})
            else:
                _LOGGER.warning("Heizplan: Lüfterstufe %s gibt es bei %s nicht (%s)", fan_mode, room["aux_climate"], modes)
        fan_switch = room.get("aux_fan_switch")
        if fan_switch:
            await self._aux_call(fan_switch.split(".", 1)[0], "turn_on", {ATTR_ENTITY_ID: fan_switch})

    async def async_set_aux_settings(self, cfg: dict[str, Any]) -> None:
        self.aux_settings = normalize_aux_settings({**self.aux_settings, **cfg})
        await self._changed()

    async def async_set_boost(self, room_id: str, on: bool) -> None:
        """Boost mit Wärmepumpe: heizt den Raum mit der Klima, bis der Soll erreicht ist."""
        room = self.rooms[room_id]
        if on and (not room.get("aux_climate") or room_kind(room) != KIND_CLIMATE):
            raise PlanError(f"{room.get('name', room_id)}: kein Klimagerät zum Zuheizen hinterlegt")
        st = self.aux_state.setdefault(room_id, {"active": False, "owned": False, "since": None})
        if on:
            st.update(boost=True, boost_since=dt_util.now().isoformat())
            self._aux_blocked.discard(room_id)
        else:
            st.update(boost=False, boost_since=None)
        await self._changed()

    async def async_set_aux_enabled(self, room_id: str, enabled: bool) -> None:
        self.rooms[room_id]["aux_enabled"] = bool(enabled)
        await self._changed()

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
        kind = kind_for_entity(climate)
        self.rooms[room_id] = {
            "name": name or (state.name if state else room_id),
            "climate": climate,
            "kind": kind,
            "valve": valve,
            "plan": default_plan(fallback_for(kind), kind),
        }
        await self._changed(rooms_changed=True)
        return room_id

    async def async_update_room(self, room_id: str, name: str | None = None,
                                valve: str | None = None, follow_season: bool | None = None,
                                months: list[int] | None = None, aux_climate: str | None = None,
                                aux_fan_mode: str | None = None, aux_fan_switch: str | None = None,
                                aux_enabled: bool | None = None) -> None:
        room = self.rooms[room_id]
        had_aux = bool(room.get("aux_climate"))
        if name is not None:
            room["name"] = name
        if valve is not None:
            room["valve"] = valve or None
        if follow_season is not None:
            room["follow_season"] = bool(follow_season)
        if months is not None:
            room["months"] = [int(m) for m in months if 1 <= int(m) <= 12] or None
        if aux_climate is not None:
            if aux_climate and not aux_climate.startswith("climate."):
                raise PlanError("Zuheizen: Klimagerät muss climate.* sein")
            if aux_climate and room_kind(room) != KIND_CLIMATE:
                raise PlanError("Zuheizen geht nur für Heizkörper-Räume")
            room["aux_climate"] = aux_climate or None
        if aux_fan_mode is not None:
            room["aux_fan_mode"] = aux_fan_mode or None
        if aux_fan_switch is not None:
            if aux_fan_switch and aux_fan_switch.split(".", 1)[0] not in ("switch", "input_boolean"):
                raise PlanError("Lüfter-Schalter muss switch.* oder input_boolean.* sein")
            room["aux_fan_switch"] = aux_fan_switch or None
        if aux_enabled is not None:
            room["aux_enabled"] = bool(aux_enabled)
        await self._changed(rooms_changed=had_aux != bool(room.get("aux_climate")))

    async def async_remove_room(self, room_id: str) -> None:
        self.rooms.pop(room_id)
        self.overrides.pop(room_id, None)
        self._last_written.pop(room_id, None)
        self.aux_state.pop(room_id, None)
        await self._changed(rooms_changed=True)

    async def async_set_plan(self, room_id: str, plan: dict[str, Any]) -> None:
        kind = room_kind(self.rooms[room_id])
        self.rooms[room_id]["plan"] = normalize_plan(plan, fallback_for(kind), kind)
        await self._changed()

    async def async_set_day(self, room_id: str, day: str, blocks: list, copy_to: list[str] | None = None) -> None:
        if day not in DAYS:
            raise PlanError(f"Unbekannter Tag {day}")
        kind = room_kind(self.rooms[room_id])
        norm = normalize_day(blocks, fallback_for(kind), kind)
        plan = self.rooms[room_id]["plan"]
        for target in [day, *(copy_to or [])]:
            if target in DAYS:
                plan[target] = [dict(b) for b in norm]
        await self._changed()

    async def async_set_override(self, room_id: str, value: Any, minutes: int) -> None:
        if room_id not in self.rooms:
            raise PlanError(f"Unbekannter Raum {room_id}")
        until = dt_util.now() + timedelta(minutes=int(minutes))
        if room_kind(self.rooms[room_id]) == KIND_SWITCH:
            self.overrides[room_id] = {"on": bool(value), "until": until.isoformat()}
        else:
            self.overrides[room_id] = {"temp": round(float(value) * 2) / 2, "until": until.isoformat()}
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

    async def async_save_template(self, template_id: str | None, name: str, plan: dict[str, Any],
                                  kind: str = KIND_CLIMATE) -> str:
        tid = template_id or re.sub(r"[^a-z0-9_]", "_", name.lower()) or "vorlage"
        base, n = tid, 2
        while template_id is None and tid in self.templates:
            tid, n = f"{base}_{n}", n + 1
        self.templates[tid] = {"name": name, "kind": kind,
                               "plan": normalize_plan(plan, fallback_for(kind), kind)}
        self._schedule_save()
        async_dispatcher_send(self.hass, SIGNAL_UPDATED)
        return tid

    async def async_delete_template(self, template_id: str) -> None:
        self.templates.pop(template_id, None)
        self._schedule_save()
        async_dispatcher_send(self.hass, SIGNAL_UPDATED)

    async def async_apply_template(self, template_id: str, room_ids: list[str]) -> None:
        tpl = self.templates[template_id]
        tkind = tpl.get("kind", KIND_CLIMATE)
        for room_id in room_ids:
            if room_id in self.rooms and room_kind(self.rooms[room_id]) == tkind:
                self.rooms[room_id]["plan"] = normalize_plan(tpl["plan"], fallback_for(tkind), tkind)
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
                result["skipped"].append({"entity": ent, "reason": "keine Temperatur-/Schalt-Blöcke"})
                continue
            for climate in targets:
                if climate.split(".", 1)[0] not in ("climate", "switch", "input_boolean"):
                    continue
                expected = kind_for_entity(climate)
                if any(("on" in b) != (expected == KIND_SWITCH) for b in blocks):
                    result["skipped"].append({"entity": ent, "reason": f"Aktionen passen nicht zu {climate}"})
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
                kind = kind_for_entity(climate)
                self.rooms[room_id] = {
                    "name": state.name if state else room_id,
                    "climate": climate,
                    "kind": kind,
                    "valve": None,
                    "plan": default_plan(fallback_for(kind), kind),
                }
                rooms_changed = True
            kind = room_kind(self.rooms[room_id])
            plan = self.rooms[room_id]["plan"]
            for day, blocks in partial.items():
                plan[day] = normalize_day(blocks, fallback_for(kind), kind)
        await self._changed(rooms_changed=rooms_changed)
        return result
