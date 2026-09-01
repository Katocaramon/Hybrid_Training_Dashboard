"""Test del caricamento della mappatura esercizi."""

from __future__ import annotations

from pathlib import Path

import pytest

from strength_tracker.mapping import MappingError, load_mapping

REALE = Path("config/exercise_mapping.yaml")


def scrivi(tmp_path: Path, testo: str) -> Path:
    p = tmp_path / "map.yaml"
    p.write_text(testo, encoding="utf-8")
    return p


def test_mappatura_del_repo_e_valida():
    m = load_mapping(REALE)
    assert len(m.exercises) > 0
    assert m.focus_groups == ("hamstring", "adduttori", "glutei")
    # il pull-up assistito e' l'unica chiave confermata da un file reale
    trazioni = m.by_raw_key["pull_up/band_assisted_pull_up"]
    assert trazioni.name == "Trazioni assistite"
    assert trazioni.primary_group == "dorsali"
    # il peso registrato e' l'assistenza, non il carico
    assert trazioni.weight_mode == "assistenza"


def test_i_sette_esercizi_del_programma_ci_sono():
    nomi = {e.name for e in load_mapping(REALE).exercises}
    for atteso in [
        "Trap bar deadlift",
        "Romanian deadlift",  # bilanciere o manubri: una voce sola
        "Bulgarian split squat",
        "Step-up",
        "Copenhagen plank",
        "Trazioni assistite",  # ex "band-assisted pull-up"
        "Military press",
    ]:
        assert atteso in nomi


def test_gruppi_focus_coperti_da_almeno_un_esercizio():
    m = load_mapping(REALE)
    gruppi = {e.primary_group for e in m.exercises} | {
        g for e in m.exercises for g in e.secondary_groups
    }
    for focus in m.focus_groups:
        assert focus in gruppi, f"nessun esercizio tocca {focus}"


def test_file_assente_da_mappatura_vuota(tmp_path):
    m = load_mapping(tmp_path / "non_esiste.yaml")
    assert m.exercises == () and m.by_raw_key == {}


def test_chiave_grezza_duplicata_e_un_errore(tmp_path):
    p = scrivi(
        tmp_path,
        """
exercises:
  - name: Uno
    match: [squat/step_up]
  - name: Due
    match: [squat/step_up]
""",
    )
    with pytest.raises(MappingError, match="assegnata sia"):
        load_mapping(p)


def test_voce_senza_nome_e_un_errore(tmp_path):
    p = scrivi(tmp_path, "exercises:\n  - primary: glutei\n")
    with pytest.raises(MappingError, match="senza 'name'"):
        load_mapping(p)


def test_yaml_non_valido(tmp_path):
    p = scrivi(tmp_path, "exercises: [ non chiuso")
    with pytest.raises(MappingError, match="YAML non valido"):
        load_mapping(p)


def test_match_e_secondary_accettano_anche_una_stringa(tmp_path):
    p = scrivi(
        tmp_path,
        "exercises:\n  - name: Uno\n    primary: glutei\n    secondary: core\n    match: squat/step_up\n",
    )
    m = load_mapping(p)
    assert m.by_raw_key["squat/step_up"].secondary_groups == ("core",)


def test_as_rows_pronte_per_il_db():
    righe = load_mapping(REALE).as_rows()
    assert all({"raw_key", "exercise_name", "primary_group", "secondary_groups"} <= set(r) for r in righe)
