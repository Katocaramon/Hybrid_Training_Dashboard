"""Dashboard della corsa: pagina separata, database condiviso.

Da dove viene
-------------

Questa pagina nasce dal progetto `garmin-dashboard`, che leggeva i suoi
`data/*.json` scaricati dalla Personal API. Il template e' lo stesso — SVG
disegnati a mano, nessuna dipendenza — ma i numeri ora arrivano dal database
che condivide con la palestra. Risultato: una cartella sola, Garmin
interrogato una volta, e le due dashboard restano due.

Cosa cambia rispetto a prima
----------------------------

Il gruppo "Palestra" non viene piu' dalle attivita' Garmin, che non espongono
le serie, ma dalle **sedute di forza vere** (Hevy o `.fit`): stesse ore, ma
contate da dove ci sono anche carichi e ripetizioni.

Niente stime: un giorno senza misura e' assente dalla serie, non uno zero. Le
medie mobili a 7 giorni compaiono solo con almeno tre misure nella finestra,
altrimenti sarebbero la media di un dato solo travestita da tendenza.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

#: I quattro gruppi della pagina, nell'ordine in cui vanno colorati.
GRUPPI = ("Corsa", "Palestra", "Bici", "Altro")

#: `activity_group` del database -> gruppo della pagina.
GRUPPO_PAGINA = {"corsa": "Corsa", "bici": "Bici", "palestra": "Palestra"}

#: Settimane mostrate nei grafici a barre.
SETTIMANE = 16

#: Distanze dei record, con la lunghezza in km per calcolare il passo.
RECORD = (
    ("1000", "1 km", 1.0),
    ("1609", "1 miglio", 1.609),
    ("5000", "5 km", 5.0),
    ("10000", "10 km", 10.0),
    ("21098", "Mezza", 21.0975),
)


#: Nome della gara nel file di esempio. Un `cp` senza modifiche e' un errore
#: facile e silenzioso — la pagina mostrerebbe una gara finta con l'aria di
#: essere vera — quindi viene riconosciuto e fermato.
NOME_ESEMPIO = "La mia gara"


class ConfigCorsa(dict):
    """Gara e inizio del piano. Assente = la pagina non mostra il conto alla rovescia."""


def carica_config(percorso: Path | None) -> ConfigCorsa | None:
    """Legge `config/corsa.yaml` (o un `settings.json` del vecchio progetto).

    Senza file non si inventa una gara: la pagina lo dice e mostra tutto il
    resto. Serve `race_name`, `race_date` e `plan_start` (il lunedi' della
    prima settimana del piano).
    """
    if percorso is None:
        return None
    percorso = Path(percorso)
    if not percorso.is_file():
        return None
    testo = percorso.read_text(encoding="utf-8")
    if percorso.suffix.lower() == ".json":
        dati = json.loads(testo)
    else:
        import yaml

        dati = yaml.safe_load(testo)
    if not isinstance(dati, dict):
        raise ValueError(f"{percorso}: attesa una mappa con race_name, race_date, plan_start")
    mancanti = [k for k in ("race_name", "race_date", "plan_start") if not dati.get(k)]
    if mancanti:
        raise ValueError(f"{percorso}: manca {', '.join(mancanti)}")
    if str(dati["race_name"]).strip().lower() == NOME_ESEMPIO.lower():
        raise ValueError(
            f"{percorso} contiene ancora i valori del modello "
            f"(race_name: {NOME_ESEMPIO}). Scrivici la tua gara: nome, data e "
            "il lunedi' della prima settimana del piano."
        )
    for chiave in ("race_date", "plan_start"):
        date.fromisoformat(str(dati[chiave]))  # fallisce subito se la data e' storta
    return ConfigCorsa(
        name=str(dati["race_name"]),
        date=str(dati["race_date"]),
        planStart=str(dati["plan_start"]),
    )


# --------------------------------------------------------------------------
# lettura dal database
# --------------------------------------------------------------------------


def _lunedi(giorno: date) -> date:
    return giorno - timedelta(days=giorno.weekday())


def _arrotonda(valore: Any, cifre: int = 1) -> float | None:
    return None if valore is None else round(float(valore), cifre)


def attivita(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Tutte le attivita' della pagina: resistenza da Garmin, palestra da Hevy.

    Una lista sola perche' la pagina le confronta fra loro: ore per sport,
    giornate piu' dure, ultime attivita'.
    """
    fuori: list[dict[str, Any]] = []
    for riga in conn.execute(
        """SELECT session_uid, local_date, activity_group, activity_name, distance_km,
                  COALESCE(moving_time_s, total_timer_s, total_elapsed_s) AS secondi,
                  elevation_gain_m, avg_hr, training_load, activity_type_key,
                  best_1000_s, best_1609_s, best_5000_s, best_10000_s, best_21098_s
           FROM v_endurance
           WHERE local_date IS NOT NULL
           ORDER BY local_date, start_time_local"""
    ):
        fuori.append(
            {
                "id": riga["session_uid"],
                "d": riga["local_date"],
                "g": GRUPPO_PAGINA.get(riga["activity_group"] or "", "Altro"),
                "name": riga["activity_name"] or riga["activity_type_key"] or "Attivita'",
                "km": float(riga["distance_km"] or 0.0),
                "sec": float(riga["secondi"] or 0.0),
                "elev": float(riga["elevation_gain_m"] or 0.0),
                "hr": riga["avg_hr"],
                "load": float(riga["training_load"] or 0.0),
                "splits": {d: riga[f"best_{d}_s"] for d, _, _ in RECORD},
            }
        )
    # La palestra arriva dalle sedute di forza, non dall'elenco Garmin: qui
    # ci sono anche le serie, e non c'e' il rischio di contarle due volte.
    for riga in conn.execute(
        """SELECT session_uid, local_date, workout_name,
                  COALESCE(total_timer_s, total_elapsed_s) AS secondi, avg_hr
           FROM sessions
           WHERE activity_type = 'strength' AND local_date IS NOT NULL
           ORDER BY local_date, start_time_local"""
    ):
        fuori.append(
            {
                "id": riga["session_uid"],
                "d": riga["local_date"],
                "g": "Palestra",
                "name": riga["workout_name"] or "Palestra",
                "km": 0.0,
                "sec": float(riga["secondi"] or 0.0),
                "elev": 0.0,
                "hr": riga["avg_hr"],
                "load": 0.0,  # Hevy non da' il carico di allenamento Garmin
                "splits": {},
            }
        )
    fuori.sort(key=lambda a: a["d"])
    return fuori


