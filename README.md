# Flight Scout Cagliari

Cerca voli andata e ritorno partendo da **Cagliari Elmas (CAG)** verso destinazioni in tutto il mondo, con un
**controllo rigoroso del consumo API**. Distingue sempre le offerte **verificate** dai prezzi **indicativi**.

- Prezzo ordinario: fino a **100 EUR A/R per persona** (configurabile).
- Partenza da giovedì alle 17:00 (ora di Cagliari), rientro entro lunedì alle 23:00.
- Permanenze da 1 a 4 notti.
- Voli diretti e con scali.
- Sezione separata per le **destinazioni esotiche**, anche sopra soglia.

> **Non effettua prenotazioni né pagamenti.** Fornisce link per verificare e prenotare sul sito del vettore o del provider.

---

## 1. Requisiti

- Windows 10 o 11 con **Windows PowerShell** (incluso di serie).
- **Python 3.12 o successivo**. Il progetto è stato provato con Python 3.13.
  Verifica con: `py -3.13 --version` (oppure `py --list` per vedere le versioni installate).
- Una chiave **SerpApi** (facoltativa): serve solo per le ricerche live. Piano gratuito: 250 ricerche/mese.

## 2. Installazione

Apri PowerShell nella cartella del progetto (`C:\Users\andre\flight_scout_cagliari`):

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Le dipendenze vengono installate solo dentro `.venv`: non cambiano il resto del PC.

## 3. Configurazione (file `.env`)

1. Copia il modello:

   ```powershell
   Copy-Item .env.example .env
   ```

2. Apri `.env` con il Blocco note e inserisci la chiave, se la hai:

   ```dotenv
   SERPAPI_API_KEY=la_tua_chiave
   ```

3. Le altre impostazioni hanno valori prudenti (vedi `.env.example`). Le più importanti:

| Variabile | Predefinito | Significato |
|---|---|---|
| `MAX_STANDARD_PRICE_EUR` | 100 | soglia del gruppo economico (A/R per persona) |
| `EXOTIC_MAX_PRICE_EUR` | vuoto | vuoto = nessun tetto per le esotiche (la classifica limita la visualizzazione) |
| `SEARCH_HORIZON_DAYS` | 180 | quanto nel futuro cercare |
| `API_RUN_BUDGET` | 25 | chiamate API massime per esecuzione |
| `API_MONTHLY_BUDGET` | 200 | chiamate API massime al mese (sotto le 250 del piano gratuito) |
| `MAX_DESTINATIONS_TO_VERIFY` | 10 | destinazioni verificate per esecuzione (2 chiamate ciascuna) |

**Non condividere mai il file `.env`.** È escluso da Git e il programma non stampa mai le chiavi.

## 4. Primo avvio (senza spendere nulla)

Esegui in quest'ordine:

```powershell
.\run.ps1 doctor     # controlla Python, configurazione, database e chiavi mancanti
.\run.ps1 dryrun     # mostra quante ricerche verrebbero fatte: nessuna richiesta di rete
.\run.ps1 offline    # prova completa con DATI DI TEST (nessun prezzo reale)
.\run.ps1 report     # genera i report: quello offline è marcato come test
```

