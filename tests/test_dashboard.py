"""Test della dashboard generata.

Si verifica che il file sia davvero self-contained (nessuna richiesta di rete),
che i dati incorporati siano coerenti e che i casi limite non producano una
pagina rotta.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

import fitgen
from strength_tracker import dashboard, db
from strength_tracker.cli import main
from strength_tracker.ingest import ingest_path
from strength_tracker.mapping import load_mapping

MAPPATURA = Path("config/exercise_mapping.yaml")
STACCO = ((8, 8, 8), (0, 0, 0))
TRAZIONI = ((21, 21, 21), (42, 42, 42))


@pytest.fixture
def pagina(tmp_path):
    """Una dashboard generata da due sedute vere, piu' il suo HTML."""
    conn = db.connect(tmp_path / "d.db")
    cartella = tmp_path / "fit"
    for giorno, peso in ((3, 60.0), (10, 65.0)):
        fitgen.build_strength_fit(
            cartella / f"{giorno}.fit",
            start=datetime(2026, 9, giorno, 17, tzinfo=timezone.utc),
            plan=[(*STACCO, 8, peso, 40.0, 90.0), (*TRAZIONI, 5, 40.0, 30.0, 90.0)],
        )
    ingest_path(conn, cartella)
    mapping = load_mapping(MAPPATURA)
    db.refresh_exercise_map(conn, mapping.as_rows())
    percorso = dashboard.genera(conn, mapping, tmp_path / "out" / "dashboard.html")
    return percorso, percorso.read_text(encoding="utf-8"), conn


def dati_incorporati(html: str) -> dict:
    m = re.search(r'<script id="dati" type="application/json">(.*?)</script>', html, re.S)
    assert m, "blocco dati non trovato"
    return json.loads(m.group(1).replace("<\\/", "</"))


# --- self-contained --------------------------------------------------------


def test_file_generato(pagina):
    percorso, html, _ = pagina
    assert percorso.is_file()
    assert html.startswith("<!doctype html>")
    assert percorso.stat().st_size > 100_000  # Chart.js e' dentro


def test_nessuna_richiesta_di_rete(pagina):
    _, html, _ = pagina
    # nessun tag che carichi qualcosa da fuori: deve funzionare offline
    esterni = re.findall(r"<(?:script|link|img|iframe)[^>]*\b(?:src|href)\s*=", html)
    assert esterni == []


def test_chartjs_inline(pagina):
    _, html, _ = pagina
    assert "Chart.js v" in html
    assert "vendor/chart.min.js" not in html  # inlineato, non referenziato


def test_lingua_e_unita_italiane(pagina):
    _, html, _ = pagina
    assert '<html lang="it">' in html
    assert 'new Intl.NumberFormat("it-IT"' in html
    assert "Volume settimanale" in html and "Ripetizioni" in html


# --- dati incorporati ------------------------------------------------------


def test_dati_coerenti(pagina):
    _, html, _ = pagina
    d = dati_incorporati(html)
    assert d["riepilogo"]["n_sedute"] == 2
    assert d["riepilogo"]["volume_totale_kg"] == pytest.approx(8 * 60 + 8 * 65)
    assert len(d["settimane"]) == 2
    assert d["sedute"][0]["serie"], "il dettaglio serie per serie e' incorporato"


def test_trazioni_assistite_fuori_dal_tonnellaggio(pagina):
    _, html, _ = pagina
    d = dati_incorporati(html)
    prog = d["progressioni"]["Trazioni assistite"]
    assert prog["weight_mode"] == "assistenza"
    assert prog["carico_stimato"] is True
    assert all(p["volume_kg"] is None for p in prog["punti"])


def test_gruppi_sotto_osservazione_sempre_presenti(pagina):
    _, html, _ = pagina
    d = dati_incorporati(html)
    nomi = [f["nome"] for f in d["focus"]]
    assert nomi == ["hamstring", "adduttori", "glutei"]
    # nessuno dei tre e' allenato in queste sedute: devono comunque comparire
    assert all(f["presente"] is False for f in d["focus"])


