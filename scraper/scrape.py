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

ADESSO_KEYS = ["missioni", "totale_missioni", "rosso", "giallo", "verde", "bianco",
               "msb", "msa", "msi", "elisoccorso", "mezzi_altro"] + MOTIVI
OGGI_KEYS = ADESSO_KEYS + ["soccorso_alpino"]

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
FEED_MAX = 900
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

    # ---------------- feed degli eventi
    f_since = last - timedelta(hours=FEED_HOURS)
    tsf = [t for t in C.ts if t >= f_since - timedelta(minutes=30)]
    feed = []
    surge_on: dict[str, bool] = {}

    def surge_state(a, t, v):
        b = solito("in_corso", a, t)
        if not b or v is None:
            return None, b
        above = v > b["p90"] and v - b["med"] >= max(4, 0.3 * b["med"])
        back = v <= b["med"] * 1.1
        return ("su" if above else "giu" if back else "mezzo"), b

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

    def add(t, a, tags, parts):
        if parts and t > f_since:
            feed.append({"t": t.isoformat(timespec="minutes"), "aat": a, "tag": tags, "testo": " · ".join(parts)})

    for t0, t1 in zip(tsf, tsf[1:]):
        if (t1 - t0).total_seconds() / 60 > GAP_MIN:
            continue
        for a in C.aats + ["LOMBARDIA"]:
            if a == "LOMBARDIA":
                v = sum((d.get("missioni") or 0) for d in inc.get(t1, {}).values()) if t1 in inc else None
                st, b = surge_state("REGIONE", t1, v)
            else:
                i1 = inc.get(t1, {}).get(a) or {}
                i0 = inc.get(t0, {}).get(a) or {}
                v = i1.get("missioni")
                st, b = surge_state(a, t1, v)
            tags, parts = [], []
            prev = surge_on.get(a, False)
            if st == "su" and not prev:
                surge_on[a] = True
                tags.append("picco")
                parts.append(f"mezzi in uscita sopra il solito: {v} (di solito {b['med']:g})")
            elif st == "giu" and prev:
                surge_on[a] = False
                tags.append("rientro")
                parts.append(f"situazione rientrata: {v} mezzi in uscita (di solito {b['med']:g})")
            if a != "LOMBARDIA" and t1 > f_since:
                o0, o1 = ogg.get(t0, {}).get(a) or {}, ogg.get(t1, {}).get(a) or {}
                dm = C.delta(a, t0, t1, "missioni")
                dr = C.delta(a, t0, t1, "rosso")
                de = C.delta(a, t0, t1, "totale_missioni")
                # tipi di mezzo partiti tra le due letture (nomi ufficiali AREU)
                tipi = []
                for k, sigla in (("msb", "MSB"), ("msi", "MSA1"), ("msa", "MSA2"), ("elisoccorso", "elisoccorso"),
                                 ("mezzi_altro", "altro mezzo")):
                    d = C.delta(a, t0, t1, k)
                    if d:
                        tipi.append((d, sigla))
                d_eli = C.delta(a, t0, t1, "elisoccorso") or 0
                if d_eli or ((i1.get("elisoccorso") or 0) > (i0.get("elisoccorso") or 0) and i0):
                    tags.append("eli")
                    if not d_eli:
                        parts.append("elisoccorso in volo")
                motivi_txt = []
                for k, (sg, pl) in NOTABILI.items():
                    d = C.delta(a, t0, t1, k)
                    if d:
                        tags.append(k)
                        motivi_txt.append(f"{d} {sg if d == 1 else pl}")
                for k, (sg, pl) in ORDINARI.items():
                    d = C.delta(a, t0, t1, k)
                    if d:
                        motivi_txt.append(f"{d} {sg if d == 1 else pl}")
                raffica = dr is not None and dm and dr >= 3 and dr / dm >= 0.5
                if dr and (raffica or tags):
                    tags.insert(0, "rosso")
                # intervento complesso: molti piu' mezzi che eventi nuovi, con mezzi avanzati
                avanz = sum(C.delta(a, t0, t1, k) or 0 for k in ("msa", "msi", "elisoccorso"))
                extra = (dm or 0) - (de or 0) if de is not None else None
                complesso = extra is not None and extra >= 3 and extra >= 0.5 * max(de, 1) and avanz >= 2
                head = []
                elenco = ", ".join(f"{d} {sg}" for d, sg in tipi)
                if dm and len(tipi) == 1 and tipi[0][0] == dm:
                    sg = tipi[0][1].replace("altro mezzo", "altri mezzi" if dm > 1 else "altro mezzo")
                    head.append(f"{dm} {sg} {'inviato' if dm == 1 else 'inviati'}")
                elif dm:
                    head.append(f"{dm} {'mezzo inviato' if dm == 1 else 'mezzi inviati'}"
                                + (f" ({elenco})" if tipi else ""))
                elif tipi:
                    head.append(elenco + " inviati")
                if dr:
                    head.append(f"{dr} {'codice rosso' if dr == 1 else 'codici rossi'}")
                # rientri = partenze - variazione dei mezzi in corso; solo se i due contatori
                # della zona sono allineati (stesso orario di aggiornamento) e il conto torna
                if (dm is not None and i0 and i1 and o0 and o1 and o0.get("day") == o1.get("day")
                        and i0.get("agg") == o0.get("agg") and i1.get("agg") == o1.get("agg")
                        and i0.get("missioni") is not None and i1.get("missioni") is not None):
                    rient = dm - (i1["missioni"] - i0["missioni"])
                    if rient > 0:
                        head.append(f"{rient} {'mezzo rientrato' if rient == 1 else 'mezzi rientrati'}")
                parts = head + parts + motivi_txt
                if complesso:
                    tags.insert(0, "complesso")
                    parts.append(f"probabile intervento complesso o rinforzi: {dm} mezzi per {de} "
                                 f"{'nuovo evento' if de == 1 else 'nuovi eventi'}, {avanz} "
                                 f"{'mezzo avanzato' if avanz == 1 else 'mezzi avanzati'}")
                if parts and not tags:
                    tags.append("routine")
            add(t1, a, tags, parts)

        # --- tendenze regionali: lo stesso motivo in piu' zone nella stessa mezz'ora
        tw = C.near(t1 - timedelta(minutes=30), 8)
        midnight = datetime(t1.year, t1.month, t1.day, tzinfo=TZ)
        mins_today = (t1 - midnight).total_seconds() / 60
        if tw and tw < t1 and mins_today >= 120 and t1 > f_since:
            win = (t1 - tw).total_seconds() / 60
            for k, nome in TREND.items():
                if k in trend_last and t1 - trend_last[k] < timedelta(minutes=60):
                    continue
                per = {a: C.delta(a, tw, t1, k) for a in C.aats}
                per = {a: d for a, d in per.items() if d}
                tot = sum(per.values())
                cr, ce = cum_reg(t1, k), cum_reg(t1, "totale_missioni")
                dev = [C.delta(z, tw, t1, "totale_missioni") for z in C.aats]
                if cr is None or not ce or any(d is None for d in dev):
                    continue
                # atteso = quota di quel motivo negli eventi di oggi x eventi della mezz'ora
                # (cosi' il normale ciclo giorno/notte non genera falsi allarmi)
                atteso = cr / ce * sum(dev)
                # probabilita' di vedere almeno 'tot' casi per puro caso (Poisson): deve essere < 1/1000
                if len(per) >= 3 and tot >= 5 and poisson_coda(tot, max(atteso, 0.2)) < 0.001:
                    trend_last[k] = t1
                    zone_txt = ", ".join(z.capitalize() for z in sorted(per, key=lambda z: -per[z]))
                    add(t1, "LOMBARDIA", ["regionale"],
                        [f"Tendenza regionale: {tot} {nome} in {round(win)} minuti in {len(per)} zone ({zone_txt})",
                         f"di solito oggi sarebbero stati circa {atteso:.0f}"])

        # --- traguardi (pochi e solo rilevanti)
        if t1 > f_since and t0.date() == t1.date() == today:
            r0, r1 = cum_reg(t0), cum_reg(t1)
            if r0 is not None and r1 is not None:
                if ieri_tot_reg and r0 <= ieri_tot_reg < r1 and t1.hour < 23:
                    add(t1, "LOMBARDIA", ["traguardo"],
                        [f"Superate le {ieri_tot_reg:,} missioni di tutta la giornata di ieri".replace(",", "."),
                         f"oggi sono già {r1:,}".replace(",", ".")])
                if len(past_days) >= 7:
                    mx = max((day_total(d) or 0) for d in past_days)
                    if mx and r0 <= mx < r1:
                        add(t1, "LOMBARDIA", ["traguardo"],
                            [f"Giornata più intensa degli ultimi {len(past_days)} giorni: superate le {mx:,} missioni".replace(",", ".")])
            if len(past_days) >= 7:
                for a in C.aats:
                    c0, c1 = (ogg.get(t0, {}).get(a) or {}), (ogg.get(t1, {}).get(a) or {})
                    if c0.get("day") != today.isoformat() or c1.get("day") != today.isoformat():
                        continue
                    mx = max((day_total(d, a) or 0) for d in past_days)
                    if mx >= 20 and c0.get("missioni") is not None and c1.get("missioni") is not None \
                            and c0["missioni"] <= mx < c1["missioni"]:
                        add(t1, a, ["traguardo"],
                            [f"Giornata più intensa degli ultimi {len(past_days)} giorni: superate le {mx} missioni"])
            if prev_peak_max and not peak_done and t1 in inc:
                vnow = sum((d.get("missioni") or 0) for d in inc[t1].values())
                vprev = sum((d.get("missioni") or 0) for d in inc.get(t0, {}).values()) if t0 in inc else 0
                if vprev <= prev_peak_max < vnow:
                    peak_done = True
                    add(t1, "LOMBARDIA", ["traguardo"],
                        [f"Mai così tanti mezzi fuori contemporaneamente negli ultimi 7 giorni: {vnow} "
                         f"(il massimo precedente era {prev_peak_max})"])
    feed.reverse()
    out["feed.json"] = {"eventi": feed[:FEED_MAX]}
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
