"""Orchestrazione dell'ingestione.

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

from . import db
from .fit_parser import FitSkipped, iter_fit_files, parse_file, sha256_file


@dataclass
class IngestReport:
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
            f"File esaminati:      {self.scanned}",
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
    files = iter_fit_files(Path(path))
    report.scanned = len(files)

    for file in files:
        digest = sha256_file(file)
        if not force and db.file_already_ingested(conn, file, digest):
            report.already_present.append(str(file))
            continue
        try:
            activity = parse_file(file)
        except FitSkipped as exc:
            db.record_skipped_file(conn, file, digest, str(exc))
            report.skipped.append((str(file), str(exc)))
            continue

        if not force and db.session_exists(conn, activity.session_uid):
            # Stesso allenamento gia' nel DB sotto un altro percorso.
            report.already_present.append(str(file))
            db.record_duplicate_file(conn, file, digest, activity.session_uid)
            continue

        _, existed = db.store_activity(conn, activity)
        (report.updated if existed else report.ingested).append(activity.session_uid)
        report.sets_written += len(activity.sets)
        report.hr_written += len(activity.hr_samples)

    return report
