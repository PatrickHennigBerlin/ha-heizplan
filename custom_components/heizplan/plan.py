"""Reine Planlogik (ohne Home-Assistant-Abhängigkeit, damit testbar).

Ein Wochenplan ist ein dict  {"mon": [Block, ...], ..., "sun": [...]}.
Ein Block ist {"start": "HH:MM", "temp": float} und gilt bis zum Start des
nächsten Blocks (bzw. bis 24:00). Jeder Tag beginnt mit einem Block um 00:00.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

MIN_TEMP = 5.0
MAX_TEMP = 30.0


class PlanError(ValueError):
    """Ungültiger Plan."""


def _parse_hhmm(value: str) -> int:
    """'HH:MM' oder 'HH:MM:SS' -> Minuten seit Mitternacht."""
    if not isinstance(value, str):
        raise PlanError(f"Ungültige Uhrzeit: {value!r}")
    parts = value.strip().split(":")
    if len(parts) not in (2, 3):
        raise PlanError(f"Ungültige Uhrzeit: {value!r}")
    try:
        hours, minutes = int(parts[0]), int(parts[1])
    except ValueError as err:
        raise PlanError(f"Ungültige Uhrzeit: {value!r}") from err
    if hours == 24 and minutes == 0:
        return 24 * 60
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise PlanError(f"Ungültige Uhrzeit: {value!r}")
    return hours * 60 + minutes


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _temp(value: Any) -> float:
    try:
        temp = round(float(value) * 2) / 2  # 0,5-°C-Raster
    except (TypeError, ValueError) as err:
        raise PlanError(f"Ungültige Temperatur: {value!r}") from err
    if not (MIN_TEMP <= temp <= MAX_TEMP):
        raise PlanError(f"Temperatur außerhalb {MIN_TEMP}–{MAX_TEMP} °C: {temp}")
    return temp


KIND_CLIMATE = "climate"   # Blöcke mit "temp" (°C)
KIND_SWITCH = "switch"     # Blöcke mit "on" (True/False), z. B. Fußbodenheizung


def _onoff(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str) and value.lower() in ("on", "an", "ein", "true", "1"):
        return True
    if isinstance(value, str) and value.lower() in ("off", "aus", "false", "0"):
        return False
    raise PlanError(f"Ungültiger Schaltwert: {value!r}")


def block_value(block: dict[str, Any]) -> float | bool:
    """Wert eines Blocks: Temperatur (float) oder Ein/Aus (bool)."""
    if "on" in block:
        return bool(block["on"])
    return float(block["temp"])


def normalize_day(blocks: list[dict[str, Any]], fallback: Any, kind: str = KIND_CLIMATE) -> list[dict[str, Any]]:
    """Sortiert, prüft, fasst gleiche Nachbarn zusammen, sorgt für Block um 00:00."""
    if not isinstance(blocks, list):
        raise PlanError("Tag muss eine Liste von Blöcken sein")
    key = "on" if kind == KIND_SWITCH else "temp"
    conv = _onoff if kind == KIND_SWITCH else _temp
    parsed: dict[int, Any] = {}
    for block in blocks:
        if not isinstance(block, dict):
            raise PlanError("Block muss ein Objekt sein")
        start = _parse_hhmm(block.get("start", ""))
        if start >= 24 * 60:
            continue
        parsed[start] = conv(block.get(key))
    if 0 not in parsed:
        parsed[0] = conv(fallback)
    result: list[dict[str, Any]] = []
    for start in sorted(parsed):
        value = parsed[start]
        if result and result[-1][key] == value:
            continue  # gleicher Wert wie davor -> zusammenfassen
        result.append({"start": _fmt(start), key: value})
    return result


def normalize_plan(plan: dict[str, Any], fallback: Any = 20.0, kind: str = KIND_CLIMATE) -> dict[str, list]:
    """Prüft einen kompletten Wochenplan."""
    if not isinstance(plan, dict):
        raise PlanError("Plan muss ein Objekt sein")
    return {day: normalize_day(plan.get(day, []), fallback, kind) for day in DAYS}


def default_plan(value: Any = 20.0, kind: str = KIND_CLIMATE) -> dict[str, list]:
    key = "on" if kind == KIND_SWITCH else "temp"
    val = bool(value) if kind == KIND_SWITCH else float(value)
    return {day: [{"start": "00:00", key: val}] for day in DAYS}


def _day_key(dt: datetime) -> str:
    return DAYS[dt.weekday()]


def block_at(plan: dict[str, list], when: datetime) -> tuple[float | bool, int, int]:
    """(Wert, Blockstart-Minute, Blockende-Minute) für einen Zeitpunkt."""
    day = plan[_day_key(when)]
    minute = when.hour * 60 + when.minute
    current = day[0]
    end = 24 * 60
    for idx, block in enumerate(day):
        start = _parse_hhmm(block["start"])
        if start <= minute:
            current = block
            end = _parse_hhmm(day[idx + 1]["start"]) if idx + 1 < len(day) else 24 * 60
        else:
            break
    return block_value(current), _parse_hhmm(current["start"]), end


def temp_at(plan: dict[str, list], when: datetime) -> float | bool:
    return block_at(plan, when)[0]


def next_change(plan: dict[str, list], when: datetime) -> tuple[datetime, float | bool] | None:
    """Nächster Zeitpunkt, an dem sich der Planwert ändert (max. 8 Tage)."""
    current = temp_at(plan, when)
    base = when.replace(hour=0, minute=0, second=0, microsecond=0)
    minute_now = when.hour * 60 + when.minute
    for offset in range(0, 8):
        day_start = base + timedelta(days=offset)
        for block in plan[_day_key(day_start)]:
            start = _parse_hhmm(block["start"])
            if offset == 0 and start <= minute_now:
                continue
            value = block_value(block)
            if value != current:
                return day_start + timedelta(minutes=start), value
            current = value
    return None


def change_points(plan: dict[str, list]) -> list[tuple[int, int]]:
    """Alle (Wochentag, Minute) an denen ein Block beginnt – für Timer."""
    points = []
    for idx, day in enumerate(DAYS):
        for block in plan[day]:
            points.append((idx, _parse_hhmm(block["start"])))
    return points


# ---------------------------------------------------------------------------
# Import aus dem Scheduler-Component (nielsfaber)
# ---------------------------------------------------------------------------

WEEKDAY_GROUPS = {
    "daily": DAYS,
    "workday": DAYS[:5],
    "weekend": DAYS[5:],
    "mon": ["mon"], "tue": ["tue"], "wed": ["wed"], "thu": ["thu"],
    "fri": ["fri"], "sat": ["sat"], "sun": ["sun"],
}


def blocks_from_scheduler(attributes: dict[str, Any]) -> tuple[list[str], list[str], list[dict]]:
    """Liest Wochentage, Ziel-Entities und Blöcke aus einem switch.schedule_* Attributsatz.

    Unterstützt climate.set_temperature (-> {"temp"}) und
    switch/input_boolean turn_on/turn_off (-> {"on"}).
    """
    days: list[str] = []
    for group in attributes.get("weekdays", []):
        for day in WEEKDAY_GROUPS.get(group, []):
            if day not in days:
                days.append(day)
    entities = list(attributes.get("entities", []))
    slots = attributes.get("timeslots", [])
    actions = attributes.get("actions", [])
    blocks = []
    for slot, action in zip(slots, actions):
        service = action.get("service", "")
        start = slot.split(" - ")[0].strip()[:5]
        if service == "climate.set_temperature":
            temp = (action.get("data") or {}).get("temperature")
            if temp is not None:
                blocks.append({"start": start, "temp": float(temp)})
        elif service.endswith(".turn_on") or service.endswith(".turn_off"):
            blocks.append({"start": start, "on": service.endswith(".turn_on")})
    return days, entities, blocks
