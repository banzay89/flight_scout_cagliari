# Automazione su Windows (Utilità di pianificazione)

Il programma **non crea attività pianificate**: sei tu a crearle, quando sei pronto. Questa guida spiega come farlo
senza superare il budget API.

## 1. Frequenze consigliate e budget

Il piano gratuito SerpApi consente **250 ricerche al mese**. Il programma si ferma da solo al limite mensile
(`API_MONTHLY_BUDGET`, predefinito 200), ma conviene progettare le frequenze per non arrivarci.

| Attività | Quando | Chiamate massime | Al mese |
|---|---|---|---|
| `quick` | ogni domenica alle 08:17 | 10 | ~43 |
| `deep` (manuale) | 1–2 volte al mese | 25 | 25–50 |
| **Totale stimato** | | | **~70–95** |

Non impostare `quick` più volte al giorno: con 10 chiamate per esecuzione, 20 esecuzioni al mese consumerebbero
200 chiamate. Il programma tiene comunque il conteggio nel database e si ferma al limite.

## 2. Cosa fa una scansione `quick`

`run.ps1 quick` esegue:

1. `scan --live --quick`: al massimo 10 chiamate, con esplorazione prioritaria dei periodi vicini;
2. `report --format all`: aggiorna `reports\ultimo_report.html` e `ultimo_report.csv`;
3. scrive un log in `logs\run_quick_<data>.log`.

Se la chiave non è configurata, lo script termina con un messaggio chiaro e **non effettua chiamate**.

## 3. Creare l'attività (manuale)

1. Apri **Utilità di pianificazione** (Task Scheduler).
2. **Crea attività…** (non "Crea attività di base", per avere tutte le opzioni).
3. Scheda **Generale**:
   - Nome: `Flight Scout Quick`.
   - Seleziona **Esegui solo quando l'utente ha effettuato l'accesso**. Così non serve salvare la password di Windows.
4. Scheda **Trigger** → **Nuovo…**:
   - Operazione: **Settimanale**, ogni 1 settimana, **domenica**, alle **08:17**.
     (Un orario fuori dal minuto zero distribuisce meglio il carico.)
   - Spunta **Abilitato**.
5. Scheda **Operazioni** → **Nuova…**:
   - Programma: `powershell.exe`
   - Argomenti: `-NoProfile -ExecutionPolicy Bypass -File "C:\Users\andre\flight_scout_cagliari\run.ps1" quick`
   - Inizia da: `C:\Users\andre\flight_scout_cagliari`
6. Scheda **Impostazioni**:
   - Spunta **Esegui l'attività il prima possibile se è stata saltata una pianificazione**
     (così una scansione persa, per esempio con il PC spento, viene recuperata).
   - Spunta **Interrompi l'attività se è in esecuzione da più di 1 ora**.

Equivalente da riga di comando (da eseguire tu, il programma non lo fa per te):

```powershell
schtasks /Create /SC WEEKLY /D SUN /ST 08:17 /TN "FlightScoutQuick" /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Users\andre\flight_scout_cagliari\run.ps1 quick"
```

Per rimuoverla: `schtasks /Delete /TN "FlightScoutQuick" /F`.

## 4. Verifiche dopo la prima esecuzione automatica

- Apri `logs\` e controlla l'ultimo `run_quick_*.log`.
- Apri `reports\ultimo_report.html`: la sezione **Copertura e limiti** elenca periodi non controllati ed errori.
- Il comando `.\run.ps1 history` mostra le variazioni di prezzo quando ci sono almeno due rilevazioni.

## 5. Ripresa dopo un'interruzione

- Ogni chiamata API è registrata nel database **prima** di essere inviata: un'interruzione non fa perdere il conteggio.
- La cache (`CACHE_TTL_HOURS`, predefinito 24 ore) evita di pagare due volte la stessa ricerca:
  rilanciare la scansione riprende dai dati già ottenuti.
- Ctrl+C durante una scansione interrompe in modo controllato e lascia i dati salvati.

## 6. Cosa non fare

- Non impostare la scansione ogni ora o ogni giorno con `deep`: esaurirebbe il budget mensile in pochi giorni.
- Non eseguire l'attività con un account di amministrazione per "comodità": non serve.
- Non mettere la chiave SerpApi nello script o nel comando dell'attività: resta nel file `.env`.
