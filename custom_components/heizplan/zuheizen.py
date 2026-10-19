"""Zuheizen mit Klimagerät: reine Entscheidungslogik (ohne Home Assistant, testbar).

Je Raum kann ein Klimagerät (climate mit hvac_mode 'heat') mitheizen, wenn die
Heizung allein den Soll nicht erreicht.
"""

from __future__ import annotations

import math
from typing import Any

DEFAULT_AUX_SETTINGS: dict[str, Any] = {
    "on_delta": 1.5,       # einschalten ab Soll − Ist ≥ on_delta (K)
    "off_delta": 0.5,      # ausschalten ab Ist ≥ Soll − off_delta (K)
    "wait": 10,            # Einschaltbedingung muss so lange durchgehend gelten (min)
    "outside_limit": 10.0, # nur zuheizen, wenn außen kälter (°C) – nur mit Außenfühler
    "takeover": True,      # von Hand eingeschaltete Klima übernehmen
    "boost_max": 180,      # Boost endet spätestens nach so vielen Minuten
}

RUNNING_OFF = "off"
HEAT = "heat"


class AuxError(ValueError):
    """Ungültige Einstellung."""


def normalize_aux_settings(cfg: dict[str, Any] | None) -> dict[str, Any]:
    out = {**DEFAULT_AUX_SETTINGS, **(cfg or {})}
    try:
        out["on_delta"] = round(float(out["on_delta"]) * 10) / 10
        out["off_delta"] = round(float(out["off_delta"]) * 10) / 10
        out["wait"] = int(out["wait"])
        out["outside_limit"] = round(float(out["outside_limit"]) * 2) / 2
        out["boost_max"] = int(out["boost_max"])
    except (TypeError, ValueError) as err:
        raise AuxError("Zuheizen: Werte müssen Zahlen sein") from err
    out["takeover"] = bool(out["takeover"])
    if not 0.5 <= out["on_delta"] <= 10:
        raise AuxError("Abweichung: 0,5 bis 10 K")
    if not 0 <= out["off_delta"] < out["on_delta"]:
        raise AuxError("Abschaltabstand muss kleiner sein als die Abweichung")
    if not 0 <= out["wait"] <= 120:
        raise AuxError("Wartezeit: 0 bis 120 min")
    if not -30 <= out["outside_limit"] <= 30:
        raise AuxError("Außengrenze: −30 bis 30 °C")
    if not 15 <= out["boost_max"] <= 720:
        raise AuxError("Boost-Höchstdauer: 15 bis 720 min")
    return out


def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def outside_ok(outside_configured: bool, outside_value: Any, limit: float) -> bool:
    """Ohne Außenfühler immer ok. Mit Fühler: nur wenn verfügbar und kälter als die Grenze."""
    if not outside_configured:
        return True
    v = _num(outside_value)
    return v is not None and v < limit


def conditions(*, enabled: bool, target: Any, current: Any, outside_is_ok: bool,
               settings: dict[str, Any]) -> tuple[bool, bool]:
    """-> (einschalten_gewünscht, ausschalten_nötig).

    target None = Raum inaktiv (Heizperiode aus, Modus Aus …).
    Zwischen beiden Schwellen sind beide False: Zustand bleibt (Hysterese).
    """
    t, c = _num(target), _num(current)
    if not enabled or t is None or not outside_is_ok:
        return False, True
    if c is None:
        # ohne Ist-Wert nichts Neues anfangen, laufendes Zuheizen aber nicht blind beenden
        return False, False
    want_on = (t - c) >= settings["on_delta"] - 1e-9
    must_off = c >= t - settings["off_delta"] - 1e-9
    return want_on, must_off


def boost_conditions(*, target: Any, current: Any) -> tuple[bool, bool]:
    """Boost: Klima heizt, bis der Raum-Soll erreicht ist. -> (heizen, beenden)."""
    t, c = _num(target), _num(current)
    if t is None:
        return False, True          # Raum inaktiv: Boost beenden
    if c is None:
        return False, False         # ohne Ist-Wert abwarten
    reached = c >= t - 1e-9
    return not reached, reached


def climate_setpoint(target: float, attrs: dict[str, Any]) -> float:
    """Raum-Soll auf min/max und Schrittweite des Klimageräts bringen."""
    value = float(target)
    step = _num(attrs.get("target_temp_step")) or 0.5
    value = round(value / step) * step
    low, high = _num(attrs.get("min_temp")), _num(attrs.get("max_temp"))
    if low is not None:
        value = max(value, low)
    if high is not None:
        value = min(value, high)
    # Rundungsartefakte (z. B. 21.000000004) entfernen
    return round(value, 2)


def can_start(hvac_state: str | None, takeover: bool) -> bool:
    """Darf Heizplan das Klimagerät jetzt auf Heizen stellen?"""
    if hvac_state in (None, "unavailable", "unknown"):
        return False
    if takeover:
        return True
    return hvac_state == RUNNING_OFF
