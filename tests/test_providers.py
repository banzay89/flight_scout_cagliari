"""Provider: parsing di risposte (anche incomplete), errori HTTP, verifica A/R, budget e cache.

Le risposte sono costruite a mano con la struttura di SerpApi (campi usati dal programma).
Non rappresentano prezzi reali.
"""

from datetime import date, time
from decimal import Decimal

import httpx
import pytest

from flight_scout.budget import ApiBudget, BudgetExceeded, NullBudget
from flight_scout.cache import ResponseCache
from flight_scout.database import Database, month_start
from flight_scout.date_engine import TimeRules, TripWindow
from flight_scout.destination_engine import DestinationCatalog
from flight_scout.models import SearchStatus
from flight_scout.providers.base import ProviderAuthError, ProviderError
from flight_scout.providers.serpapi_client import SerpApiClient
from flight_scout.providers.serpapi_explore import parse_explore
from flight_scout.providers.serpapi_flights import SerpApiFlights

SECRET = "SEGRETO-DI-PROVA-987"
# Settimana con giovedì 22 ottobre 2026 (partenza) e lunedì 26 ottobre 2026 (rientro).
WINDOW = TripWindow(date(2026, 10, 22), date(2026, 10, 22), date(2026, 10, 26))
RULES = TimeRules("Europe/Rome", time(17, 0), time(23, 0), min_nights=1, max_nights=4)


def make_client(handler, *, budget=None, cache=None, sleeps=None):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps = sleeps if sleeps is not None else []
    client = SerpApiClient(
        api_key=SECRET,
        http=http,
        budget=budget or NullBudget(run_limit=100),
        cache=cache,
        sleep=sleeps.append,
        pause_seconds=0,
    )
    return client, sleeps


def status_handler(code: int, body=None, text: str | None = None):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if text is not None:
            return httpx.Response(code, text=text)
        return httpx.Response(code, json=body or {})

    handler.calls = calls
    return handler


# --- errori HTTP ------------------------------------------------------------

@pytest.mark.parametrize("code", [401, 403])
def test_auth_errors_stop_provider_without_leaking_key(code):
    client, _ = make_client(status_handler(code))
    with pytest.raises(ProviderAuthError) as info:
        client.search("t", {"engine": "google_flights"})
    assert SECRET not in str(info.value)


def test_not_found_is_a_normal_provider_error():
    client, _ = make_client(status_handler(404))
    with pytest.raises(ProviderError) as info:
        client.search("t", {"engine": "x"})
    assert not isinstance(info.value, ProviderAuthError)


def test_rate_limit_is_retried_with_backoff_then_reported():
    handler = status_handler(429)
    client, sleeps = make_client(handler)
    with pytest.raises(ProviderError, match="429"):
        client.search("t", {"engine": "x"})
    assert len(handler.calls) == SerpApiClient.MAX_ATTEMPTS
    assert sleeps == [1, 2, 4]  # backoff esponenziale


def test_server_error_then_success_is_recovered():
    responses = [httpx.Response(500, text="boom"), httpx.Response(200, json={"ok": True})]

    def handler(request):
        return responses.pop(0)

    client, _ = make_client(handler)
    assert client.search("t", {"engine": "x"}) == {"ok": True}


def test_network_error_is_reported_without_url_or_key():
    def handler(request):
        raise httpx.ConnectError(f"fallito verso {request.url}")

    client, _ = make_client(handler)
    with pytest.raises(ProviderError) as info:
        client.search("t", {"engine": "x"})
    assert SECRET not in str(info.value)


def test_malformed_json_is_a_provider_error():
    client, _ = make_client(status_handler(200, text="<html>non json</html>"))
    with pytest.raises(ProviderError, match="JSON"):
        client.search("t", {"engine": "x"})


def test_no_results_message_is_an_empty_answer_not_an_error():
    client, _ = make_client(status_handler(200, body={"error": "Google hasn't returned any results for this query."}))
    assert client.search("t", {"engine": "x"}) == {}


def test_other_provider_errors_are_reported_briefly():
    client, _ = make_client(status_handler(200, body={"error": "Quota non disponibile"}))
    with pytest.raises(ProviderError, match="Quota"):
        client.search("t", {"engine": "x"})


# --- budget e cache ---------------------------------------------------------

def test_budget_stops_calls_before_they_are_sent(tmp_path):
    db = Database(tmp_path / "b.db")
    budget = ApiBudget(db, "serpapi", run_limit=2, monthly_limit=200, hourly_limit=50, scan_id=None)
    handler = status_handler(200, body={"ok": True})
    client, _ = make_client(handler, budget=budget)
    client.search("a", {"n": 1})
    client.search("b", {"n": 2})
    with pytest.raises(BudgetExceeded):
        client.search("c", {"n": 3})
    assert len(handler.calls) == 2  # la terza chiamata non è mai partita
    assert db.usage_in_month("serpapi", _month_start()) == 2


