"""Motore di ricerca: pianificazione, esplorazione globale, verifica e salvataggio.

Fase A (esplorazione): ricerca flessibile da CAG, prezzi INDICATIVI e candidati.
Fase B (verifica): per i candidati migliori, ricerca specifica con date esatte. Solo qui
un'offerta può diventare VERIFIED, e solo se contiene andata e ritorno validi.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx

from flight_scout.budget import ApiBudget, BudgetExceeded, NullBudget
from flight_scout.cache import ResponseCache, make_cache_key
from flight_scout.config import Settings
from flight_scout.date_engine import TimeRules, TripWindow, generate_trip_windows
from flight_scout.database import Database, UpsertResult
from flight_scout.destination_engine import DestinationCatalog
from flight_scout.models import Offer, SearchStatus
from flight_scout.providers.base import ExploreResult, ProviderAuthError, ProviderError, ProviderUnavailable
from flight_scout.providers.offline import OfflineSerpApi
from flight_scout.providers.serpapi_client import SerpApiClient
from flight_scout.providers.serpapi_explore import SerpApiExplore
from flight_scout.providers.serpapi_flights import SerpApiFlights
from flight_scout.ranking import Criteria, preselection_score

PROVIDER = "serpapi"
MODES = ("quick", "deep")


@dataclass
class Change:
    """Offerta salvata in questa scansione, con lo stato precedente (per notifiche e storico)."""

    offer: Offer
    upsert: UpsertResult
    new_destination: bool


@dataclass
class Candidate:
    iata: str
    name: str | None
    country: str | None
    window: TripWindow
    price: Decimal | None
    interest: int
    priority: float


@dataclass
class ScanStats:
    scan_id: int
    mode: str
    offline: bool
    windows_total: int = 0
    windows_covered: int = 0
    discovery_planned: int = 0
    api_calls: int = 0
    verified: int = 0
    indicative: int = 0
    rejected: int = 0
    destinations_found: int = 0
    candidates: int = 0
    verified_destinations: int = 0
    errors: list[str] = field(default_factory=list)
    skipped_windows: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    budget_stopped: bool = False
    changes: list[Change] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "mode": self.mode,
            "offline": self.offline,
            "providers": [PROVIDER],
            "windows_total": self.windows_total,
            "windows_covered": self.windows_covered,
            "api_calls": self.api_calls,
            "verified": self.verified,
            "indicative": self.indicative,
            "rejected": self.rejected,
            "destinations_found": self.destinations_found,
            "candidates": self.candidates,
            "verified_destinations": self.verified_destinations,
            "errors": self.errors[:20],
            "error_count": len(self.errors),
            "skipped_windows": self.skipped_windows[:200],
            "stop_reason": self.stop_reason,
        }


@dataclass(frozen=True)
class ScanPlan:
    windows_total: int
    discovery: list[TripWindow]
    run_limit: int
    discovery_limit: int
    cached_discovery: int

    @property
    def expected_discovery_calls(self) -> int:
        planned = self.discovery[: self.discovery_limit]
        return len(planned) - min(self.cached_discovery, len(planned))


def discovery_priority(windows: list[TripWindow], criteria: Criteria) -> list[TripWindow]:
    """Ordine di esplorazione: periodi vicini densi, poi settimane successive a passo 2.

    Così il budget non si esaurisce sulle prime settimane, ma resta una panoramica futura.
    """
    patterns = {tuple(pattern) for pattern in criteria.discovery_patterns}
    by_week: dict = {}
    for window in windows:
        pattern = tuple(window.pattern.split("-"))
        if pattern in patterns:
            by_week.setdefault(window.week_thursday, []).append(window)
    weeks = sorted(by_week)
    if not weeks:
        return []
    order = weeks[:4] + weeks[4::2] + weeks[5::2]
    result: list[TripWindow] = []
    for week in order:
        result.extend(sorted(by_week[week], key=lambda w: w.outbound_date))
    return result


def discovery_budget(run_limit: int, share: float) -> int:
    if run_limit <= 0:
        return 0
    return min(run_limit, max(1, int(run_limit * share)))


def effective_run_limit(settings: Settings, criteria: Criteria, mode: str, max_api_calls: int | None) -> int:
    limit = settings.api_run_budget
    if max_api_calls is not None:
        limit = min(limit, max_api_calls)
    if mode == "quick":
        limit = min(limit, criteria.quick_max_api_calls)
    return limit


def _today(settings: Settings) -> date:
    return datetime.now(ZoneInfo(settings.timezone)).date()


def plan_scan(
    settings: Settings,
    criteria: Criteria,
    catalog: DestinationCatalog,
    db: Database,
    *,
    mode: str = "deep",
    max_api_calls: int | None = None,
    today=None,
) -> ScanPlan:
    """Pianificazione senza rete: quante ricerche, quante già in cache, quante ne restano."""
    today = today or _today(settings)
    windows = generate_trip_windows(today, settings.search_horizon_days, settings.min_nights, settings.max_nights)
    discovery = discovery_priority(windows, criteria)
    run_limit = effective_run_limit(settings, criteria, mode, max_api_calls)
    limit = discovery_budget(run_limit, settings.discovery_share)
    cache = ResponseCache(db, settings.cache_ttl_hours)
    explore = SerpApiExplore(_NoCallClient(), settings.origin_airport, settings.currency, settings.passengers)
    cached = 0
    for window in discovery[:limit]:
        key = make_cache_key(PROVIDER, f"explore:{window.key}", explore.params(window))
        if cache.has(key):
            cached += 1
    return ScanPlan(
        windows_total=len(windows),
        discovery=discovery,
        run_limit=run_limit,
        discovery_limit=limit,
        cached_discovery=cached,
    )


class _NoCallClient:
    """Segnaposto usato solo per calcolare i parametri in dry-run: non invia mai richieste."""

    def search(self, *_args, **_kwargs):  # pragma: no cover - non deve essere mai chiamato
        raise RuntimeError("dry-run: nessuna richiesta di rete consentita")


def run_scan(
    settings: Settings,
    criteria: Criteria,
    db: Database,
    catalog: DestinationCatalog,
    *,
    mode: str = "deep",
    offline: bool = False,
    max_api_calls: int | None = None,
    today=None,
    sleep=time.sleep,
    transport: httpx.BaseTransport | None = None,
) -> ScanStats:
    """Esegue una scansione. Ogni passo viene salvato subito: un'interruzione non fa perdere dati.

    "transport" serve ai test per sostituire la rete; in uso normale resta None.
    """
    if mode not in MODES:
        raise ValueError(f"modalità sconosciuta: {mode}")
    if not offline and not settings.serpapi_api_key:
        raise ProviderUnavailable("SERPAPI_API_KEY non impostata: impossibile eseguire una scansione live")

    today = today or _today(settings)
    rules = TimeRules.from_settings(settings)
    scan_id = db.start_scan(mode, offline)
    stats = ScanStats(scan_id=scan_id, mode=mode, offline=offline)
    run_limit = effective_run_limit(settings, criteria, mode, max_api_calls)
    origin = settings.origin_airport
    status = "error"
    http: httpx.Client | None = None
    budget = None

    try:
        if offline:
            budget = NullBudget(run_limit)
            http = httpx.Client(transport=OfflineSerpApi(catalog, origin).transport())
            # Nota: la modalità offline non usa mai la rete, anche se "transport" è fornito.
            client = SerpApiClient(
                api_key="offline", http=http, budget=budget, cache=None, sleep=sleep, pause_seconds=0
            )
        else:
            budget = ApiBudget(
                db,
                PROVIDER,
                run_limit=run_limit,
                monthly_limit=settings.api_monthly_budget,
                hourly_limit=criteria.serpapi_hourly_limit,
                scan_id=scan_id,
            )
            http = httpx.Client(timeout=30.0, transport=transport)
            client = SerpApiClient(
                api_key=settings.serpapi_api_key,
                http=http,
                budget=budget,
                cache=ResponseCache(db, settings.cache_ttl_hours),
                sleep=sleep,
            )

        explore = SerpApiExplore(client, origin, settings.currency, settings.passengers)
        flights = SerpApiFlights(
            client,
            catalog,
            rules,
            origin=origin,
            currency=settings.currency,
            passengers=settings.passengers,
            travel_class=settings.travel_class,
        )

        windows = generate_trip_windows(today, settings.search_horizon_days, settings.min_nights, settings.max_nights)
        stats.windows_total = len(windows)
        discovery = discovery_priority(windows, criteria)
        stats.discovery_planned = min(len(discovery), discovery_budget(run_limit, settings.discovery_share))
        discovery_limit = discovery_budget(run_limit, settings.discovery_share)

        candidates = _explore_phase(stats, discovery, discovery_limit, explore, budget, db, catalog, criteria, settings)
        stats.candidates = len(candidates)
        _verify_phase(stats, candidates, flights, budget, db, settings)

        status = "budget_stopped" if stats.budget_stopped else "completed"
    except KeyboardInterrupt:
        status = "interrupted"
        raise
    finally:
        if http is not None:
            http.close()
        if budget is not None:
            stats.api_calls = budget.spent
        db.finish_scan(scan_id, status, stats.summary())
    return stats


def _explore_phase(
    stats: ScanStats,
    discovery: list[TripWindow],
    discovery_limit: int,
    explore: SerpApiExplore,
    budget,
    db: Database,
    catalog: DestinationCatalog,
    criteria: Criteria,
    settings: Settings,
) -> list[Candidate]:
    best: dict[str, Candidate] = {}
    now = datetime.now(timezone.utc)
    for index, window in enumerate(discovery):
        if budget.spent >= discovery_limit:
            _skip_rest(stats, db, discovery[index:], "budget della fase di esplorazione esaurito")
            break
        try:
            results = explore.explore(window)
        except BudgetExceeded as exc:
            stats.budget_stopped = True
            stats.stop_reason = str(exc)
            _skip_rest(stats, db, discovery[index:], str(exc))
            break
        except ProviderAuthError as exc:
            stats.stop_reason = "provider non autorizzato"
            _record_error(stats, db, f"esplorazione {window.key}: {exc}")
            _skip_rest(stats, db, discovery[index:], "provider non autorizzato")
            break
        except ProviderError as exc:
            _record_error(stats, db, f"esplorazione {window.key}: {exc}")
            stats.skipped_windows.append(f"{window.key}: {exc}")
            db.log_skip(stats.scan_id, PROVIDER, window.key, str(exc))
            continue

        stats.windows_covered += 1
        for result in results:
            _record_explore(stats, db, result, catalog, criteria, settings, now, best)

    stats.destinations_found = db.discovered_count()
    return sorted(best.values(), key=lambda c: (-c.priority, c.iata))


def _record_explore(
    stats: ScanStats,
    db: Database,
    result: ExploreResult,
    catalog: DestinationCatalog,
    criteria: Criteria,
    settings: Settings,
    now: datetime,
    best: dict[str, Candidate],
) -> None:
    if result.price_eur is None:
        db.log_skip(stats.scan_id, PROVIDER, f"explore {result.destination_iata}", "prezzo non disponibile")
        return
    offer = Offer(
        provider=PROVIDER,
        destination_iata=result.destination_iata,
        destination_name=result.destination_name,
        destination_country=result.destination_country,
        outbound_date=result.window.outbound_date,
        return_date=result.window.return_date,
        price_eur=result.price_eur,
        currency=settings.currency,
        status=SearchStatus.INDICATIVE,
        observed_at=now,
        stops_hint=result.stops,
        duration_hint_minutes=result.duration_minutes,
        notes=["prezzo esplorativo: itinerario e date non verificati"],
    )
    upsert = db.upsert_offer(offer, stats.scan_id, stats.offline)
    new_destination = db.record_destination(
        result.destination_iata, result.destination_name, result.destination_country, result.price_eur
    )
    stats.changes.append(Change(offer, upsert, new_destination))
    stats.indicative += 1

    interest = catalog.interest(result.destination_iata, criteria.default_interest_score)
    priority = preselection_score(result.price_eur, interest, criteria)
    current = best.get(result.destination_iata)
    if current is None or priority > current.priority or (
        priority == current.priority and result.price_eur < (current.price or result.price_eur)
    ):
        best[result.destination_iata] = Candidate(
            iata=result.destination_iata,
            name=result.destination_name,
            country=result.destination_country,
            window=result.window,
            price=result.price_eur,
            interest=interest,
            priority=priority,
        )


def _verify_phase(
    stats: ScanStats,
    candidates: list[Candidate],
    flights: SerpApiFlights,
    budget,
    db: Database,
    settings: Settings,
) -> None:
    for position, candidate in enumerate(candidates[: settings.max_destinations_to_verify]):
        if budget.remaining_run < 2:
            stats.budget_stopped = True
            stats.stop_reason = stats.stop_reason or "budget esaurito prima di verificare tutti i candidati"
            break
        try:
            outcome = flights.verify(
                candidate.iata,
                candidate.window,
                destination_name=candidate.name,
                destination_country=candidate.country,
            )
        except BudgetExceeded as exc:
            stats.budget_stopped = True
            stats.stop_reason = str(exc)
            break
        except ProviderAuthError as exc:
            stats.stop_reason = "provider non autorizzato"
            _record_error(stats, db, f"verifica {candidate.iata}: {exc}")
            break
        except ProviderError as exc:
            _record_error(stats, db, f"verifica {candidate.iata}: {exc}")
            continue

        stats.verified_destinations += 1 if outcome.offers else 0
        for reason in outcome.rejected:
            stats.rejected += 1
            db.log_skip(stats.scan_id, PROVIDER, f"verifica {candidate.iata}", reason)
        for offer in outcome.offers:
            upsert = db.upsert_offer(offer, stats.scan_id, stats.offline)
            new_destination = db.record_destination(
                offer.destination_iata, offer.destination_name, offer.destination_country, offer.price_eur
            )
            stats.changes.append(Change(offer, upsert, new_destination))
            if offer.status == SearchStatus.VERIFIED:
                stats.verified += 1
            else:
                stats.indicative += 1


def _skip_rest(stats: ScanStats, db: Database, windows: list[TripWindow], reason: str) -> None:
    for window in windows:
        stats.skipped_windows.append(f"{window.key}: {reason}")
        db.log_skip(stats.scan_id, PROVIDER, window.key, reason)


def _record_error(stats: ScanStats, db: Database, message: str) -> None:
    stats.errors.append(message[:300])
    db.log_error(stats.scan_id, PROVIDER, message)


def coverage_text(stats: ScanStats) -> str:
    return f"{stats.windows_covered}/{stats.windows_total} finestre esplorate"


