"""Punteggi riproducibili, motivazioni leggibili e ordinamento."""

from datetime import date, datetime, timezone
from decimal import Decimal

from flight_scout.models import Offer, SearchStatus
from flight_scout.ranking import CATEGORY_BEST_BUDGET, CATEGORY_EXOTIC, preselection_score, score_offer, sort_key


def _offer(price: str, status: SearchStatus = SearchStatus.INDICATIVE, nights: int = 3, stops: int | None = 0) -> Offer:
    return Offer(
        provider="serpapi",
        destination_iata="BCN",
        outbound_date=date(2026, 10, 23),
        return_date=date(2026, 10, 23 + nights),
        price_eur=Decimal(price),
        status=status,
        observed_at=datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc),
        stops_hint=stops,
        duration_hint_minutes=150,
    )


def test_score_is_deterministic(criteria):
    first = score_offer(_offer("90"), 60, criteria)
    second = score_offer(_offer("90"), 60, criteria)
    assert first == second


def test_cheaper_offer_scores_higher_with_everything_else_equal(criteria):
    cheap = score_offer(_offer("80"), 60, criteria).score
    dear = score_offer(_offer("160"), 60, criteria).score
    assert cheap > dear


def test_verified_status_scores_higher_than_indicative(criteria):
    indicative = score_offer(_offer("90", SearchStatus.INDICATIVE), 60, criteria).score
    verified = score_offer(_offer("90", SearchStatus.VERIFIED), 60, criteria).score
    assert verified > indicative


def test_direct_flight_scores_higher_than_two_stops(criteria):
    direct = score_offer(_offer("90", stops=0), 60, criteria).score
    two_stops = score_offer(_offer("90", stops=2), 60, criteria).score
    assert direct > two_stops


def test_reasons_are_readable_and_mention_indicative_status(criteria):
    reasons = score_offer(_offer("90", SearchStatus.INDICATIVE), 60, criteria).reasons
    assert any("indicativo" in r for r in reasons)
    assert any("90.00 EUR" in r for r in reasons)


def test_budget_group_is_sorted_by_price_ascending():
    offers = [_offer("96"), _offer("72"), _offer("88")]
    ordered = sorted(offers, key=lambda o: sort_key(CATEGORY_BEST_BUDGET, o, 50, 10.0))
    assert [o.price_eur for o in ordered] == [Decimal("72"), Decimal("88"), Decimal("96")]


def test_exotic_group_is_sorted_by_interest_descending():
    low = (_offer("200"), 70)
    high = (_offer("900"), 90)
    ordered = sorted([low, high], key=lambda item: sort_key(CATEGORY_EXOTIC, item[0], item[1], 10.0))
    assert ordered[0][1] == 90


def test_preselection_prefers_cheap_and_interesting(criteria):
    cheap_boring = preselection_score(Decimal("60"), 30, criteria)
    cheap_interesting = preselection_score(Decimal("60"), 90, criteria)
    assert cheap_interesting > cheap_boring


def test_criteria_weights_sum_to_one(criteria):
    assert abs(sum(criteria.weights.values()) - 1.0) < 1e-9
