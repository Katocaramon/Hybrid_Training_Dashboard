"""Metriche derivate.

Tutto parte da `v_sets`, quindi le correzioni manuali e la mappatura sono
gia' applicate sopra i dati grezzi.

Assunzioni, tutte esplicite (i dettagli sono nel README):

* **Tonnellaggio** = peso x ripetizioni, sommato. Conta solo le serie in cui
  il peso e' davvero un carico esterno (`weight_mode = carico`). Se manca
  reps o peso la serie non contribuisce e viene contata a parte: una metrica
  non calcolabile si dichiara, non si azzera.
* **e1RM di Epley** = peso x (1 + reps / 30). E' una stima lineare tarata
  sulle serie corte: sopra le 12 ripetizioni sovrastima, e infatti viene
  marcata inaffidabile. Su una singola ripetizione la formula darebbe 1,033
  volte il peso, quindi quel caso e' trattato a parte e restituisce il peso.
* **Densita'** = tonnellaggio / tempo attivo della seduta (kg al minuto).
  Il tempo attivo e' `session.total_timer_time`, l'unico che l'orologio
  fornisce.
* **Rapporto lavoro/riposo** = durata delle serie attive / durata delle pause,
  entrambe dai messaggi `set`. Le pause non registrate non vengono stimate.
* **Deriva della FC** = FC media delle serie attive nell'ultimo terzo della
  seduta meno quella del primo terzo. E' un proxy grezzo di accumulo di
  fatica: sale anche solo perche' la seduta scalda.
* **Serie per gruppo muscolare** conta le serie attive, non il tonnellaggio:
  regge anche quando gli esercizi cambiano o il carico non e' misurabile.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

#: Oltre questa soglia la formula di Epley non e' piu' attendibile.
EPLEY_MAX_REPS = 12

#: Finestra della media mobile sul volume settimanale.
FINESTRA_MEDIA_MOBILE = 4


def epley(peso_kg: float | None, reps: int | None) -> float | None:
    """1RM stimato secondo Epley: peso x (1 + reps / 30).

    Su una singola ripetizione la formula restituirebbe 1,033 volte il peso:
    il massimale su una ripetizione e' il peso stesso, quindi il caso reps=1
    e' trattato a parte. `None` quando i dati non bastano o non hanno senso
    (peso o ripetizioni assenti, nulli o negativi).
    """
    if peso_kg is None or reps is None or reps <= 0 or peso_kg <= 0:
        return None
    if reps == 1:
        return peso_kg
    return peso_kg * (1 + reps / 30)


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params)]


def _etichetta_settimana(anno: int, settimana: int) -> str:
    lunedi = date.fromisocalendar(anno, settimana, 1)
    domenica = lunedi + timedelta(days=6)
    return f"{lunedi.strftime('%d/%m')}–{domenica.strftime('%d/%m/%Y')}"


# --------------------------------------------------------------------------
# riepilogo
# --------------------------------------------------------------------------


def riepilogo(conn: sqlite3.Connection) -> dict[str, Any]:
    """Numeri di testa: sedute, periodo coperto, volume, ritmo recente."""
    ses = conn.execute(
        """SELECT COUNT(*) AS n_sedute,
                  MIN(local_date) AS prima,
                  MAX(local_date) AS ultima,
                  SUM(total_timer_s) AS tempo_attivo_s
           FROM sessions WHERE activity_type = 'strength'"""
    ).fetchone()
    serie = conn.execute(
        """SELECT COUNT(*) AS n_serie,
                  SUM(volume_kg) AS volume_kg,
                  SUM(CASE WHEN volume_kg IS NOT NULL THEN 1 ELSE 0 END) AS n_con_volume,
                  SUM(CASE WHEN weight_mode = 'carico' THEN 1 ELSE 0 END) AS n_a_carico,
                  SUM(duration_s) AS tempo_sotto_tensione_s
           FROM v_sets WHERE set_type = 'active'"""
    ).fetchone()

    ultima = ses["ultima"]
    sedute_4w = None
    if ultima:
        limite = (date.fromisoformat(ultima) - timedelta(weeks=4)).isoformat()
        sedute_4w = conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE local_date > ?", (limite,)
        ).fetchone()["n"]

    return {
        "n_sedute": ses["n_sedute"],
        "prima_seduta": ses["prima"],
        "ultima_seduta": ses["ultima"],
        "tempo_attivo_totale_s": ses["tempo_attivo_s"],
        "n_serie_attive": serie["n_serie"] or 0,
        "volume_totale_kg": serie["volume_kg"],
        "serie_con_volume": serie["n_con_volume"] or 0,
        "serie_a_carico": serie["n_a_carico"] or 0,
        "tempo_sotto_tensione_s": serie["tempo_sotto_tensione_s"],
        "sedute_ultime_4_settimane": sedute_4w,
    }


# --------------------------------------------------------------------------
# volume
# --------------------------------------------------------------------------


def settimane_coperte(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """Tutte le settimane ISO fra la prima e l'ultima seduta, buchi compresi.

    Una settimana senza allenamento non e' un buco nei dati: e' informazione.
    """
    righe = _rows(
        conn,
        "SELECT MIN(local_date) AS prima, MAX(local_date) AS ultima FROM sessions",
    )
    if not righe or not righe[0]["prima"]:
        return []
    prima = date.fromisoformat(righe[0]["prima"])
    ultima = date.fromisoformat(righe[0]["ultima"])
    giorno = prima - timedelta(days=prima.weekday())
    out: list[tuple[int, int]] = []
    while giorno <= ultima:
        anno, settimana, _ = giorno.isocalendar()
        out.append((anno, settimana))
        giorno += timedelta(days=7)
    return out


def volume_settimanale(
    conn: sqlite3.Connection, finestra: int = FINESTRA_MEDIA_MOBILE
) -> list[dict[str, Any]]:
    """Tonnellaggio per settimana ISO, con media mobile.

    `volume_kg` e' `None` quando nessuna serie della settimana aveva sia reps
    sia peso: e' diverso da zero, e la dashboard lo dice.
    """
    dati = {
        (r["iso_year"], r["iso_week"]): r
        for r in _rows(
            conn,
            """SELECT iso_year, iso_week,
                      SUM(volume_kg) AS volume_kg,
                      COUNT(DISTINCT session_id) AS n_sedute,
                      COUNT(*) AS n_serie,
                      SUM(CASE WHEN volume_kg IS NOT NULL THEN 1 ELSE 0 END) AS n_serie_con_volume
               FROM v_sets WHERE set_type = 'active'
               GROUP BY iso_year, iso_week""",
        )
    }
    out: list[dict[str, Any]] = []
    for anno, settimana in settimane_coperte(conn):
        r = dati.get((anno, settimana), {})
        out.append(
            {
                "iso_year": anno,
                "iso_week": settimana,
                "etichetta": _etichetta_settimana(anno, settimana),
                "volume_kg": r.get("volume_kg"),
                "n_sedute": r.get("n_sedute", 0),
                "n_serie": r.get("n_serie", 0),
                "n_serie_con_volume": r.get("n_serie_con_volume", 0),
            }
        )

    # Media mobile sulle sole settimane con volume misurabile: una settimana
    # senza dati non vale zero, altrimenti la media crollerebbe per finta.
    for i, riga in enumerate(out):
        finestra_valori = [
            w["volume_kg"] for w in out[max(0, i - finestra + 1) : i + 1] if w["volume_kg"] is not None
        ]
        riga["media_mobile_kg"] = (
            round(sum(finestra_valori) / len(finestra_valori), 1) if finestra_valori else None
        )
    return out


def volume_per_gruppo(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Tonnellaggio e serie per gruppo muscolare, per settimana."""
    return _rows(
        conn,
        """SELECT iso_year, iso_week,
                  COALESCE(muscle_group, 'non mappato') AS gruppo,
                  SUM(volume_kg) AS volume_kg,
                  COUNT(*) AS n_serie,
                  SUM(COALESCE(duration_s, 0)) AS durata_s
           FROM v_sets WHERE set_type = 'active'
           GROUP BY iso_year, iso_week, gruppo
           ORDER BY iso_year, iso_week, gruppo""",
    )


