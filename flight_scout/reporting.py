"""Report: terminale (rich), HTML con filtri e CSV compatibile con Excel (separatore ';', date ISO)."""

from __future__ import annotations

import csv
import html
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from flight_scout.config import Settings
from flight_scout.database import Database, month_start
from flight_scout.destination_engine import DestinationCatalog
from flight_scout.models import Offer, SearchStatus
from flight_scout.providers.offline import OFFLINE_BANNER
from flight_scout.ranking import (
    CATEGORY_BEST_BUDGET,
    CATEGORY_EXOTIC,
    CATEGORY_LABELS,
    CATEGORY_NEAR_BUDGET,
    Criteria,
    classify,
    score_offer,
    sort_key,
)

GROUP_ORDER = (CATEGORY_BEST_BUDGET, CATEGORY_NEAR_BUDGET, CATEGORY_EXOTIC)
GOOGLE_FLIGHTS_SEARCH_PAGE = "https://www.google.com/travel/flights"


@dataclass
class Row:
    offer: Offer
    status: SearchStatus
    interest: int
    score: float
    reasons: list[str]
    categories: list[str]


@dataclass
class ReportData:
    scan_id: int
    scan_started: str
    scan_finished: str | None
    offline: bool
    summary: dict
    rows: list[Row]
    groups: dict[str, list[Row]] = field(default_factory=dict)
    discovered: int = 0
    monthly_used: int = 0
    monthly_limit: int = 0

    @property
    def banner(self) -> str | None:
        return OFFLINE_BANNER if self.offline else None


def _effective_status(offer: Offer, now: datetime, ttl_hours: int) -> SearchStatus:
    if offer.status in (SearchStatus.VERIFIED, SearchStatus.INDICATIVE):
        if now - offer.observed_at.astimezone(timezone.utc) > timedelta(hours=ttl_hours):
            return SearchStatus.STALE
    return offer.status


def collect(
    db: Database,
    settings: Settings,
    criteria: Criteria,
    catalog: DestinationCatalog,
    *,
    offline: bool,
    now: datetime | None = None,
) -> ReportData | None:
    """Raccoglie i dati dell'ultima scansione. None se non esistono scansioni concluse."""
    scan = db.latest_scan(offline)
    if scan is None:
        return None
    now = now or datetime.now(timezone.utc)
    offers = db.offers_for_scan(scan["id"])

    # Se una rotta è stata verificata in questa scansione, l'indicazione esplorativa della stessa rotta non si mostra.
    verified_routes = {
        (o.destination_iata, o.outbound_date, o.return_date)
        for o in offers
        if o.status == SearchStatus.VERIFIED
    }
    rows: list[Row] = []
    for offer in offers:
        route = (offer.destination_iata, offer.outbound_date, offer.return_date)
        if offer.status == SearchStatus.INDICATIVE and route in verified_routes:
            continue
        interest = catalog.interest(offer.destination_iata, criteria.default_interest_score)
        scored = score_offer(offer, interest, criteria)
        rows.append(
            Row(
                offer=offer,
                status=_effective_status(offer, now, settings.cache_ttl_hours),
                interest=interest,
                score=scored.score,
                reasons=scored.reasons,
                categories=classify(offer, interest, settings, criteria),
            )
        )

    groups: dict[str, list[Row]] = {name: [] for name in GROUP_ORDER}
    for row in rows:
        for category in row.categories:
            groups[category].append(row)
    for category, members in groups.items():
        members.sort(key=lambda r: sort_key(category, r.offer, r.interest, r.score))
        groups[category] = members[: settings.display_limit]

    summary = json.loads(scan["summary_json"]) if scan["summary_json"] else {}
    return ReportData(
        scan_id=scan["id"],
        scan_started=scan["started_at"],
        scan_finished=scan["finished_at"],
        offline=bool(scan["offline"]),
        summary=summary,
        rows=rows,
        groups=groups,
        discovered=db.discovered_count(),
        monthly_used=db.usage_in_month("serpapi", month_start(now)),
        monthly_limit=settings.api_monthly_budget,
    )


