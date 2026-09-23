"""Test dell'import degli export CSV di Hevy."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest

import fitgen
from strength_tracker import db
from strength_tracker import hevy
from strength_tracker import metrics as mt
from strength_tracker.cli import main
from strength_tracker.fit_parser import FitSkipped
from strength_tracker.ingest import ingest_path
from strength_tracker.mapping import load_mapping

MAPPATURA = "config/exercise_mapping.yaml"
INTESTAZIONE = [
    "title", "start_time", "end_time", "description", "exercise_title", "superset_id",
    "exercise_notes", "set_index", "set_type", "weight_kg", "reps", "distance_km",
    "duration_seconds", "rpe",
]


def riga(titolo, inizio, fine, esercizio, set_index=0, **extra):
    base = dict.fromkeys(INTESTAZIONE, "")
    base.update(
        title=titolo, start_time=inizio, end_time=fine, exercise_title=esercizio,
        set_index=set_index, set_type="normal",
    )
    base.update({k: v for k, v in extra.items()})
    return base


def scrivi_csv(path: Path, righe: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=INTESTAZIONE)
        w.writeheader()
        w.writerows(righe)
    return path


INIZIO = "Sep 21, 2026 at 6:25 AM"
FINE = "Sep 21, 2026 at 7:39 AM"


@pytest.fixture
def export(tmp_path):
    """Un export con una seduta realistica: carichi, corpo libero e un plank."""
    return scrivi_csv(
        tmp_path / "workouts.csv",
        [
            riga("Day 1 Upper Body", INIZIO, FINE, "Bench Press (Dumbbell)", 0, weight_kg="24", reps="10"),
            riga("Day 1 Upper Body", INIZIO, FINE, "Bench Press (Dumbbell)", 1, weight_kg="32", reps="10"),
            riga("Day 1 Upper Body", INIZIO, FINE, "Chin Up", 0, reps="5"),
            riga("Day 1 Upper Body", INIZIO, FINE, "Side Plank", 0, duration_seconds="23"),
            riga("Day 1 Upper Body", INIZIO, FINE, "Side Plank", 0, duration_seconds="22"),
        ],
    )


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "h.db")
    yield c
    c.close()


# --- riconoscimento del formato -------------------------------------------


def test_riconosce_un_export_hevy(export):
    assert hevy.e_un_csv_hevy(export)


def test_non_confonde_un_csv_di_correzioni(tmp_path):
    csv_correzioni = tmp_path / "correzioni.csv"
    csv_correzioni.write_text("data,serie,reps,peso_kg\n01/09/2026,7,10,24\n", encoding="utf-8")
    assert not hevy.e_un_csv_hevy(csv_correzioni)
    with pytest.raises(FitSkipped, match="non e' un export Hevy"):
        hevy.parse_csv(csv_correzioni)


def test_csv_vuoto(tmp_path):
    vuoto = scrivi_csv(tmp_path / "v.csv", [])
    with pytest.raises(FitSkipped, match="vuoto"):
        hevy.parse_csv(vuoto)


@pytest.mark.parametrize(
    "testo, atteso",
    [
        ("Sep 21, 2026 at 6:25 AM", datetime(2026, 9, 21, 6, 25)),
        ("Sep 21, 2026 at 6:25 PM", datetime(2026, 9, 21, 18, 25)),
        ("21 Sep 2026, 06:25", datetime(2026, 9, 21, 6, 25)),
        ("2026-09-21 06:25:00", datetime(2026, 9, 21, 6, 25)),
    ],
)
def test_formati_data(testo, atteso):
    assert hevy.parse_data(testo) == atteso


def test_data_incomprensibile():
    with pytest.raises(ValueError, match="non riconosciuta"):
        hevy.parse_data("domani mattina")


# --- struttura della seduta -----------------------------------------------


def test_una_seduta_per_allenamento(export):
    attivita = hevy.parse_csv(export)
    assert len(attivita) == 1
    a = attivita[0]
    assert a.session.workout_name == "Day 1 Upper Body"
    assert a.session.source == "hevy"
    assert a.session.start_time == datetime(2026, 9, 21, 6, 25)
    assert a.session.total_elapsed_s == pytest.approx(74 * 60)


def test_piu_allenamenti_nello_stesso_file(tmp_path):
    export = scrivi_csv(
        tmp_path / "w.csv",
        [
            riga("Day 1", INIZIO, FINE, "Bench Press (Dumbbell)", 0, weight_kg="24", reps="10"),
            riga("Day 2", "Sep 23, 2026 at 6:00 AM", "Sep 23, 2026 at 7:00 AM",
                 "Trap Bar Deadlift", 0, weight_kg="60", reps="8"),
        ],
    )
    attivita = hevy.parse_csv(export)
    assert [a.session.workout_name for a in attivita] == ["Day 1", "Day 2"]
    assert len({a.session_uid for a in attivita}) == 2


def test_indice_della_serie_e_l_ordine_nella_seduta(export):
    """`set_index` di Hevy riparte a ogni esercizio: non puo' essere la chiave."""
    a = hevy.parse_csv(export)[0]
    assert [s.index for s in a.sets] == [0, 1, 2, 3, 4]
    # nel file le due Side Plank hanno entrambe set_index 0
    planks = [s for s in a.sets if s.exercise_label == "Side Plank"]
    assert len(planks) == 2 and planks[0].index != planks[1].index


