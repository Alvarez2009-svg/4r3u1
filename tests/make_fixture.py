#!/usr/bin/env python3
"""Genera una risposta finta dell'API AREU con la stessa struttura di quella reale
(numeri come stringhe, alcuni campi vuoti) per provare scraper e dashboard.
Uso: python tests/make_fixture.py ORARIO_ISO SEED > fixture.json"""
import json
import math
import random
import sys
from datetime import datetime

AAT = [("BERGAMO", "ALPI", 18), ("BRESCIA", "ALPI", 19), ("COMO", "LAGHI", 8),
       ("CREMONA", "PIANURA", 5), ("LECCO", "LAGHI", 6), ("LODI", "PIANURA", 6),
       ("MANTOVA", "PIANURA", 9), ("MILANO", "METROPOLITANA", 80),
       ("MONZA", "METROPOLITANA", 12), ("PAVIA", "PIANURA", 8),
       ("SONDRIO", "ALPI", 5), ("VARESE", "LAGHI", 9)]
MOTIVI = ["medico_acuto", "caduta", "incidente_stradale", "evento_violento",
          "intossicazione", "infortunio", "soccorso_persona"]
ALL = ["animali", "msi", "elisoccorso", "trasporto_organi", "da_richiamare", "rosso",
       "verde", "totale_missioni", "intossicazione", "evento_massa", "missioni",
       "incidente_montano", "informazioni", "bianco", "consulenza", "soccorso_acqua",
       "non_noto", "soccorso_secondario", "infortunio", "motivo_altro", "soccorso_alpino",
       "incidente_stradale", "altri_motivi", "mezzi_altro", "soccorsi", "incidente_acqua",
       "trasporto_prenotato", "giallo", "caduta", "soccorso_persona", "evento_violento",
       "altro", "prestazione_ambulatoriale", "medico_acuto", "incidente_speleo",
       "soccorso_primario", "calamita_naturale", "guardia_medica", "msa",
       "totale_traffico_tel", "msb"]


def daily_curve(h):  # carico relativo nelle 24 ore: minimo alle 5, picco a meta' giornata
    return 0.55 + 0.45 * math.sin((h - 9) / 24 * 2 * math.pi) ** 2 + (0.15 if 9 <= h <= 21 else 0)


def row(aat, soreu, base, now, scale, day, rnd):
    m = max(0, int(rnd.gauss(base * scale, base * scale * 0.15 + 1)))
    r = {k: "0" for k in ALL}
    rosso = int(m * rnd.uniform(0.05, 0.18)); verde = int(m * rnd.uniform(0.2, 0.35))
    r.update(missioni=str(m), totale_missioni=str(max(0, m - rnd.randint(0, 3))),
             soccorsi=str(m), rosso=str(rosso), verde=str(verde),
             giallo=str(max(0, m - rosso - verde)), msb=str(m), soccorso_primario=str(m),
             msa=str(rnd.randint(0, 3)), msi=str(rnd.randint(0, 2)),
             elisoccorso=str(int(rnd.random() < 0.1 * (aat in ("SONDRIO", "BERGAMO", "BRESCIA", "LECCO")))))
    left = m
    for mot in MOTIVI[1:]:
        v = int(m * rnd.uniform(0, 0.12)); r[mot] = str(v); left -= v
    r["medico_acuto"] = str(max(0, left))
    for k in ("informazioni", "consulenza", "guardia_medica", "trasporto_organi"):
        if rnd.random() < 0.4:
            r[k] = ""  # l'API reale a volte manda stringhe vuote
    r.update(aat=aat, soreu=soreu, day=day, day_data=now.strftime("%d/%m/%Y"),
             aggiornato_alle=now.strftime("%H:%M"))
    return r


def main():
    now = datetime.fromisoformat(sys.argv[1])
    rnd = random.Random(int(sys.argv[2]) if len(sys.argv) > 2 else 1)
    scale = daily_curve(now.hour + now.minute / 60) * (1.1 if now.weekday() >= 5 else 1.0)
    payload = {
        "in_corso": [row(a, s, b, now, scale, "OGGI", rnd) for a, s, b in AAT],
        "del_giorno": ([row(a, s, b * 14, now, 1, "IERI", rnd) for a, s, b in AAT]
                       + [row(a, s, b * 14 * (now.hour + 1) / 24, now, 1, "OGGI", rnd) for a, s, b in AAT]),
        "author": {"name": "test"}, "subtitle": "Statistiche del giorno",
        "id": "test", "title": "Eventi AREU", "updated": now.isoformat(),
    }
    json.dump(payload, sys.stdout)


if __name__ == "__main__":
    main()