def test_monthly_budget_is_enforced_across_runs(tmp_path):
    db = Database(tmp_path / "m.db")
    first = ApiBudget(db, "serpapi", run_limit=5, monthly_limit=2, hourly_limit=50, scan_id=None)
    client, _ = make_client(status_handler(200, body={}), budget=first)
    client.search("a", {"n": 1})
    client.search("b", {"n": 2})
    second = ApiBudget(db, "serpapi", run_limit=5, monthly_limit=2, hourly_limit=50, scan_id=None)
    with pytest.raises(BudgetExceeded, match="mensile"):
        second.check()


def test_hourly_limit_is_enforced(tmp_path):
    db = Database(tmp_path / "h.db")
    budget = ApiBudget(db, "serpapi", run_limit=5, monthly_limit=200, hourly_limit=1, scan_id=None)
    budget.consume("a")
    with pytest.raises(BudgetExceeded, match="orario"):
        budget.check()


def test_cached_response_does_not_consume_budget_or_network(tmp_path):
    db = Database(tmp_path / "c.db")
    cache = ResponseCache(db, ttl_hours=24)
    budget = ApiBudget(db, "serpapi", run_limit=10, monthly_limit=200, hourly_limit=50, scan_id=None)
    handler = status_handler(200, body={"same": True})
    client, _ = make_client(handler, budget=budget, cache=cache)
    client.search("explore:x", {"engine": "e", "departure_id": "CAG"})
    client.search("explore:x", {"engine": "e", "departure_id": "CAG"})
    assert len(handler.calls) == 1
    assert budget.spent == 1


def test_expired_cache_entry_is_ignored(tmp_path):
    db = Database(tmp_path / "e.db")
    cache = ResponseCache(db, ttl_hours=24)
    cache.set("k", {"old": True})
    db.conn.execute("UPDATE cache SET created_at = '2000-01-01T00:00:00+00:00' WHERE cache_key = 'k'")
    db.conn.commit()
    assert cache.get("k") is None


def test_api_key_is_never_stored_in_cache_key(tmp_path):
    db = Database(tmp_path / "k.db")
    cache = ResponseCache(db, ttl_hours=24)
    client, _ = make_client(status_handler(200, body={"x": 1}), cache=cache)
    key = client.key_for("label", {"engine": "e"})
    assert SECRET not in key


def _month_start():
    return month_start()


# --- parsing esplorazione ----------------------------------------------------

def test_explore_parser_skips_incomplete_items_and_keeps_valid_ones():
    data = {
        "destinations": [
            {"destination_airport": {"code": "bcn"}, "name": "Barcellona", "flight_price": 99, "number_of_stops": 0},
            {"name": "senza codice", "flight_price": 50},
            {"destination_airport": {"code": "TOOLONG"}, "flight_price": 10},
            "non un oggetto",
            {"destination_airport": {"code": "LIS"}, "flight_price": True},
        ]
    }
    results = parse_explore(data, WINDOW, passengers=1)
    assert [r.destination_iata for r in results] == ["BCN", "LIS"]
    assert results[0].price_eur == Decimal("99")
    assert results[1].price_eur is None  # booleani non sono prezzi


def test_explore_parser_handles_missing_destinations_key():
    assert parse_explore({}, WINDOW, passengers=1) == []


# --- verifica A/R ------------------------------------------------------------

def _leg(dep_code, dep_time, arr_code, arr_time, duration, number):
    return {
        "departure_airport": {"id": dep_code, "time": dep_time},
        "arrival_airport": {"id": arr_code, "time": arr_time},
        "duration": duration,
        "airline": "Test Air",
        "flight_number": number,
    }


def _flights_client(first_body, second_body):
    calls = []

    def handler(request: httpx.Request):
        calls.append(dict(request.url.params))
        if "departure_token" in request.url.params:
            return httpx.Response(200, json=second_body)
        return httpx.Response(200, json=first_body)

    client, _ = make_client(handler)
    return client, calls


def _service(client, catalog=None):
    catalog = catalog or DestinationCatalog.load()
    return SerpApiFlights(
        client,
        catalog,
        RULES,
        origin="CAG",
        currency="EUR",
        passengers=1,
        travel_class=1,
    )


