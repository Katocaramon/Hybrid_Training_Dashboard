"""Import degli export CSV di Hevy.

Hevy esporta **una riga per serie**, con il nome dell'esercizio gia' in
chiaro. Rispetto al FIT dell'orologio si guadagna e si perde:

* **si guadagna** un dato di serie affidabile — reps e carichi ci sono sempre,
  perche' li scrivi tu nell'app mentre alleni, e non dipendono da cosa
  l'orologio riesce a contare;
* **si perde** la frequenza cardiaca (non c'e'), l'orario della singola serie
  (c'e' solo inizio e fine di tutta la seduta) e la durata delle pause.

Quindi deriva della FC e rapporto lavoro/riposo non sono calcolabili per le
sedute Hevy, e le metriche lo dichiarano invece di inventarle.

Struttura del CSV (verificata su un export reale, settembre 2026):

    title,start_time,end_time,description,exercise_title,superset_id,
    exercise_notes,set_index,set_type,weight_kg,reps,distance_km,
    duration_seconds,rpe

* `start_time` e `end_time` sono **di sessione**, ripetuti su ogni riga, in
  ora locale e senza fuso ("Sep 21, 2026 at 6:25 AM").
* `set_index` riparte da 0 a ogni esercizio, e lo stesso esercizio puo'
  comparire due volte nella stessa seduta (destra/sinistra, superserie): non
  e' una chiave. L'ordine delle righe e' l'ordine della seduta, ed e' quello
  che usiamo.
* Un export contiene **tutti** gli allenamenti, non uno solo: le righe vanno
  raggruppate per (titolo, inizio).
* Colonne vuote sono vuote per davvero: un plank ha `duration_seconds` e basta,
  un esercizio a corpo libero non ha `weight_kg`.
"""

from __future__ import annotations

import csv
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .fit_parser import DeviceInfo, FitSkipped, ParsedActivity, SessionRecord, SetRecord, sha256_file

log = logging.getLogger(__name__)

#: Colonne che identificano un export Hevy. Le altre sono opzionali.
COLONNE_RICHIESTE = {"title", "start_time", "exercise_title", "set_index", "set_type"}

#: Formati di data visti negli export. Il primo e' quello di default dell'app.
FORMATI_DATA = (
    "%d %b %Y, %H:%M",
    "%b %d, %Y at %I:%M %p",
    "%b %d, %Y, %I:%M %p",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)

#: Prefisso delle chiavi grezze Hevy, per non confonderle con gli slug Garmin.
PREFISSO = "hevy:"


def e_un_csv_hevy(path: Path) -> bool:
    """Riconosce un export Hevy dall'intestazione, senza leggere tutto."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            intestazione = next(csv.reader(fh), [])
    except (OSError, UnicodeDecodeError, StopIteration):
        return False
    return COLONNE_RICHIESTE <= {c.strip() for c in intestazione}


def parse_data(valore: str) -> datetime:
    """Data locale di Hevy -> datetime *naive*.

    Hevy non scrive il fuso orario. Non lo inventiamo: il timestamp resta
    naive e vale come ora locale, che e' l'unica cosa che serve per datare la
    seduta e assegnarla a una settimana.
    """
    testo = (valore or "").strip()
    for formato in FORMATI_DATA:
        try:
            return datetime.strptime(testo, formato)
        except ValueError:
            continue
    raise ValueError(f"data non riconosciuta: {valore!r}")


def _numero(valore: str | None) -> float | None:
    testo = (valore or "").strip().replace(",", ".")
    if not testo:
        return None
    try:
        return float(testo)
    except ValueError:
        return None


def _intero(valore: str | None) -> int | None:
    n = _numero(valore)
    return int(n) if n is not None else None


def exercise_key(titolo: str) -> str:
    """`Bench Press (Dumbbell)` -> `hevy:Bench Press (Dumbbell)`.

    Il titolo resta quello che scrive Hevy, cosi' e' riconoscibile a colpo
    d'occhio nel YAML di mappatura e in `unmapped`. Il prefisso evita di
    confonderlo con uno slug del catalogo Garmin.
    """
    return PREFISSO + titolo.strip()


def parse_csv(path: Path) -> list[ParsedActivity]:
    """Legge un export Hevy e restituisce una seduta per allenamento.

    Solleva `FitSkipped` se il file non e' un export Hevy o non e' leggibile,
    cosi' l'ingestione lo salta come farebbe con un .fit rotto.
    """
    path = Path(path)
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            lettore = csv.DictReader(fh)
            if not lettore.fieldnames or not COLONNE_RICHIESTE <= set(lettore.fieldnames):
                raise FitSkipped(
                    "non e' un export Hevy: mancano le colonne "
                    f"{sorted(COLONNE_RICHIESTE - set(lettore.fieldnames or []))}"
                )
            righe = list(lettore)
    except FitSkipped:
        raise
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise FitSkipped(f"CSV illeggibile ({exc.__class__.__name__}: {exc})") from exc

    if not righe:
        raise FitSkipped("export Hevy vuoto: nessun allenamento")

    digest = sha256_file(path)
    attivita: list[ParsedActivity] = []
    for chiave, gruppo in _raggruppa(righe):
        try:
            attivita.append(_costruisci(path, digest, chiave, gruppo))
        except ValueError as exc:
            log.warning("salto l'allenamento %r in %s: %s", chiave[0], path, exc)
    if not attivita:
        raise FitSkipped("nessun allenamento leggibile nell'export")
    return attivita


def _raggruppa(righe: list[dict[str, Any]]) -> Iterator[tuple[tuple[str, str], list[dict]]]:
    """Raggruppa per (titolo, inizio) conservando l'ordine del file."""
    corrente: tuple[str, str] | None = None
    blocco: list[dict[str, Any]] = []
    for riga in righe:
        chiave = ((riga.get("title") or "").strip(), (riga.get("start_time") or "").strip())
        if chiave != corrente:
            if blocco:
                yield corrente, blocco  # type: ignore[misc]
            corrente, blocco = chiave, []
        blocco.append(riga)
    if blocco:
        yield corrente, blocco  # type: ignore[misc]


