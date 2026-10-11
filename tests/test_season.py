"""Tests für die Heizperiode-Automatik (reine Funktionen)."""

from datetime import date

import pytest

from custom_components.heizplan.season import (
    SeasonError,
    date_decision,
    daily_values,
    normalize_season_auto,
    weather_decision,
)


def _fc(*days):
    """days: (iso-datum, hoch, tief)"""
    return [{"datetime": f"{d}T12:00:00+00:00", "temperature": hi, "templow": lo} for d, hi, lo in days]


def test_normalize_defaults_and_dates():
    cfg = normalize_season_auto({"on_date": "01.10.", "off_date": "1.5"})
    assert cfg["mode"] == "manual"
    assert cfg["on_date"] == "10-01"
    assert cfg["off_date"] == "05-01"
    assert cfg["on_below"] == 10.0 and cfg["off_above"] == 15.0 and cfg["days"] == 6


@pytest.mark.parametrize(
    "cfg",
    [
        {"mode": "bla"},
        {"on_date": "32.10."},
        {"on_date": "01.10.", "off_date": "01.10."},
        {"on_below": 15, "off_above": 10},
        {"days": 0},
        {"mode": "weather"},  # ohne Wetter-Entität
        {"weather_entity": "sensor.x"},
    ],
)
def test_normalize_rejects(cfg):
    with pytest.raises(SeasonError):
        normalize_season_auto(cfg)


def test_date_decision():
    assert date_decision(date(2026, 10, 1), "10-01", "05-01") is True
    assert date_decision(date(2027, 5, 1), "10-01", "05-01") is False
    assert date_decision(date(2026, 12, 24), "10-01", "05-01") is None


def test_daily_values_mean_from_today_and_skips_past():
    fc = _fc(
        ("2026-10-10", 20, 10),  # gestern -> ignoriert
        ("2026-10-11", 12, 4),
        ("2026-10-12", 11, 5),
        ("2026-10-13", 9, 3),
    )
    assert daily_values(fc, date(2026, 10, 11), 6, "mean") == [8.0, 8.0, 6.0]
    assert daily_values(fc, date(2026, 10, 11), 2, "max") == [12.0, 11.0]
    assert daily_values(fc, date(2026, 10, 11), 2, "min") == [4.0, 5.0]


def test_weather_decision_on_off_and_between():
    assert weather_decision([9, 8, 7, 9.5, 6, 5], 6, 10, 15) is True
    assert weather_decision([16, 17, 15.5, 18, 20, 16], 6, 10, 15) is False
    assert weather_decision([9, 8, 11, 9, 6, 5], 6, 10, 15) is None   # ein Tag zu warm
    assert weather_decision([9, 8, 7], 6, 10, 15) is None             # Vorhersage zu kurz
    assert weather_decision([10, 9, 9, 9, 9, 9], 6, 10, 15) is None   # genau 10 ist nicht "unter"
