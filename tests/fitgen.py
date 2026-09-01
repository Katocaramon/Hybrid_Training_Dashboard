"""Encoder FIT minimale per generare fixture di test *reali*.

I test non girano su mock: questo modulo scrive file .fit binari validi
(header, definition/data message, CRC come da specifica FIT) che vengono poi
letti dallo stesso `fitdecode` usato in produzione.

Uso:  python tests/fitgen.py    # rigenera tests/fixtures/*.fit
"""

from __future__ import annotations

import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Epoch FIT: 31/12/1989 00:00:00 UTC
FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)

# Tabella CRC-16 della specifica FIT.
_CRC_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)

# nome -> (byte del base type FIT, formato struct, dimensione, valore "invalid")
BASE_TYPES = {
    "enum": (0x00, "B", 1, 0xFF),
    "uint8": (0x02, "B", 1, 0xFF),
    "uint16": (0x84, "H", 2, 0xFFFF),
    "uint32": (0x86, "I", 4, 0xFFFFFFFF),
    "uint32z": (0x8C, "I", 4, 0x00000000),
    "string": (0x07, None, None, 0x00),
}


def crc16(data: bytes, crc: int = 0) -> int:
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc


def fit_timestamp(dt: datetime) -> int:
    return int((dt - FIT_EPOCH).total_seconds())


class FitWriter:
    """Scrive messaggi FIT riusando le definizioni gia' emesse."""

    def __init__(self) -> None:
        self.body = bytearray()
        self._layouts: dict[tuple, int] = {}
        self._next_local = 0

    def message(self, global_num: int, fields: list[tuple[int, str, object]]) -> None:
        """`fields`: lista di (field_def_num, base_type, valore).

        Il valore puo' essere `None` (si scrive il pattern invalid), una
        tupla/lista (array) o una stringa.
        """
        layout = tuple((num, bt, _slots(bt, val)) for num, bt, val in fields)
        key = (global_num, layout)
        local = self._layouts.get(key)
        if local is None:
            local = self._next_local % 16
            self._next_local += 1
            self._layouts[key] = local
            self._emit_definition(local, global_num, layout)
        self.body.append(local)
        for num, bt, val in fields:
            self.body += _encode(bt, val)

    def _emit_definition(self, local: int, global_num: int, layout: tuple) -> None:
        self.body.append(0x40 | local)
        self.body += struct.pack("<BBHB", 0, 0, global_num, len(layout))
        for num, bt, slots in layout:
            type_byte, _, size, _ = BASE_TYPES[bt]
            total = slots if bt == "string" else slots * size
            self.body += struct.pack("<BBB", num, total, type_byte)

    def to_bytes(self) -> bytes:
        header = struct.pack("<BBHI4s", 14, 0x20, 2140, len(self.body), b".FIT")
        header += struct.pack("<H", crc16(header))
        data = header + bytes(self.body)
        return data + struct.pack("<H", crc16(data))

    def write(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.to_bytes())
        return path


def _slots(base_type: str, value: object) -> int:
    if base_type == "string":
        return len(str(value or "").encode("utf-8")) + 1
    if isinstance(value, (list, tuple)):
        return len(value)
    return 1


def _encode(base_type: str, value: object) -> bytes:
    type_byte, fmt, size, invalid = BASE_TYPES[base_type]
    if base_type == "string":
        return str(value or "").encode("utf-8") + b"\x00"
    values = list(value) if isinstance(value, (list, tuple)) else [value]
    out = bytearray()
    for v in values:
        out += struct.pack("<" + fmt, invalid if v is None else int(v))
    return bytes(out)


# --- numeri di messaggio del profilo FIT usati qui -------------------------
MSG_FILE_ID = 0
MSG_SPORT = 12
MSG_SESSION = 18
MSG_RECORD = 20
MSG_WORKOUT_STEP = 27
MSG_ACTIVITY = 34
MSG_SET = 225
MSG_EXERCISE_TITLE = 264

WEIGHT_SCALE = 16  # set.weight: uint16 con scala 16 -> kg
STEP_WEIGHT_SCALE = 100  # workout_step.exercise_weight: scala 100 -> kg
DURATION_SCALE = 1000  # set.duration: uint32 con scala 1000 -> s


