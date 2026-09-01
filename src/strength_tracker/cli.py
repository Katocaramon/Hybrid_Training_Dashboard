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

def _open_db(args: argparse.Namespace):
    from . import db

    return db.connect(args.db or default_db_path())


def _load_mapping_into(conn, args: argparse.Namespace) -> int:
    """Riallinea `exercise_map` al YAML. E' una proiezione: si rifa' ogni volta."""
    from . import db
    from .mapping import MappingError, load_mapping

    path = getattr(args, "mapping", None) or default_mapping_path()
    try:
        mapping = load_mapping(path)
    except MappingError as exc:
        print(f"[strength-tracker] mappatura non valida: {exc}", file=sys.stderr)
        raise SystemExit(2)
    return db.refresh_exercise_map(conn, mapping.as_rows())


def cmd_ingest(args: argparse.Namespace) -> int:
    """Importa file .fit nel database, senza mai duplicare nulla."""
    from .ingest import ingest_path

    if not args.path.exists():
        print(f"[strength-tracker] percorso non trovato: {args.path}", file=sys.stderr)
        return 2
    conn = _open_db(args)
    _load_mapping_into(conn, args)
    report = ingest_path(conn, args.path, force=args.force)
    for line in report.as_lines():
        print(line)
    if report.scanned == 0:
        print(f"[strength-tracker] nessun file .fit sotto {args.path}", file=sys.stderr)
        return 1
    return 0


def cmd_unmapped(args: argparse.Namespace) -> int:
    """Elenca le chiavi grezze non ancora presenti nel YAML di mappatura."""
    from . import db

    conn = _open_db(args)
    _load_mapping_into(conn, args)
    righe = db.unmapped_raw_keys(conn)
    if not righe:
        print("Nessun esercizio non mappato: la mappatura copre tutte le serie nel database.")
        return 0

    if args.yaml:
        print("# Da incollare sotto 'exercises:' in config/exercise_mapping.yaml")
        for r in righe:
            etichetta = r["label"] or r["raw_key"]
            print(f"  - name: {etichetta}")
            print("    primary: DA_COMPLETARE")
            print("    secondary: []")
            print("    match:")
            print(f"      - {r['raw_key']}")
        return 0

    print(f"{len(righe)} esercizi non mappati:\n")
    print(f"{'chiave grezza':52s} {'serie':>6} {'sedute':>7}  {'dal':10s} {'al':10s} etichetta Garmin")
    print("-" * 120)
    for r in righe:
        print(
            f"{r['raw_key']:52s} {r['n_sets']:6d} {r['n_sessions']:7d}  "
            f"{_data_it(r['first_seen']):10s} {_data_it(r['last_seen']):10s} {r['label'] or ''}"
        )
    print("\nAggiungili a config/exercise_mapping.yaml (o rilancia con --yaml).")
    return 0