def _stops_text(offer: Offer) -> str:
    if offer.is_complete:
        return f"andata {offer.outbound.stops}, ritorno {offer.inbound.stops}"
    total = offer.total_stops
    return "non indicati" if total is None else f"{total} (indicativo)"


def _duration_text(offer: Offer) -> str:
    if offer.is_complete:
        out_min, in_min = offer.outbound.total_duration_minutes, offer.inbound.total_duration_minutes
        fmt = lambda m: "?" if m is None else f"{m // 60}h{m % 60:02d}"  # noqa: E731
        return f"{fmt(out_min)} / {fmt(in_min)}"
    if offer.duration_hint_minutes is not None:
        return f"{offer.duration_hint_minutes // 60}h{offer.duration_hint_minutes % 60:02d} (indicativa)"
    return "non disponibile"


def _link(offer: Offer) -> str:
    return offer.source_url or GOOGLE_FLIGHTS_SEARCH_PAGE


def _missing_text(offer: Offer) -> str:
    return "mancano: " + ", ".join(offer.missing_data) if offer.missing_data else ""


def format_timestamp(value: str | datetime) -> str:
    """Data leggibile in UTC, ad esempio 2026-10-19 07:30 UTC."""
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M") + " UTC"


def render_terminal(data: ReportData, console: Console) -> None:
    """Tabella compatta per il terminale: i dettagli completi sono nel report HTML e CSV."""
    if data.banner:
        console.print(f"[bold yellow]{data.banner}[/]")
    console.print(f"Ultima scansione: {format_timestamp(data.scan_started)}, id {data.scan_id}")
    for category in GROUP_ORDER:
        members = data.groups.get(category, [])
        console.rule(f"{CATEGORY_LABELS[category]} ({len(members)})")
        if not members:
            console.print("  Nessuna offerta in questa categoria.")
            continue
        table = Table(show_lines=False, expand=True)
        for column in ("Destinazione", "Andata", "Ritorno", "Notti", "Prezzo A/R", "Scali", "Stato", "Interesse", "Link"):
            table.add_column(column, overflow="fold")
        for row in members:
            offer = row.offer
            table.add_row(
                f"{offer.destination_name or offer.destination_iata} ({offer.destination_iata})",
                offer.outbound_date.isoformat(),
                offer.return_date.isoformat(),
                str(offer.nights),
                f"{offer.price_eur:.2f} {offer.currency}",
                _stops_text(offer),
                row.status.value,
                str(row.interest),
                f"[link={_link(offer)}]apri[/link]",
            )
        console.print(table)
    _print_footer(data, console)


def _print_footer(data: ReportData, console: Console) -> None:
    s = data.summary
    verified = sum(1 for r in data.rows if r.status == SearchStatus.VERIFIED)
    console.rule("Riepilogo")
    console.print(f"Rilevazione: {format_timestamp(data.scan_started)}")
    console.print(f"Provider: {', '.join(s.get('providers', [])) or 'n/d'}")
    console.print(f"Ricerche API eseguite in questa scansione: {s.get('api_calls', 0)} (usate nel mese: {data.monthly_used}/{data.monthly_limit})")
    console.print(f"Itinerari/offerte trovati: {len(data.rows)} (verificati: {verified})")
    console.print(f"Destinazioni scoperte (totale storico): {data.discovered}")
    console.print(f"Copertura: {s.get('windows_covered', 0)}/{s.get('windows_total', 0)} finestre esplorate")
    console.print(f"Errori: {s.get('error_count', 0)}")
    if s.get("stop_reason"):
        console.print(f"Interruzione per budget/limiti: {s['stop_reason']}")
    skipped = s.get("skipped_windows", [])
    if skipped:
        console.print(f"Periodi non verificati: {len(skipped)} (primi: {', '.join(skipped[:3])})")