def _costruisci(
    path: Path, digest: str, chiave: tuple[str, str], righe: list[dict[str, Any]]
) -> ParsedActivity:
    titolo, inizio_grezzo = chiave
    inizio = parse_data(inizio_grezzo)
    try:
        fine = parse_data(righe[0].get("end_time") or "")
    except ValueError:
        fine = None

    avvisi: list[str] = []
    serie: list[SetRecord] = []
    for ordine, riga in enumerate(righe):
        nome = (riga.get("exercise_title") or "").strip()
        if not nome:
            avvisi.append(f"riga {ordine + 1}: esercizio senza nome, saltata")
            continue
        superset = (riga.get("superset_id") or "").strip() or None
        serie.append(
            SetRecord(
                # L'indice e' l'ordine nella seduta, non `set_index` di Hevy:
                # quello riparte a ogni esercizio e non e' univoco.
                index=ordine,
                set_type="active",
                start_time=None,  # Hevy non registra l'orario della singola serie
                duration_s=_numero(riga.get("duration_seconds")),
                repetitions=_intero(riga.get("reps")),
                weight_kg=_numero(riga.get("weight_kg")),
                weight_display_unit="kilogram",
                category_raw=(),
                subcategory_raw=(),
                exercise_key=exercise_key(nome),
                exercise_label=nome,
                wkt_step_index=None,
                rpe=_numero(riga.get("rpe")),
                superset_id=superset,
                set_kind=(riga.get("set_type") or "").strip() or None,
                distance_km=_numero(riga.get("distance_km")),
            )
        )

    if not serie:
        raise ValueError("nessuna serie valida")

    durata = (fine - inizio).total_seconds() if fine else None
    if durata is not None and durata <= 0:
        avvisi.append(f"fine ({fine}) non successiva all'inizio ({inizio}): durata ignorata")
        durata = None

    sessione = SessionRecord(
        start_time=inizio,
        total_elapsed_s=durata,
        # Hevy non distingue il tempo attivo da quello totale: non c'e' un
        # cronometro che si ferma durante le pause. Lasciarlo nullo evita di
        # spacciare il tempo totale per tempo sotto carico.
        total_timer_s=None,
        avg_hr=None,
        max_hr=None,
        calories=None,
        sport="training",
        sub_sport="strength_training",
        sport_profile_name="Hevy",
        workout_name=titolo or None,
        total_training_effect=None,
        utc_offset_s=None,  # il fuso non c'e' nel file: non lo inventiamo
        body_weight_kg=None,
        source="hevy",
    )

    # Uid stabile: stesso allenamento riesportato -> stesso uid, quindi
    # reimportare l'export completo non duplica nulla.
    impronta = hashlib.sha256(f"{titolo}|{inizio.isoformat()}".encode()).hexdigest()[:12]
    return ParsedActivity(
        source_path=path,
        file_sha256=digest,
        garmin_activity_id=None,
        device=DeviceInfo(manufacturer="hevy", product=None, serial_number=None, time_created=None),
        session=sessione,
        sets=serie,
        hr_samples=[],
        warnings=avvisi,
        uid_override=f"hevy:{inizio.strftime('%Y%m%dT%H%M')}:{impronta}",
    )
