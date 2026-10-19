"""Configurazione: valori predefiniti, validazione, priorità, segnalazioni senza segreti."""

import pytest

from flight_scout.config import ConfigError, diagnose, load_settings, redact

SECRET = "CHIAVE-SEGRETA-DI-PROVA-555"


def test_defaults_match_the_specification(make_settings):
    s = make_settings()
    assert s.origin_airport == "CAG"
    assert s.currency == "EUR"
    assert s.max_standard_price_eur == 100
    assert s.exotic_max_price_eur is None
    assert (s.min_nights, s.max_nights) == (1, 4)
    assert s.timezone == "Europe/Rome"
    assert s.api_run_budget == 25 and s.api_monthly_budget == 200
    assert s.max_destinations_to_verify == 10 and s.cache_ttl_hours == 24


def test_empty_values_mean_default_or_none(make_settings):
    s = make_settings(EXOTIC_MAX_PRICE_EUR="", SERPAPI_API_KEY="")
    assert s.exotic_max_price_eur is None
    assert s.serpapi_api_key is None


def test_environment_overrides_file_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("MAX_STANDARD_PRICE_EUR=90\n", encoding="utf-8")
    s = load_settings(env_file=env_file, environ={"MAX_STANDARD_PRICE_EUR": "120"})
    assert s.max_standard_price_eur == 120


def test_invalid_nights_produce_friendly_message_without_secret_values(make_settings):
    with pytest.raises(ConfigError) as info:
        make_settings(MIN_NIGHTS="3", MAX_NIGHTS="2", SERPAPI_API_KEY=SECRET)
    message = str(info.value)
    assert "MAX_NIGHTS" in message
    assert SECRET not in message


def test_unknown_timezone_is_rejected(make_settings):
    with pytest.raises(ConfigError, match="TIMEZONE"):
        make_settings(TIMEZONE="Mars/Olympus")


def test_non_iata_origin_is_rejected(make_settings):
    with pytest.raises(ConfigError, match="ORIGIN_AIRPORT"):
        make_settings(ORIGIN_AIRPORT="CAGLIARI")


def test_missing_serpapi_key_is_a_warning_not_an_error(make_settings):
    checks = diagnose(make_settings())
    assert any(c.level == "warn" and "SERPAPI_API_KEY" in c.message for c in checks)
    assert not any(c.level == "error" for c in checks)


def test_monthly_budget_above_free_plan_is_flagged(make_settings):
    checks = diagnose(make_settings(API_MONTHLY_BUDGET="500"))
    assert any("250" in c.message for c in checks)


def test_redact_hides_secret_values(tmp_path):
    s = load_settings(env_file=tmp_path / "nope.env", environ={"SERPAPI_API_KEY": SECRET})
    assert SECRET not in redact(f"chiave={SECRET}", s)
    assert "***" in redact(f"chiave={SECRET}", s)
