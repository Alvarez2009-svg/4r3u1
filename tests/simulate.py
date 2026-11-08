#!/usr/bin/env python3
"""
Simulatore realistico dell'API AREU, per provare scraper e dashboard senza aspettare settimane.

Genera un processo a eventi: chiamate che arrivano con ritmo variabile nelle 24 ore, mezzi inviati
(MSB, MSA, MSI, elisoccorso), codici colore, motivi, durate delle missioni, ritardi di aggiornamento
delle singole zone e azzeramento dei totali a mezzanotte. Poi "legge" l'API ogni N minuti e passa
i dati allo scraper.

Uso:  python tests/simulate.py CARTELLA_PROGETTO --fine 2026-11-08T21:40 --giorni 21
"""
import argparse
import importlib.util
import math
import os
import random
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Rome")
# zona, centrale, mezzi inviati all'ora in media, quota elisoccorso, montagna
ZONE = [("BERGAMO", "ALPI", 11, .02, True), ("BRESCIA", "ALPI", 13, .02, True),
        ("COMO", "LAGHI", 6.5, .01, True), ("CREMONA", "PIANURA", 4.3, .01, False),
        ("LECCO", "LAGHI", 3.8, .03, True), ("LODI", "PIANURA", 3.2, .01, False),
        ("MANTOVA", "PIANURA", 4.6, .01, False), ("MILANO", "METROPOLITANA", 47, .002, False),
        ("MONZA", "METROPOLITANA", 9.5, .005, False), ("PAVIA", "PIANURA", 7, .01, False),
        ("SONDRIO", "ALPI", 2.1, .08, True), ("VARESE", "LAGHI", 10, .01, True)]
MOTIVI = [("medico_acuto", 52), ("caduta", 14), ("evento_violento", 3.5), ("incidente_stradale", 6),
          ("intossicazione", 4), ("infortunio", 3), ("soccorso_persona", 1.2), ("non_noto", 1.5),
          ("incidente_montano", .3), ("incidente_acqua", .1), ("animali", .1), ("altri_motivi", .1)]
CODICI = [("rosso", 14), ("giallo", 58), ("verde", 27), ("bianco", 1)]
ALL_NUM = ["animali", "msi", "elisoccorso", "trasporto_organi", "da_richiamare", "rosso", "verde",
           "totale_missioni", "intossicazione", "evento_massa", "missioni", "incidente_montano",
           "informazioni", "bianco", "consulenza", "soccorso_acqua", "non_noto", "soccorso_secondario",
           "infortunio", "motivo_altro", "soccorso_alpino", "incidente_stradale", "altri_motivi",
           "mezzi_altro", "soccorsi", "incidente_acqua", "trasporto_prenotato", "giallo", "caduta",
           "soccorso_persona", "evento_violento", "altro", "prestazione_ambulatoriale", "medico_acuto",
           "incidente_speleo", "soccorso_primario", "calamita_naturale", "guardia_medica", "msa",
           "totale_traffico_tel", "msb"]


def diurnal(h):
    # minimo verso le 4-5, picco tra le 10 e le 19
    return 0.45 + 0.75 * math.exp(-((h - 14.5) / 6.5) ** 2) + 0.12 * math.exp(-((h - 21) / 2) ** 2)


def pick(rnd, table):
    tot = sum(w for _, w in table)
    x = rnd.random() * tot
    for k, w in table:
        x -= w
        if x <= 0:
            return k
    return table[-1][0]


def simulate(start, end, seed=7):
    """Restituisce {zona: [missione...]} con missione = dict(start, end, tipo, codice, evento, motivo)."""
    rnd = random.Random(seed)
    out = {}
    for aat, soreu, rate, eli, monti in ZONE:
        miss, t, ev = [], start, 0
        while t < end:
            local = t.astimezone(TZ)
            h = local.hour + local.minute / 60
            r = rate * diurnal(h) * (1.08 if local.weekday() >= 5 else 1)
            # picco simulato a Milano nella sera dell'ultimo giorno
            if aat == "MILANO" and end - timedelta(hours=2.2) < t < end - timedelta(minutes=20):
                r *= 1.9
            t += timedelta(minutes=rnd.expovariate(r / 60))
            ev += 1
            motivo = pick(rnd, MOTIVI)
            if monti and rnd.random() < .006:
                motivo = "incidente_montano"
            n_mezzi = 2 if rnd.random() < .09 else 1
            for k in range(n_mezzi):
                if k == 1:
                    tipo = "msa" if rnd.random() < .6 else "msi"
                else:
                    x = rnd.random()
                    tipo = "elisoccorso" if x < eli or motivo == "incidente_montano" and x < .5 else \
                        "msa" if x < eli + .05 else "msi" if x < eli + .1 else "msb"
                durata = rnd.lognormvariate(math.log(70 if aat != "MILANO" else 85), .45)
                codice = "rosso" if n_mezzi == 2 and rnd.random() < .6 else pick(rnd, CODICI)
                miss.append(dict(start=t, end=t + timedelta(minutes=durata), tipo=tipo,
                                 codice=codice, evento=ev, motivo=motivo,
                                 alpino=motivo == "incidente_montano" and rnd.random() < .5))
        out[aat] = miss
    return out


