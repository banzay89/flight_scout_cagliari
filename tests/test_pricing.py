"""Prezzi A/R per persona e separazione tra budget, quasi economiche ed esotiche."""

from datetime import date, datetime, timezone
from decimal import Decimal

from flight_scout.models import Offer, SearchStatus
from flight_scout.providers.serpapi_explore import parse_explore
from flight_scout.date_engine import TripWindow
from flight_scout.ranking import (
    CATEGORY_BEST_BUDGET,
    CATEGORY_EXOTIC,
    CATEGORY_NEAR_BUDGET,
    classify,
)

WINDOW = TripWindow(date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 26))


def _offer(price: str, destination: str = "XYZ") -> Offer:
    return Offer(
        provider="serpapi",
        destination_iata=destination,
        outbound_date=WINDOW.outbound_date,
        return_date=WINDOW.return_date,
        price_eur=Decimal(price),
        status=SearchStatus.INDICATIVE,
        observed_at=datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc),
    )


def test_96_eur_ordinary_offer_is_in_budget_group(settings, criteria):
    assert classify(_offer("96"), interest=50, settings=settings, criteria=criteria) == [CATEGORY_BEST_BUDGET]


def test_120_eur_ordinary_offer_is_not_promoted_as_within_threshold(settings, criteria):
    categories = classify(_offer("120"), interest=50, settings=settings, criteria=criteria)
    assert CATEGORY_BEST_BUDGET not in categories
    assert categories == [CATEGORY_NEAR_BUDGET]


def test_180_eur_exotic_destination_is_only_exotic(settings, criteria):
    categories = classify(_offer("180"), interest=85, settings=settings, criteria=criteria)
    assert categories == [CATEGORY_EXOTIC]


def test_85_eur_exotic_destination_appears_in_both_budget_and_exotic(settings, criteria):
    categories = classify(_offer("85"), interest=85, settings=settings, criteria=criteria)
    assert CATEGORY_BEST_BUDGET in categories and CATEGORY_EXOTIC in categories


def test_exotic_price_cap_is_applied_when_configured(make_settings, criteria):
    capped = make_settings(EXOTIC_MAX_PRICE_EUR="400")
    assert classify(_offer("500"), interest=90, settings=capped, criteria=criteria) == []
    assert classify(_offer("350"), interest=90, settings=capped, criteria=criteria) == [CATEGORY_EXOTIC]


def test_empty_exotic_cap_means_no_arbitrary_limit(settings, criteria):
    assert settings.exotic_max_price_eur is None
    assert classify(_offer("2500"), interest=90, settings=settings, criteria=criteria) == [CATEGORY_EXOTIC]


def test_exploration_prices_are_per_passenger():
    data = {"destinations": [{"destination_airport": {"code": "BCN"}, "flight_price": 150}]}
    results = parse_explore(data, WINDOW, passengers=2)
    assert results[0].price_eur == Decimal("75")


def test_missing_or_non_numeric_price_is_none_not_zero():
    data = {
        "destinations": [
            {"destination_airport": {"code": "BCN"}},
            {"destination_airport": {"code": "LIS"}, "flight_price": "n/d"},
        ]
    }
    results = parse_explore(data, WINDOW, passengers=1)
    assert [r.price_eur for r in results] == [None, None]
