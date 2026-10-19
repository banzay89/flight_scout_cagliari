"""Verifica di un'offerta A/R (Google Flights via SerpApi).

Flusso in due chiamate, come previsto dall'API di Google Flights:
1. ricerca dell'andata per le date scelte: restituisce le opzioni con un "departure_token";
2. ricerca del ritorno usando il token: restituisce i voli di ritorno e il prezzo totale.

Un'offerta è VERIFIED solo se ha entrambe le tratte e supera i controlli orari.
Se manca il ritorno, o il fuso orario della destinazione è ignoto, l'offerta resta INDICATIVE.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from flight_scout.date_engine import TimeRules, TripWindow, localize, round_trip_problems
from flight_scout.destination_engine import DestinationCatalog
from flight_scout.models import Leg, Offer, SearchStatus, Segment
from flight_scout.providers.base import VerifyResult
from flight_scout.providers.serpapi_client import SerpApiClient

LOCAL_TIME_FORMAT = "%Y-%m-%d %H:%M"


class SerpApiFlights:
    ENGINE = "google_flights"

    def __init__(
        self,
        client: SerpApiClient,
        catalog: DestinationCatalog,
        rules: TimeRules,
        origin: str,
        currency: str,
        passengers: int,
        travel_class: int,
    ):
        self.client = client
        self.catalog = catalog
        self.rules = rules
        self.origin = origin
        self.currency = currency
        self.passengers = passengers
        self.travel_class = travel_class

    def base_params(self, destination: str, window: TripWindow) -> dict[str, Any]:
        return {
            "engine": self.ENGINE,
            "departure_id": self.origin,
            "arrival_id": destination,
            "outbound_date": window.outbound_date.isoformat(),
            "return_date": window.return_date.isoformat(),
            "type": 1,  # andata e ritorno
            "travel_class": self.travel_class,
            "adults": self.passengers,
            "currency": self.currency,
            "hl": "it",
            "gl": "it",
        }

    def verify(
        self,
        destination: str,
        window: TripWindow,
        destination_name: str | None = None,
        destination_country: str | None = None,
        observed_at: datetime | None = None,
    ) -> VerifyResult:
        observed_at = observed_at or datetime.now(timezone.utc)
        result = VerifyResult()
        base = self.base_params(destination, window)

        first = self.client.search(f"flights-out:{destination}:{window.key}", base)
        options = [option for option in parse_options(first, self.catalog) if option.token]
        if not options:
            result.rejected.append(f"{destination} {window.key}: nessuna opzione di andata con token di ritorno")
            return result
        outbound_option = options[0]

        second = self.client.search(
            f"flights-ret:{destination}:{window.key}",
            {**base, "departure_token": outbound_option.token},
        )
        returns = [option for option in parse_options(second, self.catalog) if option.price is not None]
        source_url = _source_url(first) or _source_url(second)
        if not returns:
            # Senza il segmento di ritorno non si può costruire un A/R: l'offerta resta solo indicativa.
            result.rejected.append(f"{destination} {window.key}: segmento di ritorno non restituito dal provider")
            return result

        return_option = min(returns, key=lambda option: option.price)
        price = return_option.price / self.passengers
        notes: list[str] = []
        outbound_leg, inbound_leg = outbound_option.leg, return_option.leg

        status = SearchStatus.VERIFIED
        problems = _timing_problems(outbound_leg, inbound_leg, self.origin, self.catalog, self.rules)
        if problems is None:
            status = SearchStatus.INDICATIVE
            notes.append("fuso orario della destinazione non disponibile: coincidenze non verificabili")
        elif problems:
            result.rejected.append(f"{destination} {window.key}: " + "; ".join(problems))
            return result

        offer = Offer(
            provider="serpapi",
            destination_iata=destination,
            destination_name=destination_name,
            destination_country=destination_country,
            outbound_date=window.outbound_date,
            return_date=window.return_date,
            price_eur=price,
            currency=self.currency,
            status=status,
            outbound=outbound_leg,
            inbound=inbound_leg,
            observed_at=observed_at,
            source_url=source_url,
            baggage_info=_baggage_info(outbound_option.extensions + return_option.extensions),
            notes=notes,
        )
        result.offers.append(offer)
        return result


class _Option:
    __slots__ = ("leg", "price", "token", "extensions")

    def __init__(self, leg: Leg, price: Decimal | None, token: str | None, extensions: list[str]):
        self.leg = leg
        self.price = price
        self.token = token
        self.extensions = extensions


def parse_options(data: dict, catalog: DestinationCatalog) -> list[_Option]:
    """Estrae le opzioni di volo. Le opzioni con dati mancanti o malformati vengono scartate."""
    options: list[_Option] = []
    for group in ("best_flights", "other_flights"):
        for item in data.get(group) or []:
            if not isinstance(item, dict):
                continue
            try:
                leg = parse_leg(item.get("flights"), item.get("layovers"), item.get("total_duration"), catalog)
            except (KeyError, TypeError, ValueError, IndexError):
                continue  # risposta incompleta per questa opzione
            options.append(
                _Option(
                    leg=leg,
                    price=_to_decimal(item.get("price")),
                    token=item.get("departure_token") if isinstance(item.get("departure_token"), str) else None,
                    extensions=[e for e in item.get("extensions") or [] if isinstance(e, str)],
                )
            )
    return options


def parse_leg(flights: Any, layovers: Any, total_duration: Any, catalog: DestinationCatalog) -> Leg:
    if not isinstance(flights, list) or not flights:
        raise ValueError("nessun segmento di volo")
    segments = []
    for raw in flights:
        dep = raw["departure_airport"]
        arr = raw["arrival_airport"]
        dep_code = str(dep["id"]).upper()
        arr_code = str(arr["id"]).upper()
        segments.append(
            Segment(
                departure_airport=dep_code,
                departure_local=datetime.strptime(dep["time"], LOCAL_TIME_FORMAT),
                departure_tz=catalog.timezone(dep_code),
                arrival_airport=arr_code,
                arrival_local=datetime.strptime(arr["time"], LOCAL_TIME_FORMAT),
                arrival_tz=catalog.timezone(arr_code),
                airline=raw.get("airline"),
                flight_number=raw.get("flight_number"),
                duration_minutes=_to_int(raw.get("duration")),
            )
        )
    return Leg(segments=segments, total_duration_minutes=_to_int(total_duration))


def _timing_problems(
    outbound: Leg,
    inbound: Leg,
    origin: str,
    catalog: DestinationCatalog,
    rules: TimeRules,
) -> list[str] | None:
    """Lista di problemi; None se mancano i fusi orari necessari per il controllo."""
    origin_tz = catalog.timezone(origin)
    departure = localize(outbound.segments[0].departure_local, origin_tz)
    outbound_arrival = outbound.segments[-1].arrival_at
    return_departure = inbound.segments[0].departure_at
    return_arrival = inbound.segments[-1].arrival_at
    if None in (departure, outbound_arrival, return_departure, return_arrival):
        return None
    return round_trip_problems(
        outbound_departure=departure,
        outbound_arrival=outbound_arrival,
        return_departure=return_departure,
        return_arrival=return_arrival,
        rules=rules,
    )


def _source_url(data: dict) -> str | None:
    metadata = data.get("search_metadata")
    if isinstance(metadata, dict):
        url = metadata.get("google_flights_url")
        if isinstance(url, str) and url.startswith("https://"):
            return url
    return None


def _baggage_info(extensions: list[str]) -> str:
    bag_lines = [text for text in extensions if "bag" in text.lower() or "bagagli" in text.lower()]
    if bag_lines:
        return "; ".join(dict.fromkeys(bag_lines))
    return "bagaglio non indicato dal provider"


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