LAST_REPORT = {}


def payload(sim, t, rnd):
    def row(aat, soreu, ms, day, agg, day_data):
        r = {k: 0 for k in ALL_NUM}
        eventi = {}
        for m in ms:
            r["missioni"] += 1
            r[m["tipo"]] += 1
            r[m["codice"]] += 1
            eventi[m["evento"]] = m
        r["totale_missioni"] = len(eventi)
        r["soccorsi"] = len(eventi)
        for m in eventi.values():
            r[m["motivo"]] += 1
            r["soccorso_alpino"] += m["alpino"]
        r = {k: str(v) for k, v in r.items()}
        for k in ("informazioni", "consulenza", "guardia_medica", "trasporto_organi"):
            if rnd.random() < .5:
                r[k] = ""   # come l'API vera, a volte stringhe vuote
        r.update(aat=aat, soreu=soreu, day=day, aggiornato_alle=agg.strftime("%H:%M"),
                 day_data=day_data.strftime("%d/%m/%Y"))
        return r

    in_corso, del_giorno = [], []
    for aat, soreu, *_ in ZONE:
        ms = sim[aat]
        # ogni zona e' aggiornata con un ritardo casuale (Sondrio a volte molto indietro)
        lag = rnd.choice([0, 1, 2, 4, 7, 11]) + (rnd.choice([0, 0, 0, 35, 70]) if aat == "SONDRIO" else 0)
        tr = (t - timedelta(minutes=lag)).astimezone(TZ)
        tr = max(tr, LAST_REPORT.get(aat, tr))   # l'orario di aggiornamento non torna indietro
        LAST_REPORT[aat] = tr
        active = [m for m in ms if m["start"] <= tr < m["end"]]
        in_corso.append(row(aat, soreu, active, "OGGI", tr, tr))
        midnight = tr.replace(hour=0, minute=0, second=0, microsecond=0)
        today = [m for m in ms if midnight <= m["start"] <= tr]
        del_giorno.append(row(aat, soreu, today, "OGGI", tr, tr))
        y0 = midnight - timedelta(days=1)
        yday = [m for m in ms if y0 <= m["start"] < midnight]
        del_giorno.append(row(aat, soreu, yday, "IERI", midnight - timedelta(minutes=rnd.randint(1, 15)), y0))
    return {"in_corso": in_corso, "del_giorno": del_giorno, "updated": t.isoformat()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("progetto")
    ap.add_argument("--fine", default="2026-11-08T21:40")
    ap.add_argument("--giorni", type=int, default=21)
    ap.add_argument("--passo-dettaglio-giorni", type=int, default=8,
                    help="negli ultimi N giorni una lettura ogni 10 minuti, prima ogni 30")
    args = ap.parse_args()

    spec = importlib.util.spec_from_file_location("scrape", os.path.join(args.progetto, "scraper", "scrape.py"))
    S = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(S)

    end = datetime.fromisoformat(args.fine).replace(tzinfo=TZ)
    start = end - timedelta(days=args.giorni)
    sim = simulate(start - timedelta(days=1), end)
    rnd = random.Random(3)
    t, n = start, 0
    while t <= end:
        p = payload(sim, t, rnd)
        inc = [S.normalize(r) for r in p["in_corso"]]
        dg = [S.normalize(r) for r in p["del_giorno"]]
        S.append_rows("in_corso", inc, t, p["updated"])
        S.append_rows("oggi", [r for r in dg if r["day"] == "OGGI"], t, p["updated"])
        finals = S.upsert_giornaliero(dg)
        n += 1
        step = 10 if end - t < timedelta(days=args.passo_dettaglio_giorni) else 30
        t += timedelta(minutes=step)
    print(f"{n} letture simulate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
