"""Test delle metriche.

Le sedute sono file .fit binari veri generati da `tests/fitgen.py`, ingeriti
in un database SQLite vero: le metriche girano sulla stessa strada che
percorrono i dati dell'orologio.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import fitgen
from strength_tracker import db
from strength_tracker import metrics as mt
from strength_tracker.cli import main
from strength_tracker.ingest import ingest_path
from strength_tracker.mapping import load_mapping

MAPPATURA = "config/exercise_mapping.yaml"

# categorie: 8/0 stacco (carico), 21/42 trazioni assistite (assistenza),
# 19/66 plank laterale (corpo libero)
STACCO = ((8, 8, 8), (0, 0, 0))
TRAZIONI = ((21, 21, 21), (42, 42, 42))
PLANK = ((19, 19, 19), (66, 66, 66))


def piano(*serie):
    return [(cat, sub, reps, peso, attivo, riposo) for cat, sub, reps, peso, attivo, riposo in serie]


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "m.db")
    yield c
    c.close()


def ingerisci(conn, tmp_path, sedute):
    """sedute: {nome: (datetime, piano)}"""
    cartella = tmp_path / "fit"
    for nome, (quando, plan) in sedute.items():
        fitgen.build_strength_fit(cartella / f"{nome}.fit", start=quando, plan=plan)
    ingest_path(conn, cartella)
    db.refresh_exercise_map(conn, load_mapping(MAPPATURA).as_rows())


# --- Epley ----------------------------------------------------------------


@pytest.mark.parametrize(
    "peso, reps, atteso",
    [
        (100.0, 1, 100.0),  # una ripetizione: il peso stesso
        (100.0, 10, 133.33),
        (60.0, 5, 70.0),
        (None, 5, None),
        (100.0, None, None),
        (100.0, 0, None),  # zero ripetizioni non e' una serie
        (0.0, 5, None),  # peso zero: e1RM non ha senso
    ],
)
def test_epley(peso, reps, atteso):
    r = mt.epley(peso, reps)
    assert r is None if atteso is None else r == pytest.approx(atteso, abs=0.01)


def test_epley_segnalato_inaffidabile_sopra_le_12_reps(conn, tmp_path):
    ingerisci(
        conn,
        tmp_path,
        {
            "a": (datetime(2026, 9, 3, 17, tzinfo=timezone.utc),
                  piano((*STACCO, 5, 100.0, 30.0, 90.0))),
            "b": (datetime(2026, 9, 10, 17, tzinfo=timezone.utc),
                  piano((*STACCO, 20, 40.0, 60.0, 90.0))),
        },
    )
    punti = {p["local_date"]: p for p in mt.progressione(conn, "Trap bar deadlift")["punti"]}
    assert punti["2026-09-03"]["e1rm_affidabile"] is True
    assert punti["2026-09-10"]["e1rm_affidabile"] is False


# --- volume ---------------------------------------------------------------


def test_volume_di_una_seduta(conn, tmp_path):
    ingerisci(
        conn,
        tmp_path,
        {"a": (datetime(2026, 9, 3, 17, tzinfo=timezone.utc),
               piano((*STACCO, 8, 60.0, 40.0, 90.0), (*STACCO, 5, 80.0, 30.0, 90.0)))},
    )
    assert mt.riepilogo(conn)["volume_totale_kg"] == pytest.approx(8 * 60 + 5 * 80)


def test_settimane_senza_allenamento_restano_nella_serie(conn, tmp_path):
    # 03/09 e 24/09: in mezzo due settimane vuote, che sono informazione.
    ingerisci(
        conn,
        tmp_path,
        {
            "a": (datetime(2026, 9, 3, 17, tzinfo=timezone.utc), piano((*STACCO, 8, 60.0, 40.0, 90.0))),
            "b": (datetime(2026, 9, 24, 17, tzinfo=timezone.utc), piano((*STACCO, 8, 70.0, 40.0, 90.0))),
        },
    )
    settimane = mt.volume_settimanale(conn)
    assert len(settimane) == 4
    assert [w["n_sedute"] for w in settimane] == [1, 0, 0, 1]
    assert settimane[1]["volume_kg"] is None  # non zero: non c'e' stato allenamento


def test_volume_nullo_se_nessuna_serie_ha_i_dati(conn, tmp_path):
    ingerisci(
        conn,
        tmp_path,
        {"a": (datetime(2026, 9, 3, 17, tzinfo=timezone.utc),
               piano((*PLANK, 12, None, 45.0, 60.0)))},
    )
    settimana = mt.volume_settimanale(conn)[0]
    assert settimana["n_sedute"] == 1
    assert settimana["volume_kg"] is None
    assert settimana["n_serie_con_volume"] == 0


def test_media_mobile_ignora_le_settimane_senza_dati(conn, tmp_path):
    ingerisci(
        conn,
        tmp_path,
        {
            "a": (datetime(2026, 9, 3, 17, tzinfo=timezone.utc), piano((*STACCO, 10, 100.0, 40.0, 90.0))),
            "b": (datetime(2026, 9, 24, 17, tzinfo=timezone.utc), piano((*STACCO, 10, 200.0, 40.0, 90.0))),
        },
    )
    settimane = mt.volume_settimanale(conn)
    # (1000 + 2000) / 2, non /4: le settimane vuote non valgono zero
    assert settimane[-1]["media_mobile_kg"] == pytest.approx(1500.0)


def test_serie_per_gruppo_muscolare(conn, tmp_path):
    ingerisci(
        conn,
        tmp_path,
        {"a": (datetime(2026, 9, 3, 17, tzinfo=timezone.utc),
               piano((*STACCO, 8, 60.0, 40.0, 90.0), (*STACCO, 8, 60.0, 40.0, 90.0),
                     (*PLANK, 12, None, 45.0, 60.0)))},
    )
    dati = mt.serie_per_gruppo(conn, focus=["adduttori"])
    assert dati["serie"]["catena_posteriore"] == [2]
    assert dati["focus"] == ["adduttori"]  # il plank con nota e' Copenhagen


# --- assistenza e corpo libero --------------------------------------------


def test_le_trazioni_assistite_non_entrano_nel_tonnellaggio(conn, tmp_path):
    ingerisci(
        conn,
        tmp_path,
        {"a": (datetime(2026, 9, 3, 17, tzinfo=timezone.utc),
               piano((*TRAZIONI, 5, 40.0, 30.0, 90.0)))},
    )
    r = conn.execute("SELECT * FROM v_sets WHERE set_type='active'").fetchone()
    assert r["exercise_name"] == "Trazioni assistite"
    assert r["weight_kg"] == 40.0  # il file dice 40, ed e' l'aiuto della macchina
    assert r["volume_kg"] is None, "40 kg di assistenza non sono 40 kg sollevati"
    # carico effettivo = peso corporeo meno l'assistenza
    assert r["carico_effettivo_kg"] == pytest.approx(71.1 - 40)
    assert r["carico_stimato"] == 1
    assert r["volume_stimato_kg"] == pytest.approx(5 * (71.1 - 40))
    assert mt.riepilogo(conn)["volume_totale_kg"] is None


def test_meno_assistenza_vuol_dire_piu_forza(conn, tmp_path):
    ingerisci(
        conn,
        tmp_path,
        {
            "a": (datetime(2026, 9, 3, 17, tzinfo=timezone.utc), piano((*TRAZIONI, 5, 40.0, 30.0, 90.0))),
            "b": (datetime(2026, 9, 10, 17, tzinfo=timezone.utc), piano((*TRAZIONI, 5, 30.0, 30.0, 90.0))),
        },
    )
    prog = mt.progressione(conn, "Trazioni assistite")
    assert prog["carico_stimato"] is True
    prima, dopo = prog["punti"]
    assert dopo["assistenza_minima_kg"] < prima["assistenza_minima_kg"]
    assert dopo["e1rm_kg"] > prima["e1rm_kg"], "meno aiuto = carico effettivo maggiore"


def test_corpo_libero_non_fa_tonnellaggio(conn, tmp_path):
    ingerisci(
        conn,
        tmp_path,
        {"a": (datetime(2026, 9, 3, 17, tzinfo=timezone.utc), piano((*PLANK, 12, None, 45.0, 60.0)))},
    )
    r = conn.execute("SELECT * FROM v_sets WHERE set_type='active'").fetchone()
    assert r["weight_mode"] == "corpo_libero"
    assert r["volume_kg"] is None
    assert r["carico_effettivo_kg"] == pytest.approx(71.1)


# --- densita', lavoro/riposo, deriva FC ------------------------------------


def test_densita_e_rapporto_lavoro_riposo(conn, tmp_path):
    ingerisci(
        conn,
        tmp_path,
        {"a": (datetime(2026, 9, 3, 17, tzinfo=timezone.utc),
               piano((*STACCO, 10, 60.0, 60.0, 60.0), (*STACCO, 10, 60.0, 60.0, 60.0)))},
    )
    s = mt.sedute(conn)[0]
    assert s["lavoro_s"] == pytest.approx(120.0)
    assert s["riposo_s"] == pytest.approx(120.0)
    assert s["rapporto_lavoro_riposo"] == pytest.approx(1.0)
    atteso = 1200 / (s["total_timer_s"] / 60)
    assert s["densita_kg_min"] == pytest.approx(atteso, abs=0.1)


def test_densita_nulla_senza_volume(conn, tmp_path):
    ingerisci(
        conn,
        tmp_path,
        {"a": (datetime(2026, 9, 3, 17, tzinfo=timezone.utc), piano((*PLANK, 12, None, 45.0, 60.0)))},
    )
    assert mt.sedute(conn)[0]["densita_kg_min"] is None


def test_fc_per_serie_e_deriva(conn, tmp_path):
    ingerisci(
        conn,
        tmp_path,
        {"a": (datetime(2026, 9, 3, 17, tzinfo=timezone.utc),
               piano(*[(*STACCO, 8, 60.0, 45.0, 60.0) for _ in range(6)]))},
    )
    seduta = mt.sedute(conn)[0]
    serie = mt.fc_per_serie(conn, seduta["session_id"])
    assert len(serie) == 6
    assert all(s["avg_bpm"] is not None and s["n_campioni"] > 0 for s in serie)
    # la fixture ha una FC che sale di 1 bpm al minuto: la deriva e' positiva
    assert seduta["fc_deriva_bpm"] > 0
    assert seduta["fc_ultime_serie"] > seduta["fc_prime_serie"]


def test_deriva_non_calcolabile_con_poche_serie(conn, tmp_path):
    ingerisci(
        conn,
        tmp_path,
        {"a": (datetime(2026, 9, 3, 17, tzinfo=timezone.utc),
               piano((*STACCO, 8, 60.0, 40.0, 90.0), (*STACCO, 8, 60.0, 40.0, 90.0)))},
    )
    assert mt.sedute(conn)[0]["fc_deriva_bpm"] is None


# --- anomalie -------------------------------------------------------------


def test_anomalie(conn, tmp_path):
    ingerisci(
        conn,
        tmp_path,
        {
            "a": (
                datetime(2026, 9, 3, 17, tzinfo=timezone.utc),
                piano(
                    (*STACCO, 8, 0.0, 40.0, 90.0),      # peso zero
                    (*STACCO, 0, 60.0, 40.0, 90.0),     # zero ripetizioni
                    (*STACCO, 50, 60.0, 40.0, 90.0),    # troppe ripetizioni
                    (*STACCO, 8, 60.0, 400.0, 90.0),    # durata anomala
                ),
            )
        },
    )
    a = mt.anomalie(conn)
    assert len(a["serie_peso_zero"]) == 1
    motivi = {r["motivo"] for r in a["serie_reps_sospette"]}
    assert motivi == {"zero ripetizioni", "troppe ripetizioni"}
    assert len(a["serie_durata_anomala"]) == 1
    assert a["esercizi_non_mappati"] == []


def test_seduta_senza_ripetizioni_segnalata(conn, tmp_path):
    ingerisci(
        conn,
        tmp_path,
        {"a": (datetime(2026, 9, 3, 17, tzinfo=timezone.utc), piano((*STACCO, None, None, 40.0, 90.0)))},
    )
    assert len(mt.anomalie(conn)["sedute_senza_ripetizioni"]) == 1


# --- CLI ------------------------------------------------------------------


def test_cli_stats(tmp_path, capsys):
    dbfile = tmp_path / "cli.db"
    cartella = tmp_path / "fit"
    fitgen.build_strength_fit(cartella / "a.fit", start=datetime(2026, 9, 3, 17, tzinfo=timezone.utc))
    main(["--db", str(dbfile), "ingest", str(cartella)])
    capsys.readouterr()
    assert main(["--db", str(dbfile), "stats"]) == 0
    out = capsys.readouterr().out
    for atteso in ["RIEPILOGO", "VOLUME SETTIMANALE", "SERIE PER GRUPPO", "ULTIME SEDUTE", "ANOMALIE"]:
        assert atteso in out
    assert "03/09/2026" in out  # date in formato italiano


def test_cli_stats_su_database_vuoto(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "vuoto.db"), "stats"]) == 1
    assert "Database vuoto" in capsys.readouterr().out


def test_cli_correct_da_csv(tmp_path, capsys):
    dbfile = tmp_path / "cli.db"
    cartella = tmp_path / "fit"
    fitgen.build_strength_fit(
        cartella / "a.fit",
        start=datetime(2026, 9, 3, 17, tzinfo=timezone.utc),
        plan=piano((*STACCO, 8, 60.0, 40.0, 90.0), (*STACCO, 8, 60.0, 40.0, 90.0)),
    )
    main(["--db", str(dbfile), "ingest", str(cartella)])
    csv = tmp_path / "correzioni.csv"
    csv.write_text(
        "# i commenti non devono rompere l'import\n"
        "data,serie,reps,peso_kg,nota\n"
        "03/09/2026,2,6,72.5,seconda serie\n",
        encoding="utf-8",
    )
    capsys.readouterr()
    assert main(["--db", str(dbfile), "correct", "--from-csv", str(csv)]) == 0
    assert "Correzioni applicate: 1" in capsys.readouterr().out

    conn = db.connect(dbfile)
    righe = conn.execute(
        "SELECT reps, weight_kg, reps_raw, weight_kg_raw FROM v_sets"
        " WHERE set_type='active' ORDER BY order_in_session"
    ).fetchall()
    assert (righe[1]["reps"], righe[1]["weight_kg"]) == (6, 72.5)
    assert (righe[1]["reps_raw"], righe[1]["weight_kg_raw"]) == (8, 60.0)  # grezzo intatto
    assert (righe[0]["reps"], righe[0]["weight_kg"]) == (8, 60.0)  # prima serie non toccata


def test_cli_correct_da_csv_giorno_ambiguo(tmp_path, capsys):
    dbfile = tmp_path / "cli.db"
    cartella = tmp_path / "fit"
    for ora, nome in ((9, "mattina"), (18, "sera")):
        fitgen.build_strength_fit(
            cartella / f"{nome}.fit", start=datetime(2026, 9, 3, ora, tzinfo=timezone.utc)
        )
    main(["--db", str(dbfile), "ingest", str(cartella)])
    csv = tmp_path / "c.csv"
    csv.write_text("data,serie,reps,peso_kg\n03/09/2026,1,6,72.5\n", encoding="utf-8")
    capsys.readouterr()
    assert main(["--db", str(dbfile), "correct", "--from-csv", str(csv)]) == 1
    assert "serve la colonna 'seduta'" in capsys.readouterr().err


def test_cli_correct_da_csv_senza_colonna_data(tmp_path, capsys):
    csv = tmp_path / "c.csv"
    csv.write_text("giorno,serie\n03/09/2026,1\n", encoding="utf-8")
    assert main(["--db", str(tmp_path / "x.db"), "correct", "--from-csv", str(csv)]) == 2
    assert "manca la colonna 'data'" in capsys.readouterr().err


def test_esempio_di_correzioni_del_repo_e_leggibile():
    percorso = Path("examples/correzioni_day1_20260901.csv")
    assert percorso.is_file()
    righe = [r for r in percorso.read_text(encoding="utf-8").splitlines() if not r.startswith("#")]
    assert righe[0].startswith("data,seduta,serie,reps,peso_kg")
    assert len(righe) == 24  # intestazione + 23 serie a carico