def test_chiave_grezza_col_prefisso(export):
    a = hevy.parse_csv(export)[0]
    assert a.sets[0].exercise_key == "hevy:Bench Press (Dumbbell)"
    assert a.sets[0].exercise_label == "Bench Press (Dumbbell)"


def test_carichi_e_volume(export):
    a = hevy.parse_csv(export)[0]
    assert (a.sets[0].repetitions, a.sets[0].weight_kg) == (10, 24.0)
    assert a.sets[0].volume_kg == pytest.approx(240.0)


def test_corpo_libero_senza_peso(export):
    a = hevy.parse_csv(export)[0]
    chin = next(s for s in a.sets if s.exercise_label == "Chin Up")
    assert chin.repetitions == 5
    assert chin.weight_kg is None and chin.volume_kg is None


def test_esercizio_a_tempo(export):
    a = hevy.parse_csv(export)[0]
    plank = next(s for s in a.sets if s.exercise_label == "Side Plank")
    assert plank.duration_s == pytest.approx(23.0)
    assert plank.repetitions is None  # un plank non ha ripetizioni


def test_cosa_hevy_non_da(export):
    a = hevy.parse_csv(export)[0]
    assert a.hr_samples == []            # nessuna frequenza cardiaca
    assert a.session.total_timer_s is None  # nessun tempo attivo
    assert a.session.utc_offset_s is None   # nessun fuso orario
    assert all(s.start_time is None for s in a.sets)  # nessun orario per serie


def test_uid_stabile_fra_letture(export):
    primo = hevy.parse_csv(export)[0].session_uid
    assert hevy.parse_csv(export)[0].session_uid == primo
    assert primo.startswith("hevy:20260921T0625")


def test_riga_senza_esercizio_segnalata(tmp_path):
    export = scrivi_csv(
        tmp_path / "w.csv",
        [
            riga("Day 1", INIZIO, FINE, "", 0, reps="10"),
            riga("Day 1", INIZIO, FINE, "Chin Up", 0, reps="5"),
        ],
    )
    a = hevy.parse_csv(export)[0]
    assert len(a.sets) == 1
    assert a.warnings and "senza nome" in a.warnings[0]


# --- ingestione ------------------------------------------------------------


def test_ingestione_di_un_export(conn, export):
    rep = ingest_path(conn, export.parent)
    assert len(rep.ingested) == 1 and rep.sets_written == 5
    assert conn.execute("SELECT source FROM sessions").fetchone()["source"] == "hevy"


def test_ingestione_idempotente(conn, export):
    ingest_path(conn, export.parent)
    rep = ingest_path(conn, export.parent)
    assert rep.ingested == [] and len(rep.already_present) == 1
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 1


def test_export_che_cresce_aggiunge_solo_il_nuovo(conn, tmp_path):
    """Hevy esporta tutto lo storico: il secondo export contiene il primo."""
    righe = [riga("Day 1", INIZIO, FINE, "Chin Up", 0, reps="5")]
    export = scrivi_csv(tmp_path / "fit" / "w.csv", righe)
    assert len(ingest_path(conn, export.parent).ingested) == 1

    righe.append(
        riga("Day 2", "Sep 23, 2026 at 6:00 AM", "Sep 23, 2026 at 7:00 AM",
             "Trap Bar Deadlift", 0, weight_kg="60", reps="8")
    )
    scrivi_csv(export, righe)
    rep = ingest_path(conn, export.parent)
    assert len(rep.ingested) == 1          # solo il nuovo allenamento
    assert len(rep.already_present) == 1   # il primo era gia' dentro
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 2


