"""Client HTTP per SerpApi: cache, budget, retry con backoff ed errori HTTP gestiti.

Non registra mai la chiave API: nei log e nelle eccezioni compare solo il tipo di errore.
"""

from __future__ import annotations

import time
from typing import Callable

import httpx

from flight_scout.cache import ResponseCache, make_cache_key
from flight_scout.providers.base import ProviderAuthError, ProviderError


class SerpApiClient:
    BASE_URL = "https://serpapi.com/search.json"
    PROVIDER = "serpapi"
    MAX_ATTEMPTS = 3

    def __init__(
        self,
        *,
        api_key: str,
        http: httpx.Client,
        budget,
        cache: ResponseCache | None,
        sleep: Callable[[float], None] = time.sleep,
        pause_seconds: float = 1.0,
    ):
        self.api_key = api_key
        self.http = http
        self.budget = budget
        self.cache = cache
        self.sleep = sleep
        self.pause_seconds = pause_seconds
        self._first_call = True

    @staticmethod
    def clean(params: dict) -> dict:
        return {k: v for k, v in params.items() if v is not None}

    def key_for(self, label: str, params: dict) -> str:
        return make_cache_key(self.PROVIDER, label, self.clean(params))

    def search(self, label: str, params: dict) -> dict:
        clean = self.clean(params)
        key = make_cache_key(self.PROVIDER, label, clean)
        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                return cached  # nessuna chiamata di rete: non consuma budget

        last_error: ProviderError | None = None
        for attempt in range(self.MAX_ATTEMPTS):
            usage_id = self.budget.consume(label)
            if not self._first_call and self.pause_seconds:
                self.sleep(self.pause_seconds)  # evita raffiche di richieste
            self._first_call = False
            try:
                response = self.http.get(self.BASE_URL, params={**clean, "api_key": self.api_key})
            except httpx.HTTPError as exc:
                self.budget.finish(usage_id, None)
                last_error = ProviderError(f"errore di rete ({type(exc).__name__})")
                self.sleep(2**attempt)
                continue
            self.budget.finish(usage_id, response.status_code)

            status = response.status_code
            if status in (401, 403):
                raise ProviderAuthError(f"HTTP {status}: chiave API non valida o piano non abilitato")
            if status == 404:
                raise ProviderError("HTTP 404: risorsa o endpoint non trovato")
            if status == 429:
                last_error = ProviderError("HTTP 429: limite di richieste raggiunto")
                self.sleep(2**attempt)
                continue
            if status >= 500:
                last_error = ProviderError(f"HTTP {status}: errore temporaneo del provider")
                self.sleep(2**attempt)
                continue
            if status != 200:
                raise ProviderError(f"HTTP {status} inatteso")

            return self._parse_body(response, key)

        raise last_error or ProviderError("ricerca non riuscita dopo i tentativi previsti")

    def _parse_body(self, response: httpx.Response, key: str) -> dict:
        try:
            data = response.json()
        except ValueError:
            raise ProviderError("risposta non in formato JSON valido") from None
        if not isinstance(data, dict):
            raise ProviderError("risposta con struttura inattesa")
        if "error" in data:
            message = str(data["error"])
            if "hasn't returned any results" in message.lower() or "no results" in message.lower():
                data = {}  # nessun risultato per queste date: non è un errore
            else:
                raise ProviderError(f"il provider ha segnalato un errore: {message[:200]}")
        if self.cache is not None:
            self.cache.set(key, data)
        return data
