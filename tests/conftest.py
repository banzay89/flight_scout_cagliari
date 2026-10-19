"""Fixture condivise. I test non usano mai credenziali reali né la rete."""

from __future__ import annotations

import socket

import pytest

from flight_scout.config import load_settings
from flight_scout.destination_engine import DestinationCatalog
from flight_scout.ranking import Criteria


@pytest.fixture
def make_settings(tmp_path):
    """Crea impostazioni isolate: database e report in una cartella temporanea."""

    def _make(**env: str):
        values = {
            "DATABASE_PATH": str(tmp_path / "test.db"),
            "REPORT_DIRECTORY": str(tmp_path / "reports"),
        }
        values.update(env)
        return load_settings(env_file=tmp_path / "missing.env", environ=values)

    return _make


@pytest.fixture
def settings(make_settings):
    return make_settings()


@pytest.fixture
def catalog():
    return DestinationCatalog.load()


@pytest.fixture
def criteria():
    return Criteria.load()


@pytest.fixture
def no_network(monkeypatch):
    """Qualsiasi tentativo di connessione reale fa fallire il test."""

    def _blocked(*_args, **_kwargs):
        raise AssertionError("connessione di rete non consentita durante il test")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
