"""Orchestrazione dell'ingestione.

Due sorgenti, un comando solo: i `.fit` dell'orologio e i dati di Hevy — CSV
esportato dall'app o payload JSON dell'API — finiscono nella stessa cartella e
`ingest` riconosce il formato dall'estensione. Un export Hevy contiene piu'
allenamenti, quindi un singolo file puo' produrre piu' sedute.

`ingest` e' idempotente su tre livelli:

1. i file gia' letti (stesso percorso, stesso sha256) vengono saltati subito,
   senza nemmeno riaprirli, salvo `--force`;
2. `sessions.session_uid` e' UNIQUE, quindi lo stesso allenamento non entra
   due volte nemmeno se il file viene rinominato o spostato altrove;
3. la riscrittura di una seduta cancella e reinserisce le sue serie e i suoi
   campioni FC, senza mai duplicarli e senza toccare le correzioni manuali.

I file rotti o non-strength non fermano il batch: vengono registrati come
saltati, con il motivo.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import db
if TYPE_CHECKING:  # pragma: no cover
    from .hevy import HevyClient

from .fit_parser import (
    FitSkipped,
    ParsedActivity,
    iter_source_files,
    parse_file,
    sha256_file,
)


@dataclass
class IngestReport:
    #: Cosa si e' esaminato: file su disco oppure allenamenti dall'API.
    etichetta_scansione: str = "File esaminati:"
    scanned: int = 0
    ingested: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    sets_written: int = 0
    hr_written: int = 0

    @property
    def ok(self) -> int:
        return len(self.ingested) + len(self.updated)

    def as_lines(self) -> list[str]:
        lines = [
            f"{self.etichetta_scansione:20s} {self.scanned}",
            f"Sedute nuove:        {len(self.ingested)}",
            f"Sedute aggiornate:   {len(self.updated)}",
            f"Gia' presenti:       {len(self.already_present)}",
            f"Saltati:             {len(self.skipped)}",
            f"Serie salvate:       {self.sets_written}",
            f"Campioni FC salvati: {self.hr_written}",
        ]
        for path, reason in self.skipped:
            lines.append(f"  ! saltato {Path(path).name}: {reason}")
        return lines


def ingest_path(
    conn: sqlite3.Connection,
    path: Path,
    *,
    force: bool = False,
) -> IngestReport:
    """Importa un file .fit o una cartella (ricorsiva)."""
    report = IngestReport()
    files = iter_source_files(Path(path))
    report.scanned = len(files)

    for file in files:
        digest = sha256_file(file)
        if not force and db.file_already_ingested(conn, file, digest):
            report.already_present.append(str(file))
            continue
        try:
            attivita = _leggi(file)
        except FitSkipped as exc:
            db.record_skipped_file(conn, file, digest, str(exc))
            report.skipped.append((str(file), str(exc)))
            continue

        nuove = 0
        for activity in attivita:
            if not force and db.session_exists(conn, activity.session_uid):
                # Stesso allenamento gia' nel DB, magari sotto un altro file.
                report.already_present.append(activity.session_uid)
                continue
            _, existed = db.store_activity(conn, activity)
            (report.updated if existed else report.ingested).append(activity.session_uid)
            report.sets_written += len(activity.sets)
            report.hr_written += len(activity.hr_samples)
            nuove += 1
        if not nuove:
            # Niente di nuovo in questo file: si registra per non riaprirlo.
            db.record_duplicate_file(conn, file, digest, attivita[0].session_uid)

    return report


def sync_hevy(
    conn: sqlite3.Connection,
    client: "HevyClient",
    *,
    force: bool = False,
    dry_run: bool = False,
    page_size: int = 10,
    max_pagine: int | None = None,
    fuso: Any = None,
) -> IngestReport:
    """Scarica gli allenamenti da Hevy e li importa, senza passare da un file.

    Le sedute gia' presenti vengono riconosciute dal `session_uid`, che e' lo
    stesso che produrrebbe l'export CSV: chi arriva dal CSV e passa a qui non
    si ritrova niente di doppio.

    Con `dry_run` non si scrive nulla: serve a controllare che chiave e
    connessione funzionino prima di toccare il database.
    """
    from .hevy import parse_api_payload

    report = IngestReport(etichetta_scansione="Allenamenti letti:")
    for grezzo in client.iter_allenamenti(page_size=page_size, max_pagine=max_pagine):
        report.scanned += 1
        try:
            attivita = parse_api_payload(
                grezzo, source_path=Path(f"hevy-api:{grezzo.get('id', '?')}"), fuso=fuso
            )
        except FitSkipped as exc:
            report.skipped.append((str(grezzo.get("id", "?")), str(exc)))
            continue
        for act in attivita:
            esiste = db.session_exists(conn, act.session_uid)
            if esiste and not force:
                report.already_present.append(act.session_uid)
                continue
            if dry_run:
                (report.updated if esiste else report.ingested).append(act.session_uid)
                report.sets_written += len(act.sets)
                continue
            _, era_presente = db.store_activity(conn, act)
            (report.updated if era_presente else report.ingested).append(act.session_uid)
            report.sets_written += len(act.sets)
    return report


def _leggi(file: Path) -> list[ParsedActivity]:
    """Legge una sorgente.

    Un `.fit` da' una seduta; un CSV o un payload JSON di Hevy anche tante.
    """
    suffisso = file.suffix.lower()
    if suffisso == ".csv":
        from .hevy import parse_csv

        return parse_csv(file)
    if suffisso == ".json":
        from .hevy import parse_json

        return parse_json(file)
    return [parse_file(file)]
