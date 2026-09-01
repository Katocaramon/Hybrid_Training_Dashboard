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

SCHEMA_VERSION = 3

# Migrazione 1: schema iniziale.
_MIGRATION_1 = """
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

# Migrazione 2: la nota dello step dell'allenamento.
# Serve perche' la stessa chiave grezza puo' voler dire esercizi diversi:
# un Copenhagen plank arriva come `plank/side_plank` (il catalogo Garmin non
# ne ha uno suo) e solo la nota "Copenhagen plank" lo distingue da un plank
# laterale vero. La mappatura puo' quindi qualificare una chiave con la nota.
_MIGRATION_2 = """
ALTER TABLE sets ADD COLUMN wkt_step_note TEXT;

DROP TABLE IF EXISTS exercise_map;
CREATE TABLE exercise_map (
    raw_key          TEXT NOT NULL,
    note             TEXT NOT NULL DEFAULT '',   -- '' = vale per qualunque nota
    exercise_name    TEXT NOT NULL,
    primary_group    TEXT,
    secondary_groups TEXT,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (raw_key, note)
);
"""

# Migrazione 3: tempi in epoch, peso corporeo e modo del carico.
#
# Gli epoch servono per incrociare le serie con i campioni di FC in SQL: gli
# ISO con fuso orario si confrontano male fra loro, un numero no.
#
# `weight_mode` esiste perche' non tutti i pesi sono carichi. Alle trazioni
# assistite il peso registrato e' l'*aiuto* della macchina: piu' e' alto, piu'
# la serie e' facile. Moltiplicarlo per le ripetizioni darebbe un tonnellaggio
# non solo sbagliato ma rovesciato di segno.
_MIGRATION_3 = """
ALTER TABLE sets ADD COLUMN start_epoch REAL;
ALTER TABLE sets ADD COLUMN end_epoch REAL;
ALTER TABLE hr_samples ADD COLUMN epoch REAL;
ALTER TABLE sessions ADD COLUMN body_weight_kg REAL;
ALTER TABLE exercise_map ADD COLUMN weight_mode TEXT NOT NULL DEFAULT 'carico';

UPDATE sets SET
    start_epoch = CAST(strftime('%s', start_time_utc) AS REAL),
    end_epoch = CAST(strftime('%s', start_time_utc) AS REAL) + COALESCE(duration_s, 0)
WHERE start_time_utc IS NOT NULL;
UPDATE hr_samples SET epoch = CAST(strftime('%s', ts_utc) AS REAL);

