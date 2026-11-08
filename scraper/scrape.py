#!/usr/bin/env python3
"""
Scraper per i dati 118 in tempo reale di AREU Lombardia.

Legge l'API pubblica usata dalla pagina
https://www.areu.lombardia.it/web/home/missioni-aat-real-time
e salva:
  data/in_corso/AAAA-MM.csv     una riga per zona (AAT) a ogni esecuzione  -> lo storico
  data/giornaliero.csv          totali di ogni giorno per zona (l'ultimo valore visto vince)
  docs/data/latest.json         ultima fotografia, per la dashboard
  docs/data/history.json        ultimi 7 giorni delle missioni in corso, per i grafici
  docs/data/baseline.json       "il solito": mediana per zona / giorno della settimana / ora

Solo libreria standard: nessun pip install necessario.
Uso:
  python scraper/scrape.py                      # scarica dall'API
  python scraper/scrape.py --from-file x.json   # usa un file (test)
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import statistics
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

API_URL = (
    "https://www.areu.lombardia.it/api/jsonws/"
    "eventiregionejson.eventiregionejson/statistiche-regione"
)
USER_AGENT = "areu-118-dashboard (progetto hobby; dati pubblici AREU)"
TZ = ZoneInfo("Europe/Rome")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
SITE_DATA_DIR = os.path.join(ROOT, "docs", "data")

TEXT_FIELDS = ["aat", "soreu", "aggiornato_alle", "day_data"]
NUM_FIELDS = [
    # contatori principali
    "missioni", "totale_missioni", "soccorsi", "totale_traffico_tel",
    # codici di gravita
    "rosso", "giallo", "verde", "bianco",
    # mezzi
    "msb", "msa", "msi", "elisoccorso", "mezzi_altro",
    # tipo di chiamata
    "soccorso_primario", "soccorso_secondario", "informazioni", "consulenza",
    "trasporto_organi", "trasporto_prenotato", "prestazione_ambulatoriale",
    "guardia_medica", "da_richiamare", "altro",
    # motivi
    "medico_acuto", "caduta", "evento_violento", "incidente_stradale",
    "incidente_acqua", "incidente_montano", "incidente_speleo", "infortunio",
    "intossicazione", "animali", "calamita_naturale", "evento_massa",
    "soccorso_persona", "soccorso_acqua", "soccorso_alpino", "non_noto",
    "motivo_altro", "altri_motivi",
]
CSV_HEADER = ["snapshot", "api_updated"] + TEXT_FIELDS + NUM_FIELDS
HISTORY_FIELDS = ["missioni", "rosso", "giallo", "verde"]
HISTORY_DAYS = 7


# --------------------------------------------------------------------------- fetch
def fetch(retries: int = 2) -> dict:
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(API_URL, headers={"User-Agent": USER_AGENT,
                                                           "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Impossibile leggere l'API AREU: {last_err}")


def to_int(v):
    """L'API restituisce stringhe: "17" -> 17, "" -> None."""
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    try:
        return int(s)
    except ValueError:
        return None


def normalize_row(row: dict) -> dict:
    out = {k: (row.get(k) or "").strip() for k in TEXT_FIELDS}
    out["day"] = (row.get("day") or "").strip()
    for k in NUM_FIELDS:
        out[k] = to_int(row.get(k))
    return out


# --------------------------------------------------------------------------- storage
def append_in_corso(rows: list[dict], snapshot: datetime, api_updated: str) -> str:
    path = os.path.join(DATA_DIR, "in_corso", snapshot.strftime("%Y-%m") + ".csv")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(CSV_HEADER)
        for r in rows:
            w.writerow([snapshot.isoformat(timespec="minutes"), api_updated]
                       + [r[k] for k in TEXT_FIELDS]
                       + ["" if r[k] is None else r[k] for k in NUM_FIELDS])
    return path


def upsert_giornaliero(rows: list[dict]) -> str:
    path = os.path.join(DATA_DIR, "giornaliero.csv")
    header = ["data", "aat", "soreu", "aggiornato_alle"] + NUM_FIELDS
    existing: dict[tuple, list] = {}
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for rec in csv.DictReader(f):
                existing[(rec["data"], rec["aat"])] = [rec.get(h, "") for h in header]
    for r in rows:
        if not r["day_data"] or not r["aat"]:
            continue
        # day_data e' gg/mm/aaaa -> aaaa-mm-gg per ordinare bene
        d, m, y = r["day_data"].split("/")
        iso = f"{y}-{m}-{d}"
        existing[(iso, r["aat"])] = [iso, r["aat"], r["soreu"], r["aggiornato_alle"]] + [
            "" if r[k] is None else r[k] for k in NUM_FIELDS]
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for key in sorted(existing):
            w.writerow(existing[key])
    return path


