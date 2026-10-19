"""Database: migrazioni, salvataggio e recupero, duplicati, storico prezzi, scansioni interrotte."""

from datetime import date, datetime, timezone
from decimal import Decimal

from flight_scout.database import Database
from flight_scout.models import Offer, SearchStatus

NOW = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)


def _offer(price: str, status=SearchStatus.INDICATIVE, observed=NOW, dest="BCN") -> Offer:
    return Offer(
        provider="serpapi",
        destination_iata=dest,
        destination_name="Barcellona",
        destination_country="Spagna",
        outbound_date=date(2026, 10, 22),
        return_date=date(2026, 10, 26),
        price_eur=Decimal(price),
        status=status,
        observed_at=observed,
    )


def test_schema_is_created_automatically_and_version_recorded(tmp_path):
    db = Database(tmp_path / "new" / "flights.db")
    assert db.schema_version == 1
    tables = {row[0] for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"scans", "offers", "price_history", "api_usage", "cache", "notifications"} <= tables
    db.close()


def test_reopening_database_does_not_repeat_migrations(tmp_path):
    path = tmp_path / "again.db"
    Database(path).close()
    db = Database(path)
    assert db.schema_version == 1
    db.close()


def test_same_offer_saved_twice_is_one_row_with_two_history_points(tmp_path):
    db = Database(tmp_path / "d.db")
    scan_a = db.start_scan("deep", offline=False)
    scan_b = db.start_scan("deep", offline=False)
    first = db.upsert_offer(_offer("90"), scan_a, offline=False)
    second = db.upsert_offer(_offer("85"), scan_b, offline=False)
    assert first.is_new and not second.is_new
    assert second.previous_price == Decimal("90")
    rows = db.conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
    history = db.conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    assert rows == 1
    assert history == 2
    db.close()


def test_history_point_is_not_duplicated_within_one_scan(tmp_path):
    db = Database(tmp_path / "dup.db")
    scan = db.start_scan("deep", offline=False)
    db.upsert_offer(_offer("90"), scan, offline=False)
    db.upsert_offer(_offer("90"), scan, offline=False)
    assert db.conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 1
    db.close()


def test_verified_offer_reports_it_superseded_an_indicative_one(tmp_path):
    from flight_scout.models import Leg, Segment

    db = Database(tmp_path / "v.db")
    scan = db.start_scan("deep", offline=False)
    db.upsert_offer(_offer("90"), scan, offline=False)
    leg = Leg(
        segments=[
            Segment(
                departure_airport="CAG",
                departure_local=datetime(2026, 10, 22, 18, 40),
                departure_tz="Europe/Rome",
                arrival_airport="BCN",
                arrival_local=datetime(2026, 10, 22, 20, 30),
                arrival_tz="Europe/Madrid",
                flight_number="TS1",
            )
        ],
        total_duration_minutes=110,
    )
    back = Leg(
        segments=[
            Segment(
                departure_airport="BCN",
                departure_local=datetime(2026, 10, 26, 12, 0),
                departure_tz="Europe/Madrid",
                arrival_airport="CAG",
                arrival_local=datetime(2026, 10, 26, 14, 10),
                arrival_tz="Europe/Rome",
                flight_number="TS2",
            )
        ],
        total_duration_minutes=130,
    )
    verified = _offer("95", SearchStatus.VERIFIED).model_copy(update={"outbound": leg, "inbound": back})
    result = db.upsert_offer(verified, scan, offline=False)
    assert result.superseded_indicative is True
    db.close()


def test_new_destination_is_recorded_once(tmp_path):
    db = Database(tmp_path / "dest.db")
    assert db.record_destination("BCN", "Barcellona", "Spagna", Decimal("90")) is True
    assert db.record_destination("BCN", "Barcellona", "Spagna", Decimal("70")) is False
    assert db.discovered_count() == 1
    db.close()


def test_notification_dedupe_key_is_stored_once(tmp_path):
    db = Database(tmp_path / "n.db")
    assert not db.notification_sent("EVT|x|90")
    db.mark_notification("EVT|x|90", "EVT", "telegram")
    db.mark_notification("EVT|x|90", "EVT", "telegram")
    assert db.notification_sent("EVT|x|90")
    assert db.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1
    db.close()


def test_interrupted_scan_keeps_saved_offers_and_status(tmp_path):
    db = Database(tmp_path / "int.db")
    scan = db.start_scan("deep", offline=False)
    db.upsert_offer(_offer("90"), scan, offline=False)
    db.finish_scan(scan, "interrupted", {"api_calls": 3})
    status = db.conn.execute("SELECT status FROM scans WHERE id = ?", (scan,)).fetchone()[0]
    assert status == "interrupted"
    assert len(db.offers_for_scan(scan)) == 1
    assert db.latest_scan(offline=False) is None  # una scansione interrotta non è un report completo
    db.close()


def test_price_history_overview_requires_two_observations(tmp_path):
    db = Database(tmp_path / "hist.db")
    s1 = db.start_scan("deep", offline=False)
    s2 = db.start_scan("deep", offline=False)
    db.upsert_offer(_offer("90"), s1, offline=False)
    assert db.history_overview(10) == []
    db.upsert_offer(_offer("80"), s2, offline=False)
    rows = db.history_overview(10)
    assert len(rows) == 1
    assert rows[0]["min_price"] == 80 and rows[0]["max_price"] == 90
    db.close()
