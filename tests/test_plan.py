"""Tests der reinen Planlogik."""

from datetime import datetime

import pytest

from custom_components.heizplan.plan import (
    PlanError,
    block_at,
    blocks_from_scheduler,
    next_change,
    normalize_day,
    normalize_plan,
    temp_at,
)

LOUNGE_WERKTAGS = {
    "weekdays": ["workday"],
    "timeslots": ["00:00:00 - 17:30:00", "17:30:00 - 23:00:00", "23:00:00 - 00:00:00"],
    "entities": ["climate.lounge"],
    "actions": [
        {"service": "climate.set_temperature", "data": {"temperature": 18}},
        {"service": "climate.set_temperature", "data": {"temperature": 23}},
        {"service": "climate.set_temperature", "data": {"temperature": 16}},
    ],
}


def test_normalize_sorts_merges_and_adds_midnight():
    day = normalize_day(
        [{"start": "17:30", "temp": 23}, {"start": "08:00", "temp": 18}, {"start": "09:00", "temp": 18}],
        16,
    )
    assert day == [
        {"start": "00:00", "temp": 16.0},
        {"start": "08:00", "temp": 18.0},
        {"start": "17:30", "temp": 23.0},
    ]


def test_normalize_rounds_to_half_degree_and_rejects_bad():
    assert normalize_day([{"start": "00:00", "temp": 21.26}], 20)[0]["temp"] == 21.5
    with pytest.raises(PlanError):
        normalize_day([{"start": "25:00", "temp": 20}], 20)
    with pytest.raises(PlanError):
        normalize_day([{"start": "00:00", "temp": 99}], 20)


def test_scheduler_import_and_lookup():
    days, entities, blocks = blocks_from_scheduler(LOUNGE_WERKTAGS)
    assert days == ["mon", "tue", "wed", "thu", "fri"]
    assert entities == ["climate.lounge"]
    plan = normalize_plan({d: blocks for d in days}, 20)
    monday = datetime(2026, 10, 12, 18, 0)  # Montag
    assert temp_at(plan, monday) == 23
    assert block_at(plan, monday)[1:] == (17 * 60 + 30, 23 * 60)
    assert temp_at(plan, datetime(2026, 10, 12, 23, 30)) == 16
    # Samstag ohne Import -> Fallback
    assert temp_at(plan, datetime(2026, 10, 17, 12, 0)) == 20


def test_next_change_crosses_midnight():
    plan = normalize_plan({d: [{"start": "00:00", "temp": 18}, {"start": "06:00", "temp": 21}] for d in
                           ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]})
    # 22:00 am Montag -> aktueller Wert 21, nächster Wechsel Di 00:00 auf 18
    assert next_change(plan, datetime(2026, 10, 12, 22, 0)) == (datetime(2026, 10, 13, 0, 0), 18.0)


def test_next_change_none_for_constant_plan():
    plan = normalize_plan({}, 20)
    assert next_change(plan, datetime(2026, 10, 12, 12, 0)) is None


FBH = {
    "weekdays": ["daily"],
    "timeslots": ["00:00:00 - 06:00:00", "06:00:00 - 09:00:00", "09:00:00 - 19:00:00",
                  "19:00:00 - 21:00:00", "21:00:00 - 00:00:00"],
    "entities": ["switch.fussbodenheizung"],
    "actions": [{"service": "switch.turn_off"}, {"service": "switch.turn_on"}, {"service": "switch.turn_off"},
                {"service": "switch.turn_on"}, {"service": "switch.turn_off"}],
}


def test_switch_plan_from_scheduler():
    days, entities, blocks = blocks_from_scheduler(FBH)
    assert entities == ["switch.fussbodenheizung"] and len(days) == 7
    plan = normalize_plan({d: blocks for d in days}, False, kind="switch")
    assert plan["mon"] == [
        {"start": "00:00", "on": False}, {"start": "06:00", "on": True}, {"start": "09:00", "on": False},
        {"start": "19:00", "on": True}, {"start": "21:00", "on": False},
    ]
    assert temp_at(plan, datetime(2026, 10, 12, 7, 0)) is True
    assert next_change(plan, datetime(2026, 10, 12, 7, 0)) == (datetime(2026, 10, 12, 9, 0), False)
