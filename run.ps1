# Flight Scout Cagliari - script di avvio per Windows PowerShell (5.1 o successivo).
#
# Esempi (da eseguire nella cartella del progetto):
#   .\run.ps1 doctor       # verifica ambiente e configurazione (nessuna chiamata a pagamento)
#   .\run.ps1 dryrun       # pianifica e stima il consumo API (nessuna chiamata)
#   .\run.ps1 offline      # prova completa con dati di TEST, senza Internet
#   .\run.ps1 live1        # UNA sola chiamata reale, per verificare la configurazione
#   .\run.ps1 quick        # scansione rapida + report (adatta a Utilità di pianificazione)
#   .\run.ps1 deep         # scansione approfondita manuale + report
#   .\run.ps1 report       # rigenera i report dai dati già nel database
#   .\run.ps1 history      # variazioni di prezzo nel tempo
#   .\run.ps1 test         # esegue la suite di test automatici
#
# Ogni esecuzione scrive un log in logs\ (senza segreti). Non apre finestre interattive.

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('doctor', 'config', 'dryrun', 'offline', 'live1', 'quick', 'deep', 'report', 'history', 'test')]
    [string]$Action = 'doctor'
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    Write-Host 'Ambiente virtuale non trovato. Crealo con:' -ForegroundColor Yellow
    Write-Host '  py -3.13 -m venv .venv'
    Write-Host '  .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt'
    exit 1
}

$logDir = Join-Path $PSScriptRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format 'yyyy-MM-dd_HH-mm-ss'
$runLog = Join-Path $logDir ("run_{0}_{1}.log" -f $Action, $stamp)

# Lo stdout va nel log e a schermo. Lo stderr resta a schermo e nel file logs\flight_scout.log
# (non si usa 2>&1: in Windows PowerShell 5.1 trasformerebbe i messaggi in errori bloccanti).
function Invoke-Step {
    param([string[]]$Arguments)
    $line = "[{0}] python -m flight_scout {1}" -f (Get-Date -Format 'HH:mm:ss'), ($Arguments -join ' ')
    Add-Content -LiteralPath $runLog -Value $line -Encoding utf8
    # Out-Host evita che il testo finisca nel valore restituito dalla funzione.
    & $python -m flight_scout @Arguments | Tee-Object -FilePath $runLog -Append | Out-Host
    return $LASTEXITCODE
}

$code = 0
switch ($Action) {
    'doctor'  { $code = Invoke-Step @('doctor') }
    'config'  { $code = Invoke-Step @('config') }
    'dryrun'  { $code = Invoke-Step @('scan', '--dry-run') }
    'offline' {
        $code = Invoke-Step @('scan', '--offline')
    }
    'live1'   {
        # Una sola chiamata reale: serve a controllare chiave e formato della risposta.
        $code = Invoke-Step @('scan', '--live', '--max-api-calls', '1', '--no-notify')
    }
    'quick'   {
        $code = Invoke-Step @('scan', '--live', '--quick')
        if ($code -eq 0) { $code = Invoke-Step @('report', '--format', 'all') }
    }
    'deep'    {
        $code = Invoke-Step @('scan', '--live', '--deep')
        if ($code -eq 0) { $code = Invoke-Step @('report', '--format', 'all') }
    }
    'report'  { $code = Invoke-Step @('report', '--format', 'all') }
    'history' { $code = Invoke-Step @('history') }
    'test'    {
        & $python -m pytest -q | Tee-Object -FilePath $runLog -Append | Out-Host
        $code = $LASTEXITCODE
    }
}

if ($code -ne 0) {
    Write-Host ("Terminato con codice {0}. Dettagli nel log: {1}" -f $code, $runLog) -ForegroundColor Red
} else {
    Write-Host ("Completato. Log: {0}" -f $runLog) -ForegroundColor Green
}
exit $code
