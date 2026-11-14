#!/usr/bin/env python3
"""
AREU 118 Lombardia - raccolta dati e calcolo degli indicatori per la dashboard.

Ogni esecuzione:
  1. legge l'API pubblica usata da https://www.areu.lombardia.it/web/home/missioni-aat-real-time
  2. aggiunge allo storico (data/, versionato nel repository):
       data/in_corso/AAAA-MM.csv   mezzi in missione in quel momento, una riga per zona
       data/oggi/AAAA-MM.csv       totali progressivi della giornata, una riga per zona
       data/giornaliero.csv        totale finale di ogni giorno per zona
  3. calcola gli indicatori e scrive i JSON letti dalla dashboard (--out, default docs/data):
       latest.json    situazione attuale per zona e regione (adesso, ultima ora, oggi, ieri)
       series.json    ultimi 7 giorni dei mezzi in missione
       arrivi.json    nuove missioni per ora, ultimi 8 giorni (heatmap e confronto con ieri)
       feed.json      eventi notevoli delle ultime 24 ore, ricavati confrontando le letture
       baseline.json  "il solito" (mediana delle ultime 8 settimane per giorno della settimana e ora)

Significato dei campi dell'API (verificato sui dati):
  missioni        = mezzi inviati (= msb + msa + msi + elisoccorso + mezzi_altro = somma dei codici colore)
  totale_missioni = eventi (= somma dei motivi); un evento grave puo' richiedere piu' mezzi
Solo libreria standard Python.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import glob
import json
import os
import statistics
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

API_URL = ("https://www.areu.lombardia.it/api/jsonws/"
           "eventiregionejson.eventiregionejson/statistiche-regione")
USER_AGENT = "areu-118-dashboard (progetto personale; dati pubblici AREU)"
TZ = ZoneInfo("Europe/Rome")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")

TEXT_FIELDS = ["aat", "soreu", "aggiornato_alle", "day_data"]
MOTIVI = ["medico_acuto", "caduta", "evento_violento", "incidente_stradale", "incidente_acqua",
          "incidente_montano", "incidente_speleo", "infortunio", "intossicazione", "animali",
          "calamita_naturale", "evento_massa", "soccorso_persona", "non_noto", "motivo_altro",
          "altri_motivi"]
NUM_FIELDS = [
    "missioni", "totale_missioni", "soccorsi", "totale_traffico_tel",
    "rosso", "giallo", "verde", "bianco",
    "msb", "msa", "msi", "elisoccorso", "mezzi_altro",
    "soccorso_primario", "soccorso_secondario", "informazioni", "consulenza",
    "trasporto_organi", "trasporto_prenotato", "prestazione_ambulatoriale",
    "guardia_medica", "da_richiamare", "altro", "soccorso_alpino", "soccorso_acqua",
] + MOTIVI
CSV_HEADER = ["snapshot", "api_updated"] + TEXT_FIELDS + NUM_FIELDS

SERVIZI = ["soccorso_secondario", "trasporto_organi", "soccorso_alpino"]   # in corso: servizi attivi per zona
ADESSO_KEYS = ["missioni", "totale_missioni", "rosso", "giallo", "verde", "bianco",
               "msb", "msa", "msi", "elisoccorso", "mezzi_altro"] + MOTIVI + SERVIZI
OGGI_KEYS = ADESSO_KEYS[:-len(SERVIZI)] + ["soccorso_alpino"]
# MSB delle postazioni AREU per zona (presentazione AREU, Forum Risk Management 2025; dal 2026 le postazioni
# sono di piu'): solo come riferimento. I mezzi sono contati nella zona dell'evento, quindi possono superarle.
# Postazioni con orari (schede descrittive AREU del bando 2026, file postazioni_2026.csv accanto a questo
# programma). Lodi e Mantova non hanno una scheda completa: per loro resta il numero AREU 2024.
POSTAZIONI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "postazioni_2026.csv")


def carica_postazioni() -> dict:
    out = defaultdict(list)
    try:
        with open(POSTAZIONI_FILE, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r["mezzo"] != "MSB":
                    continue
                a = int(r["dalle"][:2]) * 60 + int(r["dalle"][3:5])
                b = int(r["alle"][:2]) * 60 + int(r["alle"][3:5])
                out[r["zona"]].append((a, b, r["giorni"], float(r["quota_anno"] or 1)))
    except (OSError, KeyError, ValueError):
        return {}
    return dict(out)


def msb_in_servizio(post: dict, zona: str, t: datetime):
    """MSB delle postazioni attive a quest'ora e in questo giorno (le stagionali pesano per la quota dell'anno)."""
    righe = post.get(zona)
    if not righe:
        return None
    m, wd, tot = t.hour * 60 + t.minute, t.weekday(), 0.0
    for a, b, giorni, quota in righe:
        if not (a == b or (a <= m < b if a < b else (m >= a or m < b))):
            continue
        if (giorni == "lun-ven" and wd >= 5) or (giorni == "lun-sab" and wd >= 6):
            continue
        tot += quota
    return round(tot)


POSTAZIONI_MSB = {"BERGAMO": 32, "BRESCIA": 43, "COMO": 22, "CREMONA": 11, "LECCO": 11, "LODI": 6, "MANTOVA": 14,
                  "MILANO": 116, "MONZA": 17, "PAVIA": 18, "SONDRIO": 13, "VARESE": 26}

# motivi ordinari: compaiono nel testo del registro ma non lo rendono "rilevante"
ORDINARI = {
    "medico_acuto": ("malore / problema medico", "malori / problemi medici"),
    "caduta": ("caduta", "cadute"),
    "infortunio": ("infortunio", "infortuni"),
    "soccorso_persona": ("soccorso a persona", "soccorsi a persona"),
    "non_noto": ("motivo non noto", "motivi non noti"),
    "motivo_altro": ("altro motivo", "altri motivi"),
    "altri_motivi": ("altro motivo", "altri motivi"),
}
# eventi che vale la pena segnalare nel feed: (singolare, plurale)
NOTABILI = {
    "incidente_stradale": ("incidente stradale", "incidenti stradali"),
    "evento_violento": ("evento violento", "eventi violenti"),
    "incidente_montano": ("incidente in montagna", "incidenti in montagna"),
    "incidente_acqua": ("incidente in acqua", "incidenti in acqua"),
    "incidente_speleo": ("incidente speleo/forra", "incidenti speleo/forra"),
    "evento_massa": ("evento di massa", "eventi di massa"),
    "calamita_naturale": ("calamità naturale", "calamità naturali"),
    "intossicazione": ("intossicazione", "intossicazioni"),
    "soccorso_alpino": ("intervento col Soccorso Alpino", "interventi col Soccorso Alpino"),
    "animali": ("evento con animali", "eventi con animali"),
}

BASELINE_DAYS = 56
SERIES_DAYS = 7
HEATMAP_DAYS = 8
FEED_HOURS = 24
FEED_MAX = 4000      # voci del registro 'Tutti' (le rilevanti sono sempre tutte); 24 ore anche a 5 minuti
GAP_MIN = 40          # oltre questo buco tra letture un'ora non e' calcolabile


# ============================================================ lettura API
def fetch(retries: int = 2) -> dict:
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(API_URL, headers={"User-Agent": USER_AGENT,
                                                           "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Impossibile leggere l'API AREU: {last}")


def to_int(v):
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def iso_day(day_data: str) -> str:
    try:
        d, m, y = day_data.strip().split("/")
        return f"{y}-{m}-{d}"
    except ValueError:
        return ""


def normalize(row: dict) -> dict:
    out = {k: (row.get(k) or "").strip() for k in TEXT_FIELDS}
    out["day"] = (row.get("day") or "").strip()
    for k in NUM_FIELDS:
        out[k] = to_int(row.get(k))
    return out


# ============================================================ storico su disco
def migrate_header(path: str) -> None:
    """Se un CSV esistente ha colonne diverse (versione precedente dello scraper), lo riscrive
    con le colonne attuali, conservando tutti i valori presenti."""
    if not os.path.exists(path):
        return
    with open(path, newline="", encoding="utf-8") as f:
        first = next(csv.reader(f), None)
    if first is None or first == CSV_HEADER:
        return
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def append_rows(kind: str, rows: list[dict], snap: datetime, api_updated: str) -> None:
    path = os.path.join(DATA_DIR, kind, snap.strftime("%Y-%m") + ".csv")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    migrate_header(path)
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(CSV_HEADER)
        for r in rows:
            w.writerow([snap.isoformat(timespec="minutes"), api_updated]
                       + [r[k] for k in TEXT_FIELDS]
                       + ["" if r[k] is None else r[k] for k in NUM_FIELDS])


def upsert_giornaliero(rows: list[dict]) -> dict:
    """Aggiorna i totali per (giorno, zona) e restituisce {(giorno, zona): {campo: valore}}."""
    path = os.path.join(DATA_DIR, "giornaliero.csv")
    header = ["data", "aat", "soreu", "aggiornato_alle"] + NUM_FIELDS
    table: dict[tuple, dict] = {}
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for rec in csv.DictReader(f):
                table[(rec["data"], rec["aat"])] = rec
    for r in rows:
        d = iso_day(r["day_data"])
        if not d or not r["aat"]:
            continue
        rec = {"data": d, "aat": r["aat"], "soreu": r["soreu"], "aggiornato_alle": r["aggiornato_alle"]}
        rec.update({k: "" if r[k] is None else r[k] for k in NUM_FIELDS})
        table[(d, r["aat"])] = rec
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        for key in sorted(table):
            w.writerow(table[key])
    return {k: {f: to_int(v.get(f)) for f in OGGI_KEYS} for k, v in table.items()}


def load(kind: str, since: datetime, keys: list[str]) -> dict[datetime, dict[str, dict]]:
    """{snapshot: {zona: {campo: int, 'day': aaaa-mm-gg, 'agg': hh:mm, 'soreu': ...}}}"""
    out: dict[datetime, dict[str, dict]] = defaultdict(dict)
    first_month = since.strftime("%Y-%m")
    for p in sorted(glob.glob(os.path.join(DATA_DIR, kind, "*.csv"))):
        if os.path.basename(p)[:7] < first_month:
            continue
        with open(p, newline="", encoding="utf-8") as f:
            for rec in csv.DictReader(f):
                try:
                    ts = datetime.fromisoformat(rec["snapshot"])
                except (KeyError, ValueError):
                    continue
                if ts < since:
                    continue
                d = {k: to_int(rec.get(k)) for k in keys}
                d["day"] = iso_day(rec.get("day_data", ""))
                d["agg"] = rec.get("aggiornato_alle", "")
                d["soreu"] = rec.get("soreu", "")
                out[ts][rec["aat"]] = d
    return dict(out)


# ============================================================ calcoli
class Calc:
    def __init__(self, inc, ogg, finals, now: datetime):
        self.inc, self.ogg, self.finals, self.now = inc, ogg, finals, now
        self.ts = sorted(set(inc) | set(ogg))
        self.aats = sorted({a for snap in inc.values() for a in snap})
        # indice: (zona, giorno) -> [(lettura, missioni cumulate)]
        self.pts: dict[tuple, list] = defaultdict(list)
        for t in sorted(ogg):
            for a, d in ogg[t].items():
                if d["day"] and d["missioni"] is not None:
                    self.pts[(a, d["day"])].append((t, d["missioni"]))

    # -- utilita'
    def near(self, target: datetime, tol_min: int):
        """Lettura piu' vicina a target entro tol_min minuti."""
        i = bisect.bisect_left(self.ts, target)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(self.ts):
                dt = abs((self.ts[j] - target).total_seconds()) / 60
                if dt <= tol_min and (best is None or dt < best[0]):
                    best = (dt, self.ts[j])
        return best[1] if best else None

    def delta(self, aat: str, t0: datetime, t1: datetime, field: str):
        """Quanti <field> si sono aggiunti tra t0 e t1 (gestisce il passaggio di mezzanotte)."""
        p, c = self.ogg.get(t0, {}).get(aat), self.ogg.get(t1, {}).get(aat)
        if not p or not c or p.get(field) is None or c.get(field) is None:
            return None
        if c["day"] == p["day"]:
            d = c[field] - p[field]
            return d if d >= 0 else None
        if c["day"] > p["day"]:
            fin = self.finals.get((p["day"], aat), {}).get(field)
            return (max(0, fin - p[field]) if fin is not None else 0) + c[field]
        return None

    # -- ritmo e durata
    def ritmo(self, aat: str, last: datetime):
        out = {"arrivi_1h": None, "arrivi_3h": None, "durata_min": None}
        for hours, key in ((1, "arrivi_1h"), (3, "arrivi_3h")):
            t0 = self.near(last - timedelta(hours=hours), 15)
            if t0 is None or t0 >= last:
                continue
            d = self.delta(aat, t0, last, "missioni")
            if d is not None:
                minutes = (last - t0).total_seconds() / 60
                out[key] = round(d * hours * 60 / minutes)
        mezzi = (self.inc.get(last, {}).get(aat) or {}).get("missioni")
        if out["arrivi_3h"] and mezzi is not None:
            out["durata_min"] = round(mezzi / (out["arrivi_3h"] / 3) * 60)   # legge di Little
        return out

    # -- arrivi orari
    def hourly(self, aat: str, day: date):
        """Nuove missioni per ciascuna ora del giorno (None se non calcolabile)."""
        pts = self.pts.get((aat, day.isoformat()), [])
        if not pts:
            return [None] * 24
        times = [p[0] for p in pts]
        start = datetime(day.year, day.month, day.day, tzinfo=TZ)

        def cum_end(h):
            if h < 0:
                return 0
            boundary = start + timedelta(hours=h + 1)
            if boundary > self.now:
                boundary = self.now
            i = bisect.bisect_right(times, boundary) - 1   # include la lettura esattamente al confine (es. adesso)
            if i < 0:
                return None
            if (boundary - times[i]).total_seconds() / 60 > GAP_MIN:
                return None
            return pts[i][1]

        out = []
        for h in range(24):
            if start + timedelta(hours=h) >= self.now:
                out.append(None)
                continue
            a, b = cum_end(h - 1), cum_end(h)
            out.append(None if a is None or b is None or b < a else b - a)
        return out


