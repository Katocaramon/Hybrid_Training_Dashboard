"""Test dello schema, dell'ingestione idempotente e delle correzioni.

Girano su un database SQLite vero e su file .fit binari veri.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import fitgen
from strength_tracker import db
from strength_tracker.cli import main
from strength_tracker.ingest import ingest_path
from strength_tracker.mapping import load_mapping

FIXTURES = Path(__file__).parent / "fixtures"
STRENGTH = FIXTURES / "strength_session.fit"
RUNNING = FIXTURES / "running_session.fit"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def cartella(tmp_path):
    """Cartella con un FIT di forza dentro (copia: mai il sorgente originale)."""
    d = tmp_path / "fit"
    d.mkdir()
    (d / "seduta.fit").write_bytes(STRENGTH.read_bytes())
    return d


# --- schema ---------------------------------------------------------------


def test_schema_creato_e_migrazione_idempotente(conn):
    tabelle = {
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"sessions", "sets", "hr_samples", "corrections", "exercise_map"} <= tabelle
    viste = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    assert {"v_sets", "v_corrections_effective"} <= viste
    assert db.migrate(conn) == db.SCHEMA_VERSION  # richiamabile a ogni avvio
    assert conn.execute("SELECT COUNT(*) c FROM schema_migrations").fetchone()["c"] == 1


def test_cancellare_una_seduta_pulisce_serie_e_fc(conn, cartella):
    ingest_path(conn, cartella)
    sid = conn.execute("SELECT id FROM sessions").fetchone()["id"]
    conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) c FROM sets").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM hr_samples").fetchone()["c"] == 0


def test_settimana_iso_e_data_locale(conn, cartella):
    ingest_path(conn, cartella)
    r = conn.execute("SELECT * FROM sessions").fetchone()
    # 03/09/2026 17:30 UTC = 19:30 locali (+02:00) -> giovedi', settimana 36
    assert r["local_date"] == "2026-09-03"
    assert (r["iso_year"], r["iso_week"]) == (2026, 36)
    assert r["utc_offset_s"] == 7200


def test_seduta_serale_datata_nel_giorno_locale(conn, tmp_path):
    # 22:30 locali del 3 settembre = 20:30 UTC: la data deve restare il 3.
    d = tmp_path / "fit"
    fitgen.build_strength_fit(
        d / "sera.fit", start=datetime(2026, 9, 3, 20, 30, tzinfo=timezone.utc)
    )
    ingest_path(conn, d)
    r = conn.execute("SELECT local_date, start_time_local FROM sessions").fetchone()
    assert r["local_date"] == "2026-09-03"
    assert r["start_time_local"].startswith("2026-09-03T22:30")


# --- idempotenza ----------------------------------------------------------


def test_ingestione_di_base(conn, cartella):
    rep = ingest_path(conn, cartella)
    assert rep.scanned == 1 and len(rep.ingested) == 1
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM sets").fetchone()["c"] == 14
    assert conn.execute("SELECT COUNT(*) c FROM hr_samples").fetchone()["c"] == 988


def test_rilanciare_ingest_non_duplica_e_non_rilegge(conn, cartella):
    ingest_path(conn, cartella)
    rep = ingest_path(conn, cartella)
    assert len(rep.already_present) == 1
    assert rep.ingested == [] and rep.sets_written == 0
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM sets").fetchone()["c"] == 14


def test_force_riscrive_senza_duplicare(conn, cartella):
    ingest_path(conn, cartella)
    rep = ingest_path(conn, cartella, force=True)
    assert len(rep.updated) == 1 and rep.ingested == []
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM sets").fetchone()["c"] == 14
    assert conn.execute("SELECT COUNT(*) c FROM hr_samples").fetchone()["c"] == 988


def test_stesso_allenamento_con_nome_diverso_non_entra_due_volte(conn, cartella):
    ingest_path(conn, cartella)
    (cartella / "copia_rinominata.fit").write_bytes(STRENGTH.read_bytes())
    rep = ingest_path(conn, cartella)
    assert len(rep.already_present) == 2 and rep.ingested == []
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 1


def test_sedute_diverse_convivono(conn, tmp_path):
    d = tmp_path / "fit"
    fitgen.build_strength_fit(d / "a.fit", start=datetime(2026, 9, 3, 17, 30, tzinfo=timezone.utc))
    fitgen.build_strength_fit(d / "b.fit", start=datetime(2026, 9, 5, 17, 30, tzinfo=timezone.utc))
    rep = ingest_path(conn, d)
    assert len(rep.ingested) == 2
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 2


def test_session_uid_unico_a_livello_di_schema(conn, cartella):
    ingest_path(conn, cartella)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO sessions (session_uid, start_time_utc, source_path,
                                     source_sha256, ingested_at)
               SELECT session_uid, start_time_utc, source_path, source_sha256, ingested_at
               FROM sessions"""
        )


