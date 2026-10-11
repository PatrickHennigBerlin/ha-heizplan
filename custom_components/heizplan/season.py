"""Automatik für die Heizperiode: manuell, nach Datum oder nach Wettervorhersage.

Reine Funktionen ohne Home-Assistant-Abhängigkeit (testbar).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

SEASON_MANUAL = "manual"
SEASON_DATES = "dates"
SEASON_WEATHER = "weather"
SEASON_MODES = [SEASON_MANUAL, SEASON_DATES, SEASON_WEATHER]

BASIS_MEAN = "mean"   # Tagesmittel aus Höchst- und Tiefstwert
BASIS_MAX = "max"     # Tageshöchstwert
BASIS_MIN = "min"     # Tagestiefstwert
BASES = [BASIS_MEAN, BASIS_MAX, BASIS_MIN]

DEFAULT_SEASON_AUTO: dict[str, Any] = {
    "mode": SEASON_MANUAL,
    "on_date": "10-01",     # MM-TT
    "off_date": "05-01",
    "weather_entity": None,
    "on_below": 10.0,       # an, wenn alle Tage darunter
    "off_above": 15.0,      # aus, wenn alle Tage darüber
    "days": 6,
    "basis": BASIS_MEAN,
}


class SeasonError(ValueError):
    """Ungültige Einstellung."""


def _mmdd(value: Any) -> str:
    """Akzeptiert 'MM-TT', 'TT.MM.' oder 'TT.MM' und gibt 'MM-TT' zurück."""
    text = str(value or "").strip().rstrip(".")
    try:
        if "-" in text:
            month, day = (int(x) for x in text.split("-"))
        elif "." in text:
            day, month = (int(x) for x in text.split("."))
        else:
            raise ValueError
        date(2024, month, day)  # Schaltjahr -> 29.02. erlaubt
    except (ValueError, TypeError) as err:
        raise SeasonError(f"Ungültiges Datum: {value!r} (erwartet TT.MM)") from err
    return f"{month:02d}-{day:02d}"


def normalize_season_auto(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Ergänzt Standardwerte und prüft alle Felder."""
    out = {**DEFAULT_SEASON_AUTO, **(cfg or {})}
    if out["mode"] not in SEASON_MODES:
        raise SeasonError(f"Unbekannte Steuerung: {out['mode']}")
    out["on_date"] = _mmdd(out["on_date"])
    out["off_date"] = _mmdd(out["off_date"])
    if out["on_date"] == out["off_date"]:
        raise SeasonError("Ein- und Ausschaltdatum dürfen nicht gleich sein")
    try:
        out["on_below"] = round(float(out["on_below"]) * 2) / 2
        out["off_above"] = round(float(out["off_above"]) * 2) / 2
        out["days"] = int(out["days"])
    except (TypeError, ValueError) as err:
        raise SeasonError("Grenzwerte und Tage müssen Zahlen sein") from err
    if out["on_below"] >= out["off_above"]:
        raise SeasonError("„An unter“ muss kleiner sein als „Aus über“")
    if not 1 <= out["days"] <= 10:
        raise SeasonError("Tage: 1 bis 10")
    if out["basis"] not in BASES:
        raise SeasonError(f"Unbekannte Basis: {out['basis']}")
    if out["weather_entity"] in ("", None):
        out["weather_entity"] = None
    elif not str(out["weather_entity"]).startswith("weather."):
        raise SeasonError("Wetter-Entität muss weather.* sein")
    if out["mode"] == SEASON_WEATHER and not out["weather_entity"]:
        raise SeasonError("Für die Wetter-Steuerung eine Wetter-Entität wählen")
    return out


def date_decision(today: date, on_date: str, off_date: str) -> bool | None:
    """True am Einschalttag, False am Ausschalttag, sonst None."""
    key = f"{today.month:02d}-{today.day:02d}"
    if key == on_date:
        return True
    if key == off_date:
        return False
    return None


def _entry_date(entry: dict[str, Any]) -> date | None:
    raw = entry.get("datetime")
    if not raw:
        return None
    try:
        dt = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.date()


def daily_values(forecast: list[dict[str, Any]], today: date, days: int, basis: str) -> list[float]:
    """Tageswerte ab heute (heute zählt mit), höchstens `days` Stück.

    Einträge ohne passende Temperatur werden übersprungen.
    """
    values: list[float] = []
    for entry in sorted(forecast or [], key=lambda e: str(e.get("datetime", ""))):
        day = _entry_date(entry)
        if day is None or day < today:
            continue
        high = entry.get("temperature")
        low = entry.get("templow")
        if basis == BASIS_MAX:
            value = high
        elif basis == BASIS_MIN:
            value = low
        else:
            value = (float(high) + float(low)) / 2 if high is not None and low is not None else None
        if value is None:
            continue
        values.append(round(float(value), 1))
        if len(values) >= days:
            break
    return values


def weather_decision(values: list[float], days: int, on_below: float, off_above: float) -> bool | None:
    """True wenn alle Tage < on_below, False wenn alle > off_above, sonst None.

    Liefert die Vorhersage weniger als `days` Tage, wird nichts entschieden.
    """
    if len(values) < days:
        return None
    if all(v < on_below for v in values):
        return True
    if all(v > off_above for v in values):
        return False
    return None