COLORI = [("rosso", "rosso", "rossi"), ("giallo", "giallo", "gialli"), ("verde", "verde", "verdi"),
          ("bianco", "bianco", "bianchi")]
AVANZATI = ("msi", "msa")      # MSA1, MSA2


def deduci_codici(tipi: dict, codici: dict, *, no_verde_avanzati: bool = False,
                  coppia: str | None = None, fuori_prima: dict | None = None):
    """Prova a ricostruire il codice colore di ciascun tipo di mezzo partito tra due letture.

    tipi   = {"msb": 3, "msi": 1, ...}      mezzi partiti per tipo
    codici = {"rosso": 1, "giallo": 3, ...} mezzi partiti per codice (stessa somma)
    Regole opzionali (molto probabili, non certe):
      no_verde_avanzati: MSA1/MSA2 non partono in codice verde o bianco
      coppia="forte":  ogni MSA1/MSA2 ha un MSB partito nello stesso intervallo con lo stesso colore
      coppia="debole": ogni MSA1/MSA2 ha lo stesso colore di un MSB partito ora OPPURE di un mezzo
                       che era gia' fuori (fuori_prima = mezzi in corso per colore alla lettura precedente)
    Restituisce {tipo: {colore: n}} se esiste UNA SOLA ricostruzione compatibile, altrimenti None.
    """
    tipi = {k: v for k, v in tipi.items() if v}
    codici = {k: v for k, v in codici.items() if v}
    if not tipi or sum(tipi.values()) != sum(codici.values()):
        return None
    col = [c for c, _, _ in COLORI if codici.get(c)]
    ordine = [k for k in tipi if k != "msb"] + (["msb"] if "msb" in tipi else [])
    soluzioni = []

    def ripartizioni(n, colori, resto, tipo):
        vietati = ("verde", "bianco") if no_verde_avanzati and tipo in AVANZATI else ()
        colori = [c for c in colori if c not in vietati]
        if not colori:
            return
        if len(colori) == 1:
            if n <= resto[colori[0]]:
                yield {colori[0]: n}
            return
        c = colori[0]
        for x in range(min(n, resto[c]), -1, -1):
            for r in ripartizioni(n - x, colori[1:], resto, None if not vietati else "msb"):
                yield {c: x, **r}

    def valida(parz):
        if coppia is None:
            return True
        msb = parz.get("msb", {})
        for c in col:
            av = sum(parz.get(k, {}).get(c, 0) for k in AVANZATI)
            limite = msb.get(c, 0) + ((fuori_prima or {}).get(c, 0) if coppia == "debole" else 0)
            if av > limite:
                return False
        return True

    def cerca(i, resto, parz):
        if len(soluzioni) > 1:
            return
        if i == len(ordine):
            if all(v == 0 for v in resto.values()) and valida(parz):
                soluzioni.append({k: {c: n for c, n in v.items() if n} for k, v in parz.items()})
            return
        t = ordine[i]
        for rip in ripartizioni(tipi[t], col, resto, t):
            nuovo = {c: resto[c] - rip.get(c, 0) for c in col}
            cerca(i + 1, nuovo, {**parz, t: rip})

    cerca(0, dict(codici), {})
    return soluzioni[0] if len(soluzioni) == 1 else None


def ricostruisci_codici(tipi: dict, codici: dict, *, fuori_prima, controllo_scorte):
    """Codice colore per tipo di mezzo partito. Restituisce (ricostruzione, certezza) o (None, None).

    certezza: "certa" -> nessuna supposizione (un solo tipo, un solo colore, ecc.)
              "alta"  -> regole molto probabili: MSA1/MSA2 non partono in verde/bianco e hanno il colore
                         di un MSB partito ora o di un mezzo gia' fuori (versione 'debole' della coppia)
    La versione rigida della coppia (affidabile solo al 46-68%) non e' piu' usata.
    controllo_scorte: True = superato, False = fallito (cambio di colore: non dedurre), None = non verificabile
    """
    if controllo_scorte is False:
        return None, None
    tipi = {k: v or 0 for k, v in tipi.items()}
    rip, cert = deduci_codici(tipi, codici), "certa"
    if not rip and sum(tipi.get(k, 0) for k in AVANZATI):
        rip, cert = deduci_codici(tipi, codici, no_verde_avanzati=True, coppia="debole",
                                  fuori_prima=fuori_prima), "alta"
    if not rip:
        return None, None
    if controllo_scorte is None and cert == "certa":
        cert = "alta"
    return rip, cert


# ============================================================ registro: testi
TIPI_MEZZO = ("msb", "msi", "msa", "elisoccorso", "mezzi_altro")
SIGLE = {"msb": ("MSB", "MSB"), "msi": ("MSA1", "MSA1"), "msa": ("MSA2", "MSA2"),
         "elisoccorso": ("elisoccorso", "elisoccorsi"), "mezzi_altro": ("altro mezzo", "altri mezzi")}
MOT_NOME = {**ORDINARI, **NOTABILI, "medico_acuto": ("malore", "malori")}
PARTICOLARI = {"incidente_montano": "Incidente in montagna", "incidente_acqua": "Incidente in acqua",
               "incidente_speleo": "Incidente speleo / forra", "evento_massa": "Evento di massa",
               "calamita_naturale": "Calamità naturale", "soccorso_alpino": "Intervento con il Soccorso Alpino"}
SCENA = ("incidente_stradale", "evento_violento", "infortunio", "incidente_montano", "incidente_acqua",
         "incidente_speleo", "evento_massa", "calamita_naturale")
REGOLARE_MIN = 15     # letture piu' distanti: niente dettaglio fine
# Il programma funziona con letture ogni 5 o ogni 10 minuti: tutte le regole sono espresse in minuti, non in
# numero di letture.
SUBITO_MIN = 6        # MSB arrivato entro 6' (lettura successiva, se si legge ogni 5'): conta come mezzo "subito",
                      # cosi' le statistiche restano confrontabili con le letture ogni 10'
PICCO_CONFERMA_MIN = 10   # un picco deve durare almeno 10' (2 letture a 5', 2 a 10') prima di essere segnalato
RAFFICA_MIN = 10      # i codici rossi si sommano sugli ultimi 10' (una lettura a 10', due a 5')
SCADENZA_MIN = 30     # scheda senza alcun mezzo da 30': probabilmente chiusa senza invio (stima)
COMPLESSO_EXTRA = 2   # MSB in piu' delle schede nuove in ~20' (calibrato sui dati)


