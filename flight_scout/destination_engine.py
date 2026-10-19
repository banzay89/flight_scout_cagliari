"""Catalogo delle destinazioni: aeroporti, fusi orari e punteggi di interesse editoriali."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from flight_scout.config import PROJECT_ROOT

DEFAULT_CATALOG = PROJECT_ROOT / "config" / "destinations.json"


@dataclass(frozen=True)
class Airport:
    iata: str
    name: str
    country: str
    timezone: str | None
    interest_score: int | None


class DestinationCatalog:
    def __init__(self, airports: dict[str, Airport]):
        self._airports = airports

    @classmethod
    def load(cls, path: Path = DEFAULT_CATALOG) -> "DestinationCatalog":
        raw = json.loads(path.read_text(encoding="utf-8"))
        airports: dict[str, Airport] = {}
        for item in raw["airports"]:
            airport = Airport(
                iata=item["iata"].upper(),
                name=item["name"],
                country=item["country"],
                timezone=item.get("timezone"),
                interest_score=item.get("interest_score"),
            )
            airports[airport.iata] = airport
        return cls(airports)

    def get(self, iata: str) -> Airport | None:
        return self._airports.get(iata.upper())

    def timezone(self, iata: str) -> str | None:
        airport = self.get(iata)
        return airport.timezone if airport else None

    def interest(self, iata: str, default: int) -> int:
        """Punteggio di interesse; se la destinazione non è nel catalogo si usa il valore predefinito."""
        airport = self.get(iata)
        if airport is None or airport.interest_score is None:
            return default
        return airport.interest_score

    def is_known(self, iata: str) -> bool:
        return self.get(iata) is not None

    def __len__(self) -> int:
        return len(self._airports)
