"""Schema SQLite, migrazioni e scritture.

Nessun ORM: solo `sqlite3` della stdlib. Le date sono stringhe ISO 8601 (il
tipo nativo di SQLite per le date non esiste e le stringhe ISO si ordinano
correttamente).

Scelte di progetto
------------------

* **Dati grezzi e interpretazione sono separati.** `sets` contiene solo cio'
  che c'e' nel file (`raw_exercise_key`, reps, peso). La normalizzazione vive
  in `exercise_map`, ricaricata dal YAML a ogni comando, e le correzioni
  manuali in `corrections`. La vista `v_sets` sovrappone le due cose ai dati
  grezzi, che non vengono mai riscritti. Cosi' basta editare il YAML e
  rilanciare `report`: nessuna re-ingestione.
* **Le correzioni sopravvivono alla re-ingestione.** Non puntano a
  `sets.id` (che cambia con `--force`) ma alla coppia stabile
  `(session_uid, set_index)`.
* **`sessions` e' la tabella generica delle attivita'**, con `activity_type` e
  la settimana ISO gia' calcolata. Aggiungere in futuro le sedute di corsa
  vuol dire inserirle qui con `activity_type='run'` piu' una tabella di
  dettaglio con FK su `sessions(id)`: nessuna migrazione distruttiva, e
  l'incrocio settimanale palestra/corsa e' una semplice join su
  `(iso_year, iso_week)`.
* **La FC resta a campione singolo** (1 Hz, ~4300 righe per seduta). Sono
  ~700k righe l'anno a 3 sedute a settimana: SQLite le regge senza accorgersene
  e mantenere il grezzo permette di ricalcolare la FC per serie anche se
  domani cambiassero i confini delle serie. Aggregare subito farebbe risparmiare
  spazio che non e' un problema, perdendo dati che non si recuperano.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .fit_parser import ParsedActivity

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- Una riga per attivita'. Oggi solo forza; domani anche corsa, senza
-- rifare nulla (activity_type + tabella di dettaglio con FK su id).
CREATE TABLE IF NOT EXISTS sessions (
    id                    INTEGER PRIMARY KEY,
    session_uid           TEXT    NOT NULL UNIQUE,
    activity_type         TEXT    NOT NULL DEFAULT 'strength',
    garmin_activity_id    TEXT,
    sport                 TEXT,
    sub_sport             TEXT,
    start_time_utc        TEXT    NOT NULL,
    start_time_local      TEXT,
    local_date            TEXT,
    iso_year              INTEGER,
    iso_week              INTEGER,
    utc_offset_s          INTEGER,
    total_elapsed_s       REAL,
    total_timer_s         REAL,
    avg_hr                INTEGER,
    max_hr                INTEGER,
    calories              INTEGER,
    total_training_effect REAL,
    sport_profile_name    TEXT,
    workout_name          TEXT,
    device_manufacturer   TEXT,
    device_product        TEXT,
    device_serial         INTEGER,
    source_path           TEXT    NOT NULL,
    source_sha256         TEXT    NOT NULL,
    ingested_at           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_week ON sessions (iso_year, iso_week);
CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions (local_date);

-- Una riga per serie E per pausa: `set_type` distingue active da rest.
-- Solo dati grezzi del file: nessuna interpretazione.
CREATE TABLE IF NOT EXISTS sets (
    id                  INTEGER PRIMARY KEY,
    session_id          INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    set_index           INTEGER NOT NULL,
    order_in_session    INTEGER NOT NULL,
    set_type            TEXT,
    start_time_utc      TEXT,
    duration_s          REAL,
    reps                INTEGER,
    weight_kg           REAL,
    weight_display_unit TEXT,
    planned_reps        INTEGER,
    planned_weight_kg   REAL,
    wkt_step_index      INTEGER,
    raw_exercise_key    TEXT,
    raw_exercise_label  TEXT,
    category_raw        TEXT,
    subcategory_raw     TEXT,
    UNIQUE (session_id, set_index)
);
CREATE INDEX IF NOT EXISTS idx_sets_exercise ON sets (raw_exercise_key);

CREATE TABLE IF NOT EXISTS hr_samples (
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ts_utc      TEXT    NOT NULL,
    bpm         INTEGER NOT NULL,
    PRIMARY KEY (session_id, ts_utc)
) WITHOUT ROWID;

-- Override manuali: append-only, vale l'ultimo. I dati grezzi restano intatti.
-- La chiave e' (session_uid, set_index), stabile anche dopo `ingest --force`.
CREATE TABLE IF NOT EXISTS corrections (
    id           INTEGER PRIMARY KEY,
    session_uid  TEXT    NOT NULL,
    set_index    INTEGER NOT NULL,
    reps         INTEGER,
    weight_kg    REAL,
    exercise_key TEXT,
    note         TEXT,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_corrections_target ON corrections (session_uid, set_index);

-- Proiezione del YAML di mappatura: riscritta a ogni comando, mai a mano.
CREATE TABLE IF NOT EXISTS exercise_map (
    raw_key          TEXT PRIMARY KEY,
    exercise_name    TEXT NOT NULL,
    primary_group    TEXT,
    secondary_groups TEXT,
    updated_at       TEXT NOT NULL
);

-- Registro dei file gia' letti: rende `ingest` idempotente e veloce.
CREATE TABLE IF NOT EXISTS ingested_files (
    path        TEXT PRIMARY KEY,
    sha256      TEXT NOT NULL,
    session_uid TEXT,
    status      TEXT NOT NULL,   -- ok | skipped
    reason      TEXT,
    ingested_at TEXT NOT NULL
);
"""

