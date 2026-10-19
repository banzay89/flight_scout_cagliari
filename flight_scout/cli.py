"""Interfaccia da terminale. Esempi: python -m flight_scout doctor | scan --dry-run | scan --offline"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from rich.console import Console
from rich.table import Table

from flight_scout import __version__
from flight_scout.budget import ApiBudget
from flight_scout.config import PROJECT_ROOT, ConfigError, Settings, diagnose, load_settings, redact
from flight_scout.database import Database
from flight_scout.destination_engine import DEFAULT_CATALOG, DestinationCatalog
from flight_scout.notifications import budget_notice, build_notices, dispatch, enabled_senders
from flight_scout.providers.amadeus import AmadeusProvider
from flight_scout.providers.base import ProviderUnavailable
from flight_scout.ranking import DEFAULT_CRITERIA, Criteria
from flight_scout.reporting import collect, render_terminal, write_reports
from flight_scout.search_engine import plan_scan, run_scan

console = Console()
err_console = Console(stderr=True)
OFFLINE_DB_NAME = "offline_test.db"


class _RedactFilter(logging.Filter):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(str(record.getMessage()), self.settings)
        record.args = ()
        return True


def setup_logging(settings: Settings, verbose: bool) -> None:
    """I log vanno su file in logs/ (senza segreti). Su schermo solo con --verbose."""
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("flight_scout")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    file_handler = logging.FileHandler(settings.logs_dir / "flight_scout.log", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.addFilter(_RedactFilter(settings))
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(file_handler)
    if verbose:
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(logging.DEBUG)
        stream.addFilter(_RedactFilter(settings))
        logger.addHandler(stream)


def _offline_db_path(settings: Settings) -> Path:
    return settings.database_file.with_name(OFFLINE_DB_NAME)


def _load_criteria() -> Criteria:
    return Criteria.load(DEFAULT_CRITERIA)


def cmd_doctor(settings: Settings) -> int:
    console.rule("Diagnostica Flight Scout Cagliari")
    console.print(f"Versione programma: {__version__}")
    py = sys.version_info
    py_ok = py >= (3, 12)
    console.print(f"[{'green' if py_ok else 'red'}]Python {py.major}.{py.minor}.{py.micro}[/] (richiesto 3.12 o superiore)")
    try:
        ZoneInfo(settings.timezone)
        console.print(f"[green]Fuso orario {settings.timezone} disponibile[/]")
    except Exception:
        console.print("[red]Fuso orario non disponibile: installa il pacchetto tzdata[/]")
        return 1
    console.print(f"File .env: {'presente' if (PROJECT_ROOT / '.env').exists() else 'assente (uso valori predefiniti)'}")
    console.print("Configurazione: [green]valida[/]")

    failures = 0
    try:
        _load_criteria()
        console.print(f"[green]Criteri caricati[/] ({DEFAULT_CRITERIA.name})")
    except Exception as exc:
        console.print(f"[red]Criteri non leggibili: {type(exc).__name__}[/]")
        failures += 1
    try:
        catalog = DestinationCatalog.load(DEFAULT_CATALOG)
        console.print(f"[green]Catalogo destinazioni caricato[/]: {len(catalog)} aeroporti")
    except Exception as exc:
        console.print(f"[red]Catalogo non leggibile: {type(exc).__name__}[/]")
        failures += 1

    try:
        db = Database(settings.database_file)
        console.print(f"[green]Database OK[/]: {settings.database_file} (schema v{db.schema_version})")
        db.close()
    except Exception as exc:
        console.print(f"[red]Database non utilizzabile: {type(exc).__name__}[/]")
        failures += 1

    console.print("\n[bold]Provider[/]")
    if settings.serpapi_api_key:
        console.print("  SerpApi: [green]chiave presente[/] (valore non mostrato)")
    else:
        console.print("  SerpApi: [yellow]chiave mancante[/]: imposta SERPAPI_API_KEY nel file .env per le ricerche live")
    amadeus = AmadeusProvider(settings.amadeus_client_id, settings.amadeus_client_secret)
    console.print(f"  Amadeus: {amadeus.status_message()}")

    console.print("\n[bold]Controlli di configurazione[/]")
    for check in diagnose(settings):
        mark = {"ok": "[green]OK[/]", "info": "[dim]info[/]", "warn": "[yellow]attenzione[/]"}[check.level]
        console.print(f"  {mark} {check.message}")

    console.print("\nModalità di prova senza costi: [bold]python -m flight_scout scan --offline[/]")
    console.print("Pianificazione senza chiamate: [bold]python -m flight_scout scan --dry-run[/]")
    return 1 if failures else 0


def cmd_config(settings: Settings) -> int:
    table = Table(title="Parametri di ricerca (valori non riservati)")
    table.add_column("Parametro")
    table.add_column("Valore")
    for name in Settings.model_fields:
        if name in ("serpapi_api_key", "amadeus_client_id", "amadeus_client_secret", "telegram_bot_token", "smtp_password", "telegram_chat_id", "smtp_username", "email_to", "smtp_host"):
            continue
        value = getattr(settings, name)
        table.add_row(name.upper(), str(value))
    console.print(table)
    console.print("Segreti (SERPAPI_API_KEY, token, password): " + ", ".join(
        f"{label} {'presente' if getattr(settings, field) else 'mancante'}"
        for field, label in (("serpapi_api_key", "SerpApi"), ("telegram_bot_token", "Telegram"), ("smtp_password", "SMTP"))
    ))
    return 0


def cmd_scan(args: argparse.Namespace, settings: Settings) -> int:
    if not (args.dry_run or args.offline or args.live):
        err_console.print(
            "Nessuna modalità scelta. Usa uno di questi comandi:\n"
            "  python -m flight_scout scan --dry-run        (pianifica, nessuna chiamata)\n"
            "  python -m flight_scout scan --offline        (dati di test, nessuna rete)\n"
            "  python -m flight_scout scan --live --max-api-calls 1   (ricerca reale limitata)"
        )
        return 2

    criteria = _load_criteria()
    catalog = DestinationCatalog.load(DEFAULT_CATALOG)
    mode = "quick" if args.quick else "deep"
    db_path = _offline_db_path(settings) if args.offline else settings.database_file
    db = Database(db_path)
    try:
        if args.dry_run:
            return _dry_run(settings, criteria, catalog, db, mode, args.max_api_calls)

        if args.live and not settings.serpapi_api_key:
            err_console.print("SERPAPI_API_KEY non impostata nel file .env: la ricerca live non può partire.")
            return 2

        stats = run_scan(
            settings,
            criteria,
            db,
            catalog,
            mode=mode,
            offline=args.offline,
            max_api_calls=args.max_api_calls,
        )
        _print_scan_summary(stats, settings)

        if args.live and not args.no_notify:
            _notify(stats, settings, criteria, catalog, db)

        data = collect(db, settings, criteria, catalog, offline=args.offline)
        if data is not None:
            paths = write_reports(data, settings)
            console.print(f"Report aggiornati: {paths['latest_html']}")
            console.print(f"Esportazione CSV: {paths['latest_csv']}")
        return 0
    except KeyboardInterrupt:
        console.print(
            "\n[yellow]Interrotto.[/] I dati già salvati restano nel database. "
            "Riavvia la scansione: la cache evita di pagare di nuovo le stesse ricerche."
        )
        return 130
    except ProviderUnavailable as exc:
        err_console.print(str(exc))
        return 2
    finally:
        db.close()


def _dry_run(settings: Settings, criteria: Criteria, catalog: DestinationCatalog, db: Database, mode: str, max_calls: int | None) -> int:
    plan = plan_scan(settings, criteria, catalog, db, mode=mode, max_api_calls=max_calls)
    budget_view = ApiBudget(db, "serpapi", plan.run_limit, settings.api_monthly_budget, criteria.serpapi_hourly_limit, None)
    verify_calls = min(settings.max_destinations_to_verify, max(0, (plan.run_limit - plan.expected_discovery_calls) // 2)) * 2
    expected = plan.expected_discovery_calls + verify_calls

    console.rule("Pianificazione (dry-run: nessuna richiesta di rete)")
    console.print(f"Modalità: {mode}   Orizzonte: {settings.search_horizon_days} giorni   Permanenza: {settings.min_nights}-{settings.max_nights} notti")
    console.print(f"Finestre di viaggio generate: {plan.windows_total}")
    table = Table(title="Stima del consumo API")
    table.add_column("Voce")
    table.add_column("Valore", justify="right")
    table.add_row("Limite per questa esecuzione", str(plan.run_limit))
    table.add_row("Ricerche esplorative pianificate", str(min(len(plan.discovery), plan.discovery_limit)))
    table.add_row("…di cui già in cache (gratis)", str(plan.cached_discovery))
    table.add_row("Verifiche previste (2 chiamate per destinazione)", str(verify_calls))
    table.add_row("Chiamate previste (stima massima)", str(expected))
    table.add_row("Budget mensile residuo", f"{budget_view.remaining_month} su {settings.api_monthly_budget}")
    console.print(table)
    sample = plan.discovery[: min(len(plan.discovery), plan.discovery_limit)][:8]
    if sample:
        console.print("Prime ricerche previste: " + ", ".join(f"{w.outbound_date} {w.pattern}" for w in sample))
    not_covered = max(0, plan.windows_total - min(len(plan.discovery), plan.discovery_limit))
    console.print(f"Finestre senza esplorazione in questa esecuzione: circa {not_covered} (vedi report, sezione copertura)")
    return 0


def _print_scan_summary(stats, settings: Settings) -> None:
    table = Table(title=f"Scansione {stats.scan_id} ({'offline - DATI DI TEST' if stats.offline else 'live'})")
    table.add_column("Voce")
    table.add_column("Valore", justify="right")
    table.add_row("Stato", "budget esaurito" if stats.budget_stopped else "completata")
    table.add_row("Chiamate API effettuate", str(stats.api_calls))
    table.add_row("Finestre esplorate", f"{stats.windows_covered}/{stats.windows_total}")
    table.add_row("Destinazioni trovate (totale storico)", str(stats.destinations_found))
    table.add_row("Candidati alla verifica", str(stats.candidates))
    table.add_row("Offerte verificate", str(stats.verified))
    table.add_row("Prezzi indicativi", str(stats.indicative))
    table.add_row("Scartate (orari/incomplete)", str(stats.rejected))
    table.add_row("Errori", str(len(stats.errors)))
    console.print(table)
    if stats.stop_reason:
        console.print(f"[yellow]Motivo interruzione dei controlli:[/] {stats.stop_reason}")
    if stats.errors:
        for message in stats.errors[:5]:
            console.print(f"  [red]-[/] {redact(message, settings)}")


def _notify(stats, settings: Settings, criteria: Criteria, catalog: DestinationCatalog, db: Database) -> None:
    with httpx.Client(timeout=20.0) as http:
        senders = enabled_senders(settings, http)
        if not senders:
            return
        notices = build_notices(stats.changes, settings, criteria, catalog)
        month_label = datetime.now(timezone.utc).strftime("%Y-%m")
        limit_notice = budget_notice(stats, settings, month_label)
        if limit_notice:
            notices.append(limit_notice)
        sent, problems = dispatch(db, notices, senders)
    console.print(f"Notifiche inviate: {sent}")
    for problem in problems:
        console.print(f"[yellow]Notifica non inviata:[/] {problem}")


def cmd_report(args: argparse.Namespace, settings: Settings) -> int:
    criteria = _load_criteria()
    catalog = DestinationCatalog.load(DEFAULT_CATALOG)
    db = Database(_offline_db_path(settings) if args.offline else settings.database_file)
    try:
        data = collect(db, settings, criteria, catalog, offline=args.offline)
        if data is None:
            console.print("Nessuna scansione conclusa: esegui prima 'scan --dry-run' o 'scan --offline'.")
            return 1
        if args.format in ("terminal", "all"):
            render_terminal(data, console)
        if args.format in ("html", "csv", "all"):
            paths = write_reports(data, settings)
            console.print(f"HTML: {paths['latest_html']}")
            console.print(f"CSV:  {paths['latest_csv']}")
        return 0
    finally:
        db.close()


def cmd_history(args: argparse.Namespace, settings: Settings) -> int:
    db = Database(_offline_db_path(settings) if args.offline else settings.database_file)
    try:
        rows = db.history_overview(args.limit)
        if not rows:
            console.print(
                "Serie storiche troppo brevi: serve almeno una seconda rilevazione della stessa offerta "
                "per mostrare variazioni. Non è possibile dire che un prezzo sia il più basso di sempre."
            )
            return 0
        table = Table(title="Andamento dei prezzi (EUR A/R per persona)")
        for column in ("Destinazione", "Andata", "Ritorno", "Rilevazioni", "Minimo", "Massimo", "Variazione"):
            table.add_column(column)
        for row in rows:
            history = db.price_history(row["offer_key"])
            first, last = float(history[0]["price_eur"]), float(history[-1]["price_eur"])
            change = last - first
            table.add_row(
                row["destination_iata"],
                row["outbound_date"],
                row["return_date"],
                str(row["observations"]),
                f"{row['min_price']:.2f}",
                f"{row['max_price']:.2f}",
                f"{change:+.2f}",
            )
        console.print(table)
        return 0
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m flight_scout",
        description="Flight Scout Cagliari: voli A/R da Cagliari Elmas (CAG) con controllo del budget API.",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="mostra anche i log tecnici")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", metavar="comando")
    sub.required = True

    sub.add_parser("doctor", help="verifica ambiente, configurazione, database e provider")
    sub.add_parser("config", help="mostra i parametri di ricerca (senza segreti)")

    scan = sub.add_parser("scan", help="esegue una scansione (sempre con una modalità esplicita)")
    mode = scan.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="pianifica e stima il consumo, senza rete")
    mode.add_argument("--offline", action="store_true", help="usa dati di test locali, senza rete né costi")
    mode.add_argument("--live", action="store_true", help="ricerca reale con SerpApi (richiede la chiave)")
    speed = scan.add_mutually_exclusive_group()
    speed.add_argument("--quick", action="store_true", help="ricerca rapida (poche chiamate)")
    speed.add_argument("--deep", action="store_true", help="ricerca approfondita (predefinita)")
    scan.add_argument("--max-api-calls", type=int, default=None, help="limite massimo di chiamate per questa esecuzione")
    scan.add_argument("--no-notify", action="store_true", help="non inviare notifiche")

    report = sub.add_parser("report", help="genera report da dati già nel database")
    report.add_argument("--format", choices=("terminal", "html", "csv", "all"), default="all")
    report.add_argument("--offline", action="store_true", help="usa i dati di test offline")

    history = sub.add_parser("history", help="mostra le variazioni di prezzo nel tempo")
    history.add_argument("--limit", type=int, default=50)
    history.add_argument("--offline", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        err_console.print(str(exc))
        return 1
    setup_logging(settings, args.verbose)
    try:
        if args.command == "doctor":
            return cmd_doctor(settings)
        if args.command == "config":
            return cmd_config(settings)
        if args.command == "scan":
            return cmd_scan(args, settings)
        if args.command == "report":
            return cmd_report(args, settings)
        if args.command == "history":
            return cmd_history(args, settings)
    except KeyboardInterrupt:
        err_console.print("Interrotto dall'utente.")
        return 130
    except Exception as exc:  # errore imprevisto: messaggio chiaro, senza segreti
        logging.getLogger("flight_scout").exception("errore imprevisto")
        err_console.print(f"Errore imprevisto ({type(exc).__name__}). Dettagli nel file logs/flight_scout.log.")
        return 1
    return 0
