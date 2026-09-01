"""Smoke test della CLI: la Fase 1 deve garantire che il comando esista,
si installi e risponda, non che faccia gia' qualcosa."""

from __future__ import annotations

import subprocess
import sys

import pytest

from strength_tracker import __version__
from strength_tracker.cli import build_parser, main

COMANDI = ["ingest", "inspect", "unmapped", "correct", "report", "stats"]


def test_help_esce_a_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "strength-tracker" in capsys.readouterr().out


def test_senza_comando_stampa_help(capsys):
    assert main([]) == 0
    assert "COMANDO" in capsys.readouterr().out


@pytest.mark.parametrize("comando", COMANDI)
def test_ogni_comando_e_registrato(comando):
    parser = build_parser()
    azioni = parser._subparsers._group_actions[0].choices  # type: ignore[union-attr]
    assert comando in azioni


def test_comandi_non_implementati_escono_a_uno(capsys):
    # Meglio un fallimento esplicito che un successo silenzioso.
    assert main(["report"]) == 1
    assert "non e' ancora implementato" in capsys.readouterr().err


def test_correct_richiede_un_bersaglio(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "x.db"), "correct"]) == 2
    assert "set_id" in capsys.readouterr().err


def test_eseguibile_installato():
    out = subprocess.run(
        [sys.executable, "-m", "strength_tracker.cli", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert __version__ in out.stdout