def _settimane(atti: list[dict[str, Any]], oggi: date, quante: int) -> list[dict[str, Any]]:
    vuoto = {g: {"km": 0.0, "sec": 0.0, "n": 0} for g in GRUPPI}
    per_settimana: dict[str, dict[str, dict[str, float]]] = {}
    for a in atti:
        chiave = _lunedi(date.fromisoformat(a["d"])).isoformat()
        gruppo = per_settimana.setdefault(
            chiave, {g: {"km": 0.0, "sec": 0.0, "n": 0} for g in GRUPPI}
        )[a["g"]]
        gruppo["km"] += a["km"]
        gruppo["sec"] += a["sec"]
        gruppo["n"] += 1
    questa = _lunedi(oggi)
    righe = []
    for indietro in range(quante - 1, -1, -1):
        chiave = (questa - timedelta(weeks=indietro)).isoformat()
        settimana = per_settimana.get(chiave, vuoto)
        righe.append(
            {
                "w": chiave,
                **{
                    g: {k: _arrotonda(v, 2) for k, v in settimana[g].items()}
                    for g in GRUPPI
                },
            }
        )
    return righe


def _confronto(settimane: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Questa settimana (parziale) contro la media delle 4 precedenti complete."""
    if not settimane:
        return []
    corrente = settimane[-1]
    precedenti = settimane[-5:-1]
    fuori = []
    for gruppo in GRUPPI:
        if not precedenti:
            media = {"km": 0.0, "sec": 0.0, "n": 0.0}
        else:
            media = {
                k: sum(w[gruppo][k] for w in precedenti) / len(precedenti)
                for k in ("km", "sec", "n")
            }
        if corrente[gruppo]["n"] or media["n"]:
            fuori.append({"g": gruppo, "cur": corrente[gruppo], "prev": media})
    return fuori


def _giorni(
    conn: sqlite3.Connection, atti: list[dict[str, Any]], oggi: date
) -> list[dict[str, Any]]:
    """Una riga per giorno: benessere misurato piu' carico acuto e cronico.

    Il carico e' una somma su finestre mobili (7 e 28 giorni), quindi parte
    dal primo giorno con attivita': prima non sarebbe basso, sarebbe ignoto.
    """
    benessere = {riga["day"]: riga for riga in conn.execute("SELECT * FROM wellness ORDER BY day")}
    carico_per_giorno: dict[str, float] = {}
    for a in atti:
        carico_per_giorno[a["d"]] = carico_per_giorno.get(a["d"], 0.0) + a["load"]
    date_note = list(benessere) + [a["d"] for a in atti]
    if not date_note:
        return []
    primo = date.fromisoformat(min(date_note))
    prima_attivita = date.fromisoformat(min((a["d"] for a in atti), default=oggi.isoformat()))

    storia: dict[str, list[float | None]] = {"rhr": [], "sl": []}
    righe: list[dict[str, Any]] = []
    giorno = primo
    while giorno <= oggi:
        chiave = giorno.isoformat()
        riga: dict[str, Any] = {"d": chiave}
        b = benessere.get(chiave)
        if b is not None:
            for campo, colonna in (
                ("rhr", "resting_hr"),
                ("bb", "body_battery_max"),
                ("stress", "stress_avg"),
                ("hrv", "hrv_last_night"),
                ("hrv7", "hrv_weekly_avg"),
                ("st", "hrv_status"),
                ("sl", "sleep_h"),
                ("ss", "sleep_score"),
                ("deep", "deep_sleep_h"),
                ("rem", "rem_sleep_h"),
                ("kg", "body_weight_kg"),
            ):
                if b[colonna] is not None:
                    riga[campo] = b[colonna]
            if b["hrv_baseline_low"] is not None and b["hrv_baseline_high"] is not None:
                riga["lo"], riga["hi"] = b["hrv_baseline_low"], b["hrv_baseline_high"]
        # Media mobile a 7 giorni, calcolata solo con almeno 3 misure: sotto
        # quella soglia non e' una tendenza, e' rumore con una linea sopra.
        for campo in ("rhr", "sl"):
            storia[campo] = (storia[campo] + [riga.get(campo)])[-7:]
            valori = [v for v in storia[campo] if v is not None]
            if len(valori) >= 3:
                riga[f"{campo}7"] = round(sum(valori) / len(valori), 2 if campo == "sl" else 1)
        if giorno >= prima_attivita:
            riga["ac"] = round(
                sum(
                    carico_per_giorno.get((giorno - timedelta(days=i)).isoformat(), 0.0)
                    for i in range(7)
                )
            )
            riga["ch"] = round(
                sum(
                    carico_per_giorno.get((giorno - timedelta(days=i)).isoformat(), 0.0)
                    for i in range(28)
                )
                / 4
            )
        righe.append(riga)
        giorno += timedelta(days=1)
    return righe


def _record(atti: list[dict[str, Any]], oggi: date) -> list[dict[str, Any]]:
    corse = [a for a in atti if a["g"] == "Corsa"]
    limite = (oggi - timedelta(days=365)).isoformat()
    fuori = []
    for chiave, etichetta, km in RECORD:
        candidati = [(a["splits"].get(chiave), a) for a in corse if a["splits"].get(chiave)]
        if not candidati:
            continue
        tempo, migliore = min(candidati, key=lambda x: x[0])
        recenti = [c for c in candidati if c[1]["d"] >= limite]
        anno = min(recenti, key=lambda x: x[0]) if recenti else None
        fuori.append(
            {
                "label": etichetta,
                "km": km,
                "sec": tempo,
                "d": migliore["d"],
                "name": migliore["name"],
                "year": anno[0] if anno else None,
                "yearD": anno[1]["d"] if anno else None,
            }
        )
    return fuori


def _breve(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "d": a["d"],
        "name": a["name"],
        "km": _arrotonda(a["km"], 2),
        "sec": round(a["sec"]),
        "elev": round(a["elev"]),
        "load": round(a["load"]),
        "g": a["g"],
    }


def costruisci_dati(
    conn: sqlite3.Connection,
    config: ConfigCorsa | None = None,
    *,
    oggi: date | None = None,
    settimane: int = SETTIMANE,
) -> dict[str, Any]:
    """Il payload della pagina. Stessa forma che aveva nel progetto originale."""
    oggi = oggi or date.today()
    atti = attivita(conn)
    corse = [a for a in atti if a["g"] == "Corsa"]
    righe_settimana = _settimane(atti, oggi, settimane)

    km_per_settimana = sorted(
        ((w["w"], w["Corsa"]["km"]) for w in righe_settimana if w["Corsa"]["km"]),
        key=lambda x: -x[1],
    )[:3]
    mesi: dict[str, float] = {}
    for a in corse:
        mesi[a["d"][:7]] = mesi.get(a["d"][:7], 0.0) + a["km"]
    mesi_record = sorted(mesi.items(), key=lambda x: -x[1])[:3]

    ultimo_profilo = conn.execute(
        """SELECT fitness_age, chronological_age FROM wellness
           WHERE fitness_age IS NOT NULL ORDER BY day DESC LIMIT 1"""
    ).fetchone()

    return {
        "today": oggi.isoformat(),
        "generated": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "race": config,
        "weeks": righe_settimana,
        "compare": _confronto(righe_settimana),
        "rows": _giorni(conn, atti, oggi),
        "best": _record(atti, oggi),
        "longest": [_breve(a) for a in sorted(corse, key=lambda a: -a["km"])[:5]],
        "biggest": [_breve(a) for a in sorted(atti, key=lambda a: -a["load"])[:5] if a["load"]],
        "climb": [_breve(a) for a in sorted(corse, key=lambda a: -a["elev"])[:3] if a["elev"]],
        "runWeeks": [{"w": w, "km": _arrotonda(km)} for w, km in km_per_settimana],
        "runMonths": [{"m": m, "km": _arrotonda(km)} for m, km in mesi_record],
        "totals": {
            "acts": len(atti),
            "runs": len(corse),
            "runKm": round(sum(a["km"] for a in corse)),
            "since": atti[0]["d"] if atti else None,
        },
        "fitnessAge": _arrotonda(ultimo_profilo["fitness_age"]) if ultimo_profilo else None,
        "age": ultimo_profilo["chronological_age"] if ultimo_profilo else None,
        "recent": [_breve(a) for a in atti[-8:]][::-1],
    }


def genera(
    conn: sqlite3.Connection,
    destinazione: Path,
    config: ConfigCorsa | None = None,
    *,
    oggi: date | None = None,
) -> Path:
    """Scrive la dashboard della corsa. Ritorna il percorso del file."""
    from .dashboard import _cerca_asset

    template = _cerca_asset("templates") / "corsa.html"
    if not template.is_file():
        raise FileNotFoundError(f"manca {template}")
    dati = costruisci_dati(conn, config, oggi=oggi)
    # `</script>` dentro il JSON chiuderebbe il tag: va neutralizzato.
    payload = json.dumps(dati, ensure_ascii=False, allow_nan=False, separators=(",", ":")).replace(
        "</", "<\\/"
    )
    html = template.read_text(encoding="utf-8").replace("/*__DATA__*/null", payload)
    destinazione = Path(destinazione)
    destinazione.parent.mkdir(parents=True, exist_ok=True)
    destinazione.write_text(html, encoding="utf-8")
    return destinazione
