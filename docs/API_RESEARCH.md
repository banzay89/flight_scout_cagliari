# Ricerca sulle fonti dati — Flight Scout Cagliari

Data della verifica: **19 ottobre 2026**.

Questo documento distingue ciò che è stato **verificato sulla documentazione ufficiale** da ciò che è **ipotesi**
o proviene da **fonti secondarie** non confermate. Nessuna chiamata a pagamento è stata effettuata durante lo sviluppo:
nessuna chiave API reale è stata usata, quindi **nessun provider è stato testato con dati reali**.

## 1. Fonti consultate

| Fonte | Esito della verifica | Stato nel progetto |
|---|---|---|
| SerpApi — Google Travel Explore API (`serpapi.com/google-travel-explore-api`) | Verificata. `departure_id` obbligatorio, `arrival_id` facoltativo, date `outbound_date`/`return_date`, `month` per date flessibili (prossimi 6 mesi), `stops`, `currency` (default USD), risposta con elenco `destinations`. | **Implementato** (ricerca esplorativa, prezzi INDICATIVI) |
| SerpApi — prezzi e quote (`serpapi.com/pricing`) | Verificata. Piano Free: 0 $/mese, **250 ricerche/mese**, **50 ricerche/ora**. Piani a pagamento da 25 $/mese (1.000 ricerche). | Budget configurabile, predefinito 200/mese, sotto il limite gratuito |
| SerpApi — Google Flights API (`serpapi.com/google-flights-api`) | Verificata parzialmente. Per l'andata/ritorno le prime richieste restituiscono **solo le opzioni di andata**; il ritorno richiede una **seconda richiesta**. Il parametro `currency` determina la valuta (default USD). I voli hanno `flights[]`, `layovers[]`, `total_duration`, `price`, `booking_token`. | **Implementato** (verifica, 2 chiamate per destinazione) |
| Amadeus for Developers (`developers.amadeus.com`) | **Pagina principale non leggibile** durante la verifica. Una ricerca secondaria indica che il portale Self-Service sarebbe stato chiuso il 17 luglio 2026 e che l'accesso rimanente richiede un contratto commerciale. Non confermato sul sito ufficiale. La documentazione di *Flight Inspiration Search* risulta datata. | **Adapter predisposto ma disabilitato** (`providers/amadeus.py`). Nessuna richiesta viene mai inviata. |
| Skyscanner Partners (`partners.skyscanner.net`) | La pagina non indica un accesso self-service né un piano gratuito: l'accesso passa da una candidatura come partner. | **Non implementato** |
| Travelpayouts (`travelpayouts.com`) | Fonti secondarie: registrazione gratuita per la *Data API* (tariffe più economiche trovate dagli utenti nelle ultime 48 ore, dati in cache), ricerca live soggetta a soglie di traffico. Non verificato sulla documentazione ufficiale durante questa sessione. | **Non implementato**: candidato per una fase successiva (prezzi indicativi) |
| Google Flights non ufficiale / scraping | Non utilizzato. La specifica vieta lo scraping aggressivo e l'aggiramento di limiti o autenticazioni. | Escluso |

## 2. Soluzione scelta

**SerpApi** è l'unico provider per cui sono state verificate, sulla documentazione ufficiale:

- l'accesso con registrazione, senza accordo commerciale;
- un piano gratuito (250 ricerche/mese);
- la ricerca esplorativa di destinazioni flessibili (Travel Explore);
- la ricerca di voli con scali e la possibilità di ottenere il ritorno.

Il progetto dipende da una sola fonte live per evitare di basarsi su un provider potenzialmente non accessibile.
L'architettura a provider (`providers/`) permette di aggiungere altre fonti senza modificare il motore.

## 3. Limiti verificati e ipotesi da confermare

### Verificato sulla documentazione
- Piano gratuito: 250 ricerche/mese, 50 ricerche/ora.
- Le ricerche già in cache non sono conteggiate da SerpApi.
- Cache lato SerpApi: 1 ora (indicazione della pagina Travel Explore).
- Andata/ritorno con Google Flights: ritorno ottenuto con una seconda richiesta.
- Formato orari: `YYYY-MM-DD HH:MM` (ricavato dagli esempi della documentazione).

### Ipotesi NON confermate: da verificare con la prima chiamata reale
1. **Prezzo totale o per persona?** La documentazione non lo specifica. Il programma assume che il campo `price`
   sia il **totale per tutti i passeggeri** e lo divide per `PASSENGERS`. Con 1 adulto il valore non cambia.
2. **Nome del campo `departure_token`.** Non compare negli esempi della pagina. Il programma lo cerca con questo nome;
   se manca, l'offerta viene scartata e il motivo è registrato come "nessuna opzione di andata con token".
3. **Unità di `flight_duration` (Travel Explore)**: il programma assume minuti.
4. **Campo con il link a Google Flights** (`search_metadata.google_flights_url`): non confermato. Se assente,
   il report mostra il link alla pagina generica di Google Flights per la verifica manuale, senza inventare un link specifico.
5. **Prezzi esplorativi** (`flight_price` in Travel Explore): il programma li tratta sempre come INDICATIVI.

Per confermare queste ipotesi occorre una sola chiamata reale controllata:

```powershell
.\run.ps1 live1
```

Poi verificare nel database e nel log quali campi sono arrivati (vedi README, sezione "Prima ricerca reale").

## 4. Costi potenziali

| Scenario | Chiamate | Costo |
|---|---|---|
| Dry-run, offline, doctor | 0 | 0 € |
| Ricerca rapida (`quick`): max 10 chiamate | 10 per esecuzione | nel piano gratuito |
| Ricerca approfondita (`deep`): max 25 chiamate | 25 per esecuzione | nel piano gratuito |
| Budget mensile predefinito | 200/mese | entro i 250 del piano gratuito |

Una verifica completa di una destinazione costa **2 chiamate** (andata + ritorno).
Il programma non attiva mai piani a pagamento né acquisti di crediti.

## 5. Copertura e limitazioni

- L'esplorazione copre **finestre di viaggio** (tre pattern settimanali per settimana), non ogni combinazione di date:
  con 180 giorni di orizzonte esistono centinaia di finestre, mentre il budget ne consente poche decine per esecuzione.
  Il report elenca le finestre non esplorate.
- La verifica è limitata a `MAX_DESTINATIONS_TO_VERIFY` destinazioni per esecuzione.
- Le destinazioni senza fuso orario nel catalogo (`config/destinations.json`) non possono diventare VERIFIED: il controllo
  delle coincidenze richiede gli orari locali. Il catalogo va esteso manualmente.
- Il punteggio di interesse è un **valore editoriale iniziale**, non un dato oggettivo. È modificabile in `config/destinations.json`.
