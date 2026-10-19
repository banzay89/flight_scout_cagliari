"""Database SQLite locale: offerte, storico prezzi, scansioni, consumo API, notifiche e cache.

Le migrazioni usano PRAGMA user_version: ogni versione aggiunge uno step in MIGRATIONS.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from flight_scout.models import Offer

SCHEMA_V1 = """
CREATE TABLE scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT NOT NULL,
    offline INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    summary_json TEXT
);

CREATE TABLE offers (
    offer_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    destination_iata TEXT NOT NULL,
    outbound_date TEXT NOT NULL,
    return_date TEXT NOT NULL,
    nights INTEGER NOT NULL,
    price_eur TEXT NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    is_complete INTEGER NOT NULL,
    total_stops INTEGER,
    offer_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_scan_id INTEGER REFERENCES scans(id),
    offline INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_offers_route ON offers(destination_iata, outbound_date, return_date);
CREATE INDEX idx_offers_scan ON offers(last_scan_id);

CREATE TABLE price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_key TEXT NOT NULL REFERENCES offers(offer_key),
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    observed_at TEXT NOT NULL,
    price_eur TEXT NOT NULL,
    status TEXT NOT NULL,
    UNIQUE(offer_key, scan_id)
);
CREATE INDEX idx_history_offer ON price_history(offer_key, observed_at);

CREATE TABLE discovered_destinations (
    iata TEXT PRIMARY KEY,
    name TEXT,
    country TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    min_price_eur TEXT
);

CREATE TABLE api_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    provider TEXT NOT NULL,
    label TEXT NOT NULL,
    scan_id INTEGER,
    http_status INTEGER
);
CREATE INDEX idx_usage_provider_ts ON api_usage(provider, ts);

