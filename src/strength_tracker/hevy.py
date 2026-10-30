"""Import dei dati Hevy: export CSV e payload dell'API.

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

Il payload dell'**API** (`/v1/workouts`) dice le stesse cose meglio:

* gli orari sono in **UTC vero** con il fuso, non in ora locale muta: si puo'
  ricostruire sia l'istante sia la data locale;
* ogni esercizio porta un `exercise_template_id` stabile accanto al titolo;
* le serie sono annidate dentro gli esercizi, con `type` che distingue le
  serie di riscaldamento (`warmup`) da quelle vere (`normal`);
* `supersets_id` e `distance_meters` al posto di `superset_id` e `distance_km`.

**Le due strade non si incrociano.** Un allenamento che arriva dall'API e'
identificato dall'id che Hevy stesso gli da' (`hevy:<workout_id>`): e' stabile
per costruzione, non dipende da arrotondamenti dell'orario ne' dal cambio
dell'ora. Il CSV quell'id non ce l'ha, quindi li' l'identita' si ricostruisce
da titolo piu' ora locale al minuto, ed e' una ricostruzione: sullo stesso
allenamento le due chiavi possono non coincidere.

Conseguenza pratica: **scegli una sorgente sola**. Mescolare l'export CSV e il
sync via API sullo stesso periodo crea sedute doppie; se succede, il controllo
sulle sedute sovrapposte in `metrics.sedute_doppie` lo segnala.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
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


def session_uid(titolo: str, inizio_locale: datetime) -> str:
    """Identita' ricostruita di un allenamento: titolo e ora locale al minuto.

    Serve solo all'export CSV, che non porta l'id dell'allenamento. E' una
    ricostruzione, non un'identita' vera: se Hevy scrive l'orario arrotondato
    diversamente da come lo calcoliamo noi, la chiave cambia. Dove l'id c'e'
    — cioe' nell'API — si usa quello.
    """
    minuto = inizio_locale.replace(second=0, microsecond=0, tzinfo=None)
    impronta = hashlib.sha256(f"{titolo}|{minuto.isoformat()}".encode()).hexdigest()[:12]
    return f"hevy:{minuto.strftime('%Y%m%dT%H%M')}:{impronta}"


def fuso_locale(nome: str | None = None) -> tzinfo:
    """Fuso con cui leggere gli orari UTC dell'API.

    Hevy registra l'istante in UTC ma non dice in che fuso ti sei allenato.
    Si usa quello del computer, che per un attrezzo locale e' la risposta
    giusta; `STRENGTH_TRACKER_TZ` lo sovrascrive (serve anche ai test).
    """
    import os

    nome = nome or os.environ.get("STRENGTH_TRACKER_TZ")
    if not nome:
        return datetime.now().astimezone().tzinfo or timezone.utc
    from zoneinfo import ZoneInfo

    return ZoneInfo(nome)


def e_un_json_hevy(dati: Any) -> bool:
    """Riconosce un payload dell'API Hevy gia' caricato."""
    if not isinstance(dati, dict):
        return False
    if isinstance(dati.get("workout"), dict):
        return "exercises" in dati["workout"]
    if isinstance(dati.get("workouts"), list):
        return all(isinstance(w, dict) and "exercises" in w for w in dati["workouts"])
    return "exercises" in dati and "start_time" in dati


def parse_json(path: Path, fuso: tzinfo | None = None) -> list[ParsedActivity]:
    """Legge un payload dell'API Hevy salvato su file."""
    path = Path(path)
    try:
        dati = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FitSkipped(f"JSON illeggibile ({exc.__class__.__name__}: {exc})") from exc
    if not e_un_json_hevy(dati):
        raise FitSkipped("non e' un payload di allenamenti Hevy")
    return parse_api_payload(dati, source_path=path, fuso=fuso)


def parse_api_payload(
    dati: Any, *, source_path: Path, fuso: tzinfo | None = None, digest: str = ""
) -> list[ParsedActivity]:
    """Converte la risposta dell'API Hevy in attivita'.

    Accetta sia `{"workout": {...}}` sia `{"workouts": [...]}` sia il singolo
    allenamento nudo: sono le tre forme che l'API e i suoi wrapper usano.
    """
    fuso = fuso or fuso_locale()
    if isinstance(dati.get("workout"), dict):
        grezzi = [dati["workout"]]
    elif isinstance(dati.get("workouts"), list):
        grezzi = dati["workouts"]
    else:
        grezzi = [dati]

    attivita: list[ParsedActivity] = []
    for w in grezzi:
        try:
            attivita.append(_da_api(w, Path(source_path), digest, fuso))
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("salto l'allenamento %r: %s", w.get("title"), exc)
    if not attivita:
        raise FitSkipped("nessun allenamento leggibile nel payload")
    return attivita


