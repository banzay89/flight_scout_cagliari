"""Report: CSV per Excel, HTML con filtri e note di sicurezza, terminale con avviso di test."""

from datetime import datetime, timezone
from io import StringIO

from rich.console import Console

from flight_scout.database import Database
from flight_scout.destination_engine import DestinationCatalog
from flight_scout.models import Leg, Offer, SearchStatus, Segment
from flight_scout.providers.offline import OFFLINE_BANNER
from flight_scout.reporting import collect, render_csv, render_html, render_terminal
from flight_scout.search_engine import ScanStats
from datetime import date
from decimal import Decimal

NOW = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)


def _leg(dep, arr, dep_t, arr_t, number):
    return Leg(
        segments=[
            Segment(
                departure_airport=dep,
                departure_local=datetime.fromisoformat(dep_t),
                departure_tz="Europe/Rome" if dep == "CAG" else "Europe/Madrid",
                arrival_airport=arr,
                arrival_local=datetime.fromisoformat(arr_t),
                arrival_tz="Europe/Madrid" if arr == "BCN" else "Europe/Rome",
                flight_number=number,
            )
        ],
        total_duration_minutes=120,
    )


def _verified(price="92.50", name="Barcellona"):
    return Offer(
        provider="serpapi",
        destination_iata="BCN",
        destination_name=name,
        destination_country="Spagna",
        outbound_date=date(2026, 10, 22),
        return_date=date(2026, 10, 26),
        price_eur=Decimal(price),
        status=SearchStatus.VERIFIED,
        outbound=_leg("CAG", "BCN", "2026-10-22 18:40", "2026-10-22 20:30", "TS1"),
        inbound=_leg("BCN", "CAG", "2026-10-26 12:00", "2026-10-26 14:10", "TS2"),
        observed_at=NOW,
        source_url="https://www.google.com/travel/flights/search?tfs=prova",
        baggage_info="bagaglio non indicato dal provider",
    )


def _indicative(price="105", dest="BCN"):
    return Offer(
        provider="serpapi",
        destination_iata=dest,
        destination_name="Barcellona",
        destination_country="Spagna",
        outbound_date=date(2026, 10, 22),
        return_date=date(2026, 10, 26),
        price_eur=Decimal(price),
        status=SearchStatus.INDICATIVE,
        observed_at=NOW,
        stops_hint=0,
        duration_hint_minutes=120,
    )


def _populate(db: Database, offers, offline: bool):
    scan = db.start_scan("deep", offline)
    stats = ScanStats(scan_id=scan, mode="deep", offline=offline, windows_total=10, windows_covered=4)
    for offer in offers:
        db.upsert_offer(offer, scan, offline)
    db.finish_scan(scan, "completed", stats.summary())
    return scan


def _collect(db, settings, criteria, offline=False):
    return collect(db, settings, criteria, DestinationCatalog.load(), offline=offline, now=NOW)


def test_csv_uses_semicolons_iso_dates_and_decimal_comma(tmp_path, settings, criteria):
    db = Database(tmp_path / "r.db")
    _populate(db, [_verified()], offline=False)
    text = render_csv(_collect(db, settings, criteria))
    assert text.startswith("﻿")  # BOM per Excel
    lines = text.strip("﻿").splitlines()
    assert lines[0].split(";")[0] == "categorie"
    row = lines[1].split(";")
    assert row[4] == "2026-10-22" and row[5] == "2026-10-26"
    assert row[7] == "92,50"
    assert row[13] == "VERIFIED"
    db.close()


def test_html_contains_sections_filters_and_escapes_untrusted_names(tmp_path, settings, criteria):
    db = Database(tmp_path / "h.db")
    _populate(db, [_verified(name="<script>alert(1)</script>")], offline=False)
    data = _collect(db, settings, criteria)
    html_text = render_html(data, NOW)
    assert "Migliori offerte economiche" in html_text
    assert "Destinazioni esotiche" in html_text
    assert 'id="f-price"' in html_text and 'id="f-status"' in html_text
    assert "<script>alert(1)</script>" not in html_text
    assert "&lt;script&gt;" in html_text
    db.close()


def test_offline_report_is_marked_as_test_data(tmp_path, settings, criteria):
    db = Database(tmp_path / "t.db")
    _populate(db, [_verified()], offline=True)
    data = _collect(db, settings, criteria, offline=True)
    assert OFFLINE_BANNER in render_html(data, NOW)
    out = StringIO()
    render_terminal(data, Console(file=out, width=160))
    assert OFFLINE_BANNER in out.getvalue()
    db.close()


def test_indicative_price_is_not_shown_as_verified(tmp_path, settings, criteria):
    db = Database(tmp_path / "i.db")
    _populate(db, [_indicative(price="90")], offline=False)
    data = _collect(db, settings, criteria)
    assert all(row.status == SearchStatus.INDICATIVE for row in data.rows)
    assert "INDICATIVE" in render_csv(data)
    db.close()


def test_indicative_rows_are_hidden_when_route_was_verified(tmp_path, settings, criteria):
    db = Database(tmp_path / "s.db")
    _populate(db, [_indicative(price="90"), _verified(price="95")], offline=False)
    data = _collect(db, settings, criteria)
    statuses = [row.status for row in data.rows]
    assert statuses == [SearchStatus.VERIFIED]
    db.close()


def test_missing_return_is_listed_as_missing_data(tmp_path, settings, criteria):
    db = Database(tmp_path / "m.db")
    incomplete = _verified().model_copy(update={"inbound": None, "status": SearchStatus.INDICATIVE})
    _populate(db, [incomplete], offline=False)
    data = _collect(db, settings, criteria)
    assert "segmento di ritorno" in render_csv(data)
    db.close()


def test_report_returns_none_without_completed_scan(tmp_path, settings, criteria):
    db = Database(tmp_path / "none.db")
    assert _collect(db, settings, criteria) is None
    db.close()
