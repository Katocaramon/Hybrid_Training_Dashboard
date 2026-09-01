"""Interfaccia a riga di comando.

Flusso normale del dopo-allenamento:

    strength-tracker ingest ~/Downloads/palestra
    strength-tracker report

oppure `make session FIT=~/Downloads/palestra`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import (
    __version__,
    default_db_path,
    default_mapping_path,
    default_output_path,
)

# Fase in cui ciascun comando viene implementato: finche' non e' pronto il
# comando esce con codice 1 e lo dice, invece di fingere di aver lavorato.
_PHASE = {
    "ingest": "3 (schema SQLite e ingestione)",
    "unmapped": "4 (mappatura esercizi)",
    "correct": "3 (schema SQLite e ingestione)",
    "report": "5 (dashboard)",
    "stats": "4 (metriche)",
}


def _todo(command: str) -> int:
    print(
        f"[strength-tracker] '{command}' non e' ancora implementato "
        f"(previsto nella Fase {_PHASE[command]}).",
        file=sys.stderr,
    )
    return 1


def cmd_inspect(args: argparse.Namespace) -> int:
    """Dump JSON della struttura reale di un file .fit."""
    from .fit_parser import inspect_file

    if not args.path.is_file():
        print(f"[strength-tracker] file non trovato: {args.path}", file=sys.stderr)
        return 2
    data = inspect_file(
        args.path,
        raw_messages=args.raw,
        raw_limit=args.raw_limit,
        include_hr=args.include_hr,
    )
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"[strength-tracker] ispezione scritta in {args.out}", file=sys.stderr)
    else:
        print(text)
    if "skipped" in data:
        print(f"[strength-tracker] file saltato: {data['skipped']}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="strength-tracker",
        description=(
            "Analisi locale delle sedute di forza registrate su Garmin. "
            "Nessuna rete, nessun cloud: i file .fit li fornisci tu."
        ),
    )
    parser.add_argument("--version", action="version", version=f"strength-tracker {__version__}")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"percorso del database SQLite (default: {default_db_path()})",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMANDO")

    p_ingest = sub.add_parser(
        "ingest", help="importa uno o piu' file .fit (file singolo o cartella, ricorsivo)"
    )
    p_ingest.add_argument("path", type=Path, help="file .fit o cartella da importare")
    p_ingest.add_argument(
        "--force",
        action="store_true",
        help="rilegge anche i file gia' processati (resta idempotente: nessun duplicato)",
    )
    p_ingest.set_defaults(func=lambda args: _todo("ingest"))

    p_inspect = sub.add_parser(
        "inspect",
        help="dump JSON dei messaggi di un file .fit, per ispezionare la struttura reale",
    )
    p_inspect.add_argument("path", type=Path, help="file .fit da ispezionare")
    p_inspect.add_argument(
        "--out", type=Path, default=None, metavar="PATH", help="scrive il JSON su file invece che su stdout"
    )
    p_inspect.add_argument(
        "--raw",
        action="store_true",
        help="include un campione grezzo di ogni tipo di messaggio, campi unknown_* compresi",
    )
    p_inspect.add_argument(
        "--raw-limit", type=int, default=3, metavar="N", help="messaggi grezzi per tipo (default 3)"
    )
    p_inspect.add_argument(
        "--include-hr", action="store_true", help="include tutti i campioni di frequenza cardiaca"
    )
    p_inspect.set_defaults(func=cmd_inspect)

    p_unmapped = sub.add_parser(
        "unmapped", help="elenca i nomi esercizio grezzi presenti nel DB e non ancora mappati"
    )
    p_unmapped.set_defaults(func=lambda args: _todo("unmapped"))

    p_correct = sub.add_parser(
        "correct",
        help="registra una correzione manuale su una serie (i dati grezzi non vengono toccati)",
    )
    p_correct.add_argument("set_id", type=int, help="id della serie da correggere")
    p_correct.add_argument("--reps", type=int, default=None, help="ripetizioni corrette")
    p_correct.add_argument("--weight", type=float, default=None, metavar="KG", help="peso corretto in kg")
    p_correct.add_argument("--note", default=None, help="nota libera sulla correzione")
    p_correct.set_defaults(func=lambda args: _todo("correct"))

    p_report = sub.add_parser("report", help="genera la dashboard HTML")
    p_report.add_argument(
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"file HTML da generare (default: {default_output_path()})",
    )
    p_report.add_argument(
        "--mapping",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"mappatura esercizi (default: {default_mapping_path()})",
    )
    p_report.set_defaults(func=lambda args: _todo("report"))

    p_stats = sub.add_parser("stats", help="riepilogo testuale rapido nel terminale")
    p_stats.set_defaults(func=lambda args: _todo("stats"))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 0
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
