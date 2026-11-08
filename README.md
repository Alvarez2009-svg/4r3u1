# 118 Lombardia · Sala situazione

Dashboard in tempo reale delle missioni 118 in Lombardia, costruita sui dati pubblici di AREU.
Ogni 10 minuti GitHub legge i dati, li aggiunge allo storico, calcola gli indicatori e ripubblica il sito. **Costo: zero** (repository pubblico).

```
ogni 10 min
GitHub Actions ─► scraper/scrape.py ─► data/*.csv          (storico, salvato nel repository)
                                    └─► _site/data/*.json   (indicatori, pubblicati con il sito)
                                         └─► GitHub Pages: docs/index.html
```

## Cosa mostra

| Sezione | Orizzonte | Cosa |
|---|---|---|
| Insight | adesso | le 4 cose più insolite del momento, scritte in frasi |
| Mezzi sul territorio | adesso | mezzi in missione per tipo (MSB, MSA, MSI, elisoccorso) e gravità |
| Ritmo delle uscite | ultima ora | nuovi invii, "uno ogni N secondi", durata media stimata delle missioni |
| Le 12 zone | adesso | mappa colorata rispetto al solito; tocca una zona per concentrare tutta la dashboard su di essa |
| Registro eventi | 24 ore | picchi, elisoccorsi, incidenti in montagna, raffiche di codici rossi |
| Mezzi nel tempo | 24 ore / 7 giorni | andamento con la fascia del "solito" |
| Ora per ora / motivi | oggi vs ieri | confronto alla stessa ora |
| Respiro della settimana | 8 giorni | heatmap degli invii per ora |

### Come sono calcolati gli insight
L'API dice solo "quanti adesso" e "quanti da mezzanotte". Confrontando due letture consecutive si ricava il resto:
- **nuovi invii** = differenza dei totali progressivi tra due letture (gestisce il passaggio di mezzanotte);
- **durata media di una missione** ≈ mezzi in missione ÷ invii all'ora (legge di Little, ultime 3 ore). Durate lunghe possono indicare mezzi trattenuti in pronto soccorso;
- **il solito** = mediana delle ultime 8 settimane per stesso giorno della settimana e stessa ora; "picchi abituali" = 90° percentile;
- **dato fermo**: AREU aggiorna le zone in momenti diversi; se una zona non si aggiorna da oltre 30 minuti la dashboard lo segnala.

Significato dei campi (verificato sui dati): `missioni` = mezzi inviati (= MSB + MSA + MSI + elisoccorso = somma dei codici colore); `totale_missioni` = eventi (= somma dei motivi).

## Prima installazione

1. Crea un repository **pubblico** su GitHub e carica `scraper`, `docs`, `tests`, `README.md`.
2. Crea il file `.github/workflows/scrape.yml` (Add file → Create new file) e incolla il contenuto di quello nello zip.
3. Settings → Actions → General → Workflow permissions → **Read and write permissions** → Save.
4. Settings → Pages → Build and deployment → Source: **GitHub Actions**.
5. Actions → "Raccolta dati AREU 118" → **Run workflow**.

## Aggiornare dalla versione precedente

1. Carica i nuovi `scraper/scrape.py` e `docs/index.html` (Add file → Upload files: sovrascrivono i vecchi). Facoltativo: `tests/`.
2. Apri `.github/workflows/scrape.yml`, clicca la matita, sostituisci tutto il contenuto con quello nuovo, Commit.
3. Settings → Pages → Source: cambia da "Deploy from a branch" a **GitHub Actions**.
4. Actions → Run workflow.

Lo storico già raccolto viene convertito automaticamente al nuovo formato. La cartella `docs/data` del repository non serve più: puoi cancellarla oppure lasciarla.

## Tempi
- **Subito**: numeri in tempo reale, flotta, gravità, motivi, zone.
- **Dopo 1 ora**: ritmo delle uscite, durata stimata, registro eventi, grafico ora per ora.
- **Dopo 1 giorno**: confronto con ieri alla stessa ora.
- **Dopo 1–2 settimane**: confronto con il solito, colori della mappa, picchi.

## Provare in locale

```bash
python3 tests/simulate.py . --giorni 21          # 3 settimane di dati simulati (in data/)
python3 scraper/scrape.py --only-build --now 2026-11-08T21:40+01:00
cd docs && python3 -m http.server 8000           # http://localhost:8000
```
(Usa una copia del progetto: la simulazione scrive in `data/`.)

## Note
- L'API (`/api/jsonws/eventiregionejson.eventiregionejson/statistiche-regione`) è pubblica ma non documentata: se AREU la cambia, la raccolta fallisce e GitHub ti avvisa via email.
- Non leggere più spesso di ogni 5–10 minuti: è l'infrastruttura di un servizio di emergenza.
- Storico: circa 15–20 MB di CSV al mese.
- Progetto personale, non ufficiale. Cita sempre AREU come fonte.
