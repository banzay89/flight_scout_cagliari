"""Notifiche facoltative (Telegram ed e-mail), deduplicate nel database.

- Se un canale non è configurato resta disattivato: il programma funziona comunque.
- Ogni evento ha una chiave di deduplica: lo stesso risultato allo stesso prezzo non viene inviato due volte.
- I messaggi non contengono mai token, password o chiavi API.
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass
from decimal import Decimal
from email.message import EmailMessage
from typing import Protocol

import httpx

from flight_scout.config import Settings
from flight_scout.database import Database
from flight_scout.destination_engine import DestinationCatalog
from flight_scout.models import Offer, SearchStatus
from flight_scout.ranking import Criteria
from flight_scout.search_engine import Change, ScanStats

EVENT_NEW_UNDER_CAP = "NUOVA_OFFERTA_SOTTO_SOGLIA"
EVENT_PRICE_DROP = "CALO_PREZZO"
EVENT_EXOTIC = "ESOTICA_SCOPERTA"
EVENT_INTEREST = "DESTINAZIONE_INTERESSANTE"
EVENT_VERIFIED = "OFFERTA_VERIFICATA"
EVENT_MONTHLY_LIMIT = "LIMITE_MENSILE_RAGGIUNTO"


@dataclass(frozen=True)
class Notice:
    event: str
    dedupe_key: str
    text: str


class Sender(Protocol):
    name: str

    def send(self, text: str) -> None: ...


def describe_offer(offer: Offer) -> list[str]:
    stops_out = offer.outbound.stops if offer.outbound else "?"
    stops_in = offer.inbound.stops if offer.inbound else "?"
    lines = [
        f"Destinazione: {offer.destination_name or offer.destination_iata} ({offer.destination_iata})"
        + (f", {offer.destination_country}" if offer.destination_country else ""),
        f"Date: {offer.outbound_date.isoformat()} -> {offer.return_date.isoformat()} ({offer.nights} notti)",
        f"Prezzo: {offer.price_eur:.2f} {offer.currency} A/R per persona",
        f"Scali: andata {stops_out}, ritorno {stops_in}",
        "Stato: " + ("prezzo verificato" if offer.status == SearchStatus.VERIFIED else "prezzo indicativo, da verificare"),
        f"Rilevato: {offer.observed_at:%Y-%m-%d %H:%M} UTC",
        "Fonte: " + (offer.source_url or "verifica manualmente il volo sul sito del vettore o su Google Flights"),
    ]
    return lines


def build_notices(
    changes: list[Change],
    settings: Settings,
    criteria: Criteria,
    catalog: DestinationCatalog,
) -> list[Notice]:
    notices: list[Notice] = []
    budget_cap = Decimal(str(criteria.notify_budget_max_eur))
    # Decimal per non mescolare valori monetari e float.
    keep_ratio = (Decimal(100) - Decimal(str(criteria.price_drop_percent))) / Decimal(100)
    # Per destinazione di interesse si tiene solo l'offerta nuova più economica della scansione,
    # così una scansione non genera decine di messaggi per la stessa città.
    interesting_best: dict[str, Change] = {}
    for change in changes:
        offer = change.offer
        key = offer.offer_key()
        price = offer.price_eur
        interest = catalog.interest(offer.destination_iata, criteria.default_interest_score)

        if offer.status == SearchStatus.VERIFIED and change.upsert.superseded_indicative:
            notices.append(_notice(EVENT_VERIFIED, f"{key}|{price}", "Offerta indicativa verificata", offer))
        elif change.upsert.is_new and price <= budget_cap:
            notices.append(
                _notice(EVENT_NEW_UNDER_CAP, f"{key}|{price}", f"Nuova offerta sotto {budget_cap:.0f} EUR A/R", offer)
            )
        elif change.upsert.is_new and interest >= criteria.notify_min_interest:
            current = interesting_best.get(offer.destination_iata)
            if current is None or price < current.offer.price_eur:
                interesting_best[offer.destination_iata] = change

        previous = change.upsert.previous_price
        if previous is not None and not change.upsert.is_new and price <= previous * keep_ratio:
            notices.append(
                _notice(EVENT_PRICE_DROP, f"{key}|{price}", f"Calo di prezzo: da {previous:.2f} a {price:.2f} EUR", offer)
            )

        if change.new_destination and interest >= criteria.exotic_min_interest:
            notices.append(
                _notice(EVENT_EXOTIC, f"{offer.destination_iata}", "Destinazione interessante scoperta", offer)
            )

    for iata, change in interesting_best.items():
        offer = change.offer
        notices.append(
            _notice(
                EVENT_INTEREST,
                f"{iata}|{offer.offer_key()}|{offer.price_eur}",
                f"Destinazione di interesse (punteggio {catalog.interest(iata, criteria.default_interest_score)}), "
                f"sopra {budget_cap:.0f} EUR A/R",
                offer,
            )
        )
    return notices


def budget_notice(stats: ScanStats, settings: Settings, month_label: str) -> Notice | None:
    if stats.stop_reason and "mensile" in stats.stop_reason:
        return Notice(
            EVENT_MONTHLY_LIMIT,
            f"{EVENT_MONTHLY_LIMIT}|{month_label}",
            f"Limite API mensile raggiunto ({settings.api_monthly_budget} chiamate). Le ricerche live sono sospese fino al mese successivo.",
        )
    return None


def _notice(event: str, dedupe: str, title: str, offer: Offer) -> Notice:
    text = "\n".join([f"[Flight Scout] {title}", *describe_offer(offer)])
    return Notice(event=event, dedupe_key=f"{event}|{dedupe}", text=text)


class TelegramSender:
    name = "telegram"

    def __init__(self, token: str, chat_id: str, http: httpx.Client):
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._http = http

    def send(self, text: str) -> None:
        try:
            response = self._http.post(
                self._url, data={"chat_id": self._chat_id, "text": text}, timeout=20.0
            )
        except httpx.HTTPError as exc:
            # L'URL contiene il token: si riporta solo il tipo di errore.
            raise RuntimeError(f"invio Telegram fallito ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise RuntimeError(f"invio Telegram fallito (HTTP {response.status_code})")


class EmailSender:
    name = "email"

    def __init__(self, settings: Settings):
        self._settings = settings

    def send(self, text: str) -> None:
        s = self._settings
        message = EmailMessage()
        message["Subject"] = "[Flight Scout] Nuova notifica"
        message["From"] = s.smtp_username
        message["To"] = s.email_to
        message.set_content(text)
        try:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(s.smtp_username, s.smtp_password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise RuntimeError(f"invio e-mail fallito ({type(exc).__name__})") from None


def enabled_senders(settings: Settings, http: httpx.Client) -> list[Sender]:
    senders: list[Sender] = []
    if settings.telegram_bot_token and settings.telegram_chat_id:
        senders.append(TelegramSender(settings.telegram_bot_token, settings.telegram_chat_id, http))
    if settings.smtp_host and settings.smtp_username and settings.smtp_password and settings.email_to:
        senders.append(EmailSender(settings))
    return senders


def dispatch(db: Database, notices: list[Notice], senders: list[Sender]) -> tuple[int, list[str]]:
    """Invia le notifiche non ancora inviate. Restituisce (inviate, errori senza segreti)."""
    sent = 0
    problems: list[str] = []
    for notice in notices:
        for sender in senders:
            key = f"{notice.dedupe_key}|{sender.name}"
            if db.notification_sent(key):
                continue
            try:
                sender.send(notice.text)
            except RuntimeError as exc:
                problems.append(str(exc))
                continue
            db.mark_notification(key, notice.event, sender.name)
            sent += 1
    return sent, problems
