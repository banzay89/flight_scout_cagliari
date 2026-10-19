"""Adapter Amadeus: predisposto ma DISABILITATO.

Motivo (vedi docs/API_RESEARCH.md): le fonti consultate indicano che il portale Self-Service
di Amadeus sarebbe stato chiuso a luglio 2026, e l'accesso restante richiede un contratto
commerciale. Non è stato possibile verificarlo direttamente sul sito ufficiale.

Finché non viene verificato un accesso reale, questo adapter non effettua mai richieste.
"""

from __future__ import annotations


class AmadeusProvider:
    name = "amadeus"

    def __init__(self, client_id: str | None, client_secret: str | None):
        self._credentials_present = bool(client_id and client_secret)

    def is_configured(self) -> bool:
        return False  # disabilitato per scelta: vedi docstring

    def status_message(self) -> str:
        if self._credentials_present:
            return "credenziali presenti, ma adapter disabilitato: accesso non verificato"
        return "non attivo: accesso non verificato, vedi docs/API_RESEARCH.md"