def cmd_correct(args: argparse.Namespace) -> int:
    """Registra un override manuale su una serie, senza toccare i dati grezzi."""
    from . import db

    conn = _open_db(args)
    if args.from_csv:
        return _correct_from_csv(conn, args.from_csv)
    if args.set_id is None:
        print(
            "[strength-tracker] serve un set_id, oppure --from-csv FILE",
            file=sys.stderr,
        )
        return 2
    try:
        corr_id = db.add_correction(
            conn,
            args.set_id,
            reps=args.reps,
            weight_kg=args.weight,
            exercise_key=args.exercise,
            note=args.note,
        )
    except KeyError as exc:
        print(f"[strength-tracker] {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"[strength-tracker] {exc}", file=sys.stderr)
        return 2
    riga = conn.execute(
        "SELECT * FROM v_sets WHERE set_id = ?", (args.set_id,)
    ).fetchone()
    print(
        f"Correzione #{corr_id} sulla serie {args.set_id} "
        f"({_data_it(riga['local_date'])}, {riga['exercise_name'] or 'esercizio ignoto'}): "
        f"reps={riga['reps']} peso={riga['weight_kg']} kg "
        f"(nel file: reps={riga['reps_raw']} peso={riga['weight_kg_raw']})"
    )
    return 0


def _correct_from_csv(conn, percorso: Path) -> int:
    """Importa correzioni in blocco, numerando le serie come Garmin Connect."""
    import csv

    from . import db

    if not percorso.is_file():
        print(f"[strength-tracker] file non trovato: {percorso}", file=sys.stderr)
        return 2
    fatte, errori = 0, []
    with open(percorso, newline="", encoding="utf-8") as fh:
        # Le righe di commento vanno tolte prima di DictReader, altrimenti la
        # prima diventa l'intestazione e l'import scivola via in silenzio.
        righe_utili = [r for r in fh if r.strip() and not r.lstrip().startswith("#")]
    lettore = csv.DictReader(righe_utili)
    if not lettore.fieldnames or "data" not in lettore.fieldnames:
        print(
            f"[strength-tracker] {percorso}: manca la colonna 'data' "
            f"(intestazione trovata: {lettore.fieldnames})",
            file=sys.stderr,
        )
        return 2
    if True:
        for n, riga in enumerate(lettore, start=2):
            if not (riga.get("data") or "").strip():
                errori.append(f"riga {n}: colonna 'data' vuota")
                continue
            try:
                set_id = db.set_id_by_position(
                    conn,
                    _data_iso(riga["data"]),
                    int(riga["serie"]),
                    (riga.get("seduta") or "").strip() or None,
                )
                db.add_correction(
                    conn,
                    set_id,
                    reps=_int_o_none(riga.get("reps")),
                    weight_kg=_float_o_none(riga.get("peso_kg")),
                    note=(riga.get("nota") or "").strip() or f"import da {percorso.name}",
                )
                fatte += 1
            except (KeyError, ValueError) as exc:
                errori.append(f"riga {n}: {exc}")
    print(f"Correzioni applicate: {fatte}")
    for errore in errori:
        print(f"  ! {errore}", file=sys.stderr)
    return 1 if errori and not fatte else 0


def _int_o_none(v: str | None) -> int | None:
    v = (v or "").strip()
    return int(v) if v else None


def _float_o_none(v: str | None) -> float | None:
    v = (v or "").strip().replace(",", ".")
    return float(v) if v else None


def _data_iso(v: str) -> str:
    """DD/MM/YYYY (o gia' ISO) -> YYYY-MM-DD."""
    v = v.strip()
    if "/" in v:
        g, m, a = v.split("/")
        return f"{a}-{m.zfill(2)}-{g.zfill(2)}"
    return v


def _durata(secondi: float | None) -> str:
    if not secondi:
        return "-"
    minuti, s = divmod(int(secondi), 60)
    ore, minuti = divmod(minuti, 60)
    return f"{ore}h{minuti:02d}" if ore else f"{minuti}'{s:02d}\""


def _kg(v: float | None, suffisso: str = " kg") -> str:
    return "n/d" if v is None else f"{v:,.0f}{suffisso}".replace(",", ".")


def cmd_report(args: argparse.Namespace) -> int:
    """Genera la dashboard HTML."""
    from . import dashboard
    from .mapping import load_mapping

    conn = _open_db(args)
    _load_mapping_into(conn, args)
    mapping = load_mapping(getattr(args, "mapping", None) or default_mapping_path())
    percorso = dashboard.genera(conn, mapping, args.output or default_output_path())
    print(f"Dashboard generata: {percorso}  ({percorso.stat().st_size / 1024:.0f} KB)")
    if not conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]:
        print(
            "[strength-tracker] il database e' vuoto: la dashboard non ha dati da mostrare.",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """Riepilogo testuale rapido nel terminale."""
    from . import metrics as mt

    conn = _open_db(args)
    _load_mapping_into(conn, args)
    r = mt.riepilogo(conn)
    if not r["n_sedute"]:
        print("Database vuoto: lancia prima `strength-tracker ingest <path>`.")
        return 1

    print("RIEPILOGO")
    print(f"  Sedute                {r['n_sedute']}")
    print(
        f"  Periodo               {_data_it(r['prima_seduta'])} - {_data_it(r['ultima_seduta'])}"
    )
    print(f"  Ultime 4 settimane    {r['sedute_ultime_4_settimane']} sedute")
    print(f"  Serie attive          {r['n_serie_attive']}")
    print(f"  Tempo attivo          {_durata(r['tempo_attivo_totale_s'])}")
    print(f"  Tempo sotto tensione  {_durata(r['tempo_sotto_tensione_s'])}")
    if r["volume_totale_kg"] is None:
        print("  Volume totale         non calcolabile: nessuna serie ha reps e peso")
    else:
        print(
            f"  Volume totale         {_kg(r['volume_totale_kg'])}"
            f"  (su {r['serie_con_volume']}/{r['serie_a_carico']} serie a carico)"
        )

    print("\nVOLUME SETTIMANALE")
    for w in mt.volume_settimanale(conn):
        volume = (
            "non calcolabile" if w["volume_kg"] is None else _kg(w["volume_kg"])
        )
        media = "" if w["media_mobile_kg"] is None else f"  media 4 sett. {_kg(w['media_mobile_kg'])}"
        print(f"  {w['etichetta']}  {w['n_sedute']} sedute  {volume}{media}")

    print("\nSERIE PER GRUPPO MUSCOLARE (a settimana)")
    dati = mt.serie_per_gruppo(conn, focus=_focus(args))
    for gruppo in dati["gruppi"]:
        serie = "  ".join(f"{n:>3}" for n in dati["serie"][gruppo])
        marchio = " *" if gruppo in dati["focus"] else "  "
        print(f" {marchio}{gruppo:20s} {serie}")
    if dati["focus"]:
        print("  * gruppi sotto osservazione (interferenza con la corsa)")

    print("\nULTIME SEDUTE")
    for s in mt.sedute(conn)[:8]:
        densita = "n/d" if s["densita_kg_min"] is None else f"{s['densita_kg_min']} kg/min"
        deriva = "n/d" if s["fc_deriva_bpm"] is None else f"{s['fc_deriva_bpm']:+g} bpm"
        nome = (s["workout_name"] or "-")[:22]
        print(
            f"  {_data_it(s['local_date'])}  {nome:22s} {s['n_serie']:>3} serie  "
            f"vol {_kg(s['volume_kg'])}  dens {densita}  "
            f"L/R {s['rapporto_lavoro_riposo'] or 'n/d'}  deriva FC {deriva}"
        )

    a = mt.anomalie(conn)
    totale = sum(len(v) for v in a.values())
    print(f"\nANOMALIE ({totale})")
    etichette = {
        "esercizi_non_mappati": "esercizi non mappati",
        "serie_peso_zero": "serie con peso zero",
        "serie_reps_sospette": "serie con ripetizioni sospette",
        "serie_durata_anomala": "serie di durata anomala (>5')",
        "sedute_senza_ripetizioni": "sedute senza alcuna ripetizione registrata",
    }
    for chiave, voci in a.items():
        if voci:
            print(f"  {len(voci):>3}  {etichette[chiave]}")
    if not totale:
        print("  nessuna")
    return 0


def _focus(args: argparse.Namespace) -> list[str]:
    from .mapping import load_mapping

    try:
        return list(load_mapping(getattr(args, "mapping", None) or default_mapping_path()).focus_groups)
    except Exception:
        return []


def _data_it(iso: str | None) -> str:
    """YYYY-MM-DD -> DD/MM/YYYY."""
    if not iso:
        return "-"
    parti = iso.split("-")
    return f"{parti[2]}/{parti[1]}/{parti[0]}" if len(parti) == 3 else iso


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
        "--mapping",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"mappatura esercizi (default: {default_mapping_path()})",
    )
    p_ingest.add_argument(
        "--force",
        action="store_true",
        help="rilegge anche i file gia' processati (resta idempotente: nessun duplicato)",
    )
    p_ingest.set_defaults(func=cmd_ingest)

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
    p_unmapped.add_argument(
        "--mapping",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"mappatura esercizi (default: {default_mapping_path()})",
    )
    p_unmapped.add_argument(
        "--yaml",
        action="store_true",
        help="stampa le voci gia' pronte da incollare in exercise_mapping.yaml",
    )
    p_unmapped.set_defaults(func=cmd_unmapped)

    p_correct = sub.add_parser(
        "correct",
        help="registra una correzione manuale su una serie (i dati grezzi non vengono toccati)",
    )
    p_correct.add_argument(
        "set_id", type=int, nargs="?", help="id della serie da correggere"
    )
    p_correct.add_argument(
        "--from-csv",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            "importa piu' correzioni da un CSV con colonne "
            "data,serie,reps,peso_kg[,seduta,nota]; 'serie' e' il numero "
            "della serie attiva come lo mostra Garmin Connect"
        ),
    )
    p_correct.add_argument("--reps", type=int, default=None, help="ripetizioni corrette")
    p_correct.add_argument("--weight", type=float, default=None, metavar="KG", help="peso corretto in kg")
    p_correct.add_argument(
        "--exercise",
        default=None,
        metavar="RAW_KEY",
        help="riassegna la serie a un'altra chiave grezza (es. deadlift/barbell_deadlift)",
    )
    p_correct.add_argument("--note", default=None, help="nota libera sulla correzione")
    p_correct.set_defaults(func=cmd_correct)

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
    p_report.set_defaults(func=cmd_report)

    p_stats = sub.add_parser("stats", help="riepilogo testuale rapido nel terminale")
    p_stats.add_argument(
        "--mapping",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"mappatura esercizi (default: {default_mapping_path()})",
    )
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 0
    try:
        return int(args.func(args))
    except BrokenPipeError:
        # `strength-tracker stats | head` non e' un errore.
        try:
            sys.stdout.close()
        except BrokenPipeError:
            pass
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
