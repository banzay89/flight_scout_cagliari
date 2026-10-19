"""Motore temporale: finestre di viaggio, orari, fusi orari, cambio dell'ora, anno nuovo, anni bisestili."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from flight_scout.date_engine import (
    TimeRules,
    generate_trip_windows,
    localize,
    outbound_departure_problems,
    return_deadline,
    round_trip_problems,
)

ROME = ZoneInfo("Europe/Rome")
MADRID = ZoneInfo("Europe/Madrid")
UTC = timezone.utc
RULES = TimeRules(
    timezone="Europe/Rome",
    thursday_earliest=time(17, 0),
    monday_latest=time(23, 0),
    min_nights=1,
    max_nights=4,
)


def pairs(start: date, horizon: int = 60, min_nights: int = 1, max_nights: int = 4) -> set[tuple[date, date]]:
    return {
        (w.outbound_date, w.return_date)
        for w in generate_trip_windows(start, horizon, min_nights, max_nights)
    }


# --- finestre di viaggio --------------------------------------------------

def test_spec_example_combinations_are_generated():
    # Settimana del 19 ottobre 2026 (lunedì): giovedì 22 ottobre, ecc.
    found = pairs(date(2026, 10, 19))
    thu, fri, sat, sun, mon = (date(2026, 10, d) for d in (22, 23, 24, 25, 26))
    expected = {
        (thu, fri), (thu, sat), (thu, sun), (thu, mon),
        (fri, sat), (fri, sun), (fri, mon),
        (sat, sun), (sat, mon),
        (sun, mon),
    }
    assert expected <= found


def test_outbound_days_are_thursday_to_sunday_only():
    for window in generate_trip_windows(date(2026, 10, 19), 120, 1, 4):
        assert window.outbound_date.weekday() in (3, 4, 5, 6), window


def test_return_never_after_monday_of_the_same_week():
    for window in generate_trip_windows(date(2026, 10, 19), 120, 1, 4):
        monday = window.week_thursday + timedelta(days=4)
        assert window.return_date <= monday
        assert window.return_date > window.outbound_date


def test_nights_respect_configured_minimum_and_maximum():
    windows = generate_trip_windows(date(2026, 10, 19), 120, 2, 3)
    assert windows
    assert all(2 <= w.nights <= 3 for w in windows)


def test_one_night_stays_are_included_by_default():
    assert (date(2026, 10, 25), date(2026, 10, 26)) in pairs(date(2026, 10, 19))  # domenica -> lunedì


def test_new_year_thursday_to_monday_is_generated():
    found = pairs(date(2026, 12, 28), horizon=20)
    assert (date(2026, 12, 31), date(2027, 1, 4)) in found  # giovedì 31 dic -> lunedì 4 gen
    assert (date(2027, 1, 1), date(2027, 1, 3)) in found  # venerdì 1 gen -> domenica 3 gen


def test_leap_year_february_29_is_not_a_trip_day_but_week_is_handled():
    # 29 febbraio 2028 è un martedì: nessuna finestra può partire o rientrare quel giorno.
    found = pairs(date(2028, 2, 20), horizon=30)
    assert (date(2028, 2, 24), date(2028, 2, 28)) in found  # giovedì -> lunedì, anno bisestile
    assert (date(2028, 2, 27), date(2028, 2, 28)) in found  # domenica -> lunedì
    assert all(out.day != 29 or out.month != 2 for out, _ in found)
    assert all(ret != date(2028, 2, 29) for _, ret in found)


def test_start_on_friday_excludes_thursday_already_past():
    windows = generate_trip_windows(date(2026, 10, 23), 30, 1, 4)  # venerdì
    assert windows
    assert min(w.outbound_date for w in windows) >= date(2026, 10, 23)


def test_outbound_dates_stay_inside_horizon():
    start = date(2026, 10, 19)
    windows = generate_trip_windows(start, 30, 1, 4)
    assert max(w.outbound_date for w in windows) <= start + timedelta(days=30)


# --- orari di partenza e rientro -----------------------------------------

def test_thursday_departure_before_17_is_rejected():
    assert outbound_departure_problems(datetime(2026, 10, 22, 16, 59, tzinfo=ROME), RULES)


def test_thursday_departure_at_17_is_accepted():
    assert outbound_departure_problems(datetime(2026, 10, 22, 17, 0, tzinfo=ROME), RULES) == []


def test_friday_morning_departure_is_accepted():
    assert outbound_departure_problems(datetime(2026, 10, 23, 7, 0, tzinfo=ROME), RULES) == []


def test_monday_departure_is_rejected():
    assert outbound_departure_problems(datetime(2026, 10, 26, 9, 0, tzinfo=ROME), RULES)


def test_return_deadline_is_monday_2300_in_cagliari_time_summer_before_dst_end():
    deadline = return_deadline(datetime(2026, 3, 26, 18, 0, tzinfo=ROME), RULES)
    assert deadline == datetime(2026, 3, 30, 23, 0, tzinfo=ROME)
    assert deadline.utcoffset() == timedelta(hours=2)  # CEST
    assert deadline.astimezone(UTC).hour == 21


def test_return_deadline_after_dst_end_uses_winter_time():
    deadline = return_deadline(datetime(2026, 10, 22, 18, 0, tzinfo=ROME), RULES)
    assert deadline == datetime(2026, 10, 26, 23, 0, tzinfo=ROME)
    assert deadline.utcoffset() == timedelta(hours=1)  # CET, dopo il 25 ottobre
    assert deadline.astimezone(UTC).hour == 22


# --- itinerari completi ---------------------------------------------------

def _valid_round_trip(**overrides):
    base = dict(
        outbound_departure=datetime(2026, 10, 22, 18, 40, tzinfo=ROME),
        outbound_arrival=datetime(2026, 10, 22, 20, 30, tzinfo=MADRID),
        return_departure=datetime(2026, 10, 26, 12, 0, tzinfo=MADRID),
        return_arrival=datetime(2026, 10, 26, 14, 30, tzinfo=ROME),
        rules=RULES,
    )
    base.update(overrides)
    return base


def test_valid_round_trip_has_no_problems():
    assert round_trip_problems(**_valid_round_trip()) == []


def test_return_arriving_after_monday_deadline_is_rejected():
    problems = round_trip_problems(
        **_valid_round_trip(return_arrival=datetime(2026, 10, 26, 23, 30, tzinfo=ROME))
    )
    assert any("23:00" in p for p in problems)


def test_return_arriving_on_tuesday_is_rejected():
    problems = round_trip_problems(
        **_valid_round_trip(
            return_departure=datetime(2026, 10, 26, 23, 0, tzinfo=MADRID),
            return_arrival=datetime(2026, 10, 27, 1, 30, tzinfo=ROME),
        )
    )
    assert problems


def test_return_departing_before_outbound_arrival_is_rejected():
    problems = round_trip_problems(
        **_valid_round_trip(
            outbound_arrival=datetime(2026, 10, 23, 12, 0, tzinfo=MADRID),
            return_departure=datetime(2026, 10, 23, 9, 0, tzinfo=MADRID),
            return_arrival=datetime(2026, 10, 23, 11, 0, tzinfo=ROME),
        )
    )
    assert any("prima dell'arrivo" in p for p in problems)


def test_thursday_1630_departure_is_rejected_in_round_trip():
    problems = round_trip_problems(
        **_valid_round_trip(outbound_departure=datetime(2026, 10, 22, 16, 30, tzinfo=ROME))
    )
    assert any("giovedì" in p for p in problems)


def test_too_many_nights_is_rejected():
    short_rules = TimeRules("Europe/Rome", time(17, 0), time(23, 0), min_nights=1, max_nights=2)
    problems = round_trip_problems(**_valid_round_trip(rules=short_rules))
    assert any("notti" in p for p in problems)


def test_spring_dst_change_arrival_just_before_and_after_deadline():
    # Settimana con cambio dell'ora: domenica 29 marzo 2026. Dopo il cambio, il lunedì è in CEST (+2).
    base = dict(
        outbound_departure=datetime(2026, 3, 26, 18, 0, tzinfo=ROME),
        outbound_arrival=datetime(2026, 3, 26, 20, 0, tzinfo=MADRID),
        return_departure=datetime(2026, 3, 30, 10, 0, tzinfo=MADRID),
        rules=RULES,
    )
    before = datetime(2026, 3, 30, 22, 59, tzinfo=ROME)  # CEST
    after = datetime(2026, 3, 30, 23, 1, tzinfo=ROME)
    assert round_trip_problems(**base, return_arrival=before) == []
    assert round_trip_problems(**base, return_arrival=after)


def test_leap_year_round_trip_is_valid():
    problems = round_trip_problems(
        outbound_departure=datetime(2028, 2, 24, 18, 0, tzinfo=ROME),
        outbound_arrival=datetime(2028, 2, 24, 20, 0, tzinfo=MADRID),
        return_departure=datetime(2028, 2, 28, 12, 0, tzinfo=MADRID),
        return_arrival=datetime(2028, 2, 28, 14, 0, tzinfo=ROME),
        rules=RULES,
    )
    assert problems == []


def test_localize_handles_missing_values_and_aware_datetimes():
    naive = datetime(2026, 10, 22, 17, 0)
    assert localize(None, "Europe/Rome") is None
    assert localize(naive, None) is None
    aware = localize(naive, "Europe/Rome")
    assert aware.utcoffset() == timedelta(hours=2)
    already = datetime(2026, 10, 22, 17, 0, tzinfo=UTC)
    assert localize(already, "Europe/Rome") is already


def test_ambiguous_dst_hour_does_not_raise():
    # 25 ottobre 2026, 02:30: ora ripetuta. Deve restare un datetime valido.
    value = localize(datetime(2026, 10, 25, 2, 30), "Europe/Rome")
    assert value.utcoffset() is not None