def render_html(data: ReportData, generated_at: datetime) -> str:
    esc = html.escape

    def section(category: str) -> str:
        members = data.groups.get(category, [])
        head = "".join(f"<th>{esc(c)}</th>" for c in (
            "Destinazione", "Paese", "Andata", "Ritorno", "Notti", "Prezzo A/R", "Scali", "Durata A/R", "Stato", "Interesse", "Motivazione", "Link"
        ))
        body_rows = []
        for row in members:
            offer = row.offer
            stops = _stops_text(offer)
            total_stops = offer.total_stops if offer.total_stops is not None else 99
            body_rows.append(
                f'<tr data-price="{float(offer.price_eur)}" data-nights="{offer.nights}" '
                f'data-stops="{total_stops}" data-status="{esc(row.status.value)}" data-country="{esc((offer.destination_country or "").lower())}">'
                f"<td>{esc(offer.destination_name or offer.destination_iata)} ({esc(offer.destination_iata)})</td>"
                f"<td>{esc(offer.destination_country or '')}</td>"
                f"<td>{esc(offer.outbound_date.isoformat())}</td>"
                f"<td>{esc(offer.return_date.isoformat())}</td>"
                f"<td>{offer.nights}</td>"
                f"<td>{offer.price_eur:.2f} {esc(offer.currency)}</td>"
                f"<td>{esc(stops)}</td>"
                f"<td>{esc(_duration_text(offer))}</td>"
                f"<td><span class='st st-{esc(row.status.value.lower())}'>{esc(row.status.value)}</span>"
                f"{'<br><small>' + esc(_missing_text(offer)) + '</small>' if _missing_text(offer) else ''}</td>"
                f"<td>{row.interest}</td>"
                f"<td>{esc('; '.join(row.reasons[:2]))}</td>"
                f'<td><a href="{esc(_link(offer))}" rel="noopener" target="_blank">apri</a></td>'
                "</tr>"
            )
        empty = "<tr><td colspan='12'>Nessuna offerta in questa categoria.</td></tr>"
        return (
            f"<section><h2>{esc(CATEGORY_LABELS[category])} <small>({len(members)})</small></h2>"
            f"<div class='scroll'><table class='grid'><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(body_rows) or empty}</tbody></table></div></section>"
        )

    verified = sum(1 for r in data.rows if r.status == SearchStatus.VERIFIED)
    s = data.summary
    banner = f"<div class='banner'>{esc(data.banner)}</div>" if data.banner else ""
    skipped = s.get("skipped_windows", [])
    return f"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flight Scout Cagliari</title>
