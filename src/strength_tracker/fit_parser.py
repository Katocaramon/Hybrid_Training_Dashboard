"""FIT -> dataclass.

Legge i messaggi `set`, `session`, `record`, `exercise_title` e `workout_step`
da un file .fit di Strength Training. Il file sorgente viene solo letto: mai
modificato, mai spostato.

Note sulla struttura reale (Epix Pro Gen 2, firmware 2026, export Garmin
Connect `<id>_ACTIVITY.fit`) — verificata su file reali, non assunta:

* `set.category` e `set.category_subtype` sono **array** (tipicamente 3 slot,
  ripetuti o `None`): un set puo' dichiarare piu' categorie. Prendiamo le
  coppie posizione per posizione e usiamo la prima valida come chiave.
* Il catalogo Garmin e' chiuso e numerico: `category=21, subtype=42`. Il
  profilo FIT incluso in fitdecode contiene gli enum completi (53 categorie,
  51 cataloghi di nomi), quindi risolviamo la coppia in uno slug stabile tipo
  `pull_up/band_assisted_pull_up`. Se la categoria o l'indice non sono nel
  profilo teniamo il numero grezzo (`pull_up/42`, `250/7`): niente crash e
  niente nomi inventati.
* I file contengono spesso anche messaggi `exercise_title`, che sono una
  tabella (category, name) -> etichetta testuale scritta dall'orologio stesso.
  La usiamo come etichetta leggibile quando c'e'.
* `set.timestamp` e' costante e pari all'inizio della sessione: l'orario vero
  della serie e' `set.start_time`.
* `set.wkt_step_index` punta a `workout_step.message_index`: da li' si leggono
  le ripetizioni e il peso *pianificati* dall'allenamento strutturato. Sono
  valori pianificati, non eseguiti, e restano separati da quelli reali.
* **`workout_step.notes` porta il nome vero degli esercizi fuori catalogo.**
  Un Copenhagen plank viene registrato come `plank/side_plank` (il catalogo
  Garmin non ne ha uno suo) ma lo step dell'allenamento porta la nota
  "Copenhagen plank". La stessa chiave grezza puo' quindi voler dire due
  esercizi diversi in due sedute diverse: la nota e' l'unica cosa che li
  distingue, e la mappatura sa qualificarci sopra.
* Non esiste un campo `activity_id` dentro il FIT: l'id numerico di Garmin
  Connect sta solo nel nome del file esportato.
* `session.total_timer_time` e' il tempo attivo; non c'e' un campo dedicato.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import fitdecode
from fitdecode.profile import FIELD_TYPES

log = logging.getLogger(__name__)

#: Estensioni considerate file FIT durante la scansione ricorsiva.
FIT_SUFFIXES = {".fit"}

#: Nome file dell'export Garmin Connect: `<activity_id>_ACTIVITY.fit`.
_ACTIVITY_ID_RE = re.compile(r"(\d{6,})[_-]?ACTIVITY", re.IGNORECASE)

_EXERCISE_CATEGORY = FIELD_TYPES["exercise_category"].enum


class FitSkipped(Exception):
    """Il file non e' utilizzabile: va saltato con un warning, non fatale."""


@dataclass(frozen=True)
class ExerciseTitle:
    """Voce della tabella (categoria, nome) -> etichetta scritta nel file."""

    category_raw: int | None
    name_raw: int | None
    label: str


@dataclass(frozen=True)
class WorkoutStep:
    """Passo dell'allenamento strutturato: valori *pianificati*."""

    index: int
    exercise_key: str | None
    planned_reps: int | None
    planned_weight_kg: float | None
    duration_type: str | None
    intensity: str | None
    note: str | None


@dataclass(frozen=True)
class SetRecord:
    """Una serie (o una pausa) come registrata dall'orologio."""

    index: int
    set_type: str | None
    start_time: datetime | None
    duration_s: float | None
    repetitions: int | None
    weight_kg: float | None
    weight_display_unit: str | None
    category_raw: tuple[int | None, ...]
    subcategory_raw: tuple[int | None, ...]
    exercise_key: str | None
    exercise_label: str | None
    wkt_step_index: int | None
    planned_reps: int | None = None
    planned_weight_kg: float | None = None
    step_note: str | None = None

    @property
    def is_active(self) -> bool:
        return self.set_type == "active"

    @property
    def end_time(self) -> datetime | None:
        if self.start_time is None or self.duration_s is None:
            return None
        return self.start_time + timedelta(seconds=self.duration_s)

    @property
    def volume_kg(self) -> float | None:
        """Tonnellaggio della serie. `None` se manca un dato: non si stima."""
        if self.repetitions is None or self.weight_kg is None:
            return None
        return self.weight_kg * self.repetitions


