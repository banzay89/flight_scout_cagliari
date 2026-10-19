"""Notifiche: deduplica, eventi, canali disattivati, nessun segreto nei messaggi."""

from datetime import date, datetime, timezone
from decimal import Decimal

import httpx

from flight_scout.database import Database
from flight_scout.models import Offer, SearchStatus
from flight_scout.notifications import (
    EVENT_NEW_UNDER_CAP,
    EVENT_PRICE_DROP,
    TelegramSender,
    build_notices,
    dispatch,
    enabled_senders,
)
from flight_scout.search_engine import Change
from flight_scout.database import UpsertResult

NOW = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)
TOKEN = "TOKEN-TELEGRAM-PROVA-123"


def _offer(price="88", dest="BCN", status=SearchStatus.INDICATIVE):
    return Offer(
        provider="serpapi",
        destination_iata=dest,
        destination_name="Barcellona",
        destination_country="Spagna",
        outbound_date=date(2026, 10, 22),
        return_date=date(2026, 10, 26),
        price_eur=Decimal(price),
        status=status,
        observed_at=NOW,
    )


class FakeSender:
    name = "fake"

    def __init__(self):
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


def _change(offer, *, is_new=True, previous_price=None, superseded=False, new_destination=False):
    return Change(
        offer=offer,
        upsert=UpsertResult(is_new=is_new, previous_status=None, previous_price=previous_price, superseded_indicative=superseded),
        new_destination=new_destination,
    )


def test_new_offer_under_cap_triggers_notice(settings, criteria, catalog):
    notices = build_notices([_change(_offer("88"))], settings, criteria, catalog)
    assert [n.event for n in notices] == [EVENT_NEW_UNDER_CAP]
    assert "Prezzo: 88.00 EUR" in notices[0].text


def test_new_offer_above_cap_does_not_trigger_budget_notice(settings, criteria, catalog):
    notices = build_notices([_change(_offer("150"))], settings, criteria, catalog)
    assert all(n.event != EVENT_NEW_UNDER_CAP for n in notices)


def test_significant_price_drop_triggers_notice(settings, criteria, catalog):
    change = _change(_offer("80"), is_new=False, previous_price=Decimal("96"))
    notices = build_notices([change], settings, criteria, catalog)
    assert EVENT_PRICE_DROP in [n.event for n in notices]


def test_small_price_change_does_not_trigger_drop(settings, criteria, catalog):
    change = _change(_offer("94"), is_new=False, previous_price=Decimal("96"))
    notices = build_notices([change], settings, criteria, catalog)
    assert EVENT_PRICE_DROP not in [n.event for n in notices]


def test_same_result_is_not_sent_twice(tmp_path, settings, criteria, catalog):
    db = Database(tmp_path / "n.db")
    notices = build_notices([_change(_offer("88"))], settings, criteria, catalog)
    sender = FakeSender()
    first_sent, _ = dispatch(db, notices, [sender])
    second_sent, _ = dispatch(db, notices, [sender])
    assert first_sent == 1 and second_sent == 0
    assert len(sender.sent) == 1
    db.close()


def test_failed_send_is_not_marked_as_sent(tmp_path, settings, criteria, catalog):
    class Broken(FakeSender):
        def send(self, text):
            raise RuntimeError("invio fallito (prova)")

    db = Database(tmp_path / "f.db")
    notices = build_notices([_change(_offer("88"))], settings, criteria, catalog)
    sent, problems = dispatch(db, notices, [Broken()])
    assert sent == 0 and problems
    assert not db.notification_sent(f"{notices[0].dedupe_key}|fake")
    db.close()


def test_exotic_destination_notice_fires_once_per_new_destination(settings, criteria, catalog):
    notices = build_notices(
        [_change(_offer("300", dest="PPT"), new_destination=True)], settings, criteria, catalog
    )
    assert any(n.event == "ESOTICA_SCOPERTA" for n in notices)


def test_disabled_channels_when_not_configured(settings):
    with httpx.Client() as http:
        assert enabled_senders(settings, http) == []


def test_telegram_message_and_errors_do_not_leak_token(make_settings):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(401, json={"ok": False})

    settings = make_settings(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID="12345")
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        senders = enabled_senders(settings, http)
        assert [s.name for s in senders] == ["telegram"]
        try:
            senders[0].send("prova")
        except RuntimeError as exc:
            assert TOKEN not in str(exc)
            assert "401" in str(exc)
        else:
            raise AssertionError("l'errore HTTP doveva essere segnalato")


def test_telegram_transport_error_message_is_sanitized():
    def handler(request):
        raise httpx.ConnectError(f"errore verso {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        sender = TelegramSender(TOKEN, "1", http)
        try:
            sender.send("x")
        except RuntimeError as exc:
            assert TOKEN not in str(exc)
        else:
            raise AssertionError("doveva fallire")