def test_fit_e_csv_nella_stessa_cartella(conn, tmp_path):
    cartella = tmp_path / "fit"
    fitgen.build_strength_fit(cartella / "a.fit", start=datetime(2026, 9, 3, 17, tzinfo=timezone.utc))
    scrivi_csv(cartella / "w.csv", [riga("Day 1", INIZIO, FINE, "Chin Up", 0, reps="5")])
    rep = ingest_path(conn, cartella)
    assert len(rep.ingested) == 2
    sorgenti = {r["source"] for r in conn.execute("SELECT source FROM sessions")}
    assert sorgenti == {"garmin", "hevy"}


def test_un_csv_non_hevy_viene_saltato_senza_fermare_il_batch(conn, tmp_path, export):
    (export.parent / "correzioni.csv").write_text("data,serie\n01/09/2026,7\n", encoding="utf-8")
    rep = ingest_path(conn, export.parent)
    assert len(rep.ingested) == 1
    assert len(rep.skipped) == 1 and "Hevy" in rep.skipped[0][1]


# --- mappatura e metriche --------------------------------------------------


def test_stessa_voce_per_garmin_e_hevy(conn, tmp_path):
    """E' questo che rende continua la storia di un esercizio."""
    m = load_mapping(MAPPATURA)
    garmin = m.by_raw_key["bench_press/dumbbell_bench_press"]
    hevy_key = m.by_raw_key["hevy:Bench Press (Dumbbell)"]
    assert garmin.name == hevy_key.name == "Dumbbell bench press"


def test_metriche_dichiarano_cosa_manca(conn, export):
    ingest_path(conn, export.parent)
    db.refresh_exercise_map(conn, load_mapping(MAPPATURA).as_rows())
    s = mt.sedute(conn)[0]
    assert s["source"] == "hevy"
    assert s["densita_base"] == "tempo totale"  # non c'e' il tempo attivo
    assert s["rapporto_lavoro_riposo"] is None  # Hevy non registra le pause
    assert s["fc_deriva_bpm"] is None           # niente frequenza cardiaca
    assert s["volume_kg"] == pytest.approx(240 + 320)


def test_riepilogo_conta_le_sorgenti(conn, tmp_path, export):
    fitgen.build_strength_fit(export.parent / "a.fit", start=datetime(2026, 9, 3, 17, tzinfo=timezone.utc))
    ingest_path(conn, export.parent)
    assert mt.riepilogo(conn)["sedute_per_sorgente"] == {"garmin": 1, "hevy": 1}


def test_seduta_registrata_due_volte_viene_segnalata(conn, tmp_path):
    """Stessa seduta su orologio e su Hevy: il volume sarebbe contato due volte."""
    cartella = tmp_path / "fit"
    fitgen.build_strength_fit(cartella / "a.fit", start=datetime(2026, 9, 3, 15, 30, tzinfo=timezone.utc))
    scrivi_csv(
        cartella / "w.csv",
        [riga("Day 1", "Sep 3, 2026 at 5:35 PM", "Sep 3, 2026 at 6:30 PM",
              "Chin Up", 0, reps="5")],
    )
    ingest_path(conn, cartella)
    doppie = mt.sedute_doppie(conn)
    assert len(doppie) == 1
    assert {"garmin", "hevy"} <= {d["seduta_a"].split(":")[0] for d in doppie} | {
        d["seduta_b"].split(":")[0] for d in doppie
    }
    assert mt.anomalie(conn)["sedute_doppie"] == doppie


def test_sedute_in_giorni_diversi_non_sono_doppie(conn, tmp_path, export):
    fitgen.build_strength_fit(export.parent / "a.fit", start=datetime(2026, 9, 3, 17, tzinfo=timezone.utc))
    ingest_path(conn, export.parent)
    assert mt.sedute_doppie(conn) == []


# --- CLI -------------------------------------------------------------------


def test_cli_ingest_e_stats_su_hevy(tmp_path, export, capsys):
    dbfile = tmp_path / "cli.db"
    assert main(["--db", str(dbfile), "ingest", str(export)]) == 0
    capsys.readouterr()
    assert main(["--db", str(dbfile), "stats"]) == 0
    out = capsys.readouterr().out
    assert "1 hevy" in out
    assert "21/09/2026" in out