@dataclass(frozen=True)
class HrSample:
    timestamp: datetime
    bpm: int


@dataclass(frozen=True)
class SessionRecord:
    start_time: datetime | None
    total_elapsed_s: float | None
    total_timer_s: float | None
    avg_hr: int | None
    max_hr: int | None
    calories: int | None
    sport: str | None
    sub_sport: str | None
    sport_profile_name: str | None
    workout_name: str | None
    total_training_effect: float | None
    utc_offset_s: int | None

    @property
    def start_time_local(self) -> datetime | None:
        """Ora locale dell'orologio: e' quella che definisce la data della seduta."""
        if self.start_time is None:
            return None
        if self.utc_offset_s is None:
            return self.start_time
        return self.start_time.astimezone(timezone(timedelta(seconds=self.utc_offset_s)))


@dataclass(frozen=True)
class DeviceInfo:
    manufacturer: str | None
    product: str | None
    serial_number: int | None
    time_created: datetime | None


@dataclass
class ParsedActivity:
    source_path: Path
    file_sha256: str
    garmin_activity_id: str | None
    device: DeviceInfo
    session: SessionRecord
    sets: list[SetRecord] = field(default_factory=list)
    hr_samples: list[HrSample] = field(default_factory=list)
    exercise_titles: list[ExerciseTitle] = field(default_factory=list)
    workout_steps: list[WorkoutStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def session_uid(self) -> str:
        """Identita' stabile della seduta, per un'ingestione idempotente.

        In ordine di preferenza: id di Garmin Connect (dal nome del file),
        poi seriale del dispositivo + istante di creazione (stabile anche se
        il file viene riesportato e i byte cambiano), infine hash del
        contenuto.
        """
        if self.garmin_activity_id:
            return f"garmin:{self.garmin_activity_id}"
        if self.device.serial_number and self.device.time_created:
            stamp = int(self.device.time_created.timestamp())
            return f"device:{self.device.serial_number}:{stamp}"
        return f"sha256:{self.file_sha256}"

    @property
    def active_sets(self) -> list[SetRecord]:
        return [s for s in self.sets if s.is_active]


# --------------------------------------------------------------------------
# utilita' di basso livello
# --------------------------------------------------------------------------


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def iter_fit_files(root: Path) -> list[Path]:
    """File .fit sotto `root` (file singolo o cartella, ricorsiva), ordinati."""
    root = Path(root)
    if root.is_file():
        return [root] if root.suffix.lower() in FIT_SUFFIXES else []
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in FIT_SUFFIXES)


def activity_id_from_name(path: Path) -> str | None:
    match = _ACTIVITY_ID_RE.search(path.name)
    return match.group(1) if match else None