def test_ingestione_ricorsiva_di_una_cartella(conn, tmp_path):
    d = tmp_path / "fit"
    (d / "2026" / "09").mkdir(parents=True)
    fitgen.build_strength_fit(
        d / "2026" / "09" / "a.fit", start=datetime(2026, 9, 3, 17, 30, tzinfo=timezone.utc)
    )
    assert len(ingest_path(conn, d).ingested) == 1


# --- tolleranza -----------------------------------------------------------


def test_un_file_rotto_non_ferma_il_batch(conn, cartella):
    fitgen.build_truncated_fit(cartella / "rotto.fit")
    (cartella / "corsa.fit").write_bytes(RUNNING.read_bytes())
    rep = ingest_path(conn, cartella)
    assert len(rep.ingested) == 1
    assert len(rep.skipped) == 2
    assert all(motivo for _, motivo in rep.skipped)
    righe = {
        r["path"]: r["reason"]
        for r in conn.execute("SELECT path, reason FROM ingested_files WHERE status='skipped'")
    }
    assert len(righe) == 2  # il motivo resta scritto nel database


def test_file_sorgente_mai_modificato(conn, cartella):
    f = cartella / "seduta.fit"
    prima = (f.read_bytes(), f.stat().st_mtime_ns)
    ingest_path(conn, cartella)
    ingest_path(conn, cartella, force=True)
    assert (f.read_bytes(), f.stat().st_mtime_ns) == prima


# --- mappatura e non mappati ----------------------------------------------


def test_mappatura_applicata_in_lettura(conn, cartella):
    ingest_path(conn, cartella)
    db.refresh_exercise_map(
        conn,
        [
            {
                "raw_key": "pull_up/band_assisted_pull_up",
                "exercise_name": "Band-assisted pull-up",
                "primary_group": "dorsali",
                "secondary_groups": ["bicipiti"],
            }
        ],
    )
    r = conn.execute(
        "SELECT * FROM v_sets WHERE raw_exercise_key = 'pull_up/band_assisted_pull_up'"
    ).fetchone()
    assert r["exercise_name"] == "Band-assisted pull-up"
    assert r["muscle_group"] == "dorsali" and r["unmapped"] == 0


def test_esercizio_fuori_catalogo_resta_unmapped(conn, cartella):
    ingest_path(conn, cartella)
    db.refresh_exercise_map(conn, load_mapping("config/exercise_mapping.yaml").as_rows())
    chiavi = {r["raw_key"] for r in db.unmapped_raw_keys(conn)}
    assert "250/7" in chiavi


def test_modificare_il_yaml_non_richiede_re_ingestione(conn, cartella):
    ingest_path(conn, cartella)
    prima = {r["raw_key"] for r in db.unmapped_raw_keys(conn)}
    db.refresh_exercise_map(
        conn,
        [{"raw_key": "250/7", "exercise_name": "Copenhagen plank", "primary_group": "adduttori"}],
    )
    dopo = {r["raw_key"] for r in db.unmapped_raw_keys(conn)}
    assert "250/7" in prima and "250/7" not in dopo
    r = conn.execute("SELECT * FROM v_sets WHERE raw_exercise_key='250/7'").fetchone()
    assert r["exercise_name"] == "Copenhagen plank" and r["muscle_group"] == "adduttori"