# L'ultima correzione per ogni serie.
_VIEWS = """
DROP VIEW IF EXISTS v_corrections_effective;
CREATE VIEW v_corrections_effective AS
SELECT c.session_uid, c.set_index, c.reps, c.weight_kg, c.exercise_key, c.note, c.created_at
FROM corrections c
JOIN (
    SELECT session_uid, set_index, MAX(id) AS last_id
    FROM corrections GROUP BY session_uid, set_index
) last ON last.last_id = c.id;

-- La vista su cui poggia tutta l'analisi: dati grezzi + mappatura +
-- correzioni, senza che nessuna delle tre riscriva le altre.
DROP VIEW IF EXISTS v_sets;
CREATE VIEW v_sets AS
SELECT
    s.id                AS set_id,
    s.session_id,
    ses.session_uid,
    ses.local_date,
    ses.iso_year,
    ses.iso_week,
    s.set_index,
    s.order_in_session,
    s.set_type,
    s.start_time_utc,
    s.duration_s,
    COALESCE(c.exercise_key, s.raw_exercise_key)            AS raw_exercise_key,
    s.raw_exercise_label,
    COALESCE(m.exercise_name, s.raw_exercise_label, s.raw_exercise_key) AS exercise_name,
    m.primary_group     AS muscle_group,
    m.secondary_groups,
    CASE WHEN m.raw_key IS NULL THEN 1 ELSE 0 END           AS unmapped,
    COALESCE(c.reps, s.reps)                                AS reps,
    COALESCE(c.weight_kg, s.weight_kg)                      AS weight_kg,
    s.reps                                                  AS reps_raw,
    s.weight_kg                                             AS weight_kg_raw,
    s.planned_reps,
    s.planned_weight_kg,
    CASE
        WHEN c.reps IS NOT NULL OR c.weight_kg IS NOT NULL THEN 'correzione'
        ELSE 'file'
    END                                                     AS data_source,
    CASE
        WHEN COALESCE(c.reps, s.reps) IS NULL THEN NULL
        WHEN COALESCE(c.weight_kg, s.weight_kg) IS NULL THEN NULL
        ELSE COALESCE(c.reps, s.reps) * COALESCE(c.weight_kg, s.weight_kg)
    END                                                     AS volume_kg
FROM sets s
JOIN sessions ses ON ses.id = s.session_id
LEFT JOIN v_corrections_effective c
       ON c.session_uid = ses.session_uid AND c.set_index = s.set_index
LEFT JOIN exercise_map m
       ON m.raw_key = COALESCE(c.exercise_key, s.raw_exercise_key);
"""


def connect(path: Path) -> sqlite3.Connection:
    """Apre (e crea) il database, con schema aggiornato."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Applica lo schema. Idempotente: si puo' richiamare a ogni avvio."""
    conn.executescript(_SCHEMA)
    conn.executescript(_VIEWS)
    row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    current = row["v"] if row and row["v"] is not None else 0
    if current < SCHEMA_VERSION:
        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, _now()),
        )
        conn.commit()
    return SCHEMA_VERSION


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


# --------------------------------------------------------------------------
# scritture
# --------------------------------------------------------------------------


