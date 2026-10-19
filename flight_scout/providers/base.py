"""Interfaccia comune dei provider di ricerca voli e tipi di risultato condivisi."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from flight_scout.date_engine import TripWindow
from flight_scout.models import Offer


class ProviderError(Exception):
    """Errore di un provider: la ricerca specifica fallisce, il resto del programma continua."""


class ProviderAuthError(ProviderError):
    """Chiave non valida o piano non abilitato: il provider va fermato per l'intera esecuzione."""


class ProviderUnavailable(ProviderError):
    """Provider non configurato o non disponibile."""


@dataclass(frozen=True)
class ExploreResult:
    """Destinazione trovata dalla ricerca esplorativa. Il prezzo è sempre INDICATIVE."""

    destination_iata: str
    destination_name: str | None
    destination_country: str | None
    price_eur: Decimal | None
    duration_minutes: int | None
    stops: int | None
    window: TripWindow


@dataclass
class VerifyResult:
    """Esito della verifica di una destinazione: offerte valide e motivi di scarto."""

    offers: list[Offer] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
