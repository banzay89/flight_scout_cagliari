"""Cache persistente delle risposte API, con scadenza.

Una risposta già ottenuta non consuma budget. Il programma può quindi essere interrotto e
riavviato senza pagare di nuovo le stesse ricerche.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from flight_scout.database import Database


def make_cache_key(provider: str, label: str, params: dict[str, Any]) -> str:
    """Chiave deterministica. I parametri non devono contenere segreti (es. api_key)."""
    import hashlib

    canonical = json.dumps([provider, label, sorted(params.items())], sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ResponseCache:
    def __init__(self, db: Database, ttl_hours: int):
        self.db = db
        self.ttl = timedelta(hours=ttl_hours)

    def get(self, key: str) -> dict | None:
        row = self.db.cache_get(key)
        if row is None:
            return None
        created = datetime.fromisoformat(row["created_at"])
        if datetime.now(timezone.utc) - created > self.ttl:
            return None
        return json.loads(row["payload"])

    def has(self, key: str) -> bool:
        return self.get(key) is not None

    def set(self, key: str, payload: dict) -> None:
        self.db.cache_put(key, json.dumps(payload, ensure_ascii=False))
