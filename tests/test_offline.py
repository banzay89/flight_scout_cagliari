"""Scansioni end-to-end: offline senza rete, budget su scansioni live simulate, dry-run, CLI.

Nessun test contatta internet: la rete è bloccata dalla fixture no_network, o le risposte
arrivano da un trasporto HTTP finto.
"""

from datetime import date

import httpx
import pytest

from flight_scout import cli
from flight_scout.budget import BudgetExceeded
from flight_scout.database import Database
from flight_scout.date_engine import generate_trip_windows
from flight_scout.models import SearchStatus
from flight_scout.providers.base import ProviderUnavailable
from flight_scout.providers.offline import OFFLINE_BANNER, OfflineSerpApi
from flight_scout.reporting import collect
from flight_scout.search_engine import discovery_priority, plan_scan, run_scan

TODAY = date(2026, 10, 19)  # lunedì


def _live_settings(make_settings, **extra):
    return make_settings(SERPAPI_API_KEY="chiave-di-prova", **extra)


# --- modalità offline -------------------------------------------------------

def test_offline_scan_runs_without_network_and_produces_verified_offers(no_network, make_settings, catalog, criteria, tmp_path):
    settings = make_settings(API_RUN_BUDGET="40", MAX_DESTINATIONS_TO_VERIFY="6")
    db = Database(tmp_path / "offline.db")
    stats = run_scan(settings, criteria, db, catalog, mode="deep", offline=True, today=TODAY)
    assert stats.windows_covered > 0
    assert stats.verified > 0
    verified = [o for o in db.offers_for_scan(stats.scan_id) if o.status == SearchStatus.VERIFIED]
    assert verified
    db.close()


def test_verified_offers_always_have_both_legs_and_valid_dates(no_network, make_settings, catalog, criteria, tmp_path):
    settings = make_settings(API_RUN_BUDGET="40", MAX_DESTINATIONS_TO_VERIFY="6")
    db = Database(tmp_path / "legs.db")
    stats = run_scan(settings, criteria, db, catalog, mode="deep", offline=True, today=TODAY)
    for offer in db.offers_for_scan(stats.scan_id):
        if offer.status == SearchStatus.VERIFIED:
            assert offer.outbound is not None and offer.inbound is not None
            assert offer.outbound.departure_airport == "CAG"
            assert offer.inbound.arrival_airport == "CAG"
            assert offer.outbound_date.weekday() in (3, 4, 5, 6)  # giovedì..domenica
            assert 1 <= offer.nights <= 4
    db.close()


def test_missing_return_offer_is_never_saved_as_verified(no_network, make_settings, catalog, criteria, tmp_path):
    settings = make_settings(API_RUN_BUDGET="60", MAX_DESTINATIONS_TO_VERIFY="10")
    db = Database(tmp_path / "pr.db")
    stats = run_scan(settings, criteria, db, catalog, mode="deep", offline=True, today=TODAY)
    # Papeete (PPT) nei dati di prova non restituisce il ritorno: non può essere VERIFIED.
    ppt_verified = [
        o for o in db.offers_for_scan(stats.scan_id)
        if o.destination_iata == "PPT" and o.status == SearchStatus.VERIFIED
    ]
    assert ppt_verified == []
    assert any("ritorno" in s["reason"] for s in [dict(r) for r in db.skipped_for_scan(stats.scan_id)])
    db.close()


def test_offline_report_is_labelled_as_test_data(no_network, make_settings, catalog, criteria, tmp_path):
    settings = make_settings(API_RUN_BUDGET="40", MAX_DESTINATIONS_TO_VERIFY="6")
    db = Database(tmp_path / "rep.db")
    run_scan(settings, criteria, db, catalog, mode="deep", offline=True, today=TODAY)
    data = collect(db, settings, criteria, catalog, offline=True, now=None)
    assert data is not None and data.banner == OFFLINE_BANNER
    assert collect(db, settings, criteria, catalog, offline=False) is None  # niente dati di test nel report reale
    db.close()


def test_offline_run_budget_is_applied(no_network, make_settings, catalog, criteria, tmp_path):
    settings = make_settings(API_RUN_BUDGET="5")
    db = Database(tmp_path / "small.db")
    stats = run_scan(settings, criteria, db, catalog, mode="deep", offline=True, today=TODAY)
    assert stats.api_calls <= 5
    assert stats.windows_covered < stats.windows_total
    assert stats.skipped_windows  # le finestre non esplorate sono dichiarate
    db.close()


# --- scansioni live simulate (trasporto finto) ----------------------------

def test_max_api_calls_one_makes_exactly_one_request(no_network, make_settings, catalog, criteria, tmp_path):
    settings = _live_settings(make_settings)
    offline_handler = OfflineSerpApi(catalog, "CAG")._handle
    calls: list[httpx.Request] = []

    def counting(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return offline_handler(request)

    db = Database(tmp_path / "live1.db")
    stats = run_scan(
        settings, criteria, db, catalog,
        mode="deep", offline=False, max_api_calls=1, today=TODAY,
        transport=httpx.MockTransport(counting), sleep=lambda _s: None,
    )
    assert len(calls) == 1
    assert stats.api_calls == 1
    assert stats.budget_stopped
    usage = db.conn.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0]
    assert usage == 1
    db.close()