def n_nome(n: int, k: str) -> str:
    sg, pl = MOT_NOME.get(k, (k.replace("_", " "), k.replace("_", " ")))
    return f"{n} {sg if n == 1 else pl}"


def testo_motivi(mot: dict) -> str:
    return ", ".join(n_nome(v, k) for k, v in sorted(mot.items(), key=lambda kv: -kv[1]))


def testo_mezzi(d_tipi: dict, rip: dict | None) -> tuple[str, int, list]:
    """Testo dei mezzi partiti e numero di coppie MSB + avanzato.

    '1 MSB + 1 MSA2 sulla stessa missione [rosso] + 1 MSB [verde]' oppure '2 MSB (1 [rosso], 1 [giallo])'
    oppure '2 MSB + 1 MSA1'. Un MSA1/MSA2 non va mai da solo: se ha lo stesso codice di un MSB partito
    nella stessa lettura, e' sulla stessa missione (succede nel ~75% dei casi, contro ~30% per caso).
    I colori tra parentesi quadre vengono resi come badge dalla dashboard."""
    rest = {t: dict((rip or {}).get(t, {})) for t in TIPI_MEZZO}
    left = {t: max(d_tipi.get(t) or 0, 0) for t in TIPI_MEZZO}
    pezzi, coppie = [], 0
    if rip:
        for t in ("msa", "msi"):
            for c, _, _ in COLORI:
                m = min(rest[t].get(c, 0), rest["msb"].get(c, 0))
                if m:
                    pezzi.append(f"{m} MSB + {m} {SIGLE[t][0]} sulla stessa missione [{c}]")
                    rest[t][c] -= m; rest["msb"][c] -= m
                    left[t] -= m; left["msb"] -= m
                    coppie += m
    for t in TIPI_MEZZO:
        n = left[t]
        if n <= 0:
            continue
        nome = SIGLE[t][0 if n == 1 else 1]
        cols = [(c, rest[t][c]) for c, _, _ in COLORI if rip and rest[t].get(c)]
        if len(cols) == 1:
            pezzi.append(f"{n} {nome} [{cols[0][0]}]")
        elif cols:
            pezzi.append(f"{n} {nome} (" + ", ".join(f"{v} [{c}]" for c, v in cols) + ")")
        else:
            pezzi.append(f"{n} {nome}")
    # mezzi non-MSB rimasti fuori dalle coppie: (tipo, colore o None se non ricostruibile)
    soli = []
    for t in ("msa", "msi", "elisoccorso", "mezzi_altro"):
        cols = [c for c, _, _ in COLORI for _ in range(rest[t].get(c, 0))] if rip else []
        soli += [(t, cols[j] if j < len(cols) else None) for j in range(max(left[t], 0))]
    return " + ".join(pezzi), coppie, soli


def testo_colori(d_cod: dict) -> str:
    return ", ".join(f"{d_cod[c]} [{c}]" for c, _, _ in COLORI if (d_cod.get(c) or 0) > 0)


def plur(n: int, sg: str, pl: str) -> str:
    return f"{n} {sg if n == 1 else pl}"


def coda_binomiale(k: int, n: int, p: float) -> float:
    """P(X >= k) per X ~ Binomiale(n, p)."""
    import math
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1))


def poisson_coda(k: int, lam: float) -> float:
    """P(X >= k) per X ~ Poisson(lam)."""
    import math
    term, cdf = math.exp(-lam), 0.0
    for i in range(k):
        cdf += term
        term *= lam / (i + 1)
    return max(0.0, 1.0 - cdf)


def region_sum(dicts: list[dict | None], keys: list[str]):
    dicts = [d for d in dicts if d]
    if not dicts:
        return None
    out = {}
    for k in keys:
        vals = [d.get(k) for d in dicts if d.get(k) is not None]
        out[k] = sum(vals) if vals else None
    return out


def stats(vals):
    vals = sorted(vals)
    if not vals:
        return None
    p90 = vals[min(len(vals) - 1, int(round(0.9 * (len(vals) - 1))))]
    return [round(statistics.median(vals), 1), p90, len(vals)]


def wdh(t: datetime) -> str:
    return f"{t.weekday()}-{t.hour}"


def stale_minutes(agg: str, now: datetime):
    try:
        h, m = map(int, agg.split(":"))
    except ValueError:
        return None
    t = now.replace(hour=h, minute=m)
    if t > now + timedelta(minutes=5):
        t -= timedelta(days=1)
    return max(0, round((now - t).total_seconds() / 60))