def serie_per_gruppo(conn: sqlite3.Connection, focus: list[str] | None = None) -> dict[str, Any]:
    """Serie per gruppo muscolare a settimana.

    E' la metrica di volume piu' robusta quando gli esercizi cambiano o il
    carico non e' misurabile: conta le serie, non i chili.
    """
    per_settimana = volume_per_gruppo(conn)
    gruppi = sorted({r["gruppo"] for r in per_settimana})
    settimane = [
        {"iso_year": a, "iso_week": s, "etichetta": _etichetta_settimana(a, s)}
        for a, s in settimane_coperte(conn)
    ]
    indice = {(r["iso_year"], r["iso_week"], r["gruppo"]): r for r in per_settimana}
    serie = {
        g: [indice.get((w["iso_year"], w["iso_week"], g), {}).get("n_serie", 0) for w in settimane]
        for g in gruppi
    }
    volumi = {
        g: [indice.get((w["iso_year"], w["iso_week"], g), {}).get("volume_kg") for w in settimane]
        for g in gruppi
    }
    return {
        "settimane": settimane,
        "gruppi": gruppi,
        "serie": serie,
        "volume_kg": volumi,
        "focus": [g for g in (focus or []) if g in gruppi],
    }


# --------------------------------------------------------------------------
# progressione per esercizio
# --------------------------------------------------------------------------