def _file_id(w: FitWriter, created: datetime, serial: int) -> None:
    w.message(
        MSG_FILE_ID,
        [
            (0, "enum", 4),  # type = activity
            (1, "uint16", 1),  # manufacturer = garmin
            (2, "uint16", 4314),  # garmin_product = epix_gen2_pro_51
            (3, "uint32z", serial),
            (4, "uint32", fit_timestamp(created)),
        ],
    )


def _sport(w: FitWriter, sport: int, sub_sport: int, name: str) -> None:
    w.message(MSG_SPORT, [(0, "enum", sport), (1, "enum", sub_sport), (3, "string", name)])


def _set(
    w: FitWriter,
    *,
    index: int,
    start: datetime,
    session_start: datetime,
    duration_s: float,
    set_type: int,
    category: tuple | None = None,
    subcategory: tuple | None = None,
    reps: int | None = None,
    weight_kg: float | None = None,
    wkt_step_index: int | None = None,
) -> None:
    w.message(
        MSG_SET,
        [
            (254, "uint32", fit_timestamp(session_start)),
            (0, "uint32", round(duration_s * DURATION_SCALE)),
            (3, "uint16", reps),
            (4, "uint16", None if weight_kg is None else round(weight_kg * WEIGHT_SCALE)),
            (5, "uint8", set_type),
            (6, "uint32", fit_timestamp(start)),
            (7, "uint16", category or (None, None, None)),
            (8, "uint16", subcategory or (None, None, None)),
            (9, "uint16", 1 if weight_kg is not None else None),  # kilogram
            (10, "uint16", index),
            (11, "uint16", wkt_step_index),
        ],
    )


