"""Motore temporale: finestre di viaggio e regole sugli orari con fusi orari IANA (zoneinfo).

Convenzioni:
- Giovedì = 3, Lunedì = 0 (come datetime.weekday()).
- La "settimana" di una finestra va dal giovedì di riferimento al lunedì successivo.
- Tutti i datetime delle regole sugli orari devono essere "aware" (con fuso orario).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

THURSDAY = 3
MONDAY = 0
# Giorni in cui un viaggio può iniziare: giovedì, venerdì, sabato, domenica.
OUTBOUND_WEEKDAYS = (3, 4, 5, 6)
WEEKDAY_NAMES = ("LUN", "MAR", "MER", "GIO", "VEN", "SAB", "DOM")


@dataclass(frozen=True)
class TripWindow:
    """Una combinazione di date di andata e ritorno da cercare."""

    week_thursday: date
    outbound_date: date
    return_date: date

    @property
    def nights(self) -> int:
        return (self.return_date - self.outbound_date).days

    @property
    def key(self) -> str:
        return f"{self.outbound_date.isoformat()}_{self.return_date.isoformat()}"

    @property
    def pattern(self) -> str:
        """Etichetta leggibile, ad esempio GIO-LUN."""
        return f"{WEEKDAY_NAMES[self.outbound_date.weekday()]}-{WEEKDAY_NAMES[self.return_date.weekday()]}"


@dataclass(frozen=True)
class TimeRules:
    """Regole orarie: si costruiscono dalle impostazioni, ma sono facili da testare."""

    timezone: str
    thursday_earliest: time
    monday_latest: time
    min_nights: int
    max_nights: int

    @classmethod
    def from_settings(cls, settings) -> "TimeRules":
        return cls(
            timezone=settings.timezone,
            thursday_earliest=settings.outbound_thursday_earliest,
            monday_latest=settings.return_monday_latest,
            min_nights=settings.min_nights,
            max_nights=settings.max_nights,
        )


def localize(naive: datetime | None, tz_name: str | None) -> datetime | None:
    """Associa un fuso orario a un orario locale (orario da muro).

    Per gli orari ambigui del cambio dell'ora (fold) viene usata la prima occorrenza.
    """
    if naive is None or tz_name is None:
        return None
    if naive.tzinfo is not None:
        return naive
    return naive.replace(tzinfo=ZoneInfo(tz_name))


def generate_trip_windows(
    start: date,
    horizon_days: int,
    min_nights: int,
    max_nights: int,
) -> list[TripWindow]:
    """Genera tutte le combinazioni andata/ritorno nell'orizzonte di ricerca.

    Partenze ammesse: giovedì, venerdì, sabato, domenica.
    Rientro ammesso: non oltre il lunedì della stessa settimana.
    Permanenza: tra min_nights e max_nights notti.
    """
    end = start + timedelta(days=horizon_days)
    # Giovedì della settimana in cui cade "start" (può essere già passato: le date
    # di partenza precedenti a "start" vengono scartate sotto).
    thursday = start - timedelta(days=(start.weekday() - THURSDAY) % 7)
    windows: list[TripWindow] = []
    while thursday <= end:
        monday = thursday + timedelta(days=4)
        for offset in range(4):
            outbound = thursday + timedelta(days=offset)
            if outbound < start or outbound > end:
                continue
            for nights in range(min_nights, max_nights + 1):
                return_day = outbound + timedelta(days=nights)
                if return_day > monday:
                    break
                windows.append(TripWindow(thursday, outbound, return_day))
        thursday += timedelta(days=7)
    return windows


def outbound_departure_problems(departure_local: datetime, rules: TimeRules) -> list[str]:
    """Controlla la partenza da Cagliari. departure_local è già nel fuso di Cagliari."""
    if departure_local.weekday() not in OUTBOUND_WEEKDAYS:
        return ["partenza da Cagliari non ammessa: il viaggio inizia da giovedì"]
    if departure_local.weekday() == THURSDAY and departure_local.time() < rules.thursday_earliest:
        return [f"partenza di giovedì prima delle {rules.thursday_earliest:%H:%M}"]
    return []


def return_deadline(outbound_departure_local: datetime, rules: TimeRules) -> datetime:
    """Lunedì alle 23:00 (ora locale di Cagliari) della settimana della partenza."""
    day = outbound_departure_local.date()
    thursday = day - timedelta(days=day.weekday() - THURSDAY)
    monday = thursday + timedelta(days=4)
    return datetime.combine(monday, rules.monday_latest, tzinfo=ZoneInfo(rules.timezone))


def round_trip_problems(
    *,
    outbound_departure: datetime,
    outbound_arrival: datetime,
    return_departure: datetime,
    return_arrival: datetime,
    rules: TimeRules,
) -> list[str]:
    """Restituisce i motivi per cui un itinerario A/R non è valido. Lista vuota = valido.

    - outbound_arrival: arrivo finale dell'andata, dopo eventuali scali.
    - return_departure: partenza dal luogo di destinazione.
    - return_arrival: arrivo a Cagliari.
    """
    origin_zone = ZoneInfo(rules.timezone)
    departure_local = outbound_departure.astimezone(origin_zone)
    problems = outbound_departure_problems(departure_local, rules)

    if outbound_arrival <= outbound_departure:
        problems.append("arrivo dell'andata non successivo alla partenza")
    if return_departure < outbound_arrival:
        problems.append("il volo di ritorno parte prima dell'arrivo dell'andata")
    if return_arrival <= return_departure:
        problems.append("arrivo del ritorno non successivo alla partenza")
    if return_arrival.astimezone(origin_zone) > return_deadline(departure_local, rules):
        problems.append("rientro a Cagliari oltre lunedì alle 23:00 (ora locale)")

    # Pernottamenti: differenza tra la data di partenza da Cagliari e quella di partenza dalla destinazione.
    nights = (return_departure.date() - departure_local.date()).days
    if not rules.min_nights <= nights <= rules.max_nights:
        problems.append(f"{nights} notti fuori dai limiti {rules.min_nights}-{rules.max_nights}")
    return problems