def _istante(valore: str | None) -> datetime | None:
    """ISO 8601 dell'API -> datetime con fuso."""
    if not valore:
        return None
    testo = valore.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(testo)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _da_api(w: dict[str, Any], source_path: Path, digest: str, fuso: tzinfo) -> ParsedActivity:
    titolo = (w.get("title") or "").strip()
    id_hevy = str(w.get("id") or "").strip()
    inizio = _istante(w.get("start_time"))
    if inizio is None:
        raise ValueError(f"allenamento {w.get('id')!r} senza start_time valido")
    fine = _istante(w.get("end_time"))
    locale = inizio.astimezone(fuso)

    avvisi: list[str] = []
    serie: list[SetRecord] = []
    for esercizio in w.get("exercises") or []:
        nome = (esercizio.get("title") or "").strip()
        if not nome:
            avvisi.append("esercizio senza titolo, saltato")
            continue
        superset = esercizio.get("supersets_id")
        for s in esercizio.get("sets") or []:
            distanza_m = s.get("distance_meters")
            serie.append(
                SetRecord(
                    # Come per il CSV: l'ordine nella seduta, non l'indice di
                    # Hevy, che riparte a ogni esercizio.
                    index=len(serie),
                    set_type="active",
                    start_time=None,  # l'API non da' l'orario della singola serie
                    duration_s=_numero_grezzo(s.get("duration_seconds")),
                    repetitions=_intero_grezzo(s.get("reps")),
                    weight_kg=_numero_grezzo(s.get("weight_kg")),
                    weight_display_unit="kilogram",
                    category_raw=(),
                    subcategory_raw=(),
                    exercise_key=exercise_key(nome),
                    exercise_label=nome,
                    wkt_step_index=None,
                    rpe=_numero_grezzo(s.get("rpe")),
                    superset_id=None if superset is None else str(superset),
                    set_kind=(s.get("type") or "").strip() or None,
                    distance_km=None if distanza_m is None else float(distanza_m) / 1000,
                )
            )
    if not serie:
        raise ValueError(f"allenamento {titolo!r} senza serie")

    durata = (fine - inizio).total_seconds() if fine else None
    if durata is not None and durata <= 0:
        avvisi.append("fine non successiva all'inizio: durata ignorata")
        durata = None

    offset = locale.utcoffset()
    sessione = SessionRecord(
        start_time=inizio,  # istante vero, in UTC
        total_elapsed_s=durata,
        total_timer_s=None,  # Hevy non ferma un cronometro durante le pause
        avg_hr=None,
        max_hr=None,
        calories=None,
        sport="training",
        sub_sport="strength_training",
        sport_profile_name="Hevy",
        workout_name=titolo or None,
        total_training_effect=None,
        utc_offset_s=int(offset.total_seconds()) if offset else 0,
        body_weight_kg=None,
        source="hevy",
    )
    return ParsedActivity(
        source_path=source_path,
        file_sha256=digest,
        garmin_activity_id=None,
        device=DeviceInfo(manufacturer="hevy", product=None, serial_number=None, time_created=None),
        session=sessione,
        sets=serie,
        hr_samples=[],
        warnings=avvisi,
        # L'id di Hevy e' l'identita' vera: non dipende da come leggiamo
        # l'orario. Solo se manca si ripiega sulla ricostruzione.
        uid_override=f"hevy:{id_hevy}" if id_hevy else session_uid(titolo, locale),
    )


def _numero_grezzo(valore: Any) -> float | None:
    if valore is None or valore == "":
        return None
    try:
        return float(valore)
    except (TypeError, ValueError):
        return None


def _intero_grezzo(valore: Any) -> int | None:
    n = _numero_grezzo(valore)
    return int(n) if n is not None else None


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
        # Stesso uid che produrrebbe l'API per questo allenamento: reimportare
        # l'export completo, o passare all'API, non duplica niente.
        uid_override=session_uid(titolo, inizio),
    )


# --------------------------------------------------------------------------
# client dell'API
# --------------------------------------------------------------------------