CREATE INDEX IF NOT EXISTS idx_hr_epoch ON hr_samples (session_id, epoch);
CREATE INDEX IF NOT EXISTS idx_sets_epoch ON sets (session_id, start_epoch);
"""

_MIGRATIONS = [_MIGRATION_1, _MIGRATION_2, _MIGRATION_3]

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
    s.wkt_step_note,
    COALESCE(ms.exercise_name, mg.exercise_name,
             s.raw_exercise_label, s.raw_exercise_key)      AS exercise_name,
    COALESCE(ms.primary_group, mg.primary_group)            AS muscle_group,
    COALESCE(ms.secondary_groups, mg.secondary_groups)      AS secondary_groups,
    COALESCE(ms.weight_mode, mg.weight_mode, 'carico')      AS weight_mode,
    CASE WHEN ms.raw_key IS NULL AND mg.raw_key IS NULL THEN 1 ELSE 0 END AS unmapped,
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
    -- Carico effettivo sollevato dalla serie. Dipende dal modo:
    --   carico       -> il peso registrato
    --   assistenza   -> peso corporeo meno l'aiuto della macchina/elastico
    --   corpo_libero -> il peso corporeo
    -- Per assistenza e corpo libero e' una stima esplicita, segnalata da
    -- `carico_stimato`: senza peso corporeo nel file resta NULL.
    CASE COALESCE(ms.weight_mode, mg.weight_mode, 'carico')
        WHEN 'assistenza' THEN
            CASE WHEN ses.body_weight_kg IS NULL
                      OR COALESCE(c.weight_kg, s.weight_kg) IS NULL THEN NULL
                 ELSE ses.body_weight_kg - COALESCE(c.weight_kg, s.weight_kg) END
        WHEN 'corpo_libero' THEN ses.body_weight_kg
        ELSE COALESCE(c.weight_kg, s.weight_kg)
    END                                                     AS carico_effettivo_kg,
    CASE WHEN COALESCE(ms.weight_mode, mg.weight_mode, 'carico') = 'carico'
         THEN 0 ELSE 1 END                                  AS carico_stimato,
    -- Tonnellaggio: solo dove il peso e' davvero un carico esterno. Un peso
    -- che indica l'assistenza non si moltiplica per le ripetizioni, e il
    -- corpo libero non e' un carico sollevato dal bilanciere.
    CASE
        WHEN COALESCE(ms.weight_mode, mg.weight_mode, 'carico') <> 'carico' THEN NULL
        WHEN COALESCE(c.reps, s.reps) IS NULL THEN NULL
        WHEN COALESCE(c.weight_kg, s.weight_kg) IS NULL THEN NULL
        ELSE COALESCE(c.reps, s.reps) * COALESCE(c.weight_kg, s.weight_kg)
    END                                                     AS volume_kg,
    -- Tonnellaggio stimato sul carico effettivo: utile per le trazioni
    -- assistite, ma tenuto separato perche' poggia sul peso corporeo.
    CASE
        WHEN COALESCE(ms.weight_mode, mg.weight_mode, 'carico') = 'carico' THEN NULL
        WHEN COALESCE(c.reps, s.reps) IS NULL THEN NULL
        WHEN COALESCE(ms.weight_mode, mg.weight_mode) = 'assistenza'
             AND (ses.body_weight_kg IS NULL
                  OR COALESCE(c.weight_kg, s.weight_kg) IS NULL) THEN NULL
        WHEN ses.body_weight_kg IS NULL THEN NULL
        ELSE COALESCE(c.reps, s.reps) * (
            CASE COALESCE(ms.weight_mode, mg.weight_mode)
                WHEN 'assistenza' THEN ses.body_weight_kg - COALESCE(c.weight_kg, s.weight_kg)
                ELSE ses.body_weight_kg
            END)
    END                                                     AS volume_stimato_kg,
    s.start_epoch,
    s.end_epoch
FROM sets s
JOIN sessions ses ON ses.id = s.session_id
LEFT JOIN v_corrections_effective c
       ON c.session_uid = ses.session_uid AND c.set_index = s.set_index
-- Due join sulla mappatura: quella qualificata dalla nota dello step vince
-- su quella generica. E' cosi' che un Copenhagen plank si distingue da un
-- plank laterale, pur avendo la stessa chiave grezza.
LEFT JOIN exercise_map ms
       ON ms.raw_key = COALESCE(c.exercise_key, s.raw_exercise_key)
      AND ms.note <> ''
      AND ms.note = LOWER(TRIM(COALESCE(s.wkt_step_note, '')))
LEFT JOIN exercise_map mg
       ON mg.raw_key = COALESCE(c.exercise_key, s.raw_exercise_key)
      AND mg.note = '';

-- FC media e massima dentro la finestra temporale di ogni serie.
-- Il confronto e' su epoch e non su stringhe ISO: i fusi orari rendono il
-- confronto testuale inaffidabile.
DROP VIEW IF EXISTS v_set_hr;
CREATE VIEW v_set_hr AS
SELECT s.id           AS set_id,
       s.session_id,
       COUNT(h.bpm)   AS n_campioni,
       ROUND(AVG(h.bpm), 1) AS avg_bpm,
       MAX(h.bpm)     AS max_bpm
FROM sets s
JOIN hr_samples h
  ON h.session_id = s.session_id
 AND h.epoch >= s.start_epoch
 AND h.epoch < s.end_epoch
WHERE s.start_epoch IS NOT NULL AND s.end_epoch > s.start_epoch
GROUP BY s.id;
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
    """Applica le migrazioni mancanti. Idempotente: si richiama a ogni avvio.

    Ogni migrazione ha un numero e viene applicata una volta sola; le viste
    invece si ricreano sempre, cosi' cambiarle non richiede una migrazione.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    current = row["v"] if row and row["v"] is not None else 0
    for version, script in enumerate(_MIGRATIONS, start=1):
        if version <= current:
            continue
        conn.executescript(script)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, _now()),
        )
    conn.executescript(_VIEWS)
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
               total_training_effect, body_weight_kg, sport_profile_name, workout_name,
               device_manufacturer, device_product, device_serial,
               source_path, source_sha256, ingested_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
               body_weight_kg=excluded.body_weight_kg,
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
            ses.body_weight_kg,
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
               planned_weight_kg, wkt_step_index, wkt_step_note, raw_exercise_key,
               raw_exercise_label, category_raw, subcategory_raw,
               start_epoch, end_epoch)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                s.step_note,
                s.exercise_key,
                s.exercise_label,
                json.dumps([c for c in s.category_raw]),
                json.dumps([c for c in s.subcategory_raw]),
                s.start_time.timestamp() if s.start_time else None,
                s.end_time.timestamp() if s.end_time else None,
            )
            for order, s in enumerate(act.sets)
        ],
    )

    conn.execute("DELETE FROM hr_samples WHERE session_id = ?", (session_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO hr_samples (session_id, ts_utc, bpm, epoch) VALUES (?,?,?,?)",
        [(session_id, _iso(h.timestamp), h.bpm, h.timestamp.timestamp()) for h in act.hr_samples],
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
            (e.get("note") or "").strip().lower(),
            e["exercise_name"],
            e.get("primary_group"),
            json.dumps(e.get("secondary_groups") or [], ensure_ascii=False),
            e.get("weight_mode") or "carico",
            _now(),
        )
        for e in entries
    ]
    conn.execute("DELETE FROM exercise_map")
    conn.executemany(
        """INSERT INTO exercise_map (raw_key, note, exercise_name, primary_group,
                                     secondary_groups, weight_mode, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
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


def set_id_by_position(
    conn: sqlite3.Connection,
    local_date: str,
    n_serie_attiva: int,
    seduta: str | None = None,
) -> int:
    """Trova una serie per data e numero di serie *attiva*, come su Connect.

    Garmin Connect numera le serie attive da 1, pause escluse: e' il modo
    naturale di trascrivere i carichi guardando l'app. Se in quel giorno c'e'
    piu' di una seduta serve `seduta`, confrontata con il nome
    dell'allenamento o con il `session_uid`.
    """
    sessioni = conn.execute(
        """SELECT id, session_uid, workout_name FROM sessions
           WHERE local_date = ? ORDER BY start_time_utc""",
        (local_date,),
    ).fetchall()
    if not sessioni:
        raise KeyError(f"nessuna seduta il {local_date}")
    if seduta:
        sessioni = [
            r
            for r in sessioni
            if seduta.lower() in (r["workout_name"] or "").lower()
            or seduta.lower() in r["session_uid"].lower()
        ]
        if not sessioni:
            raise KeyError(f"nessuna seduta il {local_date} che corrisponda a {seduta!r}")
    if len(sessioni) > 1:
        nomi = ", ".join(r["workout_name"] or r["session_uid"] for r in sessioni)
        raise KeyError(
            f"{len(sessioni)} sedute il {local_date} ({nomi}): serve la colonna 'seduta'"
        )
    riga = conn.execute(
        """SELECT id FROM sets
           WHERE session_id = ? AND set_type = 'active'
           ORDER BY order_in_session
           LIMIT 1 OFFSET ?""",
        (sessioni[0]["id"], n_serie_attiva - 1),
    ).fetchone()
    if riga is None:
        raise KeyError(f"la seduta del {local_date} non ha una serie attiva n. {n_serie_attiva}")
    return int(riga["id"])


def unmapped_raw_keys(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Chiavi grezze presenti nel DB e non ancora nel YAML, piu' frequenti prima."""
    return conn.execute(
        """SELECT raw_exercise_key AS raw_key,
                  COALESCE(wkt_step_note, '') AS note,
                  MAX(raw_exercise_label) AS label,
                  COUNT(*) AS n_sets,
                  COUNT(DISTINCT session_id) AS n_sessions,
                  MIN(local_date) AS first_seen,
                  MAX(local_date) AS last_seen
           FROM v_sets
           WHERE set_type = 'active' AND unmapped = 1 AND raw_exercise_key IS NOT NULL
           GROUP BY raw_exercise_key, COALESCE(wkt_step_note, '')
           ORDER BY n_sets DESC, raw_key"""
    ).fetchall()