def _as_tuple(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


def _first_not_none(values: Iterable[Any]) -> Any:
    for v in values:
        if v is not None:
            return v
    return None


def exercise_key(category_raw: int | None, name_raw: int | None) -> str | None:
    """Slug stabile per una coppia (categoria, nome) del catalogo Garmin.

    Risolve i numeri con gli enum del profilo FIT quando possibile, altrimenti
    tiene il numero grezzo. Non inventa mai un nome:
    `21/42` -> `pull_up/band_assisted_pull_up`, `250/7` -> `250/7`.
    """
    if category_raw is None:
        return None
    cat_name = _EXERCISE_CATEGORY.get(category_raw)
    cat = cat_name or str(category_raw)
    if name_raw is None:
        return cat
    sub = None
    if cat_name:
        enum = FIELD_TYPES.get(f"{cat_name}_exercise_name")
        if enum is not None:
            sub = enum.enum.get(name_raw)
    return f"{cat}/{sub or name_raw}"


def _read_frames(path: Path) -> Iterator[fitdecode.FitDataMessage]:
    """Itera i messaggi dati tollerando CRC non validi (file troncati)."""
    with fitdecode.FitReader(
        str(path),
        check_crc=fitdecode.CrcCheck.WARN,
        keep_raw_chunks=False,
    ) as reader:
        for frame in reader:
            if frame.frame_type == fitdecode.FIT_FRAME_DATA:
                yield frame


def _fields(frame: fitdecode.FitDataMessage) -> dict[str, tuple[Any, Any]]:
    """{nome campo: (valore interpretato, valore grezzo)}."""
    return {f.name: (f.value, f.raw_value) for f in frame.fields}


def _val(d: dict[str, tuple[Any, Any]], name: str) -> Any:
    return d.get(name, (None, None))[0]


def _raw(d: dict[str, tuple[Any, Any]], name: str) -> Any:
    return d.get(name, (None, None))[1]


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def parse_file(path: Path) -> ParsedActivity:
    """Legge un file .fit di forza.

    Solleva `FitSkipped` se il file e' illeggibile o non e' una sessione di
    forza; il chiamante decide se e' fatale (di norma no: si salta con warning).
    """
    path = Path(path)
    warnings: list[str] = []

    titles: dict[tuple[int | None, int | None], str] = {}
    title_list: list[ExerciseTitle] = []
    steps: dict[int, WorkoutStep] = {}
    raw_sets: list[dict[str, tuple[Any, Any]]] = []
    hr_samples: list[HrSample] = []
    session_fields: dict[str, tuple[Any, Any]] | None = None
    file_id_fields: dict[str, tuple[Any, Any]] | None = None
    sport_fields: dict[str, tuple[Any, Any]] | None = None
    workout_fields: dict[str, tuple[Any, Any]] | None = None
    activity_fields: dict[str, tuple[Any, Any]] | None = None

    try:
        for frame in _read_frames(path):
            name = frame.name
            if name == "set":
                raw_sets.append(_fields(frame))
            elif name == "record":
                d = _fields(frame)
                ts, bpm = _val(d, "timestamp"), _val(d, "heart_rate")
                if ts is not None and bpm is not None:
                    hr_samples.append(HrSample(ts, int(bpm)))
            elif name == "exercise_title":
                d = _fields(frame)
                key = (_raw(d, "exercise_category"), _raw(d, "exercise_name"))
                label = _val(d, "wkt_step_name")
                if label:
                    titles[key] = label
                    title_list.append(ExerciseTitle(key[0], key[1], label))
            elif name == "workout_step":
                d = _fields(frame)
                idx = _val(d, "message_index")
                if idx is not None:
                    steps[int(idx)] = WorkoutStep(
                        index=int(idx),
                        exercise_key=exercise_key(
                            _raw(d, "exercise_category"), _raw(d, "exercise_name")
                        ),
                        planned_reps=_val(d, "duration_reps"),
                        planned_weight_kg=_val(d, "exercise_weight"),
                        duration_type=_val(d, "duration_type"),
                        intensity=_val(d, "intensity"),
                        note=_val(d, "notes"),
                    )
            elif name == "session" and session_fields is None:
                session_fields = _fields(frame)
            elif name == "file_id" and file_id_fields is None:
                file_id_fields = _fields(frame)
            elif name == "sport" and sport_fields is None:
                sport_fields = _fields(frame)
            elif name == "workout" and workout_fields is None:
                workout_fields = _fields(frame)
            elif name == "activity" and activity_fields is None:
                activity_fields = _fields(frame)
    except Exception as exc:  # fitdecode alza FitError, EOFError, struct.error...
        raise FitSkipped(f"file .fit illeggibile o corrotto ({exc.__class__.__name__}: {exc})") from exc

    if file_id_fields is not None:
        file_type = _val(file_id_fields, "type")
        if file_type not in (None, "activity"):
            raise FitSkipped(f"non e' un file di attivita' (file_id.type={file_type!r})")

    sub_sport = _val(session_fields or {}, "sub_sport") or _val(sport_fields or {}, "sub_sport")
    sport = _val(session_fields or {}, "sport") or _val(sport_fields or {}, "sport")
    if not raw_sets:
        raise FitSkipped(
            f"nessun messaggio 'set': non e' una sessione di forza (sport={sport!r}, sub_sport={sub_sport!r})"
        )
    if sub_sport not in (None, "strength_training"):
        warnings.append(
            f"sub_sport={sub_sport!r} non e' 'strength_training' ma il file contiene "
            f"{len(raw_sets)} messaggi 'set': lo tratto comunque come seduta di forza"
        )

    # Offset UTC dell'orologio: serve per datare la seduta nel fuso locale.
    utc_offset_s = None
    if activity_fields is not None:
        ts, local = _val(activity_fields, "timestamp"), _val(activity_fields, "local_timestamp")
        if ts is not None and local is not None:
            utc_offset_s = int(round((local - ts).total_seconds()))

    sf = session_fields or {}
    session = SessionRecord(
        start_time=_val(sf, "start_time"),
        total_elapsed_s=_val(sf, "total_elapsed_time"),
        total_timer_s=_val(sf, "total_timer_time"),
        avg_hr=_val(sf, "avg_heart_rate"),
        max_hr=_val(sf, "max_heart_rate"),
        calories=_val(sf, "total_calories"),
        sport=sport,
        sub_sport=sub_sport,
        sport_profile_name=_val(sf, "sport_profile_name") or _val(sport_fields or {}, "name"),
        workout_name=_val(workout_fields or {}, "wkt_name"),
        total_training_effect=_val(sf, "total_training_effect"),
        utc_offset_s=utc_offset_s,
    )
    if session_fields is None:
        warnings.append("nessun messaggio 'session': i dati di riepilogo della seduta restano nulli")

    fid = file_id_fields or {}
    device = DeviceInfo(
        manufacturer=_val(fid, "manufacturer"),
        product=_val(fid, "garmin_product") or _val(fid, "product"),
        serial_number=_val(fid, "serial_number"),
        time_created=_val(fid, "time_created"),
    )

    sets = [_build_set(d, i, titles, steps) for i, d in enumerate(raw_sets)]

    unresolved = sorted({s.exercise_key for s in sets if s.is_active and s.exercise_key is None})
    if unresolved:
        warnings.append(f"{len(unresolved)} serie attive senza categoria esercizio")

    activity = ParsedActivity(
        source_path=path,
        file_sha256=sha256_file(path),
        garmin_activity_id=activity_id_from_name(path),
        device=device,
        session=session,
        sets=sets,
        hr_samples=hr_samples,
        exercise_titles=title_list,
        workout_steps=sorted(steps.values(), key=lambda s: s.index),
        warnings=warnings,
    )
    return activity


def _build_set(
    d: dict[str, tuple[Any, Any]],
    fallback_index: int,
    titles: dict[tuple[int | None, int | None], str],
    steps: dict[int, WorkoutStep],
) -> SetRecord:
    cats = _as_tuple(_raw(d, "category"))
    subs = _as_tuple(_raw(d, "category_subtype"))
    # Gli array hanno la stessa lunghezza e vanno letti a coppie; si usa la
    # prima coppia valida, ma si conserva tutto (esercizi multi-categoria).
    pairs = list(zip(cats, subs + (None,) * max(0, len(cats) - len(subs))))
    cat_raw = _first_not_none(cats)
    sub_raw = next((s for c, s in pairs if c == cat_raw and s is not None), None)

    index = _val(d, "message_index")
    step_index = _val(d, "wkt_step_index")
    step = steps.get(int(step_index)) if step_index is not None else None

    return SetRecord(
        index=int(index) if index is not None else fallback_index,
        set_type=_val(d, "set_type"),
        start_time=_val(d, "start_time"),
        duration_s=_val(d, "duration"),
        repetitions=_val(d, "repetitions"),
        weight_kg=_val(d, "weight"),
        weight_display_unit=_val(d, "weight_display_unit"),
        category_raw=cats,
        subcategory_raw=subs,
        exercise_key=exercise_key(cat_raw, sub_raw),
        exercise_label=titles.get((cat_raw, sub_raw)),
        wkt_step_index=int(step_index) if step_index is not None else None,
        planned_reps=step.planned_reps if step else None,
        planned_weight_kg=step.planned_weight_kg if step else None,
        step_note=step.note if step else None,
    )


def parse_paths(paths: Iterable[Path]) -> Iterator[tuple[Path, ParsedActivity | None, str | None]]:
    """Parsa piu' file: `(path, attivita', motivo_dello_skip)`.

    Un file rotto non fa fallire il batch: viene restituito con l'attivita' a
    `None` e il motivo, e chi chiama logga il warning.
    """
    for path in paths:
        try:
            yield path, parse_file(path), None
        except FitSkipped as exc:
            log.warning("salto %s: %s", path, exc)
            yield path, None, str(exc)


# --------------------------------------------------------------------------
# ispezione (dump JSON)
# --------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()  # si conserva il fuso, UTC o locale che sia
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # I file veri contengono tipi inattesi (orari di sveglia, enum esotici):
    # meglio una stringa che un dump che esplode a meta'.
    return str(value)


def inspect_file(
    path: Path,
    *,
    raw_messages: bool = False,
    raw_limit: int = 3,
    include_hr: bool = False,
) -> dict[str, Any]:
    """Dump ispezionabile della struttura reale di un file .fit.

    Serve a guardare cosa scrive davvero l'orologio prima di fidarsi dello
    schema: i campi variano fra modelli e versioni firmware.
    """
    path = Path(path)
    counts: dict[str, int] = {}
    raw_dump: dict[str, list[dict[str, Any]]] = {}

    for frame in _read_frames(path):
        counts[frame.name] = counts.get(frame.name, 0) + 1
        if raw_messages and len(raw_dump.setdefault(frame.name, [])) < raw_limit:
            raw_dump[frame.name].append(
                {
                    f.name: {
                        "value": _jsonable(f.value),
                        "raw": _jsonable(f.raw_value),
                        "units": f.units,
                    }
                    for f in frame.fields
                }
            )

    out: dict[str, Any] = {
        "file": {
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
            "garmin_activity_id": activity_id_from_name(path),
        },
        "message_counts": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
    }

    try:
        act = parse_file(path)
    except FitSkipped as exc:
        out["skipped"] = str(exc)
        if raw_messages:
            out["raw_messages"] = raw_dump
        return out

    out["session_uid"] = act.session_uid
    out["device"] = {
        "manufacturer": act.device.manufacturer,
        "product": act.device.product,
        "serial_number": act.device.serial_number,
        "time_created": _jsonable(act.device.time_created),
    }
    out["session"] = {
        "start_time_utc": _jsonable(act.session.start_time),
        "start_time_local": _jsonable(act.session.start_time_local),
        "utc_offset_s": act.session.utc_offset_s,
        "total_elapsed_s": act.session.total_elapsed_s,
        "total_timer_s": act.session.total_timer_s,
        "avg_hr": act.session.avg_hr,
        "max_hr": act.session.max_hr,
        "calories": act.session.calories,
        "sport": act.session.sport,
        "sub_sport": act.session.sub_sport,
        "sport_profile_name": act.session.sport_profile_name,
        "workout_name": act.session.workout_name,
        "total_training_effect": act.session.total_training_effect,
    }
    out["exercise_titles"] = [
        {"category_raw": t.category_raw, "name_raw": t.name_raw, "label": t.label}
        for t in act.exercise_titles
    ]
    out["workout_steps"] = [
        {
            "index": s.index,
            "exercise_key": s.exercise_key,
            "planned_reps": s.planned_reps,
            "planned_weight_kg": s.planned_weight_kg,
            "duration_type": s.duration_type,
            "intensity": s.intensity,
            "note": s.note,
        }
        for s in act.workout_steps
    ]
    out["sets"] = [
        {
            "index": s.index,
            "set_type": s.set_type,
            "start_time_utc": _jsonable(s.start_time),
            "duration_s": s.duration_s,
            "repetitions": s.repetitions,
            "weight_kg": s.weight_kg,
            "weight_display_unit": s.weight_display_unit,
            "category_raw": _jsonable(s.category_raw),
            "subcategory_raw": _jsonable(s.subcategory_raw),
            "exercise_key": s.exercise_key,
            "exercise_label": s.exercise_label,
            "wkt_step_index": s.wkt_step_index,
            "planned_reps": s.planned_reps,
            "planned_weight_kg": s.planned_weight_kg,
            "step_note": s.step_note,
            "volume_kg": s.volume_kg,
        }
        for s in act.sets
    ]

    hr = [s.bpm for s in act.hr_samples]
    intervals = {
        round((b.timestamp - a.timestamp).total_seconds(), 3)
        for a, b in zip(act.hr_samples, act.hr_samples[1:])
    }
    out["hr"] = {
        "samples": len(hr),
        "first_utc": _jsonable(act.hr_samples[0].timestamp) if hr else None,
        "last_utc": _jsonable(act.hr_samples[-1].timestamp) if hr else None,
        "min_bpm": min(hr) if hr else None,
        "max_bpm": max(hr) if hr else None,
        "avg_bpm": round(sum(hr) / len(hr), 1) if hr else None,
        "sample_interval_s": sorted(intervals)[:5] if intervals else [],
    }
    if include_hr:
        out["hr"]["samples_detail"] = [
            {"t": _jsonable(s.timestamp), "bpm": s.bpm} for s in act.hr_samples
        ]

    active = act.active_sets
    out["summary"] = {
        "sets_total": len(act.sets),
        "sets_active": len(active),
        "sets_rest": len(act.sets) - len(active),
        "sets_with_reps": sum(1 for s in active if s.repetitions is not None),
        "sets_with_weight": sum(1 for s in active if s.weight_kg is not None),
        "distinct_exercises": sorted({s.exercise_key for s in active if s.exercise_key}),
        "sets_without_exercise": sum(1 for s in active if s.exercise_key is None),
        "step_notes": sorted({s.step_note for s in active if s.step_note}),
    }
    out["warnings"] = act.warnings
    if raw_messages:
        out["raw_messages"] = raw_dump
    return out