def read_in_corso(since: datetime | None = None):
    """Restituisce tuple (snapshot datetime, riga dict) da tutti i CSV mensili."""
    files = sorted(glob.glob(os.path.join(DATA_DIR, "in_corso", "*.csv")))
    if since is not None:
        first_month = since.strftime("%Y-%m")
        files = [p for p in files if os.path.basename(p)[:7] >= first_month]
    for p in files:
        with open(p, newline="", encoding="utf-8") as f:
            for rec in csv.DictReader(f):
                try:
                    ts = datetime.fromisoformat(rec["snapshot"])
                except (KeyError, ValueError):
                    continue
                if since is not None and ts < since:
                    continue
                yield ts, rec


# --------------------------------------------------------------------------- site data
def build_history(now: datetime) -> dict:
    since = now - timedelta(days=HISTORY_DAYS)
    by_ts: dict[str, dict] = defaultdict(dict)
    aats: set[str] = set()
    for ts, rec in read_in_corso(since):
        key = ts.isoformat(timespec="minutes")
        aats.add(rec["aat"])
        by_ts[key][rec["aat"]] = [to_int(rec.get(f)) for f in HISTORY_FIELDS]
    times = sorted(by_ts)
    aat_list = sorted(aats)
    values = {a: [by_ts[t].get(a) for t in times] for a in aat_list}
    return {"fields": HISTORY_FIELDS, "aats": aat_list, "t": times, "v": values}


def build_baseline(now: datetime) -> dict:
    """Mediana e 90esimo percentile delle missioni in corso per (zona, giorno sett., ora).
    Usa solo i giorni precedenti a oggi, cosi' "adesso" non influenza "il solito"."""
    today = now.date()
    buckets: dict[tuple, list[int]] = defaultdict(list)
    region_snap: dict[datetime, int] = defaultdict(int)  # totale regionale per snapshot
    days_seen: set = set()
    for ts, rec in read_in_corso():
        if ts.date() >= today:
            continue
        v = to_int(rec.get("missioni"))
        if v is None:
            continue
        days_seen.add(ts.date())
        buckets[(rec["aat"], ts.weekday(), ts.hour)].append(v)
        region_snap[ts] += v
    for ts, total in region_snap.items():
        buckets[("REGIONE", ts.weekday(), ts.hour)].append(total)

    def stats(vals):
        vals = sorted(vals)
        p90 = vals[min(len(vals) - 1, int(round(0.9 * (len(vals) - 1))))]
        return [statistics.median(vals), p90, len(vals)]

    per_aat: dict[str, dict] = defaultdict(dict)
    for (aat, wd, h), vals in buckets.items():
        per_aat[aat][f"{wd}-{h}"] = stats(vals)
    return {"giorni_di_storico": len(days_seen), "aat": dict(per_aat)}


def write_json(name: str, obj) -> None:
    os.makedirs(SITE_DATA_DIR, exist_ok=True)
    tmp = os.path.join(SITE_DATA_DIR, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, os.path.join(SITE_DATA_DIR, name))


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-file", help="usa un JSON salvato invece dell'API (per test)")
    ap.add_argument("--now", help="orario ISO da usare come snapshot (per test)")
    args = ap.parse_args()

    if args.from_file:
        with open(args.from_file, encoding="utf-8") as f:
            payload = json.load(f)
    else:
        payload = fetch()

    now = datetime.fromisoformat(args.now).astimezone(TZ) if args.now else datetime.now(TZ)
    now = now.replace(second=0, microsecond=0)

    in_corso = [normalize_row(r) for r in payload.get("in_corso", [])]
    del_giorno = [normalize_row(r) for r in payload.get("del_giorno", [])]
    if not in_corso:
        print("Attenzione: 'in_corso' vuoto, niente da salvare", file=sys.stderr)
        return 1

    api_updated = payload.get("updated", "")
    append_in_corso(in_corso, now, api_updated)
    upsert_giornaliero(del_giorno)

    write_json("latest.json", {
        "snapshot": now.isoformat(timespec="minutes"),
        "api_updated": api_updated,
        "in_corso": in_corso,
        "oggi": [r for r in del_giorno if r["day"] == "OGGI"],
        "ieri": [r for r in del_giorno if r["day"] == "IERI"],
    })
    write_json("history.json", build_history(now))
    write_json("baseline.json", build_baseline(now))

    tot = sum(r["missioni"] or 0 for r in in_corso)
    print(f"{now:%Y-%m-%d %H:%M}  zone={len(in_corso)}  missioni in corso={tot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
