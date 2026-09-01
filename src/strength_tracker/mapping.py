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
class Match:
    """Un criterio di riconoscimento: chiave grezza, con nota opzionale.

    La nota e' quella dello step dell'allenamento (`workout_step.notes`).
    Serve quando la stessa chiave grezza vuol dire esercizi diversi: il
    Copenhagen plank arriva come `plank/side_plank` con nota "Copenhagen
    plank", un plank laterale vero arriva con la stessa chiave e nessuna nota.
    """

    raw_key: str
    note: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.raw_key, self.note)


#: Come va letto il peso registrato dall'orologio.
WEIGHT_MODES = {
    "carico",        # il peso e' il carico esterno sollevato (default)
    "assistenza",    # il peso e' l'aiuto ricevuto: trazioni assistite, elastici
    "corpo_libero",  # nessun carico esterno: plank, dead bug, affondi a corpo libero
}


@dataclass(frozen=True)
class ExerciseMapping:
    name: str
    primary_group: str | None
    secondary_groups: tuple[str, ...]
    matches: tuple[Match, ...]
    weight_mode: str = "carico"

    @property
    def raw_keys(self) -> tuple[str, ...]:
        return tuple(m.raw_key for m in self.matches)


@dataclass(frozen=True)
class Mapping:
    exercises: tuple[ExerciseMapping, ...]
    focus_groups: tuple[str, ...]
    source_path: Path

    @property
    def by_match(self) -> dict[tuple[str, str], ExerciseMapping]:
        return {m.key: ex for ex in self.exercises for m in ex.matches}

    @property
    def by_raw_key(self) -> dict[str, ExerciseMapping]:
        """Solo le voci generiche (senza nota): comode da interrogare."""
        return {m.raw_key: ex for ex in self.exercises for m in ex.matches if not m.note}

    def as_rows(self) -> list[dict[str, Any]]:
        """Righe pronte per `db.refresh_exercise_map`."""
        return [
            {
                "raw_key": match.raw_key,
                "note": match.note,
                "exercise_name": ex.name,
                "primary_group": ex.primary_group,
                "secondary_groups": list(ex.secondary_groups),
                "weight_mode": ex.weight_mode,
            }
            for match, ex in ((m, ex) for ex in self.exercises for m in ex.matches)
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
    visto: dict[tuple[str, str], str] = {}
    for i, raw in enumerate(data.get("exercises") or []):
        if not isinstance(raw, dict):
            raise MappingError(f"{path}: voce #{i + 1} di 'exercises' non e' una mappa")
        name = raw.get("name")
        if not name:
            raise MappingError(f"{path}: voce #{i + 1} senza 'name'")
        voci = raw.get("match") or []
        if isinstance(voci, (str, dict)):
            voci = [voci]
        matches: list[Match] = []
        for voce in voci:
            if isinstance(voce, dict):
                if "key" not in voce:
                    raise MappingError(
                        f"{path}: voce di 'match' di {name!r} senza 'key'"
                    )
                match = Match(str(voce["key"]), str(voce.get("note") or "").strip().lower())
            else:
                match = Match(str(voce))
            if match.key in visto:
                etichetta = match.raw_key + (f" (nota: {match.note})" if match.note else "")
                raise MappingError(
                    f"{path}: la chiave grezza {etichetta!r} e' assegnata sia a "
                    f"{visto[match.key]!r} sia a {name!r}"
                )
            visto[match.key] = name
            matches.append(match)
        weight_mode = str(raw.get("weight_mode") or "carico")
        if weight_mode not in WEIGHT_MODES:
            raise MappingError(
                f"{path}: {name!r} ha weight_mode {weight_mode!r}; "
                f"valori ammessi: {', '.join(sorted(WEIGHT_MODES))}"
            )
        secondary = raw.get("secondary") or []
        if isinstance(secondary, str):
            secondary = [secondary]
        exercises.append(
            ExerciseMapping(
                name=str(name),
                primary_group=raw.get("primary"),
                secondary_groups=tuple(str(s) for s in secondary),
                matches=tuple(matches),
                weight_mode=weight_mode,
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