def build(now: datetime, finals: dict) -> dict:
    since = now - timedelta(days=BASELINE_DAYS + 1)
    inc = load("in_corso", since, ADESSO_KEYS)
    ogg = load("oggi", since, OGGI_KEYS)
    # una zona senza interventi in corso non compare nei dati "in corso" (mai una riga con 0): e' uno zero
    for t, snap in inc.items():
        for a_, o_ in (ogg.get(t) or {}).items():
            if a_ not in snap:
                snap[a_] = {**{k: 0 for k in ADESSO_KEYS}, "day": o_["day"], "agg": "", "soreu": o_["soreu"],
                            "vuota": True}
    C = Calc(inc, ogg, finals, now)
    last = max(inc) if inc else None
    today = now.date()
    out = {}

    # ---------------- baseline (giorni precedenti a oggi)
    base_inc: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    days_seen = set()
    for t, snap in inc.items():
        if t.date() >= today:
            continue
        days_seen.add(t.date())
        tot, n = 0, 0
        for a, d in snap.items():
            if d["missioni"] is not None:
                base_inc[a][wdh(t)].append(d["missioni"])
                tot += d["missioni"]; n += 1
        if n:
            base_inc["REGIONE"][wdh(t)].append(tot)
    base_arr: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    hourly_cache: dict[tuple, list] = {}
    all_days = sorted({date.fromisoformat(d["day"]) for s in ogg.values() for d in s.values() if d["day"]})
    for day in all_days:
        region = [0] * 24
        region_ok = [True] * 24
        for a in C.aats:
            hs = C.hourly(a, day)
            hourly_cache[(a, day)] = hs
            for h, v in enumerate(hs):
                if v is None:
                    region_ok[h] = False
                else:
                    region[h] += v
                    if day < today:
                        base_arr[a][f"{day.weekday()}-{h}"].append(v)
        rh = [region[h] if region_ok[h] else None for h in range(24)]
        hourly_cache[("REGIONE", day)] = rh
        if day < today:
            for h, v in enumerate(rh):
                if v is not None:
                    base_arr["REGIONE"][f"{day.weekday()}-{h}"].append(v)
    baseline = {
        "giorni_di_storico": len(days_seen),
        "in_corso": {a: {k: stats(v) for k, v in m.items()} for a, m in base_inc.items()},
        "arrivi_ora": {a: {k: stats(v) for k, v in m.items()} for a, m in base_arr.items()},
    }
    out["baseline.json"] = baseline

    def solito(table, a, t):
        v = baseline[table].get(a, {}).get(wdh(t))
        return {"med": v[0], "p90": v[1], "n": v[2]} if v and v[2] >= 2 else None

    # ---------------- latest
    if last is None:
        raise RuntimeError("nessuna lettura disponibile")
    yest = today - timedelta(days=1)
    t_ieri = C.near(last - timedelta(hours=24), 25)
    # carico: MSB fuori oltre il 90o percentile della zona negli ultimi 7 giorni. Fuori Milano le schede che
    # aspettano 15' o piu' (o restano senza invio) passano dal 13% al 21% (verificato); a Milano nessun effetto.
    POST = carica_postazioni()
    p90_msb = {}
    for a in C.aats:
        v = sorted(d[a]["msb"] for t, d in inc.items() if t >= last - timedelta(days=7)
                   and d.get(a) and d[a].get("msb") is not None)
        if len(v) >= 288:
            p90_msb[a] = v[int(0.9 * (len(v) - 1))]
    zone = []
    for a in C.aats:
        i = inc[last].get(a)
        o = ogg.get(last, {}).get(a)
        fin_ieri = finals.get((yest.isoformat(), a))
        ieri_ora = None
        if t_ieri and a in ogg.get(t_ieri, {}) and ogg[t_ieri][a]["day"] == yest.isoformat():
            ieri_ora = {k: ogg[t_ieri][a][k] for k in OGGI_KEYS}
        zone.append({
            "aat": a,
            "soreu": (i or o or {}).get("soreu", ""),
            "agg_adesso": (i or {}).get("agg"),
            "fermo_da_min": stale_minutes((i or {}).get("agg", ""), last),
            "agg_oggi": (o or {}).get("agg"),
            "adesso": {k: i[k] for k in ADESSO_KEYS} if i else None,
            "oggi": {k: o[k] for k in OGGI_KEYS} if o and o["day"] == today.isoformat() else None,
            "ieri": fin_ieri,
            "ieri_stessa_ora": ieri_ora,
            "ritmo": C.ritmo(a, last),
            "solito_adesso": solito("in_corso", a, last),
            "solito_arrivi_ora": solito("arrivi_ora", a, last),
            "postazioni_msb": POSTAZIONI_MSB.get(a),
            "msb_servizio": msb_in_servizio(POST, a, last) if a in POST else POSTAZIONI_MSB.get(a),
            "msb_servizio_fonte": "orari 2026" if a in POST else "AREU 2024, senza orari",
            "msb_p90_7g": p90_msb.get(a),
            "carico_alto": bool(a != "MILANO" and i and p90_msb.get(a) and (i.get("msb") or 0) >= p90_msb[a]
                                and (i.get("msb") or 0) >= 3),
            "senza_mezzo_zona": max(0, (i.get("totale_missioni") or 0) - (i.get("missioni") or 0)) if i else None,
        })
    reg_ritmo = {"arrivi_1h": None, "arrivi_3h": None, "durata_min": None}
    for k in ("arrivi_1h", "arrivi_3h"):
        vals = [z["ritmo"][k] for z in zone]
        reg_ritmo[k] = sum(vals) if vals and all(v is not None for v in vals) else None
    reg_adesso = region_sum([z["adesso"] for z in zone], ADESSO_KEYS)
    if reg_ritmo["arrivi_3h"] and reg_adesso and reg_adesso["missioni"] is not None:
        reg_ritmo["durata_min"] = round(reg_adesso["missioni"] / (reg_ritmo["arrivi_3h"] / 3) * 60)
    regione = {
        "aat": "LOMBARDIA",
        "adesso": reg_adesso,
        "oggi": region_sum([z["oggi"] for z in zone], OGGI_KEYS),
        "ieri": region_sum([z["ieri"] for z in zone], OGGI_KEYS),
        "ieri_stessa_ora": region_sum([z["ieri_stessa_ora"] for z in zone], OGGI_KEYS)
        if all(z["ieri_stessa_ora"] for z in zone) else None,
        "ritmo": reg_ritmo,
        "solito_adesso": solito("in_corso", "REGIONE", last),
        "solito_arrivi_ora": solito("arrivi_ora", "REGIONE", last),
    }
    out["latest.json"] = {"snapshot": last.isoformat(timespec="minutes"),
                          "giorni_di_storico": baseline["giorni_di_storico"],
                          "regione": regione, "zone": zone}

    # ---------------- serie 7 giorni
    s_since = last - timedelta(days=SERIES_DAYS)
    ts7 = [t for t in sorted(inc) if t >= s_since]
    fleet = ["missioni", "rosso", "msb", "msa", "msi", "elisoccorso"]
    reg = {k: [] for k in fleet}
    per = {a: [] for a in C.aats}
    for t in ts7:
        snap = inc[t]
        for a in C.aats:
            per[a].append((snap.get(a) or {}).get("missioni"))
        for k in fleet:
            vals = [d.get(k) for d in snap.values() if d.get(k) is not None]
            reg[k].append(sum(vals) if vals else None)
    out["series.json"] = {"t": [t.isoformat(timespec="minutes") for t in ts7],
                          "regione": reg, "zone": per}

    # ---------------- arrivi orari (heatmap)
    hdays = [today - timedelta(days=i) for i in range(HEATMAP_DAYS - 1, -1, -1)]
    out["arrivi.json"] = {
        "giorni": [d.isoformat() for d in hdays],
        "ora_corrente": now.hour,
        "regione": [hourly_cache.get(("REGIONE", d), [None] * 24) for d in hdays],
        "zone": {a: [hourly_cache.get((a, d), [None] * 24) for d in hdays] for a in C.aats},
    }

    # ---------------- registro eventi e bilancio schede / mezzi
    # Le "schede" (eventi) sono contate quando la centrale apre la scheda, i mezzi quando partono.
    # Confrontando ogni lettura con la precedente si separano: schede con mezzo, schede chiuse senza
    # invio (eventi chiusi senza che si liberi un mezzo), schede in attesa del mezzo, MSB partiti senza
    # una scheda nuova (rinforzi o schede registrate dopo). Una scheda senza MSB e un MSB senza scheda
    # nella stessa zona a meno di 60' vengono abbinati (stesso intervento, letto in due momenti).
    f_since = last - timedelta(hours=FEED_HOURS)
    midnight_today = datetime(today.year, today.month, today.day, tzinfo=TZ)
    start = min(f_since, midnight_today) - timedelta(minutes=SCADENZA_MIN + 30)
    tsf = [t for t in C.ts if t >= start]
    feed = []
    surge_on: dict[str, bool] = {}
    surge_prev: dict[str, datetime] = {}     # da quando la zona e' sopra il solito
    rossi_rec: dict[str, list] = defaultdict(list)   # (ora, rossi, mezzi) delle letture recenti, per la raffica
    hh = lambda t: t.strftime("%H:%M")

    def surge_state(a, t, v):
        b = solito("in_corso", a, t)
        if not b or v is None or b["n"] < 4:     # servono almeno 4 settimane di storico per quell'ora
            return None, b
        # "sopra il solito" in modo netto: oltre i picchi abituali di almeno 2 mezzi (o 15%) e almeno
        # 5 mezzi (o 40%) sopra la mediana, cosi' le oscillazioni casuali delle zone piccole non contano
        above = v >= b["p90"] + max(2, 0.15 * b["med"]) and v - b["med"] >= max(5, 0.4 * b["med"])
        back = v <= b["med"] * 1.1
        return ("su" if above else "giu" if back else "mezzo"), b

    def rel(t, a, cat, tit, det=""):
        if t > f_since:
            feed.append({"t": t.isoformat(timespec="minutes"), "aat": a, "k": "r",
                         "cat": cat, "tit": tit, "det": det})

    # --- dati per i traguardi: totali completi dei giorni passati (giornaliero.csv)
    past_days = sorted({d for (d, _a) in finals if d < today.isoformat()}, reverse=True)[:28]
    def day_total(d, a=None):
        if a:
            return (finals.get((d, a)) or {}).get("missioni")
        vals = [(finals.get((d, z)) or {}).get("missioni") for z in C.aats]
        return sum(vals) if vals and all(v is not None for v in vals) else None
    ieri_tot_reg = day_total(yest.isoformat())
    def cum_reg(t, k="missioni"):
        snap = ogg.get(t, {})
        vals = [d.get(k) for d in snap.values() if d.get("day") == today.isoformat()]
        return sum(v for v in vals if v is not None) if len(vals) == len(C.aats) else None
    prev_peak = [sum((d.get("missioni") or 0) for d in inc[t].values())
                 for t in inc if today - timedelta(days=7) <= t.date() < today]
    prev_peak_max = max(prev_peak) if prev_peak and len({t.date() for t in inc if t.date() < today}) >= 3 else None
    peak_done = False
    trend_last: dict[str, datetime] = {}
    TREND = {"incidente_stradale": "incidenti stradali", "caduta": "cadute",
             "intossicazione": "intossicazioni", "evento_violento": "eventi violenti"}

    # --- schede senza mezzo, per zona (saldo)
    # Il contatore "oggi" conta TUTTE le schede appena aperte; il contatore "in corso" conta solo le schede
    # che hanno un mezzo in missione (verificato sui dati: una scheda senza mezzo non vi compare finche' non
    # parte il primo mezzo). Quindi una scheda senza mezzo e' visibile solo come differenza tra schede nuove
    # e mezzi partiti, e la sua chiusura senza invio non e' visibile in nessun contatore.
    # Il saldo:
    #   - sale con le schede nuove che nella stessa lettura non ricevono nessun mezzo (MSB, oppure MSA1/MSA2,
    #     elisoccorso o altro mezzo partiti da soli per loro);
    #   - scende quando parte un MSB "in piu'" (piu' MSB che schede nuove): copertura della scheda piu' vecchia;
    #   - una scheda senza mezzo da oltre 30' esce come "probabilmente chiusa senza invio" (stima: oltre 30'
    #     gli MSB in piu' arrivano quasi solo per coincidenza; verificato sui dati e su simulazione).
    # Un MSB in piu' con il saldo a zero e' un rinforzo su un intervento gia' attivo.
    # L'eta' del buco e' un intervallo: ogni lettura con schede nuove e MSB puo' "spostare" il buco su una
    # scheda piu' recente.
    saldo: dict[str, list] = defaultdict(list)   # schede senza mezzo: {"t", "e", "rot"(prima rotazione), "day", "h"}
    stato: dict[str, dict] = defaultdict(dict)   # dal (scheda senza mezzo piu' vecchia), rot (ultima rotazione)
    conti: dict[tuple, dict] = defaultdict(lambda: defaultdict(int))     # (zona, giorno) -> contatori
    ore: dict[tuple, list] = defaultdict(lambda: [[0, 0] for _ in range(24)])  # [schede, chiuse senza invio]
    storia: dict[str, list] = defaultdict(list)  # ultimi intervalli per "piu' ambulanze sulla stessa scena"
    ultimo_avviso: dict[tuple, datetime] = {}
    saldo_prec: dict[str, int] = defaultdict(int)
    inc_rec: dict[str, list] = defaultdict(list)    # (lettura, schede per incidente, incidenti in corso prima)
    scad_rec: dict[str, list] = defaultdict(list)
    anticipo: dict[str, bool] = {}                  # "in corso" aggiornato prima di "oggi" nella lettura precedente   # schede date per chiuse negli ultimi 30' (possono essere coperte dopo)

    def esito(u, txt):
        e = u.get("e")
        if e is not None:
            es = e.setdefault("_es", {})
            es[txt] = es.get(txt, 0) + 1

    def togli_esito(u, txt):
        es = (u.get("e") or {}).get("_es")
        if es and es.get(txt):
            es[txt] -= 1
            if not es[txt]:
                del es[txt]

    def quando(u, t1):
        """Orario della copertura: preciso se il buco non puo' aver ruotato, altrimenti intervallo."""
        if u.get("rot") and u["rot"] < t1:
            return f"tra le {hh(u['rot'])} e le {hh(t1)}"
        return f"alle {hh(t1)}"

    def togli(a, n, t1, testo):
        """Toglie dal saldo le n schede piu' vecchie (FIFO) e scrive l'esito sulla voce in cui erano comparse."""
        tolte = 0
        while n > 0 and saldo[a]:
            u = saldo[a].pop(0)
            esito(u, testo(u))
            n -= 1; tolte += 1
        return tolte

    def flusso(a, t0, t1):
        o0, o1 = ogg.get(t0, {}).get(a), ogg.get(t1, {}).get(a)
        i0, i1 = inc.get(t0, {}).get(a), inc.get(t1, {}).get(a)
        if not o0 or not o1 or not o1.get("day"):
            return None
        if not o0.get("day"):
            # subito dopo mezzanotte AREU azzera i contatori e per qualche lettura lascia la data vuota: la prima
            # lettura con la data nuova conta dall'inizio della giornata (prima andava persa ~1 scheda per zona)
            if any(o0.get(k) for k in ("totale_missioni", "missioni")):
                return None
            o0 = {**o0, "day": ""}
        if o1["day"] < o0["day"]:
            return None
        stesso = o0["day"] == o1["day"]

        def dd(k):
            if o1.get(k) is None:
                return None
            if not stesso:
                return o1[k]          # nuovo giorno: conta solo la parte dopo mezzanotte
            return None if o0.get(k) is None else o1[k] - o0[k]
        f = {"o0": o0, "o1": o1, "i0": i0, "i1": i1, "stesso": stesso,
             "de": dd("totale_missioni"), "dm": dd("missioni"),
             "tipi": {k: dd(k) for k in TIPI_MEZZO}, "cod": {c: dd(c) for c, _, _ in COLORI},
             "mot": {k: dd(k) for k in MOTIVI + ["soccorso_alpino"]}}
        if f["de"] is None or f["dm"] is None or any(v is None for v in f["tipi"].values()):
            return None
        # letture coerenti: "in corso" non deve essere piu' avanti di "oggi" (verificato: con questa regola i mezzi
        # rientrati sono calcolabili nel ~99% degli intervalli e l'accordo con gli interventi in corso resta al 94-96%)
        f["allineati"] = bool(stesso and i0 and i1 and (i0.get("agg") or "") <= (o0.get("agg") or "~")
                              and (i1.get("agg") or "") <= (o1.get("agg") or "~")
                              and None not in (i0.get("missioni"), i1.get("missioni"),
                                               i0.get("totale_missioni"), i1.get("totale_missioni")))
        f["rientri"] = f["dm"] - (i1["missioni"] - i0["missioni"]) if f["allineati"] else None
        return f

    for t0, t1 in zip(tsf, tsf[1:]):
        gap = (t1 - t0).total_seconds() / 60
        regolare = gap <= REGOLARE_MIN
        vicino = gap <= GAP_MIN

        # --- picco / rientro per zona e per regione
        if vicino:
            for a in C.aats + ["LOMBARDIA"]:
                if a == "LOMBARDIA":
                    v = sum((d.get("missioni") or 0) for d in inc.get(t1, {}).values()) if t1 in inc else None
                    stt, b = surge_state("REGIONE", t1, v)
                else:
                    v = (inc.get(t1, {}).get(a) or {}).get("missioni")
                    stt, b = surge_state(a, t1, v)
                # il picco va confermato: sopra il solito da almeno 10' (non da una sola lettura), e non si ripete
                # entro 90 minuti
                if stt == "su":
                    surge_prev.setdefault(a, t1)
                else:
                    surge_prev.pop(a, None)
                confermato = a in surge_prev and t1 - surge_prev[a] >= timedelta(minutes=PICCO_CONFERMA_MIN - 1)
                if (stt == "su" and confermato and not surge_on.get(a)
                        and t1 - ultimo_avviso.get(("picco", a), datetime.min.replace(tzinfo=TZ)) > timedelta(minutes=90)):
                    surge_on[a] = True
                    ultimo_avviso[("picco", a)] = t1
                    rel(t1, a, "picco", f"Mezzi fuori sopra il solito: {v}",
                        f"di solito a quest'ora circa {b['med']:g}, picchi abituali fino a {b['p90']:g}")
                elif stt == "giu" and surge_on.get(a):
                    surge_on[a] = False
                    rel(t1, a, "rientro", f"Situazione rientrata: {v} mezzi fuori",
                        f"di solito a quest'ora circa {b['med']:g}")

        for a in C.aats:
            f = flusso(a, t0, t1)
            if f is None:
                continue
            # A volte "in corso" conta i mezzi partiti una lettura prima di "oggi" (Sondrio 14/11 16:55: +2 mezzi in
            # corso, 0 negli eventi di oggi, che li contano alle 16:58). I mezzi rientrati verrebbero negativi e poi
            # gonfiati alla lettura dopo, e i colori sembrerebbero riclassificati: in queste due letture niente
            # rientri, riclassificazioni ne' controllo con gli interventi in corso (~0,3% degli intervalli).
            if anticipo.pop(a, False) and f["allineati"]:
                f["allineati"], f["rientri"] = False, None
            if f["rientri"] is not None and f["rientri"] < 0:
                anticipo[a] = True
                f["allineati"], f["rientri"] = False, None
            day, h = f["o1"]["day"], t1.hour
            de, dm, tp, cod, mot = f["de"], f["dm"], f["tipi"], f["cod"], f["mot"]
            i0, i1, o1 = f["i0"], f["i1"], f["o1"]
            de_c, dB = max(de, 0), max(tp["msb"], 0)
            adv = sum(max(tp[k], 0) for k in ("msi", "msa", "elisoccorso"))
            ore[(a, day)][h][0] += de_c
            pos = {k: v for k, v in mot.items() if k in MOTIVI and v and v > 0}
            neg = {k: -v for k, v in mot.items() if k in MOTIVI and v and v < 0}
            entry = {"t": t1.isoformat(timespec="minutes"), "aat": a, "k": "u", "sch": de, "righe": []}
            if not regolare:
                entry["irr"] = round(gap)
            con_motivo = len(pos) == 1 and not neg and sum(pos.values()) == de_c and de_c >= 1
            st = stato[a]

            # --- dettaglio dei mezzi partiti (serve anche al saldo: colori e coppie MSB + avanzato)
            rip = None
            if dm > 0 and regolare:
                controllo = None
                if f["allineati"]:
                    controllo = True
                    for kk, dk in list(cod.items()) + list(tp.items()):
                        if dk is None or i0.get(kk) is None or i1.get(kk) is None:
                            controllo = None
                            break
                        if dk - (i1[kk] - i0[kk]) < 0:
                            controllo = False
                            break
                if all(v is not None for v in cod.values()):
                    fuori_prima = {c: (i0.get(c) or 0) for c, _, _ in COLORI} if i0 else {}
                    rip, _cert = ricostruisci_codici({k: max(v, 0) for k, v in tp.items()},
                                                     {c: max(v, 0) for c, v in cod.items()},
                                                     fuori_prima=fuori_prima, controllo_scorte=controllo)
            linea, coppie, soli = testo_mezzi(tp, rip)
            if not rip:
                # colori non ricostruibili: si assume che ogni avanzato sia in coppia con un MSB finche' ce ne sono
                # (succede nel ~75% dei casi), gli altri sono partiti da soli
                soli = soli[min(len(soli), max(dB, 0)):]
                cs = [c for c, _, _ in COLORI if (cod.get(c) or 0) > 0]
                if len(cs) == 1:                      # un solo colore in questa lettura: vale per tutti i mezzi
                    soli = [(t_, cs[0]) for t_, _ in soli]
            msb_cols = {c: v for c, v in (rip or {}).get("msb", {}).items() if v}
            if not msb_cols and adv == 0 and dB > 0:
                cs = [c for c, _, _ in COLORI if (cod.get(c) or 0) > 0]
                if len(cs) == 1:
                    msb_cols = {cs[0]: dB}

            # --- schede senza MSB: scadenze, rotazione, nuove schede, coperture
            # Due tipi di schede in attesa di un MSB:
            #   "nessuno": nessun mezzo partito -> in attesa o gia' chiusa al telefono;
            #   "avz": aperta da un solo mezzo avanzato (o altro mezzo) -> l'avanzato e' sul posto, ci si aspetta
            #          un MSB (di solito con lo stesso codice: 79% dei casi).
            sch_txt = []
            # intervallo "pulito": letture coerenti e nessun mezzo rientrato -> nessun intervento con mezzi si e'
            # chiuso, quindi la variazione degli interventi in corso = interventi aperti (verificato: errore < 1%)
            pulito = bool(regolare and f["stesso"] and f["allineati"] and f["rientri"] == 0
                          # interventi aperti senza mezzi (es. Como 13/11, ore): la loro chiusura sposterebbe il conto
                          and i0["totale_missioni"] <= i0["missioni"])
            aperti = (i1["totale_missioni"] - i0["totale_missioni"]) if pulito else None
            inc_rec[a] = [x for x in inc_rec[a] if t1 - x[0] <= timedelta(minutes=SCADENZA_MIN + 30)]
            if (pos.get("incidente_stradale") or 0) > 0:
                inc_rec[a].append((t1, pos["incidente_stradale"], (i0 or {}).get("incidente_stradale")))
            scad_rec[a] = [x for x in scad_rec[a] if t1 - x[0] <= timedelta(minutes=SCADENZA_MIN)]

            def doppia(u):
                """Scheda per incidente senza mezzo: chiamata doppia per lo stesso incidente? Verificato fuori Milano:
                con 0 incidenti gia' in corso ~9 volte su 10, con 1 ~5 su 10; con 2 o piu' non si puo' dire."""
                if u.get("mot") != "incidente_stradale" or a == "MILANO":
                    return None
                gruppo = [x for x in inc_rec[a] if u["t"] - timedelta(minutes=10) <= x[0] <= u["t"]]
                if sum(x[1] for x in gruppo) < 2 or gruppo[0][2] is None or gruppo[0][2] > 1:
                    return None
                return "quasi certa" if gruppo[0][2] == 0 else "probabile"

            def scadenze():
                scadute = [u for u in saldo[a] if t1 - u["t"] > timedelta(minutes=SCADENZA_MIN)]
                n_sc = n_av = n_dp = 0
                for u in scadute:
                    saldo[a].remove(u)
                    if u["tipo"] == "nessuno":
                        n_sc += 1
                        conti[(a, u["day"])]["senza_invio"] += 1
                        ore[(a, u["day"])][u["h"]][1] += 1
                        dp = doppia(u)
                        if dp:
                            n_dp += 1
                            u["doppia"] = True
                            conti[(a, u["day"])]["doppie"] += 1
                            u["es"] = (f"nessun mezzo in {SCADENZA_MIN} minuti: chiamata doppia per lo stesso incidente "
                                       f"({dp})")
                        else:
                            u["es"] = f"nessun mezzo in {SCADENZA_MIN} minuti, probabilmente chiusa senza invio"
                        esito(u, u["es"])
                        scad_rec[a].append((t1, u))
                    else:
                        n_av += 1
                        conti[(a, u["day"])]["solo_avanzato"] += 1
                        esito(u, f"nessun MSB in {SCADENZA_MIN} minuti: intervento con solo {u['nome']}")
                if n_sc:
                    sch_txt.append(f"{plur(n_sc, 'scheda', 'schede')} delle "
                                   f"{', '.join(sorted({hh(u['t']) for u in scadute if u['tipo'] == 'nessuno'}))} "
                                   f"senza mezzo da {SCADENZA_MIN} minuti: probabilmente chius{'a' if n_sc == 1 else 'e'} "
                                   f"senza invio" + (f" ({n_dp} chiamat{'a' if n_dp == 1 else 'e'} doppi{'a' if n_dp == 1 else 'e'} "
                                                     f"per lo stesso incidente)" if n_dp else ""))
                if n_av:
                    sch_txt.append(f"{plur(n_av, 'intervento', 'interventi')} con solo mezzo avanzato, nessun MSB in {SCADENZA_MIN} minuti")

            # dopo un buco di letture gli MSB partiti nel buco arrivano tutti insieme: prima si abbinano alle schede in
            # attesa, poi si guarda cosa e' scaduto (altrimenti le schede scadrebbero prima di poter essere coperte)
            if regolare:
                scadenze()
            if de_c >= 1 and dB >= 1:
                # il buco puo' essere passato da una scheda vecchia a una nuova
                for u in saldo[a]:
                    if u["tipo"] == "nessuno":
                        u.setdefault("rot", t1)
                st["rot"] = t1
            # schede nuove: prima coperte dagli MSB, poi dai mezzi avanzati/altri partiti da soli
            n_msb = min(de_c, dB)
            n_avz = min(de_c - n_msb, len(soli))
            nuove_senza = de_c - n_msb - n_avz
            base_u = {"e": entry, "day": day, "h": h, "mot": next(iter(pos)) if con_motivo else None}
            nuovi_avz = [{"t": t1, **base_u, "tipo": "avz", "nome": SIGLE[t_][0], "col": c_} for t_, c_ in soli[:n_avz]]
            saldo[a].extend(nuovi_avz)
            for _ in range(nuove_senza):
                saldo[a].append({"t": t1, **base_u, "tipo": "nessuno"})
            k = max(0, dB - de_c)                  # MSB in piu' rispetto alle schede nuove
            disp = dict(msb_cols)
            coppie_dopo, coperti = [], []          # coperti: [(scheda, colore del testo)]
            for _ in range(k):
                # 1) scheda con solo avanzato e MSB dello stesso codice: completa quella missione
                u = next((u for u in saldo[a] if u["tipo"] == "avz" and u.get("col") and disp.get(u["col"])), None)
                # 2) altrimenti l'MSB copre prima le schede senza nessun mezzo, e solo se non ce ne sono completa una
                #    scheda con solo avanzato di qualsiasi codice (stesso codice solo nel 79% dei casi)
                if not u and not any(v["tipo"] == "nessuno" for v in saldo[a]):
                    u = next((u for u in saldo[a] if u["tipo"] == "avz"), None)
                if u:
                    saldo[a].remove(u)
                    if u.get("col") and disp.get(u["col"]):
                        disp[u["col"]] -= 1
                    coppie_dopo.append(u)
                    continue
                u = next((u for u in saldo[a] if u["tipo"] == "nessuno"), None)
                if not u:
                    break
                saldo[a].remove(u)
                colore = f" [{next(iter(msb_cols))}]" if len(msb_cols) == 1 else \
                         (" probabilmente [verde]" if msb_cols.get("verde") and u.get("mot") != "incidente_stradale" else "")
                coperti.append((u, colore if not u.get("rot") else ""))
            rinf = k - len(coppie_dopo) - len(coperti)

            # --- controllo con gli interventi in corso (solo intervalli puliti)
            avz_su_attivo = msb_su_attivo = annullate = avz_copre = tardive = msb_nuovo = 0
            if pulito:
                d_ = n_msb + n_avz + len(coperti) - aperti       # >0: aperti meno interventi del previsto
                while d_ > 0 and nuovi_avz:                      # l'avanzato e' andato su un intervento gia' aperto
                    u = nuovi_avz.pop()
                    u["tipo"] = "nessuno"; u.pop("nome", None); u.pop("col", None)
                    n_avz -= 1; nuove_senza += 1; avz_su_attivo += 1; d_ -= 1
                while d_ > 0 and coperti:                        # l'MSB era un rinforzo: la scheda resta in attesa
                    u, _c = coperti.pop()
                    saldo[a].append(u)
                    rinf += 1; annullate += 1; d_ -= 1
                while d_ > 0 and n_msb > 0:                      # MSB su intervento gia' attivo, scheda senza mezzo
                    n_msb -= 1; nuove_senza += 1; rinf += 1; msb_su_attivo += 1; d_ -= 1
                    saldo[a].append({"t": t1, **base_u, "tipo": "nessuno"})
                s_ = -d_                                         # >0: aperti piu' interventi del previsto
                liberi = soli[n_avz:]
                while s_ > 0 and liberi:                         # avanzato partito da solo per una scheda in attesa
                    u = next((v for v in saldo[a] if v["tipo"] == "nessuno"), None)
                    if not u:
                        break
                    t_, c_ = liberi.pop(0)
                    saldo[a].remove(u)
                    saldo[a].append({"t": t1, "e": u["e"], "day": u["day"], "h": u["h"], "mot": u.get("mot"),
                                     "tipo": "avz", "nome": SIGLE[t_][0], "col": c_})
                    conti[(a, u["day"])]["coperte_dopo"] += 1
                    esito(u, f"{SIGLE[t_][0]} partito {quando(u, t1)}, poi in attesa dell'MSB")
                    avz_copre += 1; s_ -= 1
                while s_ > 0 and rinf > 0:                       # il "rinforzo" ha aperto un intervento nuovo
                    rinf -= 1; s_ -= 1
                    if scad_rec[a]:                              # scheda data per chiusa da poco: coperta in ritardo
                        _ts, u = scad_rec[a].pop()
                        conti[(a, u["day"])]["senza_invio"] -= 1
                        ore[(a, u["day"])][u["h"]][1] -= 1
                        if u.pop("doppia", False):
                            conti[(a, u["day"])]["doppie"] -= 1
                        togli_esito(u, u["es"])
                        esito(u, f"MSB partito alle {hh(t1)}, dopo oltre {SCADENZA_MIN} minuti (verificato)")
                        conti[(a, u["day"])]["coperte_dopo"] += 1
                        tardive += 1
                    else:
                        msb_nuovo += 1
                        conti[(a, day)]["msb_intervento_nuovo"] += 1
                corr = avz_su_attivo + msb_su_attivo + annullate + avz_copre + tardive + msb_nuovo
                if corr:
                    conti[(a, day)]["corretti"] += corr
                if de_c or dm:
                    conti[(a, day)]["controllati"] += 1
            saldo[a].sort(key=lambda u: u["t"])
            if not regolare:
                scadenze()

            # --- conteggi e testi
            rapide = sum(1 for u, _c in coperti if t1 - u["t"] <= timedelta(minutes=SUBITO_MIN))
            coperte = len(coperti) - rapide
            for u, colore in coperti:
                esito(u, f"MSB{colore} partito {quando(u, t1)}")
            for u in coppie_dopo:
                cb = f" [{u['col']}]" if u.get("col") and msb_cols.get(u["col"]) else ""
                esito(u, f"MSB{cb} arrivato alle {hh(t1)}")
            conti[(a, day)]["con_mezzo_subito"] += n_msb + n_avz + rapide
            if n_avz:
                grp = defaultdict(int)
                for u in nuovi_avz:
                    grp[(u["nome"], u.get("col"))] += 1
                nomi = ", ".join(f"{n_} {nome}" + (f" [{c_}]" if c_ else "") for (nome, c_), n_ in grp.items())
                if len(grp) == 1 and n_avz == 1:
                    nomi = nomi[2:]
                sch_txt.append(f"{plur(n_avz, 'scheda', 'schede')} con solo {nomi} per ora: in attesa dell'MSB")
            if avz_su_attivo:
                sch_txt.append(f"{plur(avz_su_attivo, 'mezzo avanzato', 'mezzi avanzati')} su un intervento già aperto "
                               f"(gli interventi in corso non salgono)")
            if nuove_senza:
                testo = f"{nuove_senza} senza mezzo per ora"
                if coppie and not avz_su_attivo:
                    testo += " (probabilmente la meno urgente: l'avanzato è andato con l'MSB)"
                sch_txt.append(testo)
            if coppie_dopo:
                conti[(a, day)]["msb_dopo_avanzato"] += len(coppie_dopo)
                grp = defaultdict(int)
                for u in coppie_dopo:
                    grp[(u["nome"], u.get("col"), hh(u["t"]))] += 1
                pezzi_c = []
                for (nome, col, ora), n_ in grp.items():
                    cu = f" [{col}]" if col else ""
                    pezzi_c.append(f"{n_} MSB in coppia con {'l' if n_ == 1 else 'gli'}'{nome}{cu} "
                                   f"partit{'o' if n_ == 1 else 'i'} alle {ora}".replace("gli'", "gli "))
                sch_txt.append(", ".join(pezzi_c))
            if rapide:
                sch_txt.append(f"{rapide} MSB per {'la scheda' if rapide == 1 else 'le schede'} della lettura precedente")
            if coperte:
                conti[(a, day)]["coperte_dopo"] += coperte
                sch_txt.append(f"{coperte} MSB per {'una scheda' if coperte == 1 else 'schede'} rimast"
                               f"{'a' if coperte == 1 else 'e'} senza mezzo")
            if avz_copre:
                sch_txt.append(f"{plur(avz_copre, 'scheda', 'schede')} in attesa copert{'a' if avz_copre == 1 else 'e'} "
                               f"da un mezzo avanzato (intervento aperto, MSB atteso)")
            if tardive:
                sch_txt.append(f"{tardive} MSB per {'una scheda data' if tardive == 1 else 'schede date'} per chius"
                               f"{'a' if tardive == 1 else 'e'}: coperta dopo oltre {SCADENZA_MIN} minuti")
            if msb_nuovo:
                sch_txt.append(f"{msb_nuovo} MSB su un intervento nuovo senza scheda in attesa")
            if rinf:
                conti[(a, day)]["rinforzi"] += rinf
                if pulito:
                    sch_txt.append(f"{rinf} MSB di rinforzo su intervento già attivo"
                                   + (" (verificato: gli interventi in corso non salgono)" if msb_su_attivo or annullate else ""))
                else:
                    sch_txt.append(f"{rinf} MSB senza scheda nuova (rinforzo o scheda precedente)")
                entry["_rinforzi"] = rinf
            if de < 0:
                sch_txt.append(f"correzione del conteggio: {de} {'scheda' if de == -1 else 'schede'}")

            # --- scheda presa in carico senza un mezzo della zona: gli interventi in corso superano i mezzi in
            # corso (quindi almeno un intervento aperto non ha mezzi della zona), la differenza e' appena salita e
            # in questa lettura NON si sono liberati piu' mezzi di quanti interventi si sono chiusi (altrimenti e'
            # solo un mezzo rientrato prima della chiusura della sua scheda). Verificato sui dati: Sondrio 10/11.
            if (f["stesso"] and i0 and i1 and None not in (i0.get("totale_missioni"), i0.get("missioni"),
                                                         i1.get("totale_missioni"), i1.get("missioni"))):
                liv0 = i0["totale_missioni"] - i0["missioni"]
                liv1 = i1["totale_missioni"] - i1["missioni"]
                liberati = dm - (i1["missioni"] - i0["missioni"])
                chiusi = de_c - (i1["totale_missioni"] - i0["totale_missioni"])
                attese = [u for u in saldo[a] if u["tipo"] == "nessuno"]
                n_carico = min(liv1 - max(liv0, 0), len(attese)) if liv1 >= 1 and liberati <= chiusi else 0
                if n_carico > 0:
                    # prima le schede comparse in questa lettura, poi le piu' vecchie
                    scelte = sorted(attese, key=lambda u: (u["t"] != t1, u["t"]))[:n_carico]
                    for u in scelte:
                        saldo[a].remove(u)
                        conti[(a, u["day"])]["in_carico_altro"] += 1
                        esito(u, f"presa in carico alle {hh(t1)} senza un mezzo della zona "
                                 f"(mezzo non contato o di un'altra zona)")
                    # le schede appena comparse e subito prese in carico non vanno contate come "senza mezzo"
                    n_ora = sum(1 for u in scelte if u["t"] == t1)
                    if n_ora:
                        vecchio = next((x for x in sch_txt if x.startswith(f"{nuove_senza} senza mezzo per ora")), None)
                        if vecchio:
                            j = sch_txt.index(vecchio)
                            if nuove_senza - n_ora > 0:
                                sch_txt[j] = vecchio.replace(f"{nuove_senza} senza", f"{nuove_senza - n_ora} senza", 1)
                            else:
                                sch_txt.pop(j)
                        nuove_senza -= n_ora
                    sch_txt.append(f"{plur(n_carico, 'scheda presa', 'schede prese')} in carico senza un mezzo della zona")

            # --- saldo della zona: in coda alla riga Schede se ci sono altre schede senza mezzo,
            # riga a parte solo quando torna a zero
            senza = [u for u in saldo[a] if u["tipo"] == "nessuno"]
            n_s = len(senza)
            if n_s:
                st["dal"] = senza[0]["t"]          # eta' massima = la scheda senza mezzo piu' vecchia
            else:
                st.pop("dal", None)
            if n_s != saldo_prec[a]:
                if n_s and n_s != nuove_senza:
                    sch_txt.append(f"in zona {n_s} senza mezzo")
                elif not n_s:
                    entry["righe_saldo"] = "tutte le schede hanno un mezzo"
            saldo_prec[a] = n_s

            legato = con_motivo and dB == de_c        # ogni scheda nuova ha il suo MSB e il motivo e' unico
            if linea:
                if legato:
                    linea += " · " + testo_motivi(pos)
                elif dB == 0 and de_c == 0 and adv:
                    # il controllo con gli interventi in corso puo' aver visto che l'avanzato ha aperto l'intervento di
                    # una scheda rimasta in attesa: allora il testo deve dirlo, non "intervento gia' in corso"
                    linea += (" · per una scheda in attesa (intervento nuovo)" if avz_copre
                              else " · su intervento già in corso")
                entry["righe"].append(["Mezzi", linea])
                cali = {c: -v for c, v in cod.items() if v is not None and v < 0}
                if cali and dm >= 0:
                    # un colore della giornata scende: un mezzo gia' partito ha cambiato codice (di solito declassato
                    # dopo la valutazione sul posto: da rosso a giallo, da verde a giallo). Gli aumenti dei colori
                    # comprendono quindi sia i mezzi nuovi sia il mezzo riclassificato.
                    su = [c for c, v in cod.items() if v and v > 0]
                    da = ", ".join(f"[{c}]" for c in cali)
                    a_ = " o ".join(f"[{c}]" for c in su) if su else "un altro codice"
                    n_c = sum(cali.values())
                    entry["righe"].append(["Codici", (
                        f"{plur(dm, 'mezzo nuovo', 'mezzi nuovi')} in {a_}" if dm else "nessun mezzo nuovo")
                        + f" · {plur(n_c, 'mezzo già partito passato', 'mezzi già partiti passati')} da {da} a {a_}"
                        + (" (non si sa quale codice abbia il mezzo nuovo)" if dm and len(su) > 1 else "")])
                elif not rip and dm > 0:
                    tc = testo_colori(cod)
                    sc = sum(v for v in cod.values() if v and v > 0)
                    # i colori a volte arrivano una lettura dopo il mezzo (Brescia 14/11 17:20-17:25): lo si dice,
                    # invece di attribuire ai mezzi nuovi piu' (o meno) codici di quanti sono (~0,2% degli intervalli)
                    if sc > dm:
                        tc += (f" · {plur(sc - dm, 'codice', 'codici')} di "
                               f"{'un mezzo partito' if sc - dm == 1 else 'mezzi partiti'} prima, assegnat"
                               f"{'o' if sc - dm == 1 else 'i'} in ritardo")
                    elif sc < dm:
                        tc = (tc + " · " if tc else "") + plur(dm - sc, "mezzo ancora senza codice", "mezzi ancora senza codice")
                    if tc:
                        entry["righe"].append(["Codici", tc])
            if sch_txt:
                entry["righe"].append(["Schede", " · ".join(sch_txt)])
            if entry.get("righe_saldo"):
                entry["righe"].append(["Saldo", entry.pop("righe_saldo")])
            if pos and not legato:
                entry["righe"].append(["Motivi", testo_motivi(pos)])
            if neg:
                entry["righe"].append(["Riclassif.", "motivo aggiornato per " + ", ".join(
                    f"{v} {'scheda' if v == 1 else 'schede'} prima registrat{'a' if v == 1 else 'e'} come "
                    f"{MOT_NOME.get(k, (k,))[0]}" for k, v in neg.items())])
            if f["allineati"] and all(cod.get(c) is not None and i0.get(c) is not None and i1.get(c) is not None
                                      for c, _, _ in COLORI):
                su = {c: -(cod[c] - (i1[c] - i0[c])) for c, _, _ in COLORI if cod[c] - (i1[c] - i0[c]) < 0}
                if su:
                    entry["righe"].append(["Riclassif.", ", ".join(
                        f"{n} {'mezzo passato' if n == 1 else 'mezzi passati'} a [{c}]" for c, n in su.items())])
            if f["rientri"] and f["rientri"] > 0:
                entry["righe"].append(["Rientri", plur(f["rientri"], "mezzo", "mezzi")])
            if (de or dm or neg or sch_txt) and t1 > f_since:
                feed.append(entry)
            nuovi_rinforzi = entry.pop("_rinforzi", 0)

            if not vicino or t1 <= f_since:
                continue
            # --- segnalazioni rilevanti di questo intervallo
            segnali = []
            if tp["elisoccorso"] > 0:
                tit = "Elisoccorso inviato" if tp["elisoccorso"] == 1 else f"{tp['elisoccorso']} elisoccorsi inviati"
                ce = [c for c, _, _ in COLORI if rip and rip.get("elisoccorso", {}).get(c)]
                if len(ce) == 1:
                    tit += f" in codice [{ce[0]}]"
                segnali.append((5, "eli", tit))
            elif i0 and i1 and (i1.get("elisoccorso") or 0) > (i0.get("elisoccorso") or 0):
                segnali.append((4, "eli", "Elisoccorso in volo per la zona"))
            for k, nome in PARTICOLARI.items():
                v = mot.get(k) or 0
                if v > 0:
                    segnali.append((4, "particolare", nome if v == 1 else n_nome(v, k).capitalize()))
            o1 = f["o1"]
            quota = (o1.get("rosso") or 0) / o1["missioni"] if o1.get("missioni") else 0.15
            quota = min(max(quota, 0.08), 0.5)
            # raffica: almeno 3 rossi negli ultimi 10' e improbabili per caso dato il solito tasso di rossi della
            # zona (p < 1%); al massimo una segnalazione ogni 20'
            if regolare:
                rossi_rec[a] = [x for x in rossi_rec[a] if t1 - x[0] < timedelta(minutes=RAFFICA_MIN)]
                rossi_rec[a].append((t1, max(cod.get("rosso") or 0, 0), max(dm, 0), gap))
            else:
                rossi_rec[a] = []
            dr = sum(x[1] for x in rossi_rec[a]); dm_w = sum(x[2] for x in rossi_rec[a])
            durata = round(sum(x[3] for x in rossi_rec[a]))
            if (regolare and (cod.get("rosso") or 0) > 0 and dr >= 3 and dm_w and coda_binomiale(dr, dm_w, quota) < 0.01
                    and t1 - ultimo_avviso.get(("rossi", a), datetime.min.replace(tzinfo=TZ)) > timedelta(minutes=20)):
                ultimo_avviso[("rossi", a)] = t1
                segnali.append((3, "rossi", f"{dr} mezzi in codice rosso in {durata} minuti"))
            if segnali:
                segnali.sort(key=lambda s: -s[0])
                det = [s[2] for s in segnali[1:]]
                if linea:
                    det.append(linea)
                if pos and not legato:
                    det.append(testo_motivi(pos))
                rel(t1, a, segnali[0][1], segnali[0][2], " · ".join(det))

            # --- intervento complesso: in ~20 minuti partono almeno 2 MSB senza una scheda nuova e non
            # spiegati da schede in attesa (di norma le schede superano gli MSB del 5-7%), con almeno un
            # mezzo avanzato e un motivo "di scena" (strada, violenza, infortunio, montagna...): piu'
            # ambulanze sullo stesso luogo
            storia[a] = [s_ for s_ in storia[a] if t1 - s_["t"] <= timedelta(minutes=25)]
            scena = sum(pos.get(k, 0) for k in SCENA) + max(mot.get("soccorso_alpino") or 0, 0)
            storia[a].append({"t": t1, "de": de_c, "adv": adv, "msb": dB, "ex": nuovi_rinforzi, "ok": regolare,
                              "scena": scena, "pos": pos, "tipi": {k: max(tp[k], 0) for k in ("msi", "msa", "elisoccorso")}})
            w = storia[a]
            de_w, adv_w = sum(s_["de"] for s_ in w), sum(s_["adv"] for s_ in w)
            ex_w = sum(s_["ex"] for s_ in w)   # MSB di rinforzo: senza scheda nuova e senza schede scoperte
            scena_w = sum(s_["scena"] for s_ in w)
            agg_mot = defaultdict(int)
            for s_ in w:
                for k, v in s_["pos"].items():
                    agg_mot[k] += v
            if (all(s_["ok"] for s_ in w) and nuovi_rinforzi > 0 and adv_w >= 1 and scena_w >= 1
                    and ex_w >= max(COMPLESSO_EXTRA, 0.15 * de_w)
                    and t1 - ultimo_avviso.get(("complesso", a), datetime.min.replace(tzinfo=TZ)) > timedelta(minutes=60)):
                ultimo_avviso[("complesso", a)] = t1
                mins = round((t1 - w[0]["t"]).total_seconds() / 60) + 10
                avz = {k: sum(s_["tipi"][k] for s_ in w) for k in ("msi", "msa", "elisoccorso")}
                avz_txt = " e ".join(f"{v} {SIGLE[k][0 if v == 1 else 1]}" for k, v in avz.items() if v)
                rel(t1, a, "complesso", "Probabile intervento con più ambulanze sulla stessa scena",
                    f"{ex_w} MSB in più su un intervento già aperto in circa {mins} minuti, insieme a {avz_txt} · "
                    + testo_motivi({k: v for k, v in agg_mot.items() if k in SCENA}))

        # --- tendenze regionali: lo stesso motivo in piu' zone nella stessa mezz'ora
        tw = C.near(t1 - timedelta(minutes=30), 8)
        mins_today = (t1 - datetime(t1.year, t1.month, t1.day, tzinfo=TZ)).total_seconds() / 60
        if vicino and tw and tw < t1 and mins_today >= 120 and t1 > f_since:
            win = (t1 - tw).total_seconds() / 60
            for k, nome in TREND.items():
                if k in trend_last and t1 - trend_last[k] < timedelta(minutes=60):
                    continue
                per_z = {a: C.delta(a, tw, t1, k) for a in C.aats}
                per_z = {a: d for a, d in per_z.items() if d}
                tot = sum(per_z.values())
                cr, ce = cum_reg(t1, k), cum_reg(t1, "totale_missioni")
                dev = [C.delta(z, tw, t1, "totale_missioni") for z in C.aats]
                if cr is None or not ce or any(d is None for d in dev):
                    continue
                # atteso = quota di quel motivo nelle schede di oggi x schede della mezz'ora;
                # probabilita' di vederne almeno 'tot' per caso (Poisson) < 1/1000
                atteso = cr / ce * sum(dev)
                if len(per_z) >= 3 and tot >= 5 and poisson_coda(tot, max(atteso, 0.2)) < 0.001:
                    trend_last[k] = t1
                    zone_txt = ", ".join(z.capitalize() for z in sorted(per_z, key=lambda z: -per_z[z]))
                    rel(t1, "LOMBARDIA", "regione", f"{tot} {nome} in {round(win)} minuti in {len(per_z)} zone",
                        f"{zone_txt} · di solito a quest'ora sarebbero circa {atteso:.0f}")

        # --- traguardi (pochi e solo rilevanti)
        if t1 > f_since and t0.date() == t1.date() == today:
            r0, r1 = cum_reg(t0), cum_reg(t1)
            if r0 is not None and r1 is not None:
                if ieri_tot_reg and r0 <= ieri_tot_reg < r1 and t1.hour < 23:
                    rel(t1, "LOMBARDIA", "traguardo",
                        f"Superati i {ieri_tot_reg:,} mezzi inviati in tutta la giornata di ieri".replace(",", "."),
                        f"oggi sono già {r1:,}".replace(",", "."))
                if len(past_days) >= 7:
                    mx = max((day_total(d) or 0) for d in past_days)
                    if mx and r0 <= mx < r1:
                        rel(t1, "LOMBARDIA", "traguardo",
                            f"Giornata più intensa degli ultimi {len(past_days)} giorni",
                            f"superati i {mx:,} mezzi inviati".replace(",", "."))
            if len(past_days) >= 7:
                for a in C.aats:
                    c0, c1 = (ogg.get(t0, {}).get(a) or {}), (ogg.get(t1, {}).get(a) or {})
                    if c0.get("day") != today.isoformat() or c1.get("day") != today.isoformat():
                        continue
                    mx = max((day_total(d, a) or 0) for d in past_days)
                    if mx >= 20 and c0.get("missioni") is not None and c1.get("missioni") is not None \
                            and c0["missioni"] <= mx < c1["missioni"]:
                        rel(t1, a, "traguardo", f"Giornata più intensa degli ultimi {len(past_days)} giorni",
                            f"superati i {mx} mezzi inviati")
            if prev_peak_max and not peak_done and t1 in inc:
                vnow = sum((d.get("missioni") or 0) for d in inc[t1].values())
                vprev = sum((d.get("missioni") or 0) for d in inc.get(t0, {}).values()) if t0 in inc else 0
                if vprev <= prev_peak_max < vnow:
                    peak_done = True
                    rel(t1, "LOMBARDIA", "traguardo", f"Mai così tanti mezzi fuori negli ultimi 7 giorni: {vnow}",
                        f"il massimo precedente era {prev_peak_max}")

    # esiti delle schede in attesa, scritti sulla voce in cui erano comparse
    for e in feed:
        es = e.pop("_es", None)
        if es:
            for txt, n in es.items():
                e["righe"].append(["Esito", f"{plur(n, 'scheda', 'schede')}: {txt}"])

    # --- bilancio di oggi per il pannello "Schede e centrale"
    ora_now = last.hour
    t_oggi_per = defaultdict(list)
    for t in inc:
        t_oggi_per[t.date().isoformat()].append(t)
    def bilancio(a):
        o = ogg.get(last, {}).get(a)
        if not o or not o.get("day") or o.get("totale_missioni") is None:
            return None
        giorno = o["day"]          # giorno dei dati AREU (dopo mezzanotte resta ieri finche' AREU non cambia)
        c = conti[(a, giorno)]
        st = stato[a]
        n_s = sum(1 for u in saldo[a] if u["tipo"] == "nessuno")
        n_av = sum(1 for u in saldo[a] if u["tipo"] == "avz")
        eta_min = eta_max = None
        if n_s and st.get("dal") and a != "MILANO":   # a Milano l'abbinamento e' troppo incerto
            eta_max = round((last - st["dal"]).total_seconds() / 60)
            eta_min = round((last - max(st["dal"], st.get("rot") or st["dal"])).total_seconds() / 60)
        # stima indipendente delle schede senza invio: schede di oggi - (mezzi di oggi / mezzi per intervento
        # in corso, media della giornata). Serve come controllo e per dare un intervallo nelle zone grandi.
        rapp = [inc[t][a]["missioni"] / inc[t][a]["totale_missioni"] for t in t_oggi_per[giorno]
                if (inc[t].get(a) or {}).get("totale_missioni") and inc[t][a].get("missioni") is not None]
        indip = None
        if len(rapp) >= 12 and o.get("missioni") is not None:
            indip = max(0, round(o["totale_missioni"] - o["missioni"] / (sum(rapp) / len(rapp))))
        return {
            "schede": o["totale_missioni"], "msb": o.get("msb"),
            "con_mezzo_subito": c["con_mezzo_subito"], "coperte_dopo": c["coperte_dopo"],
            "senza_invio": c["senza_invio"], "senza_invio_indip": indip,
            "senza_mezzo": n_s, "eta_min": eta_min, "eta_max": eta_max,
            "avanzato_attesa_msb": n_av, "msb_dopo_avanzato": c["msb_dopo_avanzato"],
            "solo_avanzato": c["solo_avanzato"], "rinforzi": c["rinforzi"],
            "in_carico_altro": c["in_carico_altro"], "doppie": c["doppie"],
            "msb_intervento_nuovo": c["msb_intervento_nuovo"], "corretti": c["corretti"],
            "controllati": c["controllati"],
            "ore": [ore[(a, giorno)][hh_] if (giorno != today.isoformat() or hh_ <= ora_now) else None for hh_ in range(24)],
        }
    for z in zone:
        z["schede"] = bilancio(z["aat"])
    bz = [z["schede"] for z in zone]
    if bz and all(bz):
        regione["schede"] = {k: sum(b[k] or 0 for b in bz) for k in bz[0]
                             if k not in ("ore", "eta_min", "eta_max", "senza_invio_indip")}
        ind = [b["senza_invio_indip"] for b in bz]
        regione["schede"]["senza_invio_indip"] = sum(ind) if all(v is not None for v in ind) else None
        regione["schede"]["ore"] = [None if any(b["ore"][hh_] is None for b in bz) else
                                    [sum(b["ore"][hh_][0] for b in bz), sum(b["ore"][hh_][1] for b in bz)]
                                    for hh_ in range(24)]
    else:
        regione["schede"] = None

    feed.sort(key=lambda e: e["t"], reverse=True)
    rilevanti = [e for e in feed if e["k"] == "r"]
    uscite = [e for e in feed if e["k"] == "u"][:FEED_MAX]
    out["feed.json"] = {"eventi": sorted(rilevanti + uscite, key=lambda e: e["t"], reverse=True)}
    return out


