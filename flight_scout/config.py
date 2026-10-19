"""Caricamento e validazione della configurazione (file .env e variabili d'ambiente)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"

# Valori che non devono mai comparire in log, report o messaggi.
SECRET_FIELDS = (
    "serpapi_api_key",
    "amadeus_client_id",
    "amadeus_client_secret",
    "telegram_bot_token",
    "smtp_password",
)

# Mappa della classe di viaggio verso il parametro "travel_class" di Google Flights.
CABIN_TRAVEL_CLASS = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}


class ConfigError(Exception):
    """Configurazione non valida. Il messaggio è pensato per l'utente e non contiene segreti."""


class Settings(BaseModel):
    """Parametri del programma. I nomi di variabile ricalcano quelli del file .env."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    origin_airport: str = Field("CAG", alias="ORIGIN_AIRPORT")
    currency: str = Field("EUR", alias="CURRENCY")
    passengers: int = Field(1, alias="PASSENGERS", ge=1, le=9)
    cabin_class: str = Field("economy", alias="CABIN_CLASS")
    max_standard_price_eur: Decimal = Field(Decimal("100"), alias="MAX_STANDARD_PRICE_EUR", gt=0)
    exotic_max_price_eur: Decimal | None = Field(None, alias="EXOTIC_MAX_PRICE_EUR", gt=0)
    search_horizon_days: int = Field(180, alias="SEARCH_HORIZON_DAYS", ge=7, le=365)
    min_nights: int = Field(1, alias="MIN_NIGHTS", ge=1, le=7)
    max_nights: int = Field(4, alias="MAX_NIGHTS", ge=1, le=7)
    outbound_thursday_earliest: time = Field(time(17, 0), alias="OUTBOUND_THURSDAY_EARLIEST")
    return_monday_latest: time = Field(time(23, 0), alias="RETURN_MONDAY_LATEST")
    timezone: str = Field("Europe/Rome", alias="TIMEZONE")
    api_run_budget: int = Field(25, alias="API_RUN_BUDGET", ge=0)
    api_monthly_budget: int = Field(200, alias="API_MONTHLY_BUDGET", ge=0)
    max_destinations_to_verify: int = Field(10, alias="MAX_DESTINATIONS_TO_VERIFY", ge=0)
    cache_ttl_hours: int = Field(24, alias="CACHE_TTL_HOURS", ge=1)
    display_limit: int = Field(30, alias="DISPLAY_LIMIT", ge=1)
    discovery_share: float = Field(0.6, alias="DISCOVERY_SHARE", gt=0, lt=1)
    database_path: Path = Field(Path("data/flights.db"), alias="DATABASE_PATH")
    report_directory: Path = Field(Path("reports"), alias="REPORT_DIRECTORY")

    serpapi_api_key: str | None = Field(None, alias="SERPAPI_API_KEY")
    amadeus_client_id: str | None = Field(None, alias="AMADEUS_CLIENT_ID")
    amadeus_client_secret: str | None = Field(None, alias="AMADEUS_CLIENT_SECRET")

    telegram_bot_token: str | None = Field(None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(None, alias="TELEGRAM_CHAT_ID")

    smtp_host: str | None = Field(None, alias="SMTP_HOST")
    smtp_port: int = Field(587, alias="SMTP_PORT", ge=1, le=65535)
    smtp_username: str | None = Field(None, alias="SMTP_USERNAME")
    smtp_password: str | None = Field(None, alias="SMTP_PASSWORD")
    email_to: str | None = Field(None, alias="EMAIL_TO")

    @field_validator("origin_airport")
    @classmethod
    def _origin_is_iata(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 3 or not value.isalpha():
            raise ValueError("deve essere un codice IATA di 3 lettere, ad esempio CAG")
        return value

    @field_validator("currency")
    @classmethod
    def _currency_code(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 3 or not value.isalpha():
            raise ValueError("deve essere un codice valuta di 3 lettere, ad esempio EUR")
        return value

    @field_validator("timezone")
    @classmethod
    def _timezone_exists(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("fuso orario IANA sconosciuto, ad esempio Europe/Rome") from None
        return value

    @field_validator("cabin_class")
    @classmethod
    def _cabin_known(cls, value: str) -> str:
        if value not in CABIN_TRAVEL_CLASS:
            raise ValueError(f"valori ammessi: {', '.join(CABIN_TRAVEL_CLASS)}")
        return value

    @model_validator(mode="after")
    def _nights_order(self) -> "Settings":
        if self.max_nights < self.min_nights:
            raise ValueError("MAX_NIGHTS non può essere minore di MIN_NIGHTS")
        return self

    @property
    def database_file(self) -> Path:
        return _resolve(self.database_path)

    @property
    def report_dir(self) -> Path:
        return _resolve(self.report_directory)

    @property
    def logs_dir(self) -> Path:
        return PROJECT_ROOT / "logs"

    @property
    def travel_class(self) -> int:
        return CABIN_TRAVEL_CLASS[self.cabin_class]


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path


_ENV_NAMES = frozenset(field.alias for field in Settings.model_fields.values() if field.alias)


def load_settings(
    env_file: Path = DEFAULT_ENV_FILE,
    environ: dict[str, str] | None = None,
) -> Settings:
    """Legge il file .env e poi le variabili d'ambiente (che hanno la precedenza)."""
    values: dict[str, str] = {}
    if env_file.exists():
        values.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    source = dict(os.environ) if environ is None else environ
    values.update({k: v for k, v in source.items() if k in _ENV_NAMES})
    # Le righe vuote equivalgono a "non impostato": si usa il valore predefinito.
    cleaned = {k: v.strip() for k, v in values.items() if v is not None and v.strip() != ""}
    try:
        return Settings.model_validate(cleaned)
    except ValidationError as exc:
        raise ConfigError(_friendly_errors(exc)) from None