def test_settimane_senza_allenamento_nei_dati(tmp_path):
    conn = db.connect(tmp_path / "d.db")
    cartella = tmp_path / "fit"
    for giorno in (3, 24):
        fitgen.build_strength_fit(
            cartella / f"{giorno}.fit",
            start=datetime(2026, 9, giorno, 17, tzinfo=timezone.utc),
            plan=[(*STACCO, 8, 60.0, 40.0, 90.0)],
        )
    ingest_path(conn, cartella)
    mapping = load_mapping(MAPPATURA)
    db.refresh_exercise_map(conn, mapping.as_rows())
    html = dashboard.genera(conn, mapping, tmp_path / "d.html").read_text(encoding="utf-8")
    d = dati_incorporati(html)
    assert [w["n_sedute"] for w in d["settimane"]] == [1, 0, 0, 1]
    assert d["settimane"][1]["volume_kg"] is None


def test_niente_zeri_al_posto_dei_dati_mancanti(pagina):
    _, html, _ = pagina
    assert "non calcolabile" in html  # la pagina lo dice, invece di mostrare 0


# --- casi limite -----------------------------------------------------------


def test_nome_esercizio_con_tag_script_non_rompe_la_pagina(tmp_path):
    """Un nome ostile nel YAML non deve poter chiudere il tag <script>."""
    cattivo = tmp_path / "map.yaml"
    cattivo.write_text(
        'exercises:\n'
        '  - name: "Stacco </script><script>alert(1)</script>"\n'
        "    primary: catena_posteriore\n"
        "    match: [deadlift/barbell_deadlift]\n",
        encoding="utf-8",
    )
    conn = db.connect(tmp_path / "d.db")
    cartella = tmp_path / "fit"
    fitgen.build_strength_fit(
        cartella / "a.fit",
        start=datetime(2026, 9, 3, 17, tzinfo=timezone.utc),
        plan=[(*STACCO, 8, 60.0, 40.0, 90.0)],
    )
    ingest_path(conn, cartella)
    mapping = load_mapping(cattivo)
    db.refresh_exercise_map(conn, mapping.as_rows())
    html = dashboard.genera(conn, mapping, tmp_path / "d.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html
    d = dati_incorporati(html)  # il JSON resta valido e leggibile
    assert "alert(1)" in json.dumps(d)


def test_database_vuoto_produce_comunque_una_pagina(tmp_path):
    conn = db.connect(tmp_path / "vuoto.db")
    percorso = dashboard.genera(conn, load_mapping(MAPPATURA), tmp_path / "d.html")
    html = percorso.read_text(encoding="utf-8")
    assert dati_incorporati(html)["riepilogo"]["n_sedute"] == 0
    assert "nessuna seduta" in html or "Anomalie" in html


# --- CLI ------------------------------------------------------------------


def test_cli_report(tmp_path, capsys):
    dbfile = tmp_path / "cli.db"
    cartella = tmp_path / "fit"
    fitgen.build_strength_fit(cartella / "a.fit", start=datetime(2026, 9, 3, 17, tzinfo=timezone.utc))
    main(["--db", str(dbfile), "ingest", str(cartella)])
    capsys.readouterr()
    uscita = tmp_path / "report" / "dashboard.html"
    assert main(["--db", str(dbfile), "report", "--output", str(uscita)]) == 0
    assert "Dashboard generata" in capsys.readouterr().out
    assert uscita.is_file()


def test_cli_report_su_database_vuoto(tmp_path, capsys):
    uscita = tmp_path / "d.html"
    assert main(["--db", str(tmp_path / "vuoto.db"), "report", "--output", str(uscita)]) == 1
    assert uscita.is_file()  # la pagina c'e' comunque, ma lo dice
    assert "vuoto" in capsys.readouterr().err