def write_json(out_dir: str, name: str, obj) -> None:
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, os.path.join(out_dir, name))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-file", help="usa un JSON salvato invece dell'API (test)")
    ap.add_argument("--now", help="orario ISO da usare come momento della lettura (test)")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "data"),
                    help="cartella dove scrivere i JSON della dashboard")
    ap.add_argument("--only-store", action="store_true", help="salva lo storico senza calcolare (test)")
    ap.add_argument("--only-build", action="store_true", help="ricalcola i JSON senza leggere l'API")
    args = ap.parse_args()

    now = datetime.fromisoformat(args.now).astimezone(TZ) if args.now else datetime.now(TZ)
    now = now.replace(second=0, microsecond=0)

    if args.only_build:
        finals = upsert_giornaliero([])
    else:
        payload = json.load(open(args.from_file, encoding="utf-8")) if args.from_file else fetch()
        in_corso = [normalize(r) for r in payload.get("in_corso", [])]
        del_giorno = [normalize(r) for r in payload.get("del_giorno", [])]
        if not in_corso:
            print("Attenzione: 'in_corso' vuoto, niente da salvare", file=sys.stderr)
            return 1
        api_updated = payload.get("updated", "")
        append_rows("in_corso", in_corso, now, api_updated)
        append_rows("oggi", [r for r in del_giorno if r["day"] == "OGGI"], now, api_updated)
        finals = upsert_giornaliero(del_giorno)
        if args.only_store:
            return 0

    for name, obj in build(now, finals).items():
        write_json(args.out, name, obj)
    print(f"{now:%Y-%m-%d %H:%M}  ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