<style>
:root {{ --bg:#f6f7f9; --fg:#1d2330; --muted:#5b6474; --card:#ffffff; --line:#d9dee7; --accent:#1f6feb; --warn:#fff4d6; --ok:#dff5e4; --ind:#eef2f7; --stale:#fde7e7; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg:#14171d; --fg:#e8ebf1; --muted:#9aa3b2; --card:#1d222b; --line:#333b48; --accent:#6ea8ff; --warn:#3d3420; --ok:#1d3326; --ind:#252c38; --stale:#3a2222; }} }}
:root[data-theme="dark"] {{ --bg:#14171d; --fg:#e8ebf1; --muted:#9aa3b2; --card:#1d222b; --line:#333b48; --accent:#6ea8ff; --warn:#3d3420; --ok:#1d3326; --ind:#252c38; --stale:#3a2222; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.45 system-ui, sans-serif; }}
main {{ max-width:1200px; margin:0 auto; padding:16px; }}
h1 {{ font-size:22px; margin:8px 0; }} h2 {{ font-size:18px; margin:24px 0 8px; }}
.banner {{ background:var(--warn); padding:10px 12px; border-radius:6px; font-weight:600; }}
.meta {{ color:var(--muted); font-size:13px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:8px; margin:12px 0; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:8px 10px; }}
.card b {{ display:block; font-size:18px; }}
.filters {{ display:flex; flex-wrap:wrap; gap:8px; background:var(--card); border:1px solid var(--line); border-radius:8px; padding:10px; }}
.filters label {{ font-size:13px; display:flex; flex-direction:column; gap:2px; color:var(--muted); }}
.filters input, .filters select {{ background:var(--bg); color:var(--fg); border:1px solid var(--line); border-radius:4px; padding:4px 6px; min-width:110px; }}
.scroll {{ overflow-x:auto; }}
table.grid {{ border-collapse:collapse; width:100%; background:var(--card); font-size:13px; }}
table.grid th, table.grid td {{ border-bottom:1px solid var(--line); padding:6px 8px; text-align:left; vertical-align:top; }}
table.grid th {{ color:var(--muted); font-weight:600; white-space:nowrap; }}
.st {{ padding:1px 6px; border-radius:4px; font-size:12px; background:var(--ind); }}
.st-verified {{ background:var(--ok); }} .st-stale {{ background:var(--stale); }}
a {{ color:var(--accent); }}
footer {{ margin-top:24px; color:var(--muted); font-size:13px; }}
@media (max-width:600px) {{ main {{ padding:12px; }} }}
</style>
</head>
<body>
<main>
<h1>Flight Scout Cagliari (CAG)</h1>
{banner}
<p class="meta">Report generato: {esc(generated_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M'))} UTC &middot;
Ultima scansione: {esc(data.scan_started)} UTC (id {data.scan_id})</p>
<div class="cards">
  <div class="card"><b>{s.get('api_calls', 0)}</b>ricerche API</div>
  <div class="card"><b>{len(data.rows)}</b>offerte trovate</div>
  <div class="card"><b>{verified}</b>verificate</div>
  <div class="card"><b>{data.discovered}</b>destinazioni scoperte</div>
  <div class="card"><b>{s.get('windows_covered', 0)}/{s.get('windows_total', 0)}</b>finestre coperte</div>
  <div class="card"><b>{s.get('error_count', 0)}</b>errori</div>
</div>
<div class="filters" role="group" aria-label="Filtri">
  <label>Prezzo max (EUR)<input id="f-price" type="number" min="0" step="1" placeholder="es. 100"></label>
  <label>Notti<select id="f-nights"><option value="">tutte</option><option>1</option><option>2</option><option>3</option><option>4</option></select></label>
  <label>Scali<select id="f-stops"><option value="">tutti</option><option value="0">diretto</option><option value="1">max 1</option><option value="2">max 2</option></select></label>
  <label>Stato<select id="f-status"><option value="">tutti</option><option>VERIFIED</option><option>INDICATIVE</option><option>STALE</option></select></label>
  <label>Paese<input id="f-country" type="text" placeholder="es. spagna"></label>
</div>
{section(CATEGORY_BEST_BUDGET)}
{section(CATEGORY_NEAR_BUDGET)}
{section(CATEGORY_EXOTIC)}
<section>
<h2>Copertura e limiti</h2>
<p class="meta">Periodi non verificati o saltati: {len(skipped)}{(' (primi: ' + esc('; '.join(skipped[:5])) + ')') if skipped else ''}</p>
<p class="meta">Errori registrati: {s.get('error_count', 0)}. Provider: {esc(', '.join(s.get('providers', [])) or 'n/d')}.
Prezzi INDICATIVE: provengono dalla ricerca esplorativa e non garantiscono date o itinerario. Un prezzo verificato non garantisce che il posto sia ancora disponibile.</p>
</section>
<footer>Il programma non effettua prenotazioni né pagamenti: usa i link per verificare e prenotare sul sito del vettore o del provider.</footer>
</main>
<script>
(function () {{
  var ids = ["f-price", "f-nights", "f-stops", "f-status", "f-country"];
  function apply() {{
    var price = parseFloat(document.getElementById("f-price").value);
    var nights = document.getElementById("f-nights").value;
    var stops = document.getElementById("f-stops").value;
    var status = document.getElementById("f-status").value;
    var country = document.getElementById("f-country").value.trim().toLowerCase();
    document.querySelectorAll("tbody tr[data-price]").forEach(function (tr) {{
      var ok = true;
      if (!isNaN(price) && parseFloat(tr.dataset.price) > price) ok = false;
      if (nights && tr.dataset.nights !== nights) ok = false;
      if (stops !== "" && (stops === "0" ? tr.dataset.stops !== "0" : parseInt(tr.dataset.stops, 10) > parseInt(stops, 10))) ok = false;
      if (status && tr.dataset.status !== status) ok = false;
      if (country && tr.dataset.country.indexOf(country) === -1) ok = false;
      tr.style.display = ok ? "" : "none";
    }});
  }}
  ids.forEach(function (id) {{
    var el = document.getElementById(id);
    el.addEventListener("input", apply);
    el.addEventListener("change", apply);
  }});
}})();
</script>
</body>
</html>
"""


CSV_COLUMNS = (
    "categorie",
    "destinazione_iata",
    "destinazione",
    "paese",
    "data_andata",
    "data_ritorno",
    "notti",
    "prezzo_ar_persona",
    "valuta",
    "scali_andata",
    "scali_ritorno",
    "durata_andata_min",
    "durata_ritorno_min",
    "stato",
    "punteggio",
    "interesse",
    "rilevato_utc",
    "fonte",
    "link",
    "dati_mancanti",
    "bagaglio",
)


def render_csv(data: ReportData) -> str:
    """CSV con ';' come separatore e virgola decimale, leggibile direttamente da Excel in italiano."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for row in data.rows:
        offer = row.offer
        categories = ",".join(row.categories) or "NESSUNA"
        writer.writerow(
            [
                categories,
                offer.destination_iata,
                offer.destination_name or "",
                offer.destination_country or "",
                offer.outbound_date.isoformat(),
                offer.return_date.isoformat(),
                offer.nights,
                f"{offer.price_eur:.2f}".replace(".", ","),
                offer.currency,
                offer.outbound.stops if offer.outbound else "",
                offer.inbound.stops if offer.inbound else "",
                offer.outbound.total_duration_minutes if offer.outbound else "",
                offer.inbound.total_duration_minutes if offer.inbound else "",
                row.status.value,
                f"{row.score:.1f}".replace(".", ","),
                row.interest,
                offer.observed_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                offer.provider,
                _link(offer),
                _missing_text(offer),
                offer.baggage_info or "",
            ]
        )
    return "﻿" + buffer.getvalue()  # BOM UTF-8 per riconoscere le accentuazioni in Excel


def write_reports(data: ReportData, settings: Settings, now: datetime | None = None) -> dict[str, Path]:
    now = now or datetime.now(timezone.utc)
    settings.report_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    prefix = "TEST_" if data.offline else ""
    html_path = settings.report_dir / f"{prefix}report_{stamp}.html"
    csv_path = settings.report_dir / f"{prefix}offerte_{stamp}.csv"
    latest_html = settings.report_dir / f"{prefix}ultimo_report.html"
    latest_csv = settings.report_dir / f"{prefix}ultimo_report.csv"
    html_text = render_html(data, now)
    csv_text = render_csv(data)
    for path, text in ((html_path, html_text), (latest_html, html_text)):
        path.write_text(text, encoding="utf-8")
    for path, text in ((csv_path, csv_text), (latest_csv, csv_text)):
        path.write_text(text, encoding="utf-8", newline="")
    return {"html": html_path, "csv": csv_path, "latest_html": latest_html, "latest_csv": latest_csv}
