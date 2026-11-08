# 118 Lombardia · dashboard in tempo reale

Raccoglie ogni 10 minuti i dati pubblici sulle missioni 118 di AREU Lombardia, ne costruisce lo storico e li mostra in una dashboard web.

**Costo: zero.** Tutto gira su GitHub. GitHub Actions è gratuito e senza limiti di minuti per i repository pubblici, GitHub Pages ospita la pagina gratis.

```
ogni 10 min                  salva nel repo                 la pagina legge i file
GitHub Actions ──► scrape.py ──► data/*.csv (storico) ──► docs/index.html (GitHub Pages)
                              └─► docs/data/*.json
```

## Messa online in 5 passi (circa 10 minuti)

1. **Crea un account GitHub** su https://github.com, se non ce l'hai.
2. **Crea un repository nuovo**: pulsante "New", nome per esempio `areu-118-dashboard`, visibilità **Public** (serve per avere i minuti illimitati e Pages gratis), poi "Create repository".
3. **Carica i file**: nella pagina del repo vuoto clicca "uploading an existing file" e trascina **il contenuto** di questa cartella, cioè le cartelle `scraper`, `docs`, `tests`, `.github` e questo README. Poi "Commit changes".
   - La cartella `.github` inizia con un punto e su Mac/Windows può essere nascosta. Su Mac premi `Cmd + Shift + .` nel Finder per vederla. Se il caricamento web non la prende, crea a mano il file `.github/workflows/scrape.yml` dal pulsante "Add file → Create new file" e incolla il contenuto.
4. **Dai il permesso di scrittura al bot**: Settings → Actions → General → "Workflow permissions" → seleziona **Read and write permissions** → Save.
5. **Accendi la pagina**: Settings → Pages → "Build and deployment" → Source: **Deploy from a branch** → Branch: `main`, cartella **`/docs`** → Save.

Poi vai su **Actions → "Raccolta dati AREU 118" → Run workflow** per la prima raccolta. Dopo un paio di minuti la dashboard è online su:

```
https://TUO-UTENTE.github.io/areu-118-dashboard/
```

Da lì in avanti il workflow parte da solo ogni 10 minuti.

## Cosa aspettarsi

- **Il primo giorno** vedi i numeri attuali, la gravità, i motivi e una linea che cresce a ogni lettura.
- **Dopo circa 1 settimana** si attiva il confronto con il solito (stesso giorno della settimana e stessa ora). La mappa si colora di blu (sotto il solito) e di rosso (sopra).
- **Dopo 2–3 settimane** il confronto diventa affidabile.
- GitHub può ritardare le esecuzioni programmate di qualche minuto nelle ore di punta. È normale.

## File

| File | Cosa fa |
|---|---|
| `scraper/scrape.py` | legge l'API AREU, salva lo storico, prepara i dati per la pagina (solo libreria standard Python) |
| `.github/workflows/scrape.yml` | lo esegue ogni 10 minuti e salva i dati nel repo |
| `docs/index.html` | la dashboard (HTML + JS senza librerie esterne) |
| `data/in_corso/AAAA-MM.csv` | **lo storico**: una riga per zona a ogni lettura |
| `data/giornaliero.csv` | totali giornalieri per zona |
| `tests/make_fixture.py` | genera dati finti per provare tutto in locale |

## Provarla sul tuo computer

```bash
python3 scraper/scrape.py                 # una lettura reale
cd docs && python3 -m http.server 8000    # poi apri http://localhost:8000
```

## Note

- **API**: `https://www.areu.lombardia.it/api/jsonws/eventiregionejson.eventiregionejson/statistiche-regione` è pubblica e senza autenticazione, ma non è documentata. Se AREU la cambia, lo scraper smette di funzionare e GitHub ti avvisa via email del workflow fallito.
- **Correttezza**: non leggere più spesso di ogni 5–10 minuti. È l'infrastruttura di un servizio di emergenza.
- **Crescita dello storico**: circa 8–10 MB al mese di CSV, gestibili per anni su GitHub.
- **Se la raccolta si ferma**: vai su Actions, apri il workflow e, se GitHub lo ha disattivato, premi "Enable workflow". Quando vorrai analisi pesanti, carica i CSV in DuckDB o pandas.
- **Fonte**: cita sempre AREU e specifica che la dashboard non è ufficiale (lo fa già il footer della pagina).
