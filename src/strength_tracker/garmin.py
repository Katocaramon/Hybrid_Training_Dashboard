"""Garmin Personal API: corse, bici e stato di recupero nello stesso database.

Perche' esiste
--------------

Le serie di palestra arrivano dai `.fit` e da Hevy; i chilometri di corsa, il
dislivello, l'HRV e il sonno no: in un file `.fit` di una seduta di forza non
ci sono. Senza di loro la domanda che ha dato il via al progetto — *la
palestra sta costruendo forza senza interferire con il carico di corsa?* — non
ha risposta, perche' manca il termine di confronto.

Questo modulo porta quei dati nel database condiviso, cosi' una settimana di
palestra e una settimana di corsa stanno nella stessa riga (`v_settimane`).
E' la sola parte del programma che tocca la rete, e lo fa solo quando glielo
si chiede: `sync-garmin`. Tutto il resto resta offline.

Scelte
------

* **Sola lettura.** Solo GET, piu' la POST che chiede il token. Non scrive mai
  niente su Garmin Connect.
* **Niente credenziali nel repository.** La chiave si legge da
  `GARMIN_PERSONAL_API_KEY` o da un file fuori dal repository
  (`~/.strength-tracker.env`, `.env.local`), e non viene mai stampata.
* **Le sedute di forza non entrano da qui.** L'elenco attivita' dell'API dice
  che una seduta di palestra esiste, ma non espone le serie: importarla
  creerebbe una seduta vuota che fa a pugni con quella vera di Hevy. Le
  attivita' del gruppo palestra vengono quindi saltate, con il motivo scritto
  nel rapporto.
* **Si riprende da dove era arrivata.** Ogni finestra di giorni viene salvata
  appena letta e la data raggiunta finisce in `sync_state`: un'interruzione o
  un limite di frequenza non obbligano a ricominciare. Gli ultimi giorni si
  rileggono sempre, perche' i dati di oggi sono parziali fino a notte.
* **Niente stime silenziose.** Un campo che l'API non manda resta NULL. Il
  passo al chilometro e' l'unico valore calcolato, ed e' aritmetica sulla
  velocita' media misurata, non una stima.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

#: Endpoint della Personal API. La chiave personale si genera dal proprio
#: account Garmin Connect: e' legata al singolo utente e vede solo i suoi dati.
BASE = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"

#: Il token vale 24 ore: si rinnova dopo 20, per non farselo scadere in mano.
TOKEN_MAX_AGE_S = 20 * 3600

#: Nome della variabile d'ambiente con la chiave personale.
ENV_CHIAVE = "GARMIN_PERSONAL_API_KEY"

#: File cercati, in ordine, se la variabile d'ambiente non c'e'. Stanno tutti
#: fuori dal repository tranne `.env.local`, che il `.gitignore` esclude.
FILE_CHIAVE = (
    Path.home() / ".strength-tracker.env",
    Path.home() / ".garmin-api.env",
    Path(".env.local"),
)

#: Giorni riletti a ogni sincronizzazione: oggi e ieri sono ancora parziali.
GIORNI_SOVRAPPOSTI = 3

#: Pausa fra due richieste. L'API ha limiti di frequenza e non c'e' fretta.
PAUSA_S = 0.3

_STATO_ATTIVITA = "garmin:attivita_dal"
_STATO_BENESSERE = "garmin:benessere_fino_a"


class GarminError(Exception):
    """L'API non ha risposto come doveva. Il messaggio dice cosa fare."""

    def __init__(self, messaggio: str, status: int | None = None) -> None:
        super().__init__(messaggio)
        self.status = status


# --------------------------------------------------------------------------
# gruppi di attivita'
# --------------------------------------------------------------------------

_CORSA = {
    "running",
    "treadmill_running",
    "track_running",
    "trail_running",
    "indoor_running",
    "virtual_run",
    "obstacle_run",
    "ultra_run",
}
_BICI = {
    "cycling",
    "road_biking",
    "gravel_cycling",
    "mountain_biking",
    "cyclocross",
    "indoor_cycling",
    "virtual_ride",
    "e_bike_fitness",
    "e_bike_mountain",
}
_PALESTRA = {
    "strength_training",
    "indoor_cardio",
    "hiit",
    "yoga",
    "pilates",
    "indoor_rowing",
    "fitness_equipment",
    "mobility",
    "stretching",
}
_NUOTO = {"lap_swimming", "open_water_swimming", "swimming"}

#: Gruppo -> `activity_type` in `sessions`. La palestra non ha un tipo perche'
#: non entra da qui (le serie non ci sono: vedi il docstring del modulo).
TIPO_PER_GRUPPO = {
    "corsa": "run",
    "bici": "bike",
    "nuoto": "swim",
    "altro": "endurance",
}