OUT_DIRECT = {
    "flights": [_leg("CAG", "2026-10-22 18:40", "BCN", "2026-10-22 20:30", 110, "TS1")],
    "total_duration": 110,
    "price": 120,
    "departure_token": "TOKEN-OUT",
    "extensions": ["Checked bag fee: 35 EUR (dati di prova)"],
}
RET_DIRECT = {
    "flights": [_leg("BCN", "2026-10-26 12:00", "CAG", "2026-10-26 14:10", 130, "TS2")],
    "total_duration": 130,
    "price": 120,
}


def test_complete_round_trip_is_verified():
    client, calls = _flights_client({"best_flights": [OUT_DIRECT]}, {"best_flights": [RET_DIRECT]})
    result = _service(client).verify("BCN", WINDOW, destination_name="Barcellona", destination_country="Spagna")
    assert len(result.offers) == 1
    offer = result.offers[0]
    assert offer.status == SearchStatus.VERIFIED
    assert offer.is_complete
    assert offer.total_stops == 0
    assert offer.price_eur == Decimal("120")
    assert "Checked bag" in offer.baggage_info
    assert len(calls) == 2  # andata + ritorno con token


def test_one_stop_outbound_counts_stops_and_layover():
    out = {
        "flights": [
            _leg("CAG", "2026-10-22 18:40", "FCO", "2026-10-22 19:50", 70, "TS10"),
            _leg("FCO", "2026-10-22 21:20", "BCN", "2026-10-22 23:30", 130, "TS11"),
        ],
        "layovers": [{"id": "FCO", "duration": 90}],
        "total_duration": 290,
        "price": 150,
        "departure_token": "TOKEN-1STOP",
    }
    client, _ = _flights_client({"best_flights": [out]}, {"best_flights": [RET_DIRECT]})
    offer = _service(client).verify("BCN", WINDOW).offers[0]
    assert offer.outbound.stops == 1
    assert offer.outbound.layover_airports == ["FCO"]
    assert offer.outbound.layover_minutes() == [90]
    assert offer.total_stops == 1


def test_missing_return_segment_is_never_reported_as_complete():
    client, _ = _flights_client({"best_flights": [OUT_DIRECT]}, {"best_flights": []})
    result = _service(client).verify("BCN", WINDOW)
    assert result.offers == []
    assert any("ritorno" in reason for reason in result.rejected)


def test_return_departing_before_outbound_arrival_is_rejected():
    # L'andata arriva a Barcellona alle 20:30 del 22 ottobre; il ritorno parte alle 20:00 dello stesso giorno.
    bad_return = {
        "flights": [_leg("BCN", "2026-10-22 20:00", "CAG", "2026-10-22 22:10", 130, "TSX")],
        "price": 90,
    }
    client, _ = _flights_client({"best_flights": [OUT_DIRECT]}, {"best_flights": [bad_return]})
    result = _service(client).verify("BCN", WINDOW)
    assert result.offers == []
    assert result.rejected


def test_return_arriving_after_monday_deadline_is_rejected():
    late_return = {
        "flights": [_leg("BCN", "2026-10-26 21:30", "CAG", "2026-10-27 00:10", 130, "TSL")],
        "price": 90,
    }
    client, _ = _flights_client({"best_flights": [OUT_DIRECT]}, {"best_flights": [late_return]})
    result = _service(client).verify("BCN", WINDOW)
    assert result.offers == []
    assert any("lunedì" in reason or "23:00" in reason for reason in result.rejected)


def test_unknown_destination_timezone_keeps_offer_indicative():
    out = {**OUT_DIRECT, "flights": [_leg("CAG", "2026-10-22 18:40", "XYZ", "2026-10-22 20:30", 110, "TS1")]}
    ret = {**RET_DIRECT, "flights": [_leg("XYZ", "2026-10-26 12:00", "CAG", "2026-10-26 14:10", 130, "TS2")]}
    client, _ = _flights_client({"best_flights": [out]}, {"best_flights": [ret]})
    offer = _service(client).verify("XYZ", WINDOW).offers[0]
    assert offer.status == SearchStatus.INDICATIVE
    assert offer.is_complete  # le tratte ci sono, ma le coincidenze non sono verificabili
    assert any("fuso" in note for note in offer.notes)


def test_incomplete_flight_item_is_skipped_not_invented():
    broken = {"flights": [{"departure_airport": {"id": "CAG"}}], "price": 50, "departure_token": "T"}
    client, _ = _flights_client({"best_flights": [broken]}, {"best_flights": [RET_DIRECT]})
    result = _service(client).verify("BCN", WINDOW)
    assert result.offers == []
    assert result.rejected  # nessuna opzione di andata valida