I report si trovano in `reports\`:

- `ultimo_report.html`: pagina con sezioni, filtri per prezzo, notti, scali, stato e paese.
- `ultimo_report.csv`: apribile in Excel (separatore `;`, date in formato ISO `2026-10-22`, prezzi con virgola).

> I file con prefisso `TEST_` contengono dati generati localmente: non sono prezzi reali.

## 5. Prima ricerca reale controllata

Solo quando la chiave è configurata e `doctor` non segnala errori:

```powershell
.\run.ps1 live1
```

Questo comando effettua **una sola chiamata** API. Serve a controllare che la chiave funzioni e che la risposta
arrivi nel formato atteso. Poi consulta il log in `logs\` e il riepilogo a schermo.

Per provare anche la verifica completa di un itinerario (andata + ritorno), al massimo **3 chiamate**:

```powershell
.\.venv\Scripts\python.exe -m flight_scout scan --live --max-api-calls 3
```

> **Attenzione:** il formato di alcuni campi (prezzo totale o per persona, token del ritorno) è un'ipotesi da
> confermare con questa prima chiamata. Vedi `docs/API_RESEARCH.md`, sezione 3.

## 6. Comandi

| Comando PowerShell | Cosa fa | Chiamate API |
|---|---|---|
| `.\run.ps1 doctor` | diagnostica | 0 |
| `.\run.ps1 config` | mostra i parametri (senza segreti) | 0 |
| `.\run.ps1 dryrun` | pianifica e stima il consumo | 0 |
| `.\run.ps1 offline` | scansione di test con dati locali | 0 |
| `.\run.ps1 live1` | una sola chiamata reale | 1 |
| `.\run.ps1 quick` | scansione rapida + report | fino a 10 |
| `.\run.ps1 deep` | scansione approfondita + report | fino a 25 |
| `.\run.ps1 report` | rigenera i report dal database | 0 |
| `.\run.ps1 history` | variazioni di prezzo nel tempo | 0 |
| `.\run.ps1 test` | esegue i test automatici | 0 |

Equivalenti diretti: `.\.venv\Scripts\python.exe -m flight_scout <comando>`.
Opzioni utili: `--verbose` (log tecnici a schermo), `--help`, `--max-api-calls N`, `--no-notify`.

**Lo `scan` senza opzioni non fa nulla**: il programma non effettua mai ricerche a pagamento per errore.

## 7. Come leggere i risultati

**Stati del prezzo**

- `VERIFIED`: itinerario completo (andata e ritorno) restituito dal provider per le date indicate. Il prezzo è della
  rilevazione indicata: può cambiare o esaurirsi.
- `INDICATIVE`: prezzo della ricerca esplorativa. Non garantisce date né itinerario. Da verificare sul sito.
- `STALE`: dato più vecchio di `CACHE_TTL_HOURS`. Non va considerato attuale.

**Categorie**

- **Migliori offerte economiche**: entro la soglia (100 EUR A/R di default), ordinate per prezzo crescente.
- **Quasi economiche**: sopra soglia ma entro `near_budget_max_eur` (in `config\criteria.json`). Non sono offerte conformi alla soglia.
- **Destinazioni esotiche**: destinazioni con interesse editoriale ≥ 70. Possono apparire anche nelle economiche.

Ogni riga riporta la data di rilevazione (UTC), gli scali, la durata e, quando disponibile, il link alla fonte.
Se un link specifico non è fornito dal provider, il report mostra la pagina generica di Google Flights per la verifica manuale.

## 8. Automazione con Utilità di pianificazione

Il programma **non crea attività pianificate da solo**. Se vuoi automatizzare la ricerca, crea tu l'attività:
la guida passo passo e i limiti di frequenza sono in [docs/AUTOMAZIONE_WINDOWS.md](docs/AUTOMAZIONE_WINDOWS.md).

## 9. Notifiche (facoltative)

Compila `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` oppure le variabili `SMTP_*` ed `EMAIL_TO`.
Se mancano, le notifiche restano disattivate e il programma funziona lo stesso.

Eventi notificati: nuova offerta sotto soglia, calo di prezzo significativo, destinazione esotica scoperta,
offerta indicativa poi verificata, limite mensile raggiunto. Uno stesso risultato allo stesso prezzo non viene
inviato due volte. I messaggi non contengono mai token o password.

## 10. Test

```powershell
.\run.ps1 test
```

I test non usano credenziali né Internet (la rete è bloccata nei test che lo richiedono). Coprono: finestre temporali,
orari di partenza e rientro, fusi orari, cambio dell'ora, anno nuovo, anni bisestili, parsing delle risposte incomplete,
errori HTTP 401/403/404/429/5xx, budget per esecuzione, mensile e orario, cache, database, report, notifiche,
configurazione mancante e modalità offline senza rete.

## 11. Problemi comuni

| Messaggio | Soluzione |
|---|---|
| `Ambiente virtuale non trovato` | esegui i comandi della sezione 2 |
| `SERPAPI_API_KEY non impostata` | aggiungi la chiave nel file `.env`, oppure usa `dryrun` / `offline` |
| `Configurazione non valida` | il messaggio indica la variabile sbagliata; il valore non viene mostrato |
| `budget di questa esecuzione esaurito` | normale: il report elenca i periodi non controllati |
| `limite orario del provider raggiunto` | attendi un'ora, poi rilancia: la cache evita di ripagare le ricerche già fatte |
| `HTTP 401` o `403` | chiave non valida o piano non abilitato: il provider viene fermato |
| `Interrotto` | normale con Ctrl+C: i dati già salvati restano; riavvia la scansione |

Log dettagliati: `logs\flight_scout.log` (senza segreti).

## 12. Struttura del progetto

```text
flight_scout_cagliari/
├── flight_scout/          codice del programma
│   ├── cli.py             comandi da terminale
│   ├── config.py          configurazione e controlli (.env)
│   ├── date_engine.py     finestre di viaggio e regole orarie (zoneinfo)
│   ├── search_engine.py   esplorazione, verifica, salvataggio
│   ├── ranking.py         punteggi e categorie (criteria.json)
│   ├── budget.py          limiti API per esecuzione, mensili e orari
│   ├── cache.py           cache persistente con scadenza
│   ├── database.py        SQLite con migrazioni
│   ├── reporting.py       report terminale, HTML e CSV
│   ├── notifications.py   Telegram ed e-mail, deduplicate
│   └── providers/         SerpApi (attivo), Amadeus (disabilitato), modalità offline
├── config/                destinazioni e criteri modificabili (JSON)
├── tests/                 test automatici
├── docs/                  ricerca sulle API e guida all'automazione
├── data/                  database locale (non in Git)
├── reports/               report generati (non in Git)
├── logs/                  log (non in Git)
├── run.ps1                script di avvio per PowerShell
├── .env.example           modello di configurazione senza segreti
└── requirements*.txt      dipendenze
```

## 13. Limiti attuali

- L'esplorazione copre finestre di viaggio campionate, non ogni combinazione di date: il budget non lo consentirebbe.
- Il provider live è uno solo (SerpApi). Amadeus è predisposto ma disabilitato: vedi `docs/API_RESEARCH.md`.
- Le offerte verificate richiedono che la destinazione sia nel catalogo con fuso orario (`config/destinations.json`).
- Il punteggio di interesse delle destinazioni è un valore editoriale iniziale da rivedere.
- Il formato reale delle risposte SerpApi non è stato ancora confermato con una chiamata reale: vedi sezione 5.