CREATE TABLE cache (
    cache_key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE skipped_searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    provider TEXT,
    label TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE scan_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    provider TEXT,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE notifications (
    dedupe_key TEXT PRIMARY KEY,
    event TEXT NOT NULL,
    channel TEXT NOT NULL,
    sent_at TEXT NOT NULL
);
"""

MIGRATIONS = {1: SCHEMA_V1}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class UpsertResult:
    is_new: bool
    previous_status: str | None
    previous_price: Decimal | None
    superseded_indicative: bool  # era presente un'offerta INDICATIVE per la stessa rotta


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        current = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for version in sorted(MIGRATIONS):
            if version > current:
                self.conn.executescript(MIGRATIONS[version])
                self.conn.execute(f"PRAGMA user_version = {int(version)}")
                self.conn.commit()

    @property
    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    # --- scansioni -------------------------------------------------------

    def start_scan(self, mode: str, offline: bool) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO scans (started_at, mode, offline, status) VALUES (?, ?, ?, 'running')",
                (_iso(utc_now()), mode, int(offline)),
            )
        return int(cursor.lastrowid)

    def finish_scan(self, scan_id: int, status: str, summary: dict) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE scans SET finished_at = ?, status = ?, summary_json = ? WHERE id = ?",
                (_iso(utc_now()), status, json.dumps(summary, ensure_ascii=False), scan_id),
            )

    def latest_scan(self, offline: bool) -> sqlite3.Row | None:
        """Ultima scansione conclusa (anche se fermata dal budget), separata per modalità live/test."""
        return self.conn.execute(
            "SELECT * FROM scans WHERE offline = ? AND status IN ('completed', 'budget_stopped') "
            "ORDER BY id DESC LIMIT 1",
            (int(offline),),
        ).fetchone()

    def log_error(self, scan_id: int, provider: str | None, message: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO scan_errors (scan_id, provider, message, created_at) VALUES (?, ?, ?, ?)",
                (scan_id, provider, message[:500], _iso(utc_now())),
            )

    def log_skip(self, scan_id: int, provider: str | None, label: str, reason: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO skipped_searches (scan_id, provider, label, reason) VALUES (?, ?, ?, ?)",
                (scan_id, provider, label, reason[:500]),
            )

    def skipped_for_scan(self, scan_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM skipped_searches WHERE scan_id = ? ORDER BY id", (scan_id,)
        ).fetchall()

    def errors_for_scan(self, scan_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM scan_errors WHERE scan_id = ? ORDER BY id", (scan_id,)
        ).fetchall()

    # --- offerte e storico ------------------------------------------------

    def upsert_offer(self, offer: Offer, scan_id: int, offline: bool) -> UpsertResult:
        key = offer.offer_key()
        now = _iso(utc_now())
        with self.conn:
            previous = self.conn.execute(
                "SELECT status, price_eur FROM offers WHERE offer_key = ?", (key,)
            ).fetchone()

            superseded = False
            if offer.status.value == "VERIFIED":
                # Un'offerta indicativa della stessa rotta (stessa data) viene aggiornata dalla verifica.
                superseded = self.conn.execute(
                    """SELECT 1 FROM offers WHERE destination_iata = ? AND outbound_date = ?
                       AND return_date = ? AND provider = ? AND status = 'INDICATIVE'""",
                    (
                        offer.destination_iata,
                        offer.outbound_date.isoformat(),
                        offer.return_date.isoformat(),
                        offer.provider,
                    ),
                ).fetchone() is not None

            payload = offer.model_dump_json()
            if previous is None:
                self.conn.execute(
                    """INSERT INTO offers (offer_key, provider, destination_iata, outbound_date, return_date,
                       nights, price_eur, currency, status, is_complete, total_stops, offer_json, observed_at,
                       first_seen_at, last_seen_at, last_scan_id, offline)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        key, offer.provider, offer.destination_iata,
                        offer.outbound_date.isoformat(), offer.return_date.isoformat(), offer.nights,
                        str(offer.price_eur), offer.currency, offer.status.value, int(offer.is_complete),
                        offer.total_stops, payload, _iso(offer.observed_at), now, now, scan_id, int(offline),
                    ),
                )
            else:
                self.conn.execute(
                    """UPDATE offers SET price_eur = ?, status = ?, is_complete = ?, total_stops = ?,
                       offer_json = ?, observed_at = ?, last_seen_at = ?, last_scan_id = ?
                       WHERE offer_key = ?""",
                    (
                        str(offer.price_eur), offer.status.value, int(offer.is_complete), offer.total_stops,
                        payload, _iso(offer.observed_at), now, scan_id, key,
                    ),
                )
            self.conn.execute(
                """INSERT OR IGNORE INTO price_history (offer_key, scan_id, observed_at, price_eur, status)
                   VALUES (?, ?, ?, ?, ?)""",
                (key, scan_id, _iso(offer.observed_at), str(offer.price_eur), offer.status.value),
            )

        return UpsertResult(
            is_new=previous is None,
            previous_status=previous["status"] if previous else None,
            previous_price=Decimal(previous["price_eur"]) if previous else None,
            superseded_indicative=superseded,
        )

    def offers_for_scan(self, scan_id: int) -> list[Offer]:
        rows = self.conn.execute(
            "SELECT offer_json FROM offers WHERE last_scan_id = ?", (scan_id,)
        ).fetchall()
        return [Offer.model_validate_json(row["offer_json"]) for row in rows]

    def price_history(self, offer_key: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT observed_at, price_eur, status FROM price_history WHERE offer_key = ? ORDER BY observed_at",
            (offer_key,),
        ).fetchall()

    def history_overview(self, limit: int) -> list[sqlite3.Row]:
        """Offerte con almeno due rilevazioni, con prezzo minimo e massimo osservati."""
        return self.conn.execute(
            """SELECT o.offer_key, o.destination_iata, o.outbound_date, o.return_date,
                      COUNT(h.id) AS observations,
                      MIN(CAST(h.price_eur AS REAL)) AS min_price,
                      MAX(CAST(h.price_eur AS REAL)) AS max_price
               FROM offers o JOIN price_history h ON h.offer_key = o.offer_key
               GROUP BY o.offer_key
               HAVING COUNT(h.id) >= 2
               ORDER BY o.outbound_date, o.destination_iata
               LIMIT ?""",
            (limit,),
        ).fetchall()

    # --- destinazioni scoperte ------------------------------------------

    def record_destination(self, iata: str, name: str | None, country: str | None, price: Decimal | None) -> bool:
        """Registra una destinazione. Restituisce True se è la prima volta che compare."""
        now = _iso(utc_now())
        with self.conn:
            row = self.conn.execute("SELECT min_price_eur FROM discovered_destinations WHERE iata = ?", (iata,)).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO discovered_destinations (iata, name, country, first_seen_at, last_seen_at, min_price_eur) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (iata, name, country, now, now, str(price) if price is not None else None),
                )
                return True
            new_min = row["min_price_eur"]
            if price is not None and (new_min is None or price < Decimal(new_min)):
                new_min = str(price)
            self.conn.execute(
                "UPDATE discovered_destinations SET last_seen_at = ?, min_price_eur = ?, name = COALESCE(?, name), "
                "country = COALESCE(?, country) WHERE iata = ?",
                (now, new_min, name, country, iata),
            )
            return False

    def discovered_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM discovered_destinations").fetchone()[0]

    # --- notifiche -------------------------------------------------------

    def notification_sent(self, dedupe_key: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM notifications WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone() is not None

    def mark_notification(self, dedupe_key: str, event: str, channel: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO notifications (dedupe_key, event, channel, sent_at) VALUES (?, ?, ?, ?)",
                (dedupe_key, event, channel, _iso(utc_now())),
            )

    # --- consumo API -----------------------------------------------------

    def usage_start(self, provider: str, label: str, scan_id: int | None) -> int:
        """Registra una chiamata PRIMA di inviarla: anche se il programma si interrompe, il consumo resta contato."""
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO api_usage (ts, provider, label, scan_id, http_status) VALUES (?, ?, ?, ?, NULL)",
                (_iso(utc_now()), provider, label, scan_id),
            )
        return int(cursor.lastrowid)

    def usage_finish(self, usage_id: int, http_status: int | None) -> None:
        with self.conn:
            self.conn.execute("UPDATE api_usage SET http_status = ? WHERE id = ?", (http_status, usage_id))

    def usage_in_month(self, provider: str, month_start: datetime) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM api_usage WHERE provider = ? AND ts >= ?",
            (provider, _iso(month_start)),
        ).fetchone()[0]

    def usage_since(self, provider: str, since: datetime) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM api_usage WHERE provider = ? AND ts >= ?",
            (provider, _iso(since)),
        ).fetchone()[0]

    # --- cache -----------------------------------------------------------

    def cache_get(self, cache_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT created_at, payload FROM cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()

    def cache_put(self, cache_key: str, payload: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO cache (cache_key, created_at, payload) VALUES (?, ?, ?)",
                (cache_key, _iso(utc_now()), payload),
            )


def month_start(now: datetime | None = None) -> datetime:
    now = now or utc_now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def hours_ago(hours: float) -> datetime:
    return utc_now() - timedelta(hours=hours)
