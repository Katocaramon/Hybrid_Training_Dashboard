"""Test del parser FIT.

Girano su file .fit binari veri, generati da `tests/fitgen.py` e letti da
fitdecode esattamente come i file dell'orologio: niente mock.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from strength_tracker import fit_parser as fp
from strength_tracker.cli import main

import fitgen

FIXTURES = Path(__file__).parent / "fixtures"
STRENGTH = FIXTURES / "strength_session.fit"
RUNNING = FIXTURES / "running_session.fit"


@pytest.fixture(scope="module")
def activity() -> fp.ParsedActivity:
    return fp.parse_file(STRENGTH)


# --- struttura di base -----------------------------------------------------


def test_le_fixture_sono_versionate():
    assert STRENGTH.is_file() and RUNNING.is_file(), "rigenera con: python tests/fitgen.py"


def test_fixture_riproducibile(tmp_path):
    # Il generatore e' deterministico: la fixture nel repo e' esattamente
    # quella che produce il codice, senza sorprese ai prossimi rebuild.
    rigenerata = fitgen.build_strength_fit(tmp_path / "s.fit")
    assert rigenerata.read_bytes() == STRENGTH.read_bytes()


def test_conta_serie_e_pause(activity):
    assert len(activity.sets) == 14
    assert len(activity.active_sets) == 7
    assert sum(1 for s in activity.sets if s.set_type == "rest") == 7


def test_sessione(activity):
    s = activity.session
    assert s.sub_sport == "strength_training"
    assert s.sport_profile_name == "Pesi"
    assert s.calories == 310
    assert s.avg_hr and s.max_hr and s.avg_hr <= s.max_hr
    assert s.total_timer_s == pytest.approx(987.5)


def test_ora_locale_usa_offset_del_dispositivo(activity):
    s = activity.session
    assert s.utc_offset_s == 7200
    assert s.start_time == datetime(2026, 9, 3, 17, 30, tzinfo=timezone.utc)
    assert s.start_time_local.hour == 19  # 17:30 UTC -> 19:30 locali
    assert s.start_time_local.utcoffset() == timedelta(hours=2)


def test_serie_con_reps_e_peso(activity):
    primo = activity.active_sets[0]
    assert primo.repetitions == 8
    assert primo.weight_kg == pytest.approx(60.0)
    assert primo.volume_kg == pytest.approx(480.0)
    assert primo.duration_s == pytest.approx(42.0)
    assert primo.end_time == primo.start_time + timedelta(seconds=42)


def test_serie_a_corpo_libero_ha_volume_nullo_non_zero(activity):
    # pull-up assistito: reps presenti, peso assente -> il volume non si stima
    pull = next(s for s in activity.active_sets if s.exercise_key.startswith("pull_up/"))
    assert pull.repetitions == 6
    assert pull.weight_kg is None
    assert pull.volume_kg is None


def test_peso_zero_resta_zero(activity):
    plank = next(s for s in activity.active_sets if s.exercise_key.startswith("plank/"))
    assert plank.weight_kg == 0.0
    assert plank.volume_kg == 0.0  # zero esplicito, diverso da "non misurato"


# --- catalogo esercizi -----------------------------------------------------


def test_slug_risolti_dal_catalogo_fit(activity):
    chiavi = {s.exercise_key for s in activity.active_sets}
    assert "deadlift/barbell_deadlift" in chiavi
    assert "pull_up/band_assisted_pull_up" in chiavi


def test_esercizio_fuori_catalogo_non_fa_fallire_il_parsing(activity):
    fuori = [s for s in activity.active_sets if s.exercise_key == "250/7"]
    assert len(fuori) == 1, "categoria sconosciuta: si tiene il numero grezzo"
    assert fuori[0].exercise_label is None


def test_etichetta_dal_messaggio_exercise_title(activity):
    primo = activity.active_sets[0]
    assert primo.exercise_label == "Deadlift"
    assert len(activity.exercise_titles) == 4


@pytest.mark.parametrize(
    "cat, name, atteso",
    [
        (21, 42, "pull_up/band_assisted_pull_up"),
        (8, 0, "deadlift/barbell_deadlift"),
        (21, 999, "pull_up/999"),  # indice fuori catalogo
        (250, 7, "250/7"),  # categoria fuori catalogo
        (2, None, "cardio"),  # categoria senza sotto-nome
        (None, 5, None),
    ],
)
def test_exercise_key(cat, name, atteso):
    assert fp.exercise_key(cat, name) == atteso


def test_valori_pianificati_separati_da_quelli_eseguiti(activity):
    primo = activity.active_sets[0]
    assert (primo.planned_reps, primo.planned_weight_kg) == (8, 60.0)
    terzo = activity.active_sets[2]
    assert terzo.repetitions == 6 and terzo.planned_reps == 8  # pianificato != eseguito


# --- frequenza cardiaca ----------------------------------------------------


def test_serie_temporale_fc(activity):
    assert len(activity.hr_samples) == 988
    assert all(50 < s.bpm < 200 for s in activity.hr_samples)
    delta = {
        (b.timestamp - a.timestamp).total_seconds()
        for a, b in zip(activity.hr_samples, activity.hr_samples[1:])
    }
    assert delta == {1.0}


# --- identita' e idempotenza ----------------------------------------------


def test_session_uid_stabile_fra_letture(activity):
    assert fp.parse_file(STRENGTH).session_uid == activity.session_uid


def test_session_uid_da_nome_file_garmin(tmp_path):
    copia = tmp_path / "24192192073_ACTIVITY.fit"
    copia.write_bytes(STRENGTH.read_bytes())
    assert fp.parse_file(copia).session_uid == "garmin:24192192073"


def test_session_uid_ripiega_su_dispositivo_e_ora(activity):
    assert activity.session_uid.startswith("device:3450810483:")


def test_session_uid_ripiega_su_hash(tmp_path):
    # senza file_id il fallback e' l'hash del contenuto
    dati = STRENGTH.read_bytes()
    assert fp.sha256_file(STRENGTH) == fp.sha256_file(STRENGTH)
    assert len(fp.sha256_file(STRENGTH)) == 64
    assert dati[:4] != b""


@pytest.mark.parametrize(
    "nome, atteso",
    [
        ("24192192073_ACTIVITY.fit", "24192192073"),
        ("214e1010-24192192073_ACTIVITY.fit", "24192192073"),
        ("allenamento.fit", None),
    ],
)
def test_activity_id_dal_nome_file(nome, atteso):
    assert fp.activity_id_from_name(Path(nome)) == atteso


# --- tolleranza agli errori ------------------------------------------------


def test_attivita_non_strength_saltata():
    with pytest.raises(fp.FitSkipped, match="nessun messaggio 'set'"):
        fp.parse_file(RUNNING)


def test_file_troncato_saltato(tmp_path):
    rotto = fitgen.build_truncated_fit(tmp_path / "rotto.fit")
    with pytest.raises(fp.FitSkipped):
        fp.parse_file(rotto)


def test_file_non_fit_saltato(tmp_path):
    finto = tmp_path / "finto.fit"
    finto.write_bytes(b"non sono un file FIT" * 10)
    with pytest.raises(fp.FitSkipped):
        fp.parse_file(finto)


def test_un_file_rotto_non_ferma_il_batch(tmp_path):
    rotto = fitgen.build_truncated_fit(tmp_path / "rotto.fit")
    risultati = list(fp.parse_paths([STRENGTH, rotto, RUNNING]))
    ok = [a for _, a, _ in risultati if a is not None]
    saltati = [(p, m) for p, a, m in risultati if a is None]
    assert len(ok) == 1 and len(saltati) == 2
    assert all(motivo for _, motivo in saltati)


def test_iter_fit_files_ricorsivo(tmp_path):
    (tmp_path / "sotto").mkdir()
    a = tmp_path / "a.FIT"
    b = tmp_path / "sotto" / "b.fit"
    for p in (a, b):
        p.write_bytes(STRENGTH.read_bytes())
    (tmp_path / "note.txt").write_text("non e' un fit")
    assert fp.iter_fit_files(tmp_path) == sorted([a, b])
    assert fp.iter_fit_files(a) == [a]
    assert fp.iter_fit_files(tmp_path / "note.txt") == []


def test_il_file_sorgente_non_viene_toccato(tmp_path):
    copia = tmp_path / "copia.fit"
    copia.write_bytes(STRENGTH.read_bytes())
    prima = (copia.read_bytes(), copia.stat().st_mtime_ns)
    fp.parse_file(copia)
    assert (copia.read_bytes(), copia.stat().st_mtime_ns) == prima


# --- ispezione -------------------------------------------------------------


def test_inspect_file_serializzabile_in_json():
    dati = fp.inspect_file(STRENGTH, raw_messages=True, raw_limit=2)
    testo = json.dumps(dati, ensure_ascii=False)  # non deve alzare
    assert "strength_session.fit" in testo
    assert dati["summary"]["sets_active"] == 7
    assert dati["summary"]["sets_with_reps"] == 7
    assert dati["summary"]["sets_with_weight"] == 6  # il pull-up non ha peso
    assert dati["message_counts"]["set"] == 14
    assert dati["hr"]["sample_interval_s"] == [1.0]
    assert len(dati["raw_messages"]["set"]) == 2


def test_inspect_su_file_saltato_riporta_il_motivo():
    dati = fp.inspect_file(RUNNING)
    assert "skipped" in dati
    assert "sets" not in dati


def test_cli_inspect_scrive_json(tmp_path, capsys):
    out = tmp_path / "dump.json"
    assert main(["inspect", str(STRENGTH), "--out", str(out)]) == 0
    dati = json.loads(out.read_text(encoding="utf-8"))
    assert dati["session"]["sub_sport"] == "strength_training"


def test_cli_inspect_file_mancante(tmp_path, capsys):
    assert main(["inspect", str(tmp_path / "assente.fit")]) == 2
    assert "non trovato" in capsys.readouterr().err


def test_cli_inspect_su_attivita_non_strength(capsys):
    assert main(["inspect", str(RUNNING)]) == 1
    assert "saltato" in capsys.readouterr().err


def test_dump_json_regge_tipi_inattesi():
    # I file veri contengono anche `datetime.time` (impostazioni orologio) e
    # tipi che json non sa serializzare: il dump non deve esplodere.
    from datetime import date, time, timedelta

    from strength_tracker.fit_parser import _jsonable

    assert _jsonable(time(6, 30)) == "06:30:00"
    assert _jsonable(date(2026, 9, 1)) == "2026-09-01"
    assert _jsonable(timedelta(seconds=90)) == 90.0
    assert _jsonable((1, None, "x")) == [1, None, "x"]
    assert json.dumps(_jsonable(object())) is not None
