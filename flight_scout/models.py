"""Modelli dati: segmenti di volo, tratte, offerte e stato di verifica."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field

from flight_scout.date_engine import localize


class SearchStatus(str, Enum):
    INDICATIVE = "INDICATIVE"  # prezzo indicativo, non verificato su date e itinerario esatti
    VERIFIED = "VERIFIED"  # itinerario completo restituito da una ricerca specifica per le date scelte
    STALE = "STALE"  # dato troppo vecchio per essere considerato attuale
    ERROR = "ERROR"  # ricerca fallita o risposta non valida


class Segment(BaseModel):
    """Un singolo volo (segmento) di una tratta."""

    departure_airport: str
    departure_local: datetime  # orario da muro nell'aeroporto di partenza
    departure_tz: str | None = None
    arrival_airport: str
    arrival_local: datetime  # orario da muro nell'aeroporto di arrivo
    arrival_tz: str | None = None
    airline: str | None = None
    flight_number: str | None = None
    duration_minutes: int | None = None

    @property
    def departure_at(self) -> datetime | None:
        return localize(self.departure_local, self.departure_tz)

    @property
    def arrival_at(self) -> datetime | None:
        return localize(self.arrival_local, self.arrival_tz)


class Leg(BaseModel):
    """Una tratta completa (andata o ritorno), eventualmente con scali."""

    segments: list[Segment] = Field(min_length=1)
    total_duration_minutes: int | None = None

    @property
    def stops(self) -> int:
        return len(self.segments) - 1

    @property
    def departure_airport(self) -> str:
        return self.segments[0].departure_airport

    @property
    def arrival_airport(self) -> str:
        return self.segments[-1].arrival_airport

    @property
    def layover_airports(self) -> list[str]:
        return [segment.arrival_airport for segment in self.segments[:-1]]

    def layover_minutes(self) -> list[int | None]:
        """Attese tra segmenti consecutivi; None se manca il fuso orario di uno scalo."""
        result: list[int | None] = []
        for previous, following in zip(self.segments, self.segments[1:]):
            arrived = previous.arrival_at
            departing = following.departure_at
            if arrived is None or departing is None:
                result.append(None)
            else:
                result.append(int((departing - arrived).total_seconds() // 60))
        return result


class Offer(BaseModel):
    """Un'offerta A/R per persona. Se manca una delle due tratte, l'offerta è incompleta."""

    provider: str
    destination_iata: str
    destination_name: str | None = None
    destination_country: str | None = None
    outbound_date: date
    return_date: date
    price_eur: Decimal
    currency: str = "EUR"
    status: SearchStatus
    outbound: Leg | None = None
    inbound: Leg | None = None
    observed_at: datetime  # momento della rilevazione, in UTC
    source_url: str | None = None
    baggage_info: str | None = None
    notes: list[str] = Field(default_factory=list)
    # Dati disponibili solo dalla ricerca esplorativa (senza tratte dettagliate).
    stops_hint: int | None = None
    duration_hint_minutes: int | None = None

    @property
    def nights(self) -> int:
        return (self.return_date - self.outbound_date).days

    @property
    def is_complete(self) -> bool:
        return self.outbound is not None and self.inbound is not None

    @property
    def total_stops(self) -> int | None:
        if self.is_complete:
            return self.outbound.stops + self.inbound.stops
        return self.stops_hint

    @property
    def travel_minutes(self) -> int | None:
        if self.is_complete:
            if self.outbound.total_duration_minutes is None or self.inbound.total_duration_minutes is None:
                return None
            return self.outbound.total_duration_minutes + self.inbound.total_duration_minutes
        return self.duration_hint_minutes

    @property
    def missing_data(self) -> list[str]:
        missing = []
        if self.outbound is None:
            missing.append("segmento di andata")
        if self.inbound is None:
            missing.append("segmento di ritorno")
        return missing

    def offer_key(self) -> str:
        """Chiave stabile dell'offerta: il prezzo non fa parte della chiave (vedi storico prezzi)."""
        parts = [
            self.provider,
            self.destination_iata,
            self.outbound_date.isoformat(),
            self.return_date.isoformat(),
        ]
        if self.is_complete:
            for leg in (self.outbound, self.inbound):
                parts.extend(segment.flight_number or "?" for segment in leg.segments)
        else:
            parts.append("INDICATIVE")
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
