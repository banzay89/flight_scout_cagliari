"""Ricerca esplorativa (Google Travel Explore via SerpApi): destinazioni e prezzi INDICATIVI.

Un risultato esplorativo non è una tariffa verificata: serve a scoprire candidati.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from flight_scout.date_engine import TripWindow
from flight_scout.providers.base import ExploreResult
from flight_scout.providers.serpapi_client import SerpApiClient


class SerpApiExplore:
    ENGINE = "google_travel_explore"

    def __init__(self, client: SerpApiClient, origin: str, currency: str, passengers: int):
        self.client = client
        self.origin = origin
        self.currency = currency
        self.passengers = passengers

    def params(self, window: TripWindow) -> dict[str, Any]:
        return {
            "engine": self.ENGINE,
            "departure_id": self.origin,
            "outbound_date": window.outbound_date.isoformat(),
            "return_date": window.return_date.isoformat(),
            "currency": self.currency,
            "hl": "it",
            "gl": "it",
        }

    def explore(self, window: TripWindow) -> list[ExploreResult]:
        data = self.client.search(f"explore:{window.key}", self.params(window))
        return parse_explore(data, window, self.passengers)


def parse_explore(data: dict, window: TripWindow, passengers: int) -> list[ExploreResult]:
    """Converte la risposta esplorativa. Le voci incomplete vengono saltate, non inventate."""
    results: list[ExploreResult] = []
    for item in data.get("destinations") or []:
        if not isinstance(item, dict):
            continue
        airport = item.get("destination_airport") or {}
        code = airport.get("code") if isinstance(airport, dict) else None
        if not code or not isinstance(code, str) or len(code) != 3:
            continue
        price = _to_decimal(item.get("flight_price"))
        if price is not None:
            price = price / passengers
        results.append(
            ExploreResult(
                destination_iata=code.upper(),
                destination_name=item.get("name"),
                destination_country=item.get("country"),
                price_eur=price,
                duration_minutes=_to_int(item.get("flight_duration")),
                stops=_to_int(item.get("number_of_stops")),
                window=window,
            )
        )
    return results


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