def build_strength_fit(
    path: Path,
    *,
    start: datetime | None = None,
    serial: int = 3450810483,
    utc_offset_s: int = 7200,
) -> Path:
    """Seduta di forza sintetica ma realistica.

    Contiene di proposito i casi limite che il parser deve reggere:
    serie con reps+peso, serie senza peso (corpo libero), una serie con peso
    zero, un esercizio fuori catalogo (categoria 250) e le pause.
    """
    start = start or datetime(2026, 9, 3, 17, 30, 0, tzinfo=timezone.utc)
    w = FitWriter()
    _file_id(w, start, serial)
    _sport(w, 10, 20, "Pesi")  # training / strength_training

    # tabella (categoria, nome) -> etichetta, come la scrive l'orologio
    for idx, (cat, name, label) in enumerate(
        [
            (8, 0, "Deadlift"),
            (8, 4, "Dumbbell Deadlift"),
            (21, 42, "Band-assisted Pull-up"),
            (19, 66, "Side Plank"),
        ]
    ):
        w.message(
            MSG_EXERCISE_TITLE,
            [(254, "uint16", idx), (0, "uint16", cat), (1, "uint16", name), (2, "string", label)],
        )

    # passo di allenamento strutturato: 8 reps pianificate a 60 kg
    w.message(
        MSG_WORKOUT_STEP,
        [
            (254, "uint16", 0),
            (0, "string", "Trap bar deadlift"),
            (1, "enum", 29),  # duration_type = reps
            (2, "uint32", 8),  # duration_reps
            (7, "enum", 0),  # intensity = active
            (10, "uint16", 8),  # exercise_category = deadlift
            (11, "uint16", 0),  # exercise_name = barbell_deadlift
            # attenzione: exercise_weight ha scala 100, set.weight ha scala 16
            (12, "uint16", round(60 * STEP_WEIGHT_SCALE)),
        ],
    )

    plan = [
        # (categoria, sottocategoria, reps, peso kg, durata attiva, riposo)
        ((8, 8, 8), (0, 0, 0), 8, 60.0, 42.0, 120.0),
        ((8, 8, 8), (0, 0, 0), 8, 65.0, 44.5, 120.0),
        ((8, 8, 8), (0, 0, 0), 6, 70.0, 38.0, 150.0),
        ((8, 8, 8), (4, 4, 4), 10, 24.0, 55.0, 90.0),
        ((21, 21, 21), (42, 42, 42), 6, None, 33.0, 90.0),
        ((19, 19, 19), (66, 66, 66), 12, 0.0, 45.0, 60.0),  # peso zero: anomalia da segnalare
        ((250, 250, 250), (7, 7, 7), 15, 12.5, 40.0, 60.0),  # fuori catalogo -> unmapped
    ]

    t = start
    index = 0
    hr_start = t
    for cat, sub, reps, weight, active_s, rest_s in plan:
        _set(
            w,
            index=index,
            start=t,
            session_start=start,
            duration_s=active_s,
            set_type=1,
            category=cat,
            subcategory=sub,
            reps=reps,
            weight_kg=weight,
            wkt_step_index=0 if cat[0] == 8 and sub[0] == 0 else None,
        )
        index += 1
        t += timedelta(seconds=active_s)
        _set(w, index=index, start=t, session_start=start, duration_s=rest_s, set_type=0)
        index += 1
        t += timedelta(seconds=rest_s)

    total_s = (t - start).total_seconds()
    # FC a 1 Hz con una deriva lenta, come nei file veri
    hr_values = []
    for sec in range(int(total_s) + 1):
        bpm = 92 + int(sec / 60) + (3 if (sec // 30) % 2 else 0)
        hr_values.append(bpm)
        w.message(
            MSG_RECORD,
            [
                (253, "uint32", fit_timestamp(hr_start + timedelta(seconds=sec))),
                (3, "uint8", bpm),
            ],
        )

    w.message(
        MSG_SESSION,
        [
            (254, "uint32", fit_timestamp(start)),
            (2, "uint32", fit_timestamp(start)),  # start_time
            (7, "uint32", round(total_s * 1000)),  # total_elapsed_time
            (8, "uint32", round(total_s * 1000)),  # total_timer_time
            (11, "uint16", 310),  # total_calories
            (5, "enum", 10),  # sport = training
            (6, "enum", 20),  # sub_sport = strength_training
            (16, "uint8", round(sum(hr_values) / len(hr_values))),  # avg_heart_rate
            (17, "uint8", max(hr_values)),  # max_heart_rate
            (110, "string", "Pesi"),  # sport_profile_name
        ],
    )
    w.message(
        MSG_ACTIVITY,
        [
            (253, "uint32", fit_timestamp(start)),
            (0, "uint32", round(total_s * 1000)),
            (1, "uint16", 1),  # num_sessions
            (5, "uint32", fit_timestamp(start) + utc_offset_s),  # local_timestamp
        ],
    )
    return w.write(path)


def build_running_fit(path: Path, start: datetime | None = None) -> Path:
    """Attivita' di corsa: nessun messaggio `set`, deve essere saltata."""
    start = start or datetime(2026, 9, 4, 6, 0, 0, tzinfo=timezone.utc)
    w = FitWriter()
    _file_id(w, start, 3450810483)
    _sport(w, 1, 0, "Corsa")  # running / generic
    for sec in range(0, 60):
        w.message(
            MSG_RECORD,
            [(253, "uint32", fit_timestamp(start + timedelta(seconds=sec))), (3, "uint8", 140)],
        )
    w.message(
        MSG_SESSION,
        [
            (254, "uint32", fit_timestamp(start)),
            (2, "uint32", fit_timestamp(start)),
            (7, "uint32", 60000),
            (8, "uint32", 60000),
            (5, "enum", 1),
            (6, "enum", 0),
        ],
    )
    return w.write(path)


def build_truncated_fit(path: Path, keep_fraction: float = 0.4) -> Path:
    """File troncato a meta': deve essere saltato senza far cadere il batch."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        full = build_strength_fit(Path(tmp) / "full.fit").read_bytes()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(full[: int(len(full) * keep_fraction)])
    return path


FIXTURES = Path(__file__).parent / "fixtures"


def regenerate() -> list[Path]:
    return [
        build_strength_fit(FIXTURES / "strength_session.fit"),
        build_running_fit(FIXTURES / "running_session.fit"),
    ]


if __name__ == "__main__":
    for p in regenerate():
        print(f"scritto {p} ({p.stat().st_size} byte)")
