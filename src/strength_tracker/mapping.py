"""Normalizzazione dei nomi esercizio.

Il file `config/exercise_mapping.yaml` e' la fonte di verita': associa le
chiavi grezze del catalogo Garmin (`pull_up/band_assisted_pull_up`, oppure
`250/7` quando il catalogo non le conosce) ai nomi reali del programma e al
gruppo muscolare. E' versionato ed editabile a mano; il codice non indovina.

Le chiavi grezze da incollare nel YAML sono esattamente quelle che stampa
`strength-tracker unmapped`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class MappingError(Exception):
    """Il file di mappatura non e' valido."""


@dataclass(frozen=True)
class ExerciseMapping:
    name: str
    primary_group: str | None
    secondary_groups: tuple[str, ...]
    raw_keys: tuple[str, ...]


@dataclass(frozen=True)
class Mapping:
    exercises: tuple[ExerciseMapping, ...]
    focus_groups: tuple[str, ...]
    source_path: Path

    @property
    def by_raw_key(self) -> dict[str, ExerciseMapping]:
        out: dict[str, ExerciseMapping] = {}
        for ex in self.exercises:
            for key in ex.raw_keys:
                out[key] = ex
        return out

    def as_rows(self) -> list[dict[str, Any]]:
        """Righe pronte per `db.refresh_exercise_map`."""
        return [
            {
                "raw_key": key,
                "exercise_name": ex.name,
                "primary_group": ex.primary_group,
                "secondary_groups": list(ex.secondary_groups),
            }
            for key, ex in self.by_raw_key.items()
        ]


EMPTY = Mapping(exercises=(), focus_groups=(), source_path=Path("<vuota>"))


def load_mapping(path: Path) -> Mapping:
    """Legge il YAML. Un file assente non e' un errore: mappatura vuota."""
    path = Path(path)
    if not path.is_file():
        return Mapping(exercises=(), focus_groups=(), source_path=path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise MappingError(f"{path}: YAML non valido ({exc})") from exc
    if not isinstance(data, dict):
        raise MappingError(f"{path}: il file deve contenere una mappa, non {type(data).__name__}")

    exercises: list[ExerciseMapping] = []
    visto: dict[str, str] = {}
    for i, raw in enumerate(data.get("exercises") or []):
        if not isinstance(raw, dict):
            raise MappingError(f"{path}: voce #{i + 1} di 'exercises' non e' una mappa")
        name = raw.get("name")
        if not name:
            raise MappingError(f"{path}: voce #{i + 1} senza 'name'")
        keys = raw.get("match") or []
        if isinstance(keys, str):
            keys = [keys]
        for key in keys:
            key = str(key)
            if key in visto:
                raise MappingError(
                    f"{path}: la chiave grezza {key!r} e' assegnata sia a "
                    f"{visto[key]!r} sia a {name!r}"
                )
            visto[key] = name
        secondary = raw.get("secondary") or []
        if isinstance(secondary, str):
            secondary = [secondary]
        exercises.append(
            ExerciseMapping(
                name=str(name),
                primary_group=raw.get("primary"),
                secondary_groups=tuple(str(s) for s in secondary),
                raw_keys=tuple(str(k) for k in keys),
            )
        )

    focus = data.get("focus_groups") or []
    if isinstance(focus, str):
        focus = [focus]
    return Mapping(
        exercises=tuple(exercises),
        focus_groups=tuple(str(f) for f in focus),
        source_path=path,
    )
