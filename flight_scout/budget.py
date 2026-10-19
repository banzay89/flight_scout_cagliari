"""Controllo del budget API: limite per esecuzione, mensile e orario (throughput del provider)."""

from __future__ import annotations

from flight_scout.database import Database, hours_ago, month_start


class BudgetExceeded(Exception):
    """Il budget (per esecuzione, mensile o orario) non consente altre chiamate."""


class ApiBudget:
    """Budget reale: ogni chiamata di rete viene registrata nel database prima di essere inviata."""

    def __init__(
        self,
        db: Database,
        provider: str,
        run_limit: int,
        monthly_limit: int,
        hourly_limit: int,
        scan_id: int | None,
    ):
        self.db = db
        self.provider = provider
        self.run_limit = run_limit
        self.monthly_limit = monthly_limit
        self.hourly_limit = hourly_limit
        self.scan_id = scan_id
        self.spent = 0  # chiamate di rete tentate in questa esecuzione (i retry contano)

    @property
    def remaining_run(self) -> int:
        return max(0, self.run_limit - self.spent)

    @property
    def monthly_used(self) -> int:
        return self.db.usage_in_month(self.provider, month_start())

    @property
    def remaining_month(self) -> int:
        return max(0, self.monthly_limit - self.monthly_used)

    def check(self) -> None:
        if self.spent >= self.run_limit:
            raise BudgetExceeded(f"budget di questa esecuzione esaurito ({self.run_limit} chiamate)")
        if self.monthly_used >= self.monthly_limit:
            raise BudgetExceeded(f"budget mensile esaurito ({self.monthly_limit} chiamate)")
        if self.db.usage_since(self.provider, hours_ago(1)) >= self.hourly_limit:
            raise BudgetExceeded(f"limite orario del provider raggiunto ({self.hourly_limit} chiamate/ora)")

    def consume(self, label: str) -> int:
        """Prenota una chiamata. Restituisce l'id del registro da completare con finish()."""
        self.check()
        self.spent += 1
        return self.db.usage_start(self.provider, label, self.scan_id)

    def finish(self, usage_id: int, http_status: int | None) -> None:
        self.db.usage_finish(usage_id, http_status)


class NullBudget:
    """Modalità offline: stesso limite per esecuzione, nessuna registrazione né limite mensile/orario."""

    def __init__(self, run_limit: int):
        self.run_limit = run_limit
        self.spent = 0

    @property
    def remaining_run(self) -> int:
        return max(0, self.run_limit - self.spent)

    def check(self) -> None:
        if self.spent >= self.run_limit:
            raise BudgetExceeded(f"budget di questa esecuzione esaurito ({self.run_limit} chiamate)")

    def consume(self, label: str) -> int:
        self.check()
        self.spent += 1
        return 0

    def finish(self, usage_id: int, http_status: int | None) -> None:
        return None
