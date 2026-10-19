"""Modalità offline: risposte di TEST generate localmente, nel formato di SerpApi.

ATTENZIONE: questi dati NON sono reali. Servono a provare il programma end-to-end senza
Internet e senza costi. Il codice di parsing è lo stesso usato con i dati reali: viene
sostituito solo il trasporto HTTP (httpx.MockTransport). Le offerte offline vengono salvate
in un database separato e nei report sono sempre marcate come "DATI DI TEST".
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from flight_scout.destination_engine import DestinationCatalog

OFFLINE_BANNER = "DATI DI TEST OFFLINE - NON SONO PREZZI REALI"

# Destinazioni e prezzi base (EUR A/R per persona) usati solo nei dati di test.
_TEST_DESTINATIONS = {
    "BCN": ("Barcellona", "Spagna", 38, 140, 0),
    "LIS": ("Lisbona", "Portogallo", 52, 220, 0),
    "KEF": ("Reykjavik", "Islanda", 74, 200, 0),
    "BKK": ("Bangkok", "Thailandia", 260, 840, 1),
    "NRT": ("Tokyo Narita", "Giappone", 380, 800, 1),
    "DPS": ("Bali (Denpasar)", "Indonesia", 420, 1020, 2),
    "PPT": ("Papeete (Tahiti)", "Polinesia Francese", 900, 1200, 2),
}
# Destinazione per cui il provider di test non restituisce il ritorno (caso reale di dati incompleti).
_NO_RETURN = {"PPT"}


def _price_out(destination: str, outbound: date, stop: bool) -> int:
    base = _TEST_DESTINATIONS[destination][2]
    return base + (outbound.toordinal() % 7) * 6 - (15 if stop else 0)


def _price_return(outbound: date) -> int:
    return 40 + (outbound.toordinal() % 5) * 3


def _price_total(destination: str, outbound: date, stop: bool) -> int:
    """Prezzo A/R totale: andata + ritorno (stessa logica per esplorazione e verifica)."""
    return _price_out(destination, outbound, stop) + _price_return(outbound)


class OfflineSerpApi:
    """Trasporto httpx che risponde come SerpApi, senza rete."""

    def __init__(self, catalog: DestinationCatalog, origin: str):
        self.catalog = catalog
        self.origin = origin

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        engine = params.get("engine")
        if engine == "google_travel_explore":
            payload = self._explore(params)
        elif engine == "google_flights":
            payload = self._flights(params)
        else:
            return httpx.Response(404, json={"error": "motore sconosciuto nei dati di test"})
        return httpx.Response(200, json=payload)

    def _explore(self, params: dict) -> dict:
        outbound = date.fromisoformat(params["outbound_date"])
        destinations = []
        for code, (name, country, _base, _duration, _stops) in _TEST_DESTINATIONS.items():
            destinations.append(
                {
                    "name": name,
                    "country": country,
                    "destination_airport": {"code": code},
                    "flight_price": _price_total(code, outbound, stop=False),
                    "flight_duration": _TEST_DESTINATIONS[code][3],
                    "number_of_stops": _TEST_DESTINATIONS[code][4],
                }
            )
        return {"destinations": destinations, "search_metadata": {"status": "Success", "test_data": True}}

    def _flights(self, params: dict) -> dict:
        destination = params["arrival_id"]
        outbound = date.fromisoformat(params["outbound_date"])
        return_day = date.fromisoformat(params["return_date"])
        if destination not in _TEST_DESTINATIONS:
            return {"search_metadata": {"status": "Success"}}
        name, country, _base, duration, stops = _TEST_DESTINATIONS[destination]
        token = params.get("departure_token")

        if token is None:
            options = [{"stop": False}, {"stop": True}] if stops == 0 else [{"stop": True}]
            best = []
            for option in options:
                price = _price_total(destination, outbound, option["stop"])
                flights = self._outbound_segments(destination, outbound, option["stop"], duration)
                best.append(
                    {
                        "flights": flights,
                        "layovers": [{"id": "FCO", "duration": 90}] if option["stop"] else [],
                        "total_duration": sum(s["duration"] for s in flights) + (90 if option["stop"] else 0),
                        "price": price,
                        "departure_token": f"TEST|{destination}|{outbound.isoformat()}|{int(option['stop'])}",
                        "extensions": ["Bagaglio a mano incluso (dati di test)"],
                    }
                )
            return {"best_flights": best, "other_flights": [], "search_metadata": {"status": "Success"}}

        parts = token.split("|")
        if destination in _NO_RETURN or parts[0] != "TEST":
            return {"search_metadata": {"status": "Success"}}
        stop_flag = parts[3] == "1"
        price_total = _price_total(destination, outbound, stop_flag)
        return_flights = self._return_segments(destination, return_day, duration)
        return {
            "best_flights": [
                {
                    "flights": return_flights,
                    "total_duration": duration,
                    "price": price_total,
                    "extensions": ["Bagaglio da stiva non incluso (dati di test)"],
                }
            ],
            "search_metadata": {"status": "Success"},
        }

    def _tz(self, code: str) -> ZoneInfo:
        zone = self.catalog.timezone(code)
        if zone is None:
            raise ValueError(f"aeroporto senza fuso nei dati di test: {code}")
        return ZoneInfo(zone)

    def _segment(self, dep_code: str, arr_code: str, dep_at: datetime, minutes: int, number: int) -> dict:
        arr_at = dep_at + timedelta(minutes=minutes)
        return {
            "departure_airport": {
                "name": dep_code,
                "id": dep_code,
                "time": dep_at.astimezone(self._tz(dep_code)).strftime("%Y-%m-%d %H:%M"),
            },
            "arrival_airport": {
                "name": arr_code,
                "id": arr_code,
                "time": arr_at.astimezone(self._tz(arr_code)).strftime("%Y-%m-%d %H:%M"),
            },
            "duration": minutes,
            "airline": "Dati di test",
            "flight_number": f"TS{number:03d}",
        }

    def _departure_time(self, day: date) -> datetime:
        # Giovedì: partenza nel pomeriggio, così la regola delle 17:00 viene rispettata.
        hour, minute = (18, 40) if day.weekday() == 3 else (7, 30)
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=self._tz(self.origin))

    def _outbound_segments(self, destination: str, day: date, stop: bool, duration: int) -> list[dict]:
        start = self._departure_time(day)
        if not stop:
            return [self._segment(self.origin, destination, start, duration, 100 + day.toordinal() % 50)]
        first = self._segment(self.origin, "FCO", start, 110, 200 + day.toordinal() % 50)
        # Scalo di 90 minuti: il secondo volo parte dopo l'arrivo a Fiumicino.
        second_start = start + timedelta(minutes=110 + 90)
        second = self._segment("FCO", destination, second_start, duration, 300 + day.toordinal() % 50)
        return [first, second]

    def _return_segments(self, destination: str, day: date, duration: int) -> list[dict]:
        start = datetime(day.year, day.month, day.day, 12, 0, tzinfo=self._tz(destination))
        return [self._segment(destination, self.origin, start, duration, 400 + day.toordinal() % 50)]