def gruppo_di(type_key: str | None) -> str:
    """Gruppo di confronto a cui appartiene un tipo di attivita' Garmin."""
    chiave = (type_key or "").lower()
    if chiave in _CORSA:
        return "corsa"
    if chiave in _BICI:
        return "bici"
    if chiave in _PALESTRA:
        return "palestra"
    if chiave in _NUOTO:
        return "nuoto"
    return "altro"


# --------------------------------------------------------------------------
# la chiave
# --------------------------------------------------------------------------


def leggi_chiave(env: dict[str, str] | None = None, file: Path | None = None) -> str:
    """Trova la chiave personale, senza mai stamparla.

    Prima la variabile d'ambiente, poi i file in `FILE_CHIAVE` (o quello
    indicato), nel formato `GARMIN_PERSONAL_API_KEY=...`. Chi lancia la
    dashboard da Finder non passa per `~/.zshrc`, quindi il file conta.
    """
    import os

    ambiente = os.environ if env is None else env
    dalla_variabile = (ambiente.get(ENV_CHIAVE) or "").strip()
    if dalla_variabile:
        return dalla_variabile
    candidati = (file,) if file is not None else FILE_CHIAVE
    for percorso in candidati:
        if percorso is None or not Path(percorso).exists():
            continue
        for riga in Path(percorso).read_text(encoding="utf-8").splitlines():
            riga = riga.strip()
            if riga.startswith("export "):
                riga = riga[len("export ") :].strip()
            if riga.startswith(f"{ENV_CHIAVE}="):
                chiave = riga.split("=", 1)[1].strip().strip("'\"")
                if chiave:
                    return chiave
    raise GarminError(
        "manca la chiave della Personal API di Garmin.\n"
        "  Generala dal tuo account Garmin Connect, poi:\n"
        f"    export {ENV_CHIAVE}='la-tua-chiave'\n"
        f"  oppure scrivi {ENV_CHIAVE}=... in {FILE_CHIAVE[0]}\n"
        "  (quel file sta fuori dal repository: la chiave non va committata)."
    )


# --------------------------------------------------------------------------
# il client
# --------------------------------------------------------------------------


def _transport_urllib(
    url: str,
    headers: dict[str, str],
    timeout: float,
    metodo: str = "GET",
    dati: bytes | None = None,
) -> bytes:
    """Richiesta HTTP con la sola libreria standard: nessuna dipendenza in piu'."""
    import urllib.error
    import urllib.request

    richiesta = urllib.request.Request(url, headers=headers, method=metodo, data=dati)
    try:
        with urllib.request.urlopen(richiesta, timeout=timeout) as risposta:
            if risposta.status == 204:
                return b""
            return risposta.read()
    except urllib.error.HTTPError as exc:
        raise GarminError(_spiega_http(exc.code, url), exc.code) from None
    except urllib.error.URLError as exc:
        raise GarminError(f"non riesco a contattare Garmin ({exc.reason})") from exc


def _spiega_http(codice: int, url: str) -> str:
    percorso = url.split("?")[0]
    if codice in (401, 403):
        return (
            f"Garmin ha rifiutato la chiave (HTTP {codice}): manca, e' sbagliata "
            f"o e' scaduta. Controlla {ENV_CHIAVE}."
        )
    if codice == 429:
        return "troppe richieste: Garmin ha applicato un limite, riprova fra qualche minuto"
    if codice in (502, 503):
        return f"Garmin non risponde (HTTP {codice}): servizio momentaneamente occupato"
    return f"Garmin ha risposto HTTP {codice} su {percorso}"


