"""Criteri di classificazione e punteggio: deterministici e modificabili in config/criteria.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field

from flight_scout.config import PROJECT_ROOT, Settings
from flight_scout.models import Offer, SearchStatus

DEFAULT_CRITERIA = PROJECT_ROOT / "config" / "criteria.json"

CATEGORY_BEST_BUDGET = "BEST_BUDGET"
CATEGORY_NEAR_BUDGET = "NEAR_BUDGET"
CATEGORY_EXOTIC = "EXOTIC"

CATEGORY_LABELS = {
    CATEGORY_BEST_BUDGET: "Migliori offerte economiche (entro soglia)",
    CATEGORY_NEAR_BUDGET: "Quasi economiche (sopra soglia, configurabile)",
    CATEGORY_EXOTIC: "Destinazioni esotiche e interessanti",
}


class Criteria(BaseModel):
    price_reference_eur: float = Field(gt=0)
    weights: dict[str, float]
    stops_points: dict[str, float]
    stops_points_default: float
    status_points: dict[str, float]
    duration_comfort_hours: float
    duration_penalty_per_hour: float
    default_interest_score: int = Field(ge=0, le=100)
    exotic_min_interest: int = Field(ge=0, le=100)
    # Soglie delle notifiche: separate dalla soglia di ricerca (MAX_STANDARD_PRICE_EUR).
    notify_budget_max_eur: float = Field(gt=0)
    notify_min_interest: int = Field(ge=0, le=100)
    near_budget_max_eur: float
    price_drop_percent: float = Field(gt=0)
    quick_max_api_calls: int = Field(ge=1)
    serpapi_hourly_limit: int = Field(ge=1)
    discovery_patterns: list[list[str]]

    @classmethod
    def load(cls, path: Path = DEFAULT_CRITERIA) -> "Criteria":
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.pop("_nota", None)
        return cls.model_validate(raw)


@dataclass(frozen=True)
class Scored:
    score: float
    reasons: list[str] = field(default_factory=list)


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def price_points(price: Decimal | float, criteria: Criteria) -> float:
    return _clamp(100 * (1 - float(price) / criteria.price_reference_eur))


def preselection_score(price: Decimal | None, interest: int, criteria: Criteria) -> float:
    """Priorità con cui scegliere le destinazioni da verificare (prezzo indicativo + interesse)."""
    price_part = price_points(price, criteria) if price is not None else 50.0
    return round(0.6 * price_part + 0.4 * interest, 2)


def score_offer(offer: Offer, interest: int, criteria: Criteria) -> Scored:
    """Punteggio 0-100 con motivazioni leggibili. Ogni componente è visibile e riproducibile."""
    w = criteria.weights
    reasons: list[str] = []

    price_part = price_points(offer.price_eur, criteria)
    reasons.append(f"prezzo {offer.price_eur:.2f} EUR A/R per persona")

    travel = offer.travel_minutes
    if travel is None:
        duration_part = 50.0
        reasons.append("durata non disponibile")
    else:
        hours = travel / 60
        excess = max(0.0, hours - criteria.duration_comfort_hours)
        duration_part = _clamp(100 - excess * criteria.duration_penalty_per_hour)
        reasons.append(f"viaggio complessivo {hours:.1f} h")

    stops = offer.total_stops
    if stops is None:
        stops_part = 50.0
    else:
        stops_part = criteria.stops_points.get(str(min(stops, 3)), criteria.stops_points_default)
    reasons.append(_stops_label(stops))

    status_part = criteria.status_points.get(offer.status.value, 0)
    reasons.append("prezzo verificato" if offer.status == SearchStatus.VERIFIED else "prezzo indicativo, da verificare")

    interest_part = float(interest)

    total = (
        w["price"] * price_part
        + w["duration"] * duration_part
        + w["stops"] * stops_part
        + w["status"] * status_part
        + w["interest"] * interest_part
    )
    return Scored(score=round(total, 1), reasons=reasons)


def _stops_label(stops: int | None) -> str:
    if stops is None:
        return "scali non indicati"
    if stops == 0:
        return "volo diretto"
    return f"{stops} scal{'o' if stops == 1 else 'i'}"


def classify(offer: Offer, interest: int, settings: Settings, criteria: Criteria) -> list[str]:
    """Categorie a cui appartiene l'offerta. Un'offerta può stare in più categorie."""
    categories: list[str] = []
    price = offer.price_eur
    cap = settings.max_standard_price_eur
    if price <= cap:
        categories.append(CATEGORY_BEST_BUDGET)
    elif price <= Decimal(str(criteria.near_budget_max_eur)):
        categories.append(CATEGORY_NEAR_BUDGET)
    if interest >= criteria.exotic_min_interest:
        if settings.exotic_max_price_eur is None or price <= settings.exotic_max_price_eur:
            categories.append(CATEGORY_EXOTIC)
    return categories


def sort_key(category: str, offer: Offer, interest: int, score: float) -> tuple:
    """Ordinamento di default: prezzo crescente nel gruppo budget; interesse nelle esotiche."""
    if category == CATEGORY_EXOTIC:
        return (-interest, -score, float(offer.price_eur))
    return (float(offer.price_eur), -score, offer.nights)