def test_unmapped_conta_solo_le_serie_attive(conn, cartella):
    ingest_path(conn, cartella)
    db.refresh_exercise_map(conn, [])
    totale = sum(r["n_sets"] for r in db.unmapped_raw_keys(conn))
    assert totale == 7  # le 7 pause non hanno esercizio e non compaiono


# --- volume e dati mancanti ------------------------------------------------


def test_volume_nullo_quando_manca_un_dato(conn, cartella):
    ingest_path(conn, cartella)
    r = conn.execute(
        "SELECT * FROM v_sets WHERE raw_exercise_key='pull_up/band_assisted_pull_up'"
    ).fetchone()
    assert r["reps"] == 6 and r["weight_kg"] is None
    assert r["volume_kg"] is None  # niente stime silenziose


def test_volume_calcolato_quando_i_dati_ci_sono(conn, cartella):
    ingest_path(conn, cartella)
    r = conn.execute(
        "SELECT * FROM v_sets WHERE raw_exercise_key='deadlift/barbell_deadlift' ORDER BY set_index"
    ).fetchone()
    assert r["volume_kg"] == pytest.approx(480.0)


def test_valori_pianificati_non_finiscono_nel_volume(conn, cartella):
    ingest_path(conn, cartella)
    r = conn.execute("SELECT * FROM v_sets WHERE set_index = 0").fetchone()
    assert r["planned_reps"] == 8 and r["planned_weight_kg"] == pytest.approx(60.0)
    assert r["reps"] == 8  # eseguito, non pianificato
    conn.execute("UPDATE sets SET reps = NULL WHERE set_index = 0")
    r = conn.execute("SELECT * FROM v_sets WHERE set_index = 0").fetchone()
    assert r["reps"] is None and r["volume_kg"] is None


# --- correzioni -----------------------------------------------------------


def test_correzione_non_tocca_i_dati_grezzi(conn, cartella):
    ingest_path(conn, cartella)
    sid = conn.execute("SELECT id FROM sets WHERE set_index = 0").fetchone()["id"]
    db.add_correction(conn, sid, reps=10, weight_kg=62.5, note="mi ero sbagliato")
    r = conn.execute("SELECT * FROM v_sets WHERE set_id = ?", (sid,)).fetchone()
    assert (r["reps"], r["weight_kg"]) == (10, 62.5)
    assert (r["reps_raw"], r["weight_kg_raw"]) == (8, 60.0)
    assert r["data_source"] == "correzione"
    assert r["volume_kg"] == pytest.approx(625.0)
    grezzo = conn.execute("SELECT reps, weight_kg FROM sets WHERE id = ?", (sid,)).fetchone()
    assert (grezzo["reps"], grezzo["weight_kg"]) == (8, 60.0)


def test_vale_l_ultima_correzione(conn, cartella):
    ingest_path(conn, cartella)
    sid = conn.execute("SELECT id FROM sets WHERE set_index = 0").fetchone()["id"]
    db.add_correction(conn, sid, reps=9)
    db.add_correction(conn, sid, reps=11)
    r = conn.execute("SELECT reps FROM v_sets WHERE set_id = ?", (sid,)).fetchone()
    assert r["reps"] == 11
    # lo storico resta: le correzioni sono append-only
    assert conn.execute("SELECT COUNT(*) c FROM corrections").fetchone()["c"] == 2