def session_exists(conn: sqlite3.Connection, session_uid: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM sessions WHERE session_uid = ?", (session_uid,)).fetchone()
        is not None
    )


def file_already_ingested(conn: sqlite3.Connection, path: Path, sha256: str) -> bool:
    row = conn.execute(
        "SELECT sha256, status FROM ingested_files WHERE path = ?", (str(path),)
    ).fetchone()
    return row is not None and row["sha256"] == sha256 and row["status"] == "ok"


def record_skipped_file(conn: sqlite3.Connection, path: Path, sha256: str, reason: str) -> None:
    conn.execute(
        """INSERT INTO ingested_files (path, sha256, session_uid, status, reason, ingested_at)
           VALUES (?, ?, NULL, 'skipped', ?, ?)
           ON CONFLICT(path) DO UPDATE SET
             sha256=excluded.sha256, status='skipped', reason=excluded.reason,
             ingested_at=excluded.ingested_at""",
        (str(path), sha256, reason, _now()),
    )
    conn.commit()


def record_duplicate_file(
    conn: sqlite3.Connection, path: Path, sha256: str, session_uid: str
) -> None:
    """File diverso, seduta gia' nel DB: si registra per non riaprirlo piu'."""
    conn.execute(
        """INSERT INTO ingested_files (path, sha256, session_uid, status, reason, ingested_at)
           VALUES (?, ?, ?, 'ok', ?, ?)
           ON CONFLICT(path) DO UPDATE SET
             sha256=excluded.sha256, session_uid=excluded.session_uid,
             status='ok', reason=excluded.reason, ingested_at=excluded.ingested_at""",
        (str(path), sha256, session_uid, f"duplicato di {session_uid}", _now()),
    )
    conn.commit()