def _friendly_errors(exc: ValidationError) -> str:
    lines = ["Configurazione non valida. Correggi il file .env:"]
    for error in exc.errors():
        alias = str(error["loc"][0]) if error["loc"] else "?"
        # Il valore inserito non viene mostrato: potrebbe essere un segreto.
        lines.append(f"  - {alias}: {error['msg']}")
    return "\n".join(lines)


@dataclass(frozen=True)
class Check:
    level: str  # "ok", "info", "warn"
    message: str


def diagnose(settings: Settings) -> list[Check]:
    """Controlli di configurazione senza mostrare alcun segreto."""
    checks: list[Check] = []
    if settings.serpapi_api_key:
        checks.append(Check("ok", "SERPAPI_API_KEY presente (valore non mostrato)"))
    else:
        checks.append(
            Check(
                "warn",
                "SERPAPI_API_KEY mancante: le ricerche live non sono disponibili. "
                "Dry-run e modalità offline funzionano comunque.",
            )
        )
    if settings.amadeus_client_id and settings.amadeus_client_secret:
        checks.append(Check("info", "Amadeus: credenziali presenti, ma l'adapter è disabilitato (vedi docs/API_RESEARCH.md)"))
    else:
        checks.append(Check("info", "Amadeus non configurato (non necessario)"))
    if settings.telegram_bot_token and settings.telegram_chat_id:
        checks.append(Check("ok", "Notifiche Telegram configurate"))
    else:
        checks.append(Check("info", "Notifiche Telegram disattivate"))
    if settings.smtp_host and settings.smtp_username and settings.smtp_password and settings.email_to:
        checks.append(Check("ok", "Notifiche e-mail configurate"))
    else:
        checks.append(Check("info", "Notifiche e-mail disattivate"))
    if settings.api_monthly_budget > 250:
        checks.append(
            Check(
                "warn",
                f"API_MONTHLY_BUDGET={settings.api_monthly_budget} supera le 250 ricerche/mese "
                "del piano gratuito SerpApi: verifica il piano attivo.",
            )
        )
    if settings.api_run_budget > settings.api_monthly_budget:
        checks.append(Check("warn", "API_RUN_BUDGET è maggiore di API_MONTHLY_BUDGET: il limite mensile prevale."))
    return checks


def redact(text: str, settings: Settings) -> str:
    """Sostituisce i valori segreti presenti nel testo con una maschera."""
    for name in SECRET_FIELDS:
        secret = getattr(settings, name)
        if secret:
            text = text.replace(secret, "***")
    return text