def test_le_correzioni_sopravvivono_a_ingest_force(conn, cartella):
    ingest_path(conn, cartella)
    sid = conn.execute("SELECT id FROM sets WHERE set_index = 0").fetchone()["id"]
    db.add_correction(conn, sid, reps=10, weight_kg=62.5)
    ingest_path(conn, cartella, force=True)
    nuovo = conn.execute("SELECT id FROM sets WHERE set_index = 0").fetchone()["id"]
    r = conn.execute("SELECT * FROM v_sets WHERE set_id = ?", (nuovo,)).fetchone()
    assert (r["reps"], r["weight_kg"]) == (10, 62.5), "la correzione non deve andare persa"


def test_correzione_puo_riassegnare_l_esercizio(conn, cartella):
    ingest_path(conn, cartella)
    db.refresh_exercise_map(
        conn,
        [{"raw_key": "hip_stability/standing_adduction", "exercise_name": "Copenhagen plank",
          "primary_group": "adduttori", "secondary_groups": []}],
    )
    sid = conn.execute("SELECT id FROM sets WHERE raw_exercise_key = '250/7'").fetchone()["id"]
    db.add_correction(conn, sid, exercise_key="hip_stability/standing_adduction")
    r = conn.execute("SELECT * FROM v_sets WHERE set_id = ?", (sid,)).fetchone()
    assert r["exercise_name"] == "Copenhagen plank" and r["unmapped"] == 0


def test_correzione_su_serie_inesistente(conn):
    with pytest.raises(KeyError):
        db.add_correction(conn, 999, reps=5)


def test_correzione_vuota_rifiutata(conn, cartella):
    ingest_path(conn, cartella)
    sid = conn.execute("SELECT id FROM sets LIMIT 1").fetchone()["id"]
    with pytest.raises(ValueError):
        db.add_correction(conn, sid)


# --- CLI ------------------------------------------------------------------


def test_cli_ingest_unmapped_correct(tmp_path, cartella, capsys, monkeypatch):
    dbfile = tmp_path / "cli.db"
    assert main(["--db", str(dbfile), "ingest", str(cartella)]) == 0
    assert "Sedute nuove:        1" in capsys.readouterr().out

    assert main(["--db", str(dbfile), "unmapped"]) == 0
    out = capsys.readouterr().out
    assert "250/7" in out  # l'esercizio fuori catalogo va completato a mano

    assert main(["--db", str(dbfile), "unmapped", "--yaml"]) == 0
    assert "match:" in capsys.readouterr().out

    conn = db.connect(dbfile)
    sid = conn.execute("SELECT id FROM sets WHERE set_index = 0").fetchone()["id"]
    assert main(["--db", str(dbfile), "correct", str(sid), "--reps", "10"]) == 0
    assert "Correzione" in capsys.readouterr().out
    assert conn.execute("SELECT reps FROM v_sets WHERE set_id=?", (sid,)).fetchone()["reps"] == 10


def test_cli_ingest_percorso_inesistente(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "x.db"), "ingest", str(tmp_path / "assente")]) == 2
    assert "non trovato" in capsys.readouterr().err


def test_cli_ingest_cartella_senza_fit(tmp_path, capsys):
    vuota = tmp_path / "vuota"
    vuota.mkdir()
    assert main(["--db", str(tmp_path / "x.db"), "ingest", str(vuota)]) == 1
    assert "nessun file .fit" in capsys.readouterr().err


def test_cli_correct_su_serie_inesistente(tmp_path, cartella, capsys):
    dbfile = tmp_path / "cli.db"
    main(["--db", str(dbfile), "ingest", str(cartella)])
    capsys.readouterr()
    assert main(["--db", str(dbfile), "correct", "9999", "--reps", "5"]) == 2
    assert "nessuna serie" in capsys.readouterr().err


def test_cli_mappatura_non_valida(tmp_path, cartella, capsys):
    cattiva = tmp_path / "map.yaml"
    cattiva.write_text("exercises: [ non chiuso")
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(tmp_path / "x.db"), "ingest", str(cartella), "--mapping", str(cattiva)])
    assert exc.value.code == 2
    assert "mappatura non valida" in capsys.readouterr().err