def store_activity(conn: sqlite3.Connection, act: ParsedActivity) -> tuple[int, bool]:
    """Salva (o rimpiazza) una seduta. Ritorna `(session_id, era_gia_presente)`.

    Le serie e i campioni FC della seduta vengono riscritti per intero: e' cosi'
    che `--force` resta idempotente. Le correzioni manuali non vengono toccate,
    perche' non dipendono dagli id delle righe.
    """
    ses = act.session
    local = ses.start_time_local
    iso_year, iso_week = (local.isocalendar()[:2] if local else (None, None))
    existed = session_exists(conn, act.session_uid)

    conn.execute(
        """INSERT INTO sessions (
               session_uid, activity_type, garmin_activity_id, sport, sub_sport,
               start_time_utc, start_time_local, local_date, iso_year, iso_week,
               utc_offset_s, total_elapsed_s, total_timer_s, avg_hr, max_hr, calories,
               total_training_effect, sport_profile_name, workout_name,
               device_manufacturer, device_product, device_serial,
               source_path, source_sha256, ingested_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(session_uid) DO UPDATE SET
               sport=excluded.sport, sub_sport=excluded.sub_sport,
               start_time_utc=excluded.start_time_utc,
               start_time_local=excluded.start_time_local,
               local_date=excluded.local_date, iso_year=excluded.iso_year,
               iso_week=excluded.iso_week, utc_offset_s=excluded.utc_offset_s,
               total_elapsed_s=excluded.total_elapsed_s,
               total_timer_s=excluded.total_timer_s, avg_hr=excluded.avg_hr,
               max_hr=excluded.max_hr, calories=excluded.calories,
               total_training_effect=excluded.total_training_effect,
               sport_profile_name=excluded.sport_profile_name,
               workout_name=excluded.workout_name,
               device_manufacturer=excluded.device_manufacturer,
               device_product=excluded.device_product,
               device_serial=excluded.device_serial,
               source_path=excluded.source_path, source_sha256=excluded.source_sha256,
               ingested_at=excluded.ingested_at""",
        (
            act.session_uid,
            "strength",
            act.garmin_activity_id,
            ses.sport,
            ses.sub_sport,
            _iso(ses.start_time),
            _iso(local),
            local.date().isoformat() if local else None,
            iso_year,
            iso_week,
            ses.utc_offset_s,
            ses.total_elapsed_s,
            ses.total_timer_s,
            ses.avg_hr,
            ses.max_hr,
            ses.calories,
            ses.total_training_effect,
            ses.sport_profile_name,
            ses.workout_name,
            act.device.manufacturer,
            act.device.product,
            act.device.serial_number,
            str(act.source_path),
            act.file_sha256,
            _now(),
        ),
    )
    session_id = conn.execute(
        "SELECT id FROM sessions WHERE session_uid = ?", (act.session_uid,)
    ).fetchone()["id"]

    conn.execute("DELETE FROM sets WHERE session_id = ?", (session_id,))
    conn.executemany(
        """INSERT INTO sets (
               session_id, set_index, order_in_session, set_type, start_time_utc,
               duration_s, reps, weight_kg, weight_display_unit, planned_reps,
               planned_weight_kg, wkt_step_index, raw_exercise_key,
               raw_exercise_label, category_raw, subcategory_raw)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                session_id,
                s.index,
                order,
                s.set_type,
                _iso(s.start_time),
                s.duration_s,
                s.repetitions,
                s.weight_kg,
                s.weight_display_unit,
                s.planned_reps,
                s.planned_weight_kg,
                s.wkt_step_index,
                s.exercise_key,
                s.exercise_label,
                json.dumps([c for c in s.category_raw]),
                json.dumps([c for c in s.subcategory_raw]),
            )
            for order, s in enumerate(act.sets)
        ],
    )

    conn.execute("DELETE FROM hr_samples WHERE session_id = ?", (session_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO hr_samples (session_id, ts_utc, bpm) VALUES (?,?,?)",
        [(session_id, _iso(h.timestamp), h.bpm) for h in act.hr_samples],
    )

    conn.execute(
        """INSERT INTO ingested_files (path, sha256, session_uid, status, reason, ingested_at)
           VALUES (?, ?, ?, 'ok', NULL, ?)
           ON CONFLICT(path) DO UPDATE SET
             sha256=excluded.sha256, session_uid=excluded.session_uid,
             status='ok', reason=NULL, ingested_at=excluded.ingested_at""",
        (str(act.source_path), act.file_sha256, act.session_uid, _now()),
    )
    conn.commit()
    return session_id, existed


def refresh_exercise_map(conn: sqlite3.Connection, entries: Iterable[dict[str, Any]]) -> int:
    """Riscrive `exercise_map` dal YAML. E' una proiezione, non una fonte."""
    rows = [
        (
            e["raw_key"],
            e["exercise_name"],
            e.get("primary_group"),
            json.dumps(e.get("secondary_groups") or [], ensure_ascii=False),
            _now(),
        )
        for e in entries
    ]
    conn.execute("DELETE FROM exercise_map")
    conn.executemany(
        """INSERT INTO exercise_map (raw_key, exercise_name, primary_group,
                                     secondary_groups, updated_at)
           VALUES (?,?,?,?,?)""",
        rows,
    )
    conn.commit()
    return len(rows)


def add_correction(
    conn: sqlite3.Connection,
    set_id: int,
    *,
    reps: int | None = None,
    weight_kg: float | None = None,
    exercise_key: str | None = None,
    note: str | None = None,
) -> int:
    """Registra un override manuale su una serie. I dati grezzi restano.

    `set_id` e' l'id comodo mostrato in dashboard e da `stats`; internamente
    si salva la coppia stabile (session_uid, set_index).
    """
    row = conn.execute(
        """SELECT ses.session_uid AS uid, s.set_index AS idx
           FROM sets s JOIN sessions ses ON ses.id = s.session_id
           WHERE s.id = ?""",
        (set_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"nessuna serie con id {set_id}")
    if reps is None and weight_kg is None and exercise_key is None:
        raise ValueError("una correzione deve cambiare almeno reps, peso o esercizio")
    cur = conn.execute(
        """INSERT INTO corrections (session_uid, set_index, reps, weight_kg,
                                    exercise_key, note, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (row["uid"], row["idx"], reps, weight_kg, exercise_key, note, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def unmapped_raw_keys(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Chiavi grezze presenti nel DB e non ancora nel YAML, piu' frequenti prima."""
    return conn.execute(
        """SELECT raw_exercise_key AS raw_key,
                  MAX(raw_exercise_label) AS label,
                  COUNT(*) AS n_sets,
                  COUNT(DISTINCT session_id) AS n_sessions,
                  MIN(local_date) AS first_seen,
                  MAX(local_date) AS last_seen
           FROM v_sets
           WHERE set_type = 'active' AND unmapped = 1 AND raw_exercise_key IS NOT NULL
           GROUP BY raw_exercise_key
           ORDER BY n_sets DESC, raw_key"""
    ).fetchall()