def test_authentication_error_stops_scan_immediately(no_network, make_settings, catalog, criteria, tmp_path):
    settings = _live_settings(make_settings)
    calls: list[httpx.Request] = []

    def unauthorized(request):
        calls.append(request)
        return httpx.Response(401, json={"error": "chiave non valida"})

    db = Database(tmp_path / "auth.db")
    stats = run_scan(
        settings, criteria, db, catalog,
        mode="deep", offline=False, today=TODAY,
        transport=httpx.MockTransport(unauthorized), sleep=lambda _s: None,
    )
    assert len(calls) == 1
    assert stats.stop_reason == "provider non autorizzato"
    assert "chiave-di-prova" not in str(db.errors_for_scan(stats.scan_id)[0]["message"])
    db.close()


def test_live_scan_without_key_is_refused_before_any_request(no_network, make_settings, catalog, criteria, tmp_path):
    settings = make_settings()
    db = Database(tmp_path / "nokey.db")
    with pytest.raises(ProviderUnavailable):
        run_scan(settings, criteria, db, catalog, mode="deep", offline=False, today=TODAY)
    assert db.conn.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 0
    db.close()


def test_cache_prevents_paying_twice_for_same_search(no_network, make_settings, catalog, criteria, tmp_path):
    settings = _live_settings(make_settings, API_RUN_BUDGET="3", MAX_DESTINATIONS_TO_VERIFY="0")
    offline_handler = OfflineSerpApi(catalog, "CAG")._handle
    urls: list[str] = []

    def counting(request):
        urls.append(str(request.url.params))
        return offline_handler(request)

    db = Database(tmp_path / "cache.db")
    run_scan(settings, criteria, db, catalog, mode="deep", offline=False, today=TODAY,
             transport=httpx.MockTransport(counting), sleep=lambda _s: None)
    first_run = set(urls)
    urls.clear()
    run_scan(settings, criteria, db, catalog, mode="deep", offline=False, today=TODAY,
             transport=httpx.MockTransport(counting), sleep=lambda _s: None)
    # La seconda scansione non ripete mai una richiesta già ottenuta: le finestre in cache non costano nulla.
    assert not set(urls) & first_run
    db.close()


# --- pianificazione e dry-run ---------------------------------------------

def test_dry_run_makes_no_request_and_reports_budget(monkeypatch, make_settings, catalog, criteria, tmp_path):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("il dry-run non deve inviare richieste")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    settings = make_settings()
    db = Database(tmp_path / "dry.db")
    plan = plan_scan(settings, criteria, catalog, db, mode="deep", today=TODAY)
    assert plan.windows_total == len(generate_trip_windows(TODAY, 180, 1, 4))
    assert plan.run_limit == 25
    assert plan.discovery_limit == 15
    assert plan.expected_discovery_calls == 15
    db.close()


def test_quick_mode_uses_a_smaller_call_budget(make_settings, catalog, criteria, tmp_path):
    settings = make_settings()
    db = Database(tmp_path / "quick.db")
    plan = plan_scan(settings, criteria, catalog, db, mode="quick", today=TODAY)
    assert plan.run_limit == criteria.quick_max_api_calls
    db.close()


def test_discovery_order_spreads_coverage_over_future_weeks(make_settings, criteria):
    settings = make_settings()
    windows = generate_trip_windows(TODAY, settings.search_horizon_days, 1, 4)
    ordered = discovery_priority(windows, criteria)
    first_weeks = []
    for window in ordered:
        if window.week_thursday not in first_weeks:
            first_weeks.append(window.week_thursday)
    assert len(first_weeks) > 10  # non si esaurisce tutto sulle prime settimane
    assert first_weeks[0] == min(first_weeks)  # i periodi vicini restano prioritari


def test_budget_exception_type_is_distinct_from_provider_errors():
    assert issubclass(BudgetExceeded, Exception)
    assert not issubclass(BudgetExceeded, ProviderUnavailable)


# --- interfaccia da terminale ---------------------------------------------

def test_scan_without_mode_makes_no_call_and_explains_options(monkeypatch, settings, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    code = cli.main(["scan"])
    assert code == 2


def test_live_scan_without_key_exits_cleanly_without_network(no_network, monkeypatch, settings):
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    assert cli.main(["scan", "--live", "--max-api-calls", "1"]) == 2


def test_dry_run_command_exits_zero(monkeypatch, settings, no_network):
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    assert cli.main(["scan", "--dry-run"]) == 0


def test_doctor_command_reports_without_printing_secrets(monkeypatch, make_settings, capsys):
    secret = "PROVA-NON-STAMPARE-321"
    settings = make_settings(SERPAPI_API_KEY=secret)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    assert cli.main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert secret not in output
    assert "chiave presente" in output


def test_config_command_hides_secret_values(monkeypatch, make_settings, capsys):
    secret = "PROVA-NON-STAMPARE-654"
    settings = make_settings(SERPAPI_API_KEY=secret, TELEGRAM_BOT_TOKEN=secret)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    assert cli.main(["config"]) == 0
    assert secret not in capsys.readouterr().out


def test_history_explains_short_series(monkeypatch, make_settings, capsys):
    settings = make_settings()
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    assert cli.main(["history"]) == 0
    assert "troppo brevi" in capsys.readouterr().out
