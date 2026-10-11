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


def normalize_day(blocks: list[dict[str, Any]], fallback_temp: float) -> list[dict[str, Any]]:
    """Sortiert, prüft, fasst gleiche Nachbarn zusammen, sorgt für Block um 00:00."""
    if not isinstance(blocks, list):
        raise PlanError("Tag muss eine Liste von Blöcken sein")
    parsed: dict[int, float] = {}
    for block in blocks:
        if not isinstance(block, dict):
            raise PlanError("Block muss ein Objekt sein")
        start = _parse_hhmm(block.get("start", ""))
        if start >= 24 * 60:
            continue
        parsed[start] = _temp(block.get("temp"))
    if 0 not in parsed:
        parsed[0] = _temp(fallback_temp)
    result: list[dict[str, Any]] = []
    for start in sorted(parsed):
        temp = parsed[start]
        if result and result[-1]["temp"] == temp:
            continue  # gleicher Wert wie davor -> zusammenfassen
        result.append({"start": _fmt(start), "temp": temp})
    return result


def normalize_plan(plan: dict[str, Any], fallback_temp: float = 20.0) -> dict[str, list]:
    """Prüft einen kompletten Wochenplan."""
    if not isinstance(plan, dict):
        raise PlanError("Plan muss ein Objekt sein")
    result = {}
    for day in DAYS:
        result[day] = normalize_day(plan.get(day, []), fallback_temp)
    return result


def default_plan(temp: float = 20.0) -> dict[str, list]:
    return {day: [{"start": "00:00", "temp": float(temp)}] for day in DAYS}


def _day_key(dt: datetime) -> str:
    return DAYS[dt.weekday()]


def block_at(plan: dict[str, list], when: datetime) -> tuple[float, int, int]:
    """(Temperatur, Blockstart-Minute, Blockende-Minute) für einen Zeitpunkt."""
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
    return float(current["temp"]), _parse_hhmm(current["start"]), end


def temp_at(plan: dict[str, list], when: datetime) -> float:
    return block_at(plan, when)[0]


def next_change(plan: dict[str, list], when: datetime) -> tuple[datetime, float] | None:
    """Nächster Zeitpunkt, an dem sich die Plantemperatur ändert (max. 8 Tage)."""
    current = temp_at(plan, when)
    base = when.replace(hour=0, minute=0, second=0, microsecond=0)
    minute_now = when.hour * 60 + when.minute
    for offset in range(0, 8):
        day_start = base + timedelta(days=offset)
        for block in plan[_day_key(day_start)]:
            start = _parse_hhmm(block["start"])
            if offset == 0 and start <= minute_now:
                continue
            if float(block["temp"]) != current:
                return day_start + timedelta(minutes=start), float(block["temp"])
            current = float(block["temp"])
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

    Unterstützt nur climate.set_temperature-Aktionen.
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
        if action.get("service") != "climate.set_temperature":
            continue
        temp = (action.get("data") or {}).get("temperature")
        if temp is None:
            continue
        start = slot.split(" - ")[0].strip()
        blocks.append({"start": start[:5], "temp": float(temp)})
    return days, entities, blocks