@dataclass
class GarminClient:
    """Client in sola lettura per la Personal API.

    `transport` esiste per poter provare paginazione, rinnovo del token e
    gestione degli errori senza rete; in produzione e' quello su urllib.
    """

    api_key: str
    base_url: str = BASE
    timeout: float = 60.0
    transport: Callable[..., bytes] | None = None
    #: Dove ricordare il token fra due lanci. `None` = solo in memoria.
    token_path: Path | None = None
    pausa: float = PAUSA_S
    dormi: Callable[[float], None] = time.sleep
    adesso: Callable[[], float] = time.time
    _token: str | None = field(default=None, init=False, repr=False)
    _token_nato: float = field(default=0.0, init=False, repr=False)

    # -- token ------------------------------------------------------------

    def token(self, *, force: bool = False) -> str:
        if not force:
            if self._token and self.adesso() - self._token_nato < TOKEN_MAX_AGE_S:
                return self._token
            salvato = self._token_da_disco()
            if salvato is not None:
                self._token, self._token_nato = salvato
                return self._token
        self._token = self._nuovo_token()
        self._token_nato = self.adesso()
        self._token_su_disco(self._token, self._token_nato)
        return self._token

    def _token_da_disco(self) -> tuple[str, float] | None:
        if self.token_path is None or not Path(self.token_path).exists():
            return None
        try:
            salvato = json.loads(Path(self.token_path).read_text(encoding="utf-8"))
            nato = float(salvato["created"])
            token = str(salvato["token"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if self.adesso() - nato >= TOKEN_MAX_AGE_S:
            return None
        return token, nato

    def _token_su_disco(self, token: str, nato: float) -> None:
        if self.token_path is None:
            return
        percorso = Path(self.token_path)
        percorso.parent.mkdir(parents=True, exist_ok=True)
        percorso.write_text(json.dumps({"token": token, "created": nato}), encoding="utf-8")
        try:
            percorso.chmod(0o600)  # e' una credenziale: solo il proprietario
        except OSError:
            pass

    def _nuovo_token(self) -> str:
        if not self.api_key:
            raise GarminError(f"manca la chiave API: esporta {ENV_CHIAVE}")
        trasporto = self.transport or _transport_urllib
        grezzo = trasporto(
            f"{self.base_url.rstrip('/')}{TOKEN_PATH}",
            {"Authorization": f"ApiKey {self.api_key}"},
            self.timeout,
            "POST",
            b"",
        )
        token = grezzo.decode("utf-8").strip().strip('"')
        if not token:
            raise GarminError("Garmin ha restituito un token vuoto")
        return token

    # -- richieste --------------------------------------------------------

    def get(self, percorso: str, *, tentativi: int = 4) -> Any:
        """GET sull'API. Ritorna il JSON, oppure None se la risposta e' vuota.

        Rinnova il token una volta sola su 401 (scaduto prima del previsto) e
        ritenta sugli errori temporanei: 429 e' il limite di frequenza, 502 e
        503 sono il servizio occupato. Un 500 non si ritenta qui: di solito
        vuol dire "risposta troppo grande", e chi chiama riduce la finestra.
        """
        trasporto = self.transport or _transport_urllib
        url = f"{self.base_url.rstrip('/')}{percorso}"
        rinnovato = False
        for tentativo in range(tentativi + 1):
            if self.pausa:
                self.dormi(self.pausa)
            try:
                grezzo = trasporto(
                    url,
                    {
                        "Authorization": f"Bearer {self.token()}",
                        "Accept": "application/json",
                    },
                    self.timeout,
                    "GET",
                    None,
                )
            except GarminError as exc:
                if exc.status in (401, 403) and not rinnovato:
                    rinnovato = True
                    self.token(force=True)
                    continue
                if exc.status in (429, 502, 503) and tentativo < tentativi:
                    self.dormi(5 * (tentativo + 1))
                    continue
                raise
            if not grezzo:
                return None
            try:
                return json.loads(grezzo.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GarminError(f"risposta non JSON da {percorso.split('?')[0]}") from exc
        raise GarminError(f"nessuna risposta utile da {percorso.split('?')[0]}")

    def profilo(self, giorno: date) -> dict[str, Any] | None:
        """Eta' fitness del giorno. None se Garmin non la espone per quella data."""
        try:
            dati = self.get(f"/fitnessage-service/fitnessage/v2/{giorno.isoformat()}")
        except GarminError as exc:
            if exc.status in (400, 404, 500):
                return None
            raise
        return dati if isinstance(dati, dict) else None

    # -- attivita' --------------------------------------------------------

    def pagina_attivita(self, offset: int, limite: int) -> list[dict[str, Any]]:
        dati = self.get(
            f"/activitylist-service/activities/search/activities?start={offset}&limit={limite}"
        )
        if dati is None:
            return []
        if not isinstance(dati, list):
            raise GarminError("l'elenco attivita' non e' una lista")
        return dati

    def iter_attivita(self, dal: date | None = None, limite: int = 20) -> Iterator[dict[str, Any]]:
        """Attivita' dalla piu' recente, fino a `dal` compreso.

        La pagina si dimezza sui 500: con molte attivita' lunghe la risposta
        puo' superare quello che il servizio riesce a mettere insieme.
        """
        offset = 0
        corrente = limite
        while True:
            try:
                pagina = self.pagina_attivita(offset, corrente)
            except GarminError as exc:
                if exc.status == 500 and corrente > 1:
                    corrente = max(1, corrente // 2)
                    continue
                raise
            if not pagina:
                return
            piu_vecchia = None
            for attivita in pagina:
                yield attivita
                giorno = (attivita.get("startTimeLocal") or "")[:10]
                if giorno:
                    piu_vecchia = giorno if piu_vecchia is None else min(piu_vecchia, giorno)
            offset += len(pagina)
            if len(pagina) < corrente:
                return
            if dal is not None and piu_vecchia and piu_vecchia < dal.isoformat():
                return
            corrente = limite


# --------------------------------------------------------------------------
# normalizzazione delle attivita'
# --------------------------------------------------------------------------


@dataclass
class AttivitaResistenza:
    """Un'attivita' di resistenza pronta per `sessions` + `endurance_details`."""

    session_uid: str
    garmin_activity_id: str | None
    activity_type: str
    activity_group: str
    activity_name: str | None
    sport: str | None
    sub_sport: str | None
    start_time_utc: str
    start_time_local: str | None
    local_date: str | None
    iso_year: int | None
    iso_week: int | None
    utc_offset_s: int | None
    total_elapsed_s: float | None
    total_timer_s: float | None
    avg_hr: int | None
    max_hr: int | None
    calories: int | None
    total_training_effect: float | None
    details: dict[str, Any]
    raw: Any
    source: str = "garmin-api"
    source_path: str = ""
    source_sha256: str = ""


def _numero(valore: Any) -> float | None:
    """Un numero, o None. Mai 0 al posto di "non pervenuto"."""
    if isinstance(valore, bool) or valore is None:
        return None
    if isinstance(valore, (int, float)):
        return float(valore)
    return None


def _intero(valore: Any) -> int | None:
    numero = _numero(valore)
    return None if numero is None else int(round(numero))


def _ora(testo: Any) -> datetime | None:
    """Garmin manda `2026-03-14 07:12:33` (senza fuso). None se manca."""
    if not isinstance(testo, str) or not testo.strip():
        return None
    for formato in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(testo.strip()[:26], formato)
        except ValueError:
            continue
    return None


def impronta(grezzo: Any) -> str:
    """Impronta stabile del payload: dice se un'attivita' e' cambiata."""
    canonico = json.dumps(grezzo, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def da_api(grezzo: dict[str, Any]) -> AttivitaResistenza:
    """Normalizza un'attivita' dell'elenco Garmin.

    L'identita' e' `garmin:<activityId>`, la stessa che usa il lettore di
    `.fit`: la stessa attivita' letta dai due lati non si duplica.
    """
    id_attivita = grezzo.get("activityId")
    if id_attivita is None:
        raise ValueError("attivita' senza activityId")
    tipo = (grezzo.get("activityType") or {})
    type_key = tipo.get("typeKey")
    gruppo = gruppo_di(type_key)

    locale = _ora(grezzo.get("startTimeLocal"))
    utc = _ora(grezzo.get("startTimeGMT")) or locale
    if utc is None:
        raise ValueError(f"attivita' {id_attivita} senza orario di inizio")
    offset = None
    if locale is not None and _ora(grezzo.get("startTimeGMT")) is not None:
        offset = int(round((locale - utc).total_seconds()))
    anno, settimana = (locale or utc).isocalendar()[:2]

    distanza_m = _numero(grezzo.get("distance"))
    durata_s = _numero(grezzo.get("duration"))
    movimento_s = _numero(grezzo.get("movingDuration"))
    velocita = _numero(grezzo.get("averageSpeed"))  # m/s
    # Passo: aritmetica sulla velocita' media misurata. Senza velocita' si
    # ricava da distanza e tempo; se manca uno dei due resta NULL.
    passo = None
    if velocita and velocita > 0:
        passo = 1000.0 / velocita
    elif distanza_m and distanza_m > 0:
        tempo = movimento_s or durata_s
        if tempo:
            passo = tempo / (distanza_m / 1000.0)
    passo_massimo = _numero(grezzo.get("maxSpeed"))
    falcata_cm = _numero(grezzo.get("avgStrideLength"))

    dettaglio = {
        "activity_type_key": type_key,
        "distance_km": None if distanza_m is None else distanza_m / 1000.0,
        "moving_time_s": movimento_s,
        "elevation_gain_m": _numero(grezzo.get("elevationGain")),
        "elevation_loss_m": _numero(grezzo.get("elevationLoss")),
        "avg_pace_s_km": passo,
        "avg_speed_kmh": None if velocita is None else velocita * 3.6,
        "max_speed_kmh": None if passo_massimo is None else passo_massimo * 3.6,
        "avg_cadence_spm": _numero(
            grezzo.get("averageRunningCadenceInStepsPerMinute")
            if grezzo.get("averageRunningCadenceInStepsPerMinute") is not None
            else grezzo.get("averageBikingCadenceInRevPerMinute")
        ),
        "avg_power_w": _numero(grezzo.get("avgPower")),
        "norm_power_w": _numero(grezzo.get("normPower")),
        "training_load": _numero(grezzo.get("activityTrainingLoad")),
        "aerobic_te": _numero(grezzo.get("aerobicTrainingEffect")),
        "anaerobic_te": _numero(grezzo.get("anaerobicTrainingEffect")),
        "vo2max": _numero(grezzo.get("vO2MaxValue")),
        "avg_ground_contact_ms": _numero(grezzo.get("avgGroundContactTime")),
        "avg_stride_m": None if falcata_cm is None else falcata_cm / 100.0,
    }
    for distanza in ("1000", "1609", "5000", "10000", "21098"):
        dettaglio[f"best_{distanza}_s"] = _numero(grezzo.get(f"fastestSplit_{distanza}"))

    return AttivitaResistenza(
        session_uid=f"garmin:{id_attivita}",
        garmin_activity_id=str(id_attivita),
        activity_type=TIPO_PER_GRUPPO.get(gruppo, "endurance"),
        activity_group=gruppo,
        activity_name=grezzo.get("activityName") or type_key,
        sport=type_key,
        sub_sport=None,
        start_time_utc=utc.isoformat(),
        start_time_local=locale.isoformat() if locale else None,
        local_date=(locale or utc).date().isoformat(),
        iso_year=anno,
        iso_week=settimana,
        utc_offset_s=offset,
        total_elapsed_s=_numero(grezzo.get("elapsedDuration")) or durata_s,
        total_timer_s=durata_s,
        avg_hr=_intero(grezzo.get("averageHR")),
        max_hr=_intero(grezzo.get("maxHR")),
        calories=_intero(grezzo.get("calories")),
        total_training_effect=_numero(grezzo.get("aerobicTrainingEffect")),
        details=dettaglio,
        raw=grezzo,
        source_path=f"garmin-api:activities/{id_attivita}",
        source_sha256=impronta(grezzo),
    )


# --------------------------------------------------------------------------
# normalizzazione del benessere
# --------------------------------------------------------------------------


def _giorni_utili(risposta: Any) -> list[dict[str, Any]]:
    dati = (risposta or {}).get("data") if isinstance(risposta, dict) else None
    return [
        g
        for g in (dati or [])
        if isinstance(g, dict) and (g.get("includesWellnessData") or g.get("includesActivityData"))
    ]


def _estrai_daily(risposta: Any) -> list[tuple[str, dict[str, Any]]]:
    return [(g["calendarDate"], g) for g in _giorni_utili(risposta) if g.get("calendarDate")]


def _estrai_sonno(risposta: Any) -> list[tuple[str, dict[str, Any]]]:
    righe = risposta if isinstance(risposta, list) else []
    return [
        (g["calendarDate"], g)
        for g in righe
        if isinstance(g, dict) and g.get("calendarDate") and g.get("sleepTimeSeconds")
    ]


def _estrai_hrv(risposta: Any) -> list[tuple[str, dict[str, Any]]]:
    righe = (risposta or {}).get("hrvSummaries") if isinstance(risposta, dict) else None
    return [
        (g["calendarDate"], g)
        for g in (righe or [])
        if isinstance(g, dict) and g.get("calendarDate") and g.get("lastNightAvg")
    ]


def _estrai_peso(risposta: Any) -> list[tuple[str, dict[str, Any]]]:
    righe = (risposta or {}).get("dailyWeightSummaries") if isinstance(risposta, dict) else None
    return [
        (g["summaryDate"], g)
        for g in (righe or [])
        if isinstance(g, dict) and g.get("summaryDate") and g.get("latestWeight")
    ]


def _passi(giorno: dict[str, Any]) -> Any:
    """I passi del giorno: Garmin li manda piatti o annidati, a seconda del campo."""
    if giorno.get("totalSteps") is not None:
        return giorno["totalSteps"]
    annidato = giorno.get("steps")
    if isinstance(annidato, dict):
        return annidato.get("totalSteps")
    return annidato


def _normalizza_daily(giorno: dict[str, Any]) -> dict[str, Any]:
    batteria = giorno.get("bodyBattery") or {}
    stress = _intero((giorno.get("stress") or {}).get("avgLevel"))
    return {
        # Garmin manda -1 quando non ha misurato: non e' uno zero di stress.
        "resting_hr": _intero((giorno.get("heartRate") or {}).get("restingValue")),
        "body_battery_max": _intero(batteria.get("maxValue")),
        "body_battery_min": _intero(batteria.get("minValue")),
        "stress_avg": stress if stress is not None and stress >= 0 else None,
        "steps": _intero(_passi(giorno)),
    }


def _normalizza_sonno(giorno: dict[str, Any]) -> dict[str, Any]:
    secondi = _numero(giorno.get("sleepTimeSeconds"))
    profondo = _numero(giorno.get("deepSleepSeconds"))
    rem = _numero(giorno.get("remSleepSeconds"))
    return {
        "sleep_h": None if secondi is None else round(secondi / 3600.0, 2),
        "sleep_score": _intero(
            ((giorno.get("sleepScores") or {}).get("overall") or {}).get("value")
        ),
        "deep_sleep_h": None if profondo is None else round(profondo / 3600.0, 2),
        "rem_sleep_h": None if rem is None else round(rem / 3600.0, 2),
    }


def _normalizza_hrv(giorno: dict[str, Any]) -> dict[str, Any]:
    base = giorno.get("baseline") or {}
    return {
        "hrv_last_night": _numero(giorno.get("lastNightAvg")),
        "hrv_weekly_avg": _numero(giorno.get("weeklyAvg")),
        "hrv_status": giorno.get("status"),
        "hrv_baseline_low": _numero(base.get("balancedLow")),
        "hrv_baseline_high": _numero(base.get("balancedUpper")),
    }


def _normalizza_peso(giorno: dict[str, Any]) -> dict[str, Any]:
    grammi = _numero((giorno.get("latestWeight") or {}).get("weight"))
    return {"body_weight_kg": None if grammi is None else round(grammi / 1000.0, 2)}


def _normalizza_profilo(profilo: dict[str, Any]) -> dict[str, Any]:
    """Eta' fitness e VO2max del giorno. Campi assenti -> NULL, come sempre."""
    eta = profilo.get("fitnessAge") if isinstance(profilo.get("fitnessAge"), dict) else profilo
    return {
        "fitness_age": _numero(eta.get("fitnessAge")),
        "chronological_age": _intero(eta.get("chronologicalAge")),
        "vo2max": _numero(eta.get("vo2MaxRunning") or eta.get("vo2MaxValue")),
    }


@dataclass(frozen=True)
class Dataset:
    """Una serie di dati quotidiani, con la sua finestra e il suo estrattore."""

    etichetta: str
    chiave: str
    giorni_finestra: int
    url: str
    estrai: Callable[[Any], list[tuple[str, dict[str, Any]]]]
    normalizza: Callable[[dict[str, Any]], dict[str, Any]]


#: Le quattro serie che servono per leggere il recupero. Le finestre sono
#: quelle che l'API regge: piu' larghe, risponde 500.
DATASET = (
    Dataset(
        "riepiloghi giornalieri",
        "daily",
        60,
        "/usersummary-service/v2/dailysummaries?startDate={a}&endDate={b}",
        _estrai_daily,
        _normalizza_daily,
    ),
    Dataset(
        "sonno",
        "sleep",
        31,
        "/sleep-service/sleep/dailySleepSummariesByDate?startDate={a}&endDate={b}",
        _estrai_sonno,
        _normalizza_sonno,
    ),
    Dataset(
        "HRV",
        "hrv",
        365,
        "/hrv-service/hrv/daily/{a}/{b}",
        _estrai_hrv,
        _normalizza_hrv,
    ),
    Dataset(
        "peso",
        "weight",
        90,
        "/weight-service/weight/range/{a}/{b}",
        _estrai_peso,
        _normalizza_peso,
    ),
)


# --------------------------------------------------------------------------
# sincronizzazione
# --------------------------------------------------------------------------


@dataclass
class RapportoGarmin:
    attivita_lette: int = 0
    attivita_nuove: list[str] = field(default_factory=list)
    attivita_aggiornate: list[str] = field(default_factory=list)
    attivita_invariate: list[str] = field(default_factory=list)
    attivita_saltate: list[tuple[str, str]] = field(default_factory=list)
    giorni_nuovi: int = 0
    giorni_aggiornati: int = 0
    dal: str | None = None
    al: str | None = None

    @property
    def ok(self) -> int:
        return len(self.attivita_nuove) + len(self.attivita_aggiornate)

    def as_lines(self) -> list[str]:
        righe = [
            f"Attivita' lette:     {self.attivita_lette}",
            f"Nuove:               {len(self.attivita_nuove)}",
            f"Aggiornate:          {len(self.attivita_aggiornate)}",
            f"Gia' presenti:       {len(self.attivita_invariate)}",
            f"Saltate:             {len(self.attivita_saltate)}",
            f"Giorni di benessere: {self.giorni_nuovi} nuovi, "
            f"{self.giorni_aggiornati} aggiornati",
        ]
        if self.dal and self.al:
            righe.append(f"Periodo:             dal {self.dal} al {self.al}")
        motivi: dict[str, int] = {}
        for _, motivo in self.attivita_saltate:
            motivi[motivo] = motivi.get(motivo, 0) + 1
        for motivo, quante in sorted(motivi.items(), key=lambda x: -x[1]):
            righe.append(f"  - {quante} x {motivo}")
        return righe


def _finestre(dal: date, al: date, giorni: int) -> Iterator[tuple[date, date]]:
    corrente = dal
    while corrente <= al:
        fine = min(corrente + timedelta(days=giorni - 1), al)
        yield corrente, fine
        corrente = fine + timedelta(days=1)


def sync_garmin(
    conn: Any,
    client: GarminClient,
    *,
    dal: date | None = None,
    giorni: int = 365,
    solo_attivita: bool = False,
    solo_benessere: bool = False,
    force: bool = False,
    dry_run: bool = False,
    oggi: date | None = None,
    registra: Callable[[str], None] | None = None,
) -> RapportoGarmin:
    """Scarica attivita' e benessere da Garmin e li scrive nel database.

    Senza `dal` riprende da dove era arrivata l'ultima sincronizzazione (meno
    i giorni di sovrapposizione), e al primo giro scende di `giorni` indietro.

    Con `dry_run` non si scrive nulla: serve a controllare chiave e
    connessione prima di toccare il database.
    """
    from . import db

    log = registra or (lambda _msg: None)
    fine = oggi or date.today()
    inizio = dal
    if inizio is None:
        ultimo = db.sync_state_get(conn, _STATO_BENESSERE)
        if ultimo and not force:
            inizio = date.fromisoformat(ultimo) - timedelta(days=GIORNI_SOVRAPPOSTI)
        else:
            inizio = fine - timedelta(days=giorni)
    inizio = min(inizio, fine)
    rapporto = RapportoGarmin(dal=inizio.isoformat(), al=fine.isoformat())

    if not solo_benessere:
        log("[attivita'] scarico l'elenco...")
        for grezzo in client.iter_attivita(dal=inizio):
            rapporto.attivita_lette += 1
            try:
                attivita = da_api(grezzo)
            except ValueError as exc:
                rapporto.attivita_saltate.append((str(grezzo.get("activityId", "?")), str(exc)))
                continue
            if attivita.local_date and attivita.local_date < inizio.isoformat():
                continue
            if attivita.activity_group == "palestra":
                rapporto.attivita_saltate.append(
                    (
                        attivita.session_uid,
                        "seduta di palestra: le serie arrivano da Hevy o dal .fit",
                    )
                )
                continue
            esistente = conn.execute(
                "SELECT activity_type, source_sha256 FROM sessions WHERE session_uid = ?",
                (attivita.session_uid,),
            ).fetchone()
            if esistente is not None and esistente["activity_type"] == "strength":
                rapporto.attivita_saltate.append(
                    (attivita.session_uid, "seduta di forza gia' nel database: non la sovrascrivo")
                )
                continue
            if (
                esistente is not None
                and not force
                and esistente["source_sha256"] == attivita.source_sha256
            ):
                rapporto.attivita_invariate.append(attivita.session_uid)
                continue
            if dry_run:
                destinazione = (
                    rapporto.attivita_aggiornate
                    if esistente is not None
                    else rapporto.attivita_nuove
                )
                destinazione.append(attivita.session_uid)
                continue
            _, era_presente = db.store_endurance_activity(conn, attivita)
            (rapporto.attivita_aggiornate if era_presente else rapporto.attivita_nuove).append(
                attivita.session_uid
            )
        log(f"    {rapporto.attivita_lette} attivita' lette, {rapporto.ok} scritte")

    if solo_attivita:
        return rapporto

    # Benessere: una lettura per serie e per finestra. I valori di piu' serie
    # confluiscono nella stessa riga del giorno, in upsert.
    for dataset in DATASET:
        log(f"[{dataset.etichetta}] dal {inizio} al {fine}")
        ampiezza = dataset.giorni_finestra
        corrente = inizio
        while corrente <= fine:
            termine = min(corrente + timedelta(days=ampiezza - 1), fine)
            try:
                risposta = client.get(
                    dataset.url.format(a=corrente.isoformat(), b=termine.isoformat())
                )
            except GarminError as exc:
                if exc.status == 500 and ampiezza > 1:
                    ampiezza = max(1, ampiezza // 2)
                    log(f"    risposta troppo grande, finestra ridotta a {ampiezza} giorni")
                    continue
                if exc.status in (400, 404, 500):
                    log(f"    salto {corrente} - {termine} (HTTP {exc.status})")
                    risposta = None
                else:
                    raise
            for giorno, elemento in dataset.estrai(risposta):
                valori = dataset.normalizza(elemento)
                if not any(v is not None for v in valori.values()):
                    continue
                if dry_run:
                    rapporto.giorni_nuovi += 1
                    continue
                era_nuovo = db.store_wellness_day(
                    conn, giorno, valori, raw={dataset.chiave: elemento}
                )
                if era_nuovo:
                    rapporto.giorni_nuovi += 1
                else:
                    rapporto.giorni_aggiornati += 1
            corrente = termine + timedelta(days=1)
            ampiezza = dataset.giorni_finestra

    # L'eta' fitness si chiede per un giorno solo: e' l'ultimo che abbiamo.
    log("[profilo] eta' fitness")
    profilo = client.profilo(fine)
    if profilo:
        valori = _normalizza_profilo(profilo)
        if any(v is not None for v in valori.values()) and not dry_run:
            if db.store_wellness_day(conn, fine.isoformat(), valori, raw={"profile": profilo}):
                rapporto.giorni_nuovi += 1
            else:
                rapporto.giorni_aggiornati += 1

    if not dry_run:
        db.sync_state_set(conn, _STATO_BENESSERE, fine.isoformat())
        db.sync_state_set(conn, _STATO_ATTIVITA, inizio.isoformat())
    return rapporto


# --------------------------------------------------------------------------
# importazione da file: lo storico gia' scaricato dall'altro progetto
# --------------------------------------------------------------------------

#: `nome file` -> dataset, per la cartella `data/` del progetto corsa.
FILE_DATASET = {f"{d.chiave}.json": d for d in DATASET}


def importa_cartella(
    conn: Any,
    cartella: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    registra: Callable[[str], None] | None = None,
) -> RapportoGarmin:
    """Importa lo storico Garmin gia' salvato come JSON, senza toccare la rete.

    E' la strada per recuperare in un colpo i mesi che il progetto corsa aveva
    gia' scaricato in `data/`: `activities.json` piu' i quattro file dei dati
    quotidiani. Stessi normalizzatori della sincronizzazione, quindi stessi
    campi e stessa identita': rifare poi `sync-garmin` non duplica niente.
    """
    from . import db

    log = registra or (lambda _msg: None)
    cartella = Path(cartella)
    rapporto = RapportoGarmin()

    attivita_file = cartella / "activities.json"
    if attivita_file.exists():
        grezze = json.loads(attivita_file.read_text(encoding="utf-8"))
        valori = grezze.values() if isinstance(grezze, dict) else grezze
        log(f"[attivita'] {attivita_file.name}")
        for grezzo in valori:
            rapporto.attivita_lette += 1
            try:
                attivita = da_api(grezzo)
            except (ValueError, AttributeError) as exc:
                rapporto.attivita_saltate.append((str(grezzo)[:40], str(exc)))
                continue
            if attivita.activity_group == "palestra":
                rapporto.attivita_saltate.append(
                    (
                        attivita.session_uid,
                        "seduta di palestra: le serie arrivano da Hevy o dal .fit",
                    )
                )
                continue
            esistente = conn.execute(
                "SELECT activity_type, source_sha256 FROM sessions WHERE session_uid = ?",
                (attivita.session_uid,),
            ).fetchone()
            if esistente is not None and esistente["activity_type"] == "strength":
                rapporto.attivita_saltate.append(
                    (attivita.session_uid, "seduta di forza gia' nel database: non la sovrascrivo")
                )
                continue
            if (
                esistente is not None
                and not force
                and esistente["source_sha256"] == attivita.source_sha256
            ):
                rapporto.attivita_invariate.append(attivita.session_uid)
                continue
            if dry_run:
                destinazione = (
                    rapporto.attivita_aggiornate
                    if esistente is not None
                    else rapporto.attivita_nuove
                )
                destinazione.append(attivita.session_uid)
                continue
            _, era_presente = db.store_endurance_activity(conn, attivita)
            (rapporto.attivita_aggiornate if era_presente else rapporto.attivita_nuove).append(
                attivita.session_uid
            )

    # `profile.json` dell'altro progetto: un'istantanea, senza data propria.
    # La si attribuisce all'ultimo giorno noto, che e' quello in cui l'ha letta.
    profilo_file = cartella / "profile.json"
    profilo: dict[str, Any] | None = None
    if profilo_file.exists():
        contenuto = json.loads(profilo_file.read_text(encoding="utf-8"))
        if isinstance(contenuto, dict):
            profilo = contenuto

    giorni: list[str] = []
    for nome, dataset in FILE_DATASET.items():
        percorso = cartella / nome
        if not percorso.exists():
            continue
        contenuto = json.loads(percorso.read_text(encoding="utf-8"))
        if not isinstance(contenuto, dict):
            log(f"    {nome}: non e' un indice per giorno, lo salto")
            continue
        log(f"[{dataset.etichetta}] {nome}: {len(contenuto)} giorni")
        for giorno, elemento in sorted(contenuto.items()):
            if not isinstance(elemento, dict):
                continue
            valori = dataset.normalizza(elemento)
            if not any(v is not None for v in valori.values()):
                continue
            giorni.append(giorno)
            if dry_run:
                rapporto.giorni_nuovi += 1
                continue
            if db.store_wellness_day(conn, giorno, valori, raw={dataset.chiave: elemento}):
                rapporto.giorni_nuovi += 1
            else:
                rapporto.giorni_aggiornati += 1

    if giorni and profilo:
        valori = _normalizza_profilo(profilo)
        if any(v is not None for v in valori.values()) and not dry_run:
            db.store_wellness_day(conn, max(giorni), valori, raw={"profile": profilo})

    if giorni:
        rapporto.dal, rapporto.al = min(giorni), max(giorni)
    if giorni and not dry_run:
        # Da qui in poi tocca alla rete: `sync-garmin` riprende dall'ultimo
        # giorno importato, non da un anno indietro.
        db.sync_state_set(conn, _STATO_BENESSERE, max(giorni))
    return rapporto