def esercizi_disponibili(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows(
        conn,
        """SELECT exercise_name AS nome,
                  MAX(weight_mode) AS weight_mode,
                  COUNT(*) AS n_serie,
                  COUNT(DISTINCT session_id) AS n_sedute,
                  COUNT(weight_kg) AS n_con_peso,
                  COALESCE(SUM(volume_kg), 0) AS volume_kg,
                  MAX(muscle_group) AS gruppo
           FROM v_sets
           WHERE set_type = 'active' AND exercise_name IS NOT NULL
           GROUP BY exercise_name
           -- Prima gli esercizi su cui la progressione ha qualcosa da dire:
           -- uno di loro e' il default del selettore.
           ORDER BY volume_kg DESC, (n_con_peso > 0) DESC, n_serie DESC, nome""",
    )


def progressione(conn: sqlite3.Connection, esercizio: str) -> dict[str, Any]:
    """Andamento di un esercizio: peso migliore, e1RM, reps totali, per seduta."""
    righe = _rows(
        conn,
        """SELECT local_date, session_id, weight_mode,
                  reps, weight_kg, carico_effettivo_kg, volume_kg, carico_stimato
           FROM v_sets
           WHERE set_type = 'active' AND exercise_name = ?
           ORDER BY local_date, order_in_session""",
        (esercizio,),
    )
    per_data: dict[str, dict[str, Any]] = {}
    for r in righe:
        d = per_data.setdefault(
            r["local_date"],
            {
                "local_date": r["local_date"],
                "weight_mode": r["weight_mode"],
                "n_serie": 0,
                "reps_totali": 0,
                "peso_migliore_kg": None,
                "carico_effettivo_migliore_kg": None,
                "assistenza_minima_kg": None,
                "e1rm_kg": None,
                "e1rm_affidabile": None,
                "volume_kg": None,
                "serie_senza_dati": 0,
            },
        )
        d["n_serie"] += 1
        if r["reps"] is None:
            d["serie_senza_dati"] += 1
        else:
            d["reps_totali"] += r["reps"]
        if r["weight_kg"] is not None:
            d["peso_migliore_kg"] = max(d["peso_migliore_kg"] or 0.0, r["weight_kg"])
            if r["weight_mode"] == "assistenza":
                minimo = d["assistenza_minima_kg"]
                d["assistenza_minima_kg"] = r["weight_kg"] if minimo is None else min(minimo, r["weight_kg"])
        if r["volume_kg"] is not None:
            d["volume_kg"] = (d["volume_kg"] or 0.0) + r["volume_kg"]
        carico = r["carico_effettivo_kg"]
        if carico is not None:
            d["carico_effettivo_migliore_kg"] = max(d["carico_effettivo_migliore_kg"] or 0.0, carico)
        stima = epley(carico, r["reps"])
        if stima is not None and (d["e1rm_kg"] is None or stima > d["e1rm_kg"]):
            d["e1rm_kg"] = round(stima, 1)
            d["e1rm_affidabile"] = (r["reps"] or 0) <= EPLEY_MAX_REPS
    punti = sorted(per_data.values(), key=lambda d: d["local_date"])
    stimato = bool(righe) and righe[0]["carico_stimato"] == 1
    return {
        "esercizio": esercizio,
        "weight_mode": righe[0]["weight_mode"] if righe else "carico",
        "carico_stimato": stimato,
        "punti": punti,
    }


# --------------------------------------------------------------------------
# seduta: densita', lavoro/riposo, deriva FC
# --------------------------------------------------------------------------


def sedute(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Una riga per seduta, con densita', lavoro/riposo e deriva della FC."""
    base = _rows(
        conn,
        """SELECT s.id AS session_id, s.session_uid, s.local_date, s.start_time_local,
                  s.total_timer_s, s.avg_hr, s.max_hr, s.calories, s.workout_name,
                  s.body_weight_kg
           FROM sessions s WHERE s.activity_type = 'strength'
           ORDER BY s.local_date DESC""",
    )
    aggregati = {
        r["session_id"]: r
        for r in _rows(
            conn,
            """SELECT session_id,
                      SUM(CASE WHEN set_type='active' THEN 1 ELSE 0 END) AS n_serie,
                      SUM(CASE WHEN set_type='active' THEN COALESCE(duration_s,0) END) AS lavoro_s,
                      SUM(CASE WHEN set_type='rest'   THEN COALESCE(duration_s,0) END) AS riposo_s,
                      SUM(CASE WHEN set_type='active' THEN volume_kg END) AS volume_kg,
                      SUM(CASE WHEN set_type='active' AND volume_kg IS NULL THEN 1 ELSE 0 END)
                          AS serie_senza_volume
               FROM v_sets GROUP BY session_id""",
        )
    }
    for riga in base:
        agg = aggregati.get(riga["session_id"], {})
        riga.update(
            {
                "n_serie": agg.get("n_serie", 0),
                "lavoro_s": agg.get("lavoro_s"),
                "riposo_s": agg.get("riposo_s"),
                "volume_kg": agg.get("volume_kg"),
                "serie_senza_volume": agg.get("serie_senza_volume", 0),
            }
        )
        tempo = riga["total_timer_s"]
        riga["densita_kg_min"] = (
            round(riga["volume_kg"] / (tempo / 60), 1)
            if riga["volume_kg"] is not None and tempo
            else None
        )
        riposo = riga["riposo_s"]
        riga["rapporto_lavoro_riposo"] = (
            round(riga["lavoro_s"] / riposo, 2) if riga["lavoro_s"] and riposo else None
        )
        riga.update(deriva_fc(conn, riga["session_id"]))
    return base


def fc_per_serie(conn: sqlite3.Connection, session_id: int) -> list[dict[str, Any]]:
    """FC media di ogni serie attiva, in ordine di esecuzione."""
    return _rows(
        conn,
        """SELECT v.set_id, v.set_index, v.order_in_session, v.exercise_name,
                  h.avg_bpm, h.max_bpm, h.n_campioni
           FROM v_sets v
           LEFT JOIN v_set_hr h ON h.set_id = v.set_id
           WHERE v.session_id = ? AND v.set_type = 'active'
           ORDER BY v.order_in_session""",
        (session_id,),
    )


def deriva_fc(conn: sqlite3.Connection, session_id: int) -> dict[str, Any]:
    """Ultimo terzo meno primo terzo della FC media per serie.

    Proxy grezzo di accumulo di fatica. Serve almeno una serie per terzo:
    sotto le tre serie con FC la deriva non e' calcolabile e resta `None`.
    """
    valori = [r["avg_bpm"] for r in fc_per_serie(conn, session_id) if r["avg_bpm"] is not None]
    if len(valori) < 3:
        return {"fc_deriva_bpm": None, "fc_prime_serie": None, "fc_ultime_serie": None}
    terzo = max(1, len(valori) // 3)
    primo = sum(valori[:terzo]) / terzo
    ultimo = sum(valori[-terzo:]) / terzo
    return {
        "fc_deriva_bpm": round(ultimo - primo, 1),
        "fc_prime_serie": round(primo, 1),
        "fc_ultime_serie": round(ultimo, 1),
    }


# --------------------------------------------------------------------------
# anomalie
# --------------------------------------------------------------------------


def anomalie(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """Cose da guardare prima di fidarsi dei numeri."""
    non_mappati = _rows(
        conn,
        """SELECT raw_exercise_key AS raw_key, COALESCE(wkt_step_note,'') AS nota,
                  MAX(raw_exercise_label) AS etichetta, COUNT(*) AS n_serie
           FROM v_sets
           WHERE set_type='active' AND unmapped=1 AND raw_exercise_key IS NOT NULL
           GROUP BY raw_exercise_key, COALESCE(wkt_step_note,'')
           ORDER BY n_serie DESC""",
    )
    peso_zero = _rows(
        conn,
        """SELECT set_id, local_date, exercise_name, reps, duration_s
           FROM v_sets
           WHERE set_type='active' AND weight_mode='carico' AND weight_kg = 0
           ORDER BY local_date DESC, order_in_session""",
    )
    reps_sospette = _rows(
        conn,
        """SELECT set_id, local_date, exercise_name, reps, weight_kg, duration_s,
                  CASE WHEN reps = 0 THEN 'zero ripetizioni'
                       ELSE 'troppe ripetizioni' END AS motivo
           FROM v_sets
           WHERE set_type='active' AND (reps = 0 OR reps > 30)
           ORDER BY local_date DESC, order_in_session""",
    )
    durata_anomala = _rows(
        conn,
        """SELECT set_id, local_date, exercise_name, reps, duration_s
           FROM v_sets
           WHERE set_type='active' AND duration_s > 300
           ORDER BY duration_s DESC""",
    )
    sedute_senza_carico = _rows(
        conn,
        """SELECT session_uid, local_date, COUNT(*) AS n_serie
           FROM v_sets
           WHERE set_type='active'
           GROUP BY session_id
           HAVING SUM(CASE WHEN reps IS NOT NULL THEN 1 ELSE 0 END) = 0
           ORDER BY local_date DESC""",
    )
    return {
        "esercizi_non_mappati": non_mappati,
        "serie_peso_zero": peso_zero,
        "serie_reps_sospette": reps_sospette,
        "serie_durata_anomala": durata_anomala,
        "sedute_senza_ripetizioni": sedute_senza_carico,
    }


def dettaglio_seduta(conn: sqlite3.Connection, session_id: int) -> list[dict[str, Any]]:
    """Tutte le serie di una seduta, per la tabella espandibile."""
    return _rows(
        conn,
        """SELECT v.*, h.avg_bpm, h.max_bpm
           FROM v_sets v LEFT JOIN v_set_hr h ON h.set_id = v.set_id
           WHERE v.session_id = ?
           ORDER BY v.order_in_session""",
        (session_id,),
    )