#: Endpoint pubblico di Hevy. La chiave va nell'header `api-key`.
API_BASE = "https://api.hevyapp.com/v1"

#: Hevy pagina gli allenamenti a blocchi piccoli.
PAGE_SIZE = 10


class HevyAPIError(Exception):
    """L'API non ha risposto come doveva. Il messaggio dice cosa fare."""


def _transport_urllib(url: str, headers: dict[str, str], timeout: float) -> bytes:
    """Chiamata HTTP con la sola libreria standard: nessuna dipendenza in piu'."""
    import urllib.error
    import urllib.request

    richiesta = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(richiesta, timeout=timeout) as risposta:
            return risposta.read()
    except urllib.error.HTTPError as exc:
        raise HevyAPIError(_spiega_http(exc.code, url)) from exc
    except urllib.error.URLError as exc:
        raise HevyAPIError(f"non riesco a contattare Hevy ({exc.reason})") from exc


def _spiega_http(codice: int, url: str) -> str:
    if codice in (401, 403):
        return (
            f"Hevy ha rifiutato la chiave API (HTTP {codice}). Controlla "
            "HEVY_API_KEY: la chiave si genera nelle impostazioni del profilo "
            "Hevy, nella sezione sviluppatore."
        )
    if codice == 404:
        return f"endpoint non trovato ({url}): l'API di Hevy potrebbe essere cambiata"
    if codice == 429:
        return "troppe richieste: Hevy ha applicato un limite, riprova fra qualche minuto"
    return f"Hevy ha risposto HTTP {codice}"


@dataclass
class HevyClient:
    """Lettura degli allenamenti dall'API di Hevy.

    Solo richieste GET: questo strumento non scrive mai su Hevy.
    `transport` esiste per poter provare paginazione e gestione degli errori
    senza rete; in produzione e' quello basato su urllib.
    """

    api_key: str
    base_url: str = API_BASE
    timeout: float = 30.0
    transport: Any = None

    def _get(self, percorso: str, **parametri: Any) -> dict[str, Any]:
        from urllib.parse import urlencode

        if not self.api_key:
            raise HevyAPIError(
                "manca la chiave API: passa --api-key oppure esporta HEVY_API_KEY"
            )
        url = f"{self.base_url.rstrip('/')}/{percorso.lstrip('/')}"
        if parametri:
            url = f"{url}?{urlencode(parametri)}"
        trasporto = self.transport or _transport_urllib
        grezzo = trasporto(url, {"api-key": self.api_key, "Accept": "application/json"}, self.timeout)
        try:
            dati = json.loads(grezzo)
        except (TypeError, json.JSONDecodeError) as exc:
            raise HevyAPIError(f"risposta non JSON da {url}") from exc
        if not isinstance(dati, dict):
            raise HevyAPIError(f"risposta inattesa da {url}: {type(dati).__name__}")
        return dati

    def pagina_allenamenti(self, pagina: int, page_size: int = PAGE_SIZE) -> dict[str, Any]:
        return self._get("workouts", page=pagina, pageSize=page_size)

    def allenamento(self, workout_id: str) -> dict[str, Any]:
        dati = self._get(f"workouts/{workout_id}")
        return dati.get("workout") or dati

    def iter_allenamenti(
        self, page_size: int = PAGE_SIZE, max_pagine: int | None = None
    ) -> Iterator[dict[str, Any]]:
        """Tutti gli allenamenti, pagina per pagina.

        Si percorrono tutte le pagine: l'ordine in cui Hevy le restituisce non
        e' garantito per data, quindi fermarsi al primo allenamento gia' noto
        rischierebbe di saltarne altri. Sono poche decine di richieste anche
        per anni di allenamenti.

        Se la pagina non porta gia' esercizi e serie, il dettaglio viene
        chiesto per ogni allenamento.
        """
        pagina = 1
        while True:
            dati = self.pagina_allenamenti(pagina, page_size)
            allenamenti = dati.get("workouts")
            if not isinstance(allenamenti, list):
                raise HevyAPIError("risposta senza elenco 'workouts'")
            for w in allenamenti:
                if "exercises" not in w and w.get("id"):
                    w = self.allenamento(str(w["id"]))
                yield w
            totale = dati.get("page_count")
            if dati.get("has_next_page") is False:
                return
            if isinstance(totale, int) and pagina >= totale:
                return
            if not allenamenti:
                return
            pagina += 1
            if max_pagine is not None and pagina > max_pagine:
                return
