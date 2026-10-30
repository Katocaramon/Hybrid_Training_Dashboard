"""Test dell'import degli export CSV di Hevy."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest

import fitgen
from strength_tracker import db
from strength_tracker import hevy
from strength_tracker import metrics as mt
from strength_tracker.cli import main
from strength_tracker.fit_parser import FitSkipped
from strength_tracker.ingest import ingest_path
from strength_tracker.mapping import load_mapping

MAPPATURA = "config/exercise_mapping.yaml"
INTESTAZIONE = [
    "title", "start_time", "end_time", "description", "exercise_title", "superset_id",
    "exercise_notes", "set_index", "set_type", "weight_kg", "reps", "distance_km",
    "duration_seconds", "rpe",
]


def riga(titolo, inizio, fine, esercizio, set_index=0, **extra):
    base = dict.fromkeys(INTESTAZIONE, "")
    base.update(
        title=titolo, start_time=inizio, end_time=fine, exercise_title=esercizio,
        set_index=set_index, set_type="normal",
    )
    base.update({k: v for k, v in extra.items()})
    return base


def scrivi_csv(path: Path, righe: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=INTESTAZIONE)
        w.writeheader()
        w.writerows(righe)
    return path


INIZIO = "Sep 21, 2026 at 6:25 AM"
FINE = "Sep 21, 2026 at 7:39 AM"


@pytest.fixture
def export(tmp_path):
    """Un export con una seduta realistica: carichi, corpo libero e un plank."""
    return scrivi_csv(
        tmp_path / "workouts.csv",
        [
            riga("Day 1 Upper Body", INIZIO, FINE, "Bench Press (Dumbbell)", 0, weight_kg="24", reps="10"),
            riga("Day 1 Upper Body", INIZIO, FINE, "Bench Press (Dumbbell)", 1, weight_kg="32", reps="10"),
            riga("Day 1 Upper Body", INIZIO, FINE, "Chin Up", 0, reps="5"),
            riga("Day 1 Upper Body", INIZIO, FINE, "Side Plank", 0, duration_seconds="23"),
            riga("Day 1 Upper Body", INIZIO, FINE, "Side Plank", 0, duration_seconds="22"),
        ],
    )


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "h.db")
    yield c
    c.close()


# --- riconoscimento del formato -------------------------------------------


def test_riconosce_un_export_hevy(export):
    assert hevy.e_un_csv_hevy(export)


def test_non_confonde_un_csv_di_correzioni(tmp_path):
    csv_correzioni = tmp_path / "correzioni.csv"
    csv_correzioni.write_text("data,serie,reps,peso_kg\n01/09/2026,7,10,24\n", encoding="utf-8")
    assert not hevy.e_un_csv_hevy(csv_correzioni)
    with pytest.raises(FitSkipped, match="non e' un export Hevy"):
        hevy.parse_csv(csv_correzioni)


def test_csv_vuoto(tmp_path):
    vuoto = scrivi_csv(tmp_path / "v.csv", [])
    with pytest.raises(FitSkipped, match="vuoto"):
        hevy.parse_csv(vuoto)


@pytest.mark.parametrize(
    "testo, atteso",
    [
        ("Sep 21, 2026 at 6:25 AM", datetime(2026, 9, 21, 6, 25)),
        ("Sep 21, 2026 at 6:25 PM", datetime(2026, 9, 21, 18, 25)),
        ("21 Sep 2026, 06:25", datetime(2026, 9, 21, 6, 25)),
        ("2026-09-21 06:25:00", datetime(2026, 9, 21, 6, 25)),
    ],
)
def test_formati_data(testo, atteso):
    assert hevy.parse_data(testo) == atteso


def test_data_incomprensibile():
    with pytest.raises(ValueError, match="non riconosciuta"):
        hevy.parse_data("domani mattina")


# --- struttura della seduta -----------------------------------------------


def test_una_seduta_per_allenamento(export):
    attivita = hevy.parse_csv(export)
    assert len(attivita) == 1
    a = attivita[0]
    assert a.session.workout_name == "Day 1 Upper Body"
    assert a.session.source == "hevy"
    assert a.session.start_time == datetime(2026, 9, 21, 6, 25)
    assert a.session.total_elapsed_s == pytest.approx(74 * 60)


def test_piu_allenamenti_nello_stesso_file(tmp_path):
    export = scrivi_csv(
        tmp_path / "w.csv",
        [
            riga("Day 1", INIZIO, FINE, "Bench Press (Dumbbell)", 0, weight_kg="24", reps="10"),
            riga("Day 2", "Sep 23, 2026 at 6:00 AM", "Sep 23, 2026 at 7:00 AM",
                 "Trap Bar Deadlift", 0, weight_kg="60", reps="8"),
        ],
    )
    attivita = hevy.parse_csv(export)
    assert [a.session.workout_name for a in attivita] == ["Day 1", "Day 2"]
    assert len({a.session_uid for a in attivita}) == 2


def test_indice_della_serie_e_l_ordine_nella_seduta(export):
    """`set_index` di Hevy riparte a ogni esercizio: non puo' essere la chiave."""
    a = hevy.parse_csv(export)[0]
    assert [s.index for s in a.sets] == [0, 1, 2, 3, 4]
    # nel file le due Side Plank hanno entrambe set_index 0
    planks = [s for s in a.sets if s.exercise_label == "Side Plank"]
    assert len(planks) == 2 and planks[0].index != planks[1].index


def test_chiave_grezza_col_prefisso(export):
    a = hevy.parse_csv(export)[0]
    assert a.sets[0].exercise_key == "hevy:Bench Press (Dumbbell)"
    assert a.sets[0].exercise_label == "Bench Press (Dumbbell)"


def test_carichi_e_volume(export):
    a = hevy.parse_csv(export)[0]
    assert (a.sets[0].repetitions, a.sets[0].weight_kg) == (10, 24.0)
    assert a.sets[0].volume_kg == pytest.approx(240.0)


def test_corpo_libero_senza_peso(export):
    a = hevy.parse_csv(export)[0]
    chin = next(s for s in a.sets if s.exercise_label == "Chin Up")
    assert chin.repetitions == 5
    assert chin.weight_kg is None and chin.volume_kg is None


def test_esercizio_a_tempo(export):
    a = hevy.parse_csv(export)[0]
    plank = next(s for s in a.sets if s.exercise_label == "Side Plank")
    assert plank.duration_s == pytest.approx(23.0)
    assert plank.repetitions is None  # un plank non ha ripetizioni


def test_cosa_hevy_non_da(export):
    a = hevy.parse_csv(export)[0]
    assert a.hr_samples == []            # nessuna frequenza cardiaca
    assert a.session.total_timer_s is None  # nessun tempo attivo
    assert a.session.utc_offset_s is None   # nessun fuso orario
    assert all(s.start_time is None for s in a.sets)  # nessun orario per serie


def test_uid_stabile_fra_letture(export):
    primo = hevy.parse_csv(export)[0].session_uid
    assert hevy.parse_csv(export)[0].session_uid == primo
    assert primo.startswith("hevy:20260921T0625")


def test_riga_senza_esercizio_segnalata(tmp_path):
    export = scrivi_csv(
        tmp_path / "w.csv",
        [
            riga("Day 1", INIZIO, FINE, "", 0, reps="10"),
            riga("Day 1", INIZIO, FINE, "Chin Up", 0, reps="5"),
        ],
    )
    a = hevy.parse_csv(export)[0]
    assert len(a.sets) == 1
    assert a.warnings and "senza nome" in a.warnings[0]


# --- ingestione ------------------------------------------------------------


def test_ingestione_di_un_export(conn, export):
    rep = ingest_path(conn, export.parent)
    assert len(rep.ingested) == 1 and rep.sets_written == 5
    assert conn.execute("SELECT source FROM sessions").fetchone()["source"] == "hevy"


def test_ingestione_idempotente(conn, export):
    ingest_path(conn, export.parent)
    rep = ingest_path(conn, export.parent)
    assert rep.ingested == [] and len(rep.already_present) == 1
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 1


def test_export_che_cresce_aggiunge_solo_il_nuovo(conn, tmp_path):
    """Hevy esporta tutto lo storico: il secondo export contiene il primo."""
    righe = [riga("Day 1", INIZIO, FINE, "Chin Up", 0, reps="5")]
    export = scrivi_csv(tmp_path / "fit" / "w.csv", righe)
    assert len(ingest_path(conn, export.parent).ingested) == 1

    righe.append(
        riga("Day 2", "Sep 23, 2026 at 6:00 AM", "Sep 23, 2026 at 7:00 AM",
             "Trap Bar Deadlift", 0, weight_kg="60", reps="8")
    )
    scrivi_csv(export, righe)
    rep = ingest_path(conn, export.parent)
    assert len(rep.ingested) == 1          # solo il nuovo allenamento
    assert len(rep.already_present) == 1   # il primo era gia' dentro
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 2


def test_fit_e_csv_nella_stessa_cartella(conn, tmp_path):
    cartella = tmp_path / "fit"
    fitgen.build_strength_fit(cartella / "a.fit", start=datetime(2026, 9, 3, 17, tzinfo=timezone.utc))
    scrivi_csv(cartella / "w.csv", [riga("Day 1", INIZIO, FINE, "Chin Up", 0, reps="5")])
    rep = ingest_path(conn, cartella)
    assert len(rep.ingested) == 2
    sorgenti = {r["source"] for r in conn.execute("SELECT source FROM sessions")}
    assert sorgenti == {"garmin", "hevy"}


def test_un_csv_non_hevy_viene_saltato_senza_fermare_il_batch(conn, tmp_path, export):
    (export.parent / "correzioni.csv").write_text("data,serie\n01/09/2026,7\n", encoding="utf-8")
    rep = ingest_path(conn, export.parent)
    assert len(rep.ingested) == 1
    assert len(rep.skipped) == 1 and "Hevy" in rep.skipped[0][1]


# --- mappatura e metriche --------------------------------------------------


def test_stessa_voce_per_garmin_e_hevy(conn, tmp_path):
    """E' questo che rende continua la storia di un esercizio."""
    m = load_mapping(MAPPATURA)
    garmin = m.by_raw_key["bench_press/dumbbell_bench_press"]
    hevy_key = m.by_raw_key["hevy:Bench Press (Dumbbell)"]
    assert garmin.name == hevy_key.name == "Dumbbell bench press"


def test_metriche_dichiarano_cosa_manca(conn, export):
    ingest_path(conn, export.parent)
    db.refresh_exercise_map(conn, load_mapping(MAPPATURA).as_rows())
    s = mt.sedute(conn)[0]
    assert s["source"] == "hevy"
    assert s["densita_base"] == "tempo totale"  # non c'e' il tempo attivo
    assert s["rapporto_lavoro_riposo"] is None  # Hevy non registra le pause
    assert s["fc_deriva_bpm"] is None           # niente frequenza cardiaca
    assert s["volume_kg"] == pytest.approx(240 + 320)


def test_riepilogo_conta_le_sorgenti(conn, tmp_path, export):
    fitgen.build_strength_fit(export.parent / "a.fit", start=datetime(2026, 9, 3, 17, tzinfo=timezone.utc))
    ingest_path(conn, export.parent)
    assert mt.riepilogo(conn)["sedute_per_sorgente"] == {"garmin": 1, "hevy": 1}


def test_seduta_registrata_due_volte_viene_segnalata(conn, tmp_path):
    """Stessa seduta su orologio e su Hevy: il volume sarebbe contato due volte."""
    cartella = tmp_path / "fit"
    fitgen.build_strength_fit(cartella / "a.fit", start=datetime(2026, 9, 3, 15, 30, tzinfo=timezone.utc))
    scrivi_csv(
        cartella / "w.csv",
        [riga("Day 1", "Sep 3, 2026 at 5:35 PM", "Sep 3, 2026 at 6:30 PM",
              "Chin Up", 0, reps="5")],
    )
    ingest_path(conn, cartella)
    doppie = mt.sedute_doppie(conn)
    assert len(doppie) == 1
    assert {"garmin", "hevy"} <= {d["seduta_a"].split(":")[0] for d in doppie} | {
        d["seduta_b"].split(":")[0] for d in doppie
    }
    assert mt.anomalie(conn)["sedute_doppie"] == doppie


def test_sedute_in_giorni_diversi_non_sono_doppie(conn, tmp_path, export):
    fitgen.build_strength_fit(export.parent / "a.fit", start=datetime(2026, 9, 3, 17, tzinfo=timezone.utc))
    ingest_path(conn, export.parent)
    assert mt.sedute_doppie(conn) == []


# --- CLI -------------------------------------------------------------------


def test_cli_ingest_e_stats_su_hevy(tmp_path, export, capsys):
    dbfile = tmp_path / "cli.db"
    assert main(["--db", str(dbfile), "ingest", str(export)]) == 0
    capsys.readouterr()
    assert main(["--db", str(dbfile), "stats"]) == 0
    out = capsys.readouterr().out
    assert "1 hevy" in out
    assert "21/09/2026" in out


# =========================================================================
# payload dell'API
# =========================================================================

#: Stessa forma del payload reale di `/v1/workouts`: serie annidate dentro
#: gli esercizi, orari in UTC, `supersets_id` e `distance_meters`.
def payload_api(**extra):
    w = {
        "id": "e4810419-6391-4556-9924-508a20534c1f",
        "title": "Day 2 - Lower Body",
        "start_time": "2026-09-24T04:00:38+00:00",
        "end_time": "2026-09-24T05:23:31+00:00",
        "exercises": [
            {
                "index": 0,
                "title": "Rowing Machine",
                "exercise_template_id": "0222DB42",
                "sets": [{"index": 0, "type": "normal", "distance_meters": 1500,
                          "duration_seconds": 360}],
            },
            {
                "index": 1,
                "title": "Deadlift (Trap bar)",
                "exercise_template_id": "B923B230",
                "sets": [
                    {"index": 0, "type": "warmup", "weight_kg": 40, "reps": 5},
                    {"index": 1, "type": "normal", "weight_kg": 70, "reps": 5},
                ],
            },
            {
                "index": 2,
                "title": "Copenhagen plank",
                "exercise_template_id": "f3e4b7ac",
                "supersets_id": 3,
                "sets": [{"index": 0, "type": "normal", "duration_seconds": 32}],
            },
        ],
    }
    w.update(extra)
    return {"workout": w}


@pytest.fixture
def roma(monkeypatch):
    """Gli orari dell'API sono UTC: serve un fuso per datarli."""
    monkeypatch.setenv("STRENGTH_TRACKER_TZ", "Europe/Rome")


def scrivi_json(path: Path, dati) -> Path:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dati), encoding="utf-8")
    return path


def test_riconosce_il_payload_api():
    assert hevy.e_un_json_hevy(payload_api())
    assert hevy.e_un_json_hevy({"workouts": [payload_api()["workout"]]})
    assert hevy.e_un_json_hevy(payload_api()["workout"])
    assert not hevy.e_un_json_hevy({"qualcosa": "altro"})


def test_parse_api_struttura(roma, tmp_path):
    a = hevy.parse_json(scrivi_json(tmp_path / "w.json", payload_api()))[0]
    assert a.session.source == "hevy"
    assert a.session.workout_name == "Day 2 - Lower Body"
    assert len(a.sets) == 4  # 1 + 2 + 1, appiattite in ordine di seduta
    assert [s.index for s in a.sets] == [0, 1, 2, 3]
    assert a.hr_samples == []


def test_parse_api_orari_in_utc_con_fuso_locale(roma, tmp_path):
    a = hevy.parse_json(scrivi_json(tmp_path / "w.json", payload_api()))[0]
    # 04:00 UTC in settembre = 06:00 a Roma (ora legale)
    assert a.session.start_time.hour == 4
    assert a.session.utc_offset_s == 7200
    assert a.session.start_time_local.strftime("%Y-%m-%d %H:%M") == "2026-09-24 06:00"


def test_parse_api_ora_solare(roma, tmp_path):
    """Dopo il cambio d'ora l'offset e' diverso: lo dice il fuso, non noi."""
    dati = payload_api(start_time="2026-10-29T05:48:54+00:00",
                       end_time="2026-10-29T07:13:19+00:00")
    a = hevy.parse_json(scrivi_json(tmp_path / "w.json", dati))[0]
    assert a.session.utc_offset_s == 3600
    assert a.session.start_time_local.strftime("%H:%M") == "06:48"


def test_api_e_csv_danno_lo_stesso_uid(roma, tmp_path):
    """Passare dal CSV all'API non deve duplicare le sedute."""
    da_api = hevy.parse_json(scrivi_json(tmp_path / "w.json", payload_api()))[0]
    da_csv = hevy.parse_csv(
        scrivi_csv(
            tmp_path / "w.csv",
            [riga("Day 2 - Lower Body", "Sep 24, 2026 at 6:00 AM",
                  "Sep 24, 2026 at 7:23 AM", "Deadlift (Trap bar)", 0,
                  weight_kg="70", reps="5")],
        )
    )[0]
    assert da_api.session_uid == da_csv.session_uid


def test_parse_api_campi_delle_serie(roma, tmp_path):
    a = hevy.parse_json(scrivi_json(tmp_path / "w.json", payload_api()))[0]
    vogatore, riscaldamento, lavoro, plank = a.sets
    assert vogatore.duration_s == 360 and vogatore.distance_km == pytest.approx(1.5)
    assert riscaldamento.set_kind == "warmup"   # distinguibile dalle serie vere
    assert (lavoro.weight_kg, lavoro.repetitions) == (70.0, 5)
    assert lavoro.volume_kg == pytest.approx(350.0)
    assert plank.superset_id == "3" and plank.duration_s == 32
    assert plank.repetitions is None
    assert all(s.exercise_key.startswith("hevy:") for s in a.sets)


def test_parse_api_piu_allenamenti(roma, tmp_path):
    primo = payload_api()["workout"]
    secondo = dict(primo, id="altro", title="Day 1", start_time="2026-09-21T04:25:08+00:00",
                   end_time="2026-09-21T05:39:25+00:00")
    attivita = hevy.parse_json(scrivi_json(tmp_path / "w.json", {"workouts": [primo, secondo]}))
    assert len(attivita) == 2
    assert len({a.session_uid for a in attivita}) == 2


def test_allenamento_senza_orario_saltato_senza_fermare_gli_altri(roma, tmp_path):
    rotto = dict(payload_api()["workout"], id="rotto", start_time=None)
    buono = payload_api()["workout"]
    attivita = hevy.parse_json(scrivi_json(tmp_path / "w.json", {"workouts": [rotto, buono]}))
    assert len(attivita) == 1


def test_json_non_hevy_saltato(tmp_path):
    with pytest.raises(FitSkipped, match="non e' un payload"):
        hevy.parse_json(scrivi_json(tmp_path / "x.json", {"foo": [1, 2]}))


def test_json_malformato_saltato(tmp_path):
    rotto = tmp_path / "x.json"
    rotto.write_text("{non json", encoding="utf-8")
    with pytest.raises(FitSkipped, match="JSON illeggibile"):
        hevy.parse_json(rotto)


def test_ingestione_del_payload_api(conn, roma, tmp_path):
    cartella = tmp_path / "fit"
    scrivi_json(cartella / "api.json", payload_api())
    rep = ingest_path(conn, cartella)
    assert len(rep.ingested) == 1 and rep.sets_written == 4
    r = conn.execute("SELECT source, local_date FROM sessions").fetchone()
    assert (r["source"], r["local_date"]) == ("hevy", "2026-09-24")


def test_csv_e_json_dello_stesso_allenamento_non_si_duplicano(conn, roma, tmp_path):
    cartella = tmp_path / "fit"
    scrivi_json(cartella / "api.json", payload_api())
    scrivi_csv(
        cartella / "export.csv",
        [riga("Day 2 - Lower Body", "Sep 24, 2026 at 6:00 AM",
              "Sep 24, 2026 at 7:23 AM", "Deadlift (Trap bar)", 0,
              weight_kg="70", reps="5")],
    )
    ingest_path(conn, cartella)
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 1


def test_misura_del_lavoro_per_esercizi_a_tempo(conn, roma, tmp_path):
    """Un plank non ha ripetizioni: a misurarlo e' il tempo sotto tensione."""
    cartella = tmp_path / "fit"
    scrivi_json(cartella / "api.json", payload_api())
    ingest_path(conn, cartella)
    db.refresh_exercise_map(conn, load_mapping(MAPPATURA).as_rows())

    plank = mt.progressione(conn, "Copenhagen plank")
    assert plank["misura_lavoro"] == "durata"
    assert plank["punti"][0]["durata_totale_s"] == pytest.approx(32.0)
    assert plank["punti"][0]["reps_totali"] == 0

    stacco = mt.progressione(conn, "Trap bar deadlift")
    assert stacco["misura_lavoro"] == "reps"
    assert stacco["punti"][0]["reps_totali"] == 10  # 5 di riscaldamento + 5


# =========================================================================
# client dell'API e sync
# =========================================================================


class FintaApi:
    """API di Hevy finta: serve pagine vere e registra cosa le viene chiesto.

    Non prova a essere la rete: prova la paginazione, il recupero del
    dettaglio e la traduzione degli errori. Che l'indirizzo e l'header siano
    quelli giusti lo dice solo una chiamata reale, ed e' a questo che serve
    `sync-hevy --dry-run`.
    """

    def __init__(self, pagine, dettagli=None, errore=None):
        self.pagine = pagine
        self.dettagli = dettagli or {}
        self.errore = errore
        self.chiamate = []

    def __call__(self, url, headers, timeout):
        import json as _json

        self.chiamate.append((url, headers))
        if self.errore:
            raise hevy.HevyAPIError(self.errore)
        if "/workouts/" in url:
            wid = url.rsplit("/", 1)[-1]
            return _json.dumps({"workout": self.dettagli[wid]}).encode()
        pagina = int(url.split("page=")[1].split("&")[0])
        return _json.dumps(self.pagine[pagina - 1]).encode()


def workout(idw="w1", titolo="Day 1", inizio="2026-09-24T04:00:38+00:00", esercizi=None):
    return {
        "id": idw,
        "title": titolo,
        "start_time": inizio,
        "end_time": "2026-09-24T05:23:31+00:00",
        "exercises": esercizi
        if esercizi is not None
        else [
            {
                "index": 0,
                "title": "Deadlift (Trap bar)",
                "sets": [{"index": 0, "type": "normal", "weight_kg": 70, "reps": 5}],
            }
        ],
    }


def test_client_richiede_la_chiave():
    client = hevy.HevyClient(api_key="", transport=FintaApi([{"workouts": []}]))
    with pytest.raises(hevy.HevyAPIError, match="manca la chiave API"):
        list(client.iter_allenamenti())


def test_client_manda_la_chiave_nell_header():
    finta = FintaApi([{"workouts": [workout()], "page_count": 1}])
    client = hevy.HevyClient(api_key="segreta", transport=finta)
    list(client.iter_allenamenti())
    url, headers = finta.chiamate[0]
    assert headers["api-key"] == "segreta"
    assert url.startswith("https://api.hevyapp.com/v1/workouts?")
    assert "page=1" in url and "pageSize=10" in url


def test_client_percorre_tutte_le_pagine():
    """L'ordine delle pagine non e' garantito per data: non ci si ferma prima."""
    finta = FintaApi(
        [
            {"workouts": [workout("a")], "page_count": 2, "has_next_page": True},
            {"workouts": [workout("b", inizio="2026-09-21T04:25:08+00:00")],
             "page_count": 2, "has_next_page": False},
        ]
    )
    client = hevy.HevyClient(api_key="k", transport=finta)
    assert [w["id"] for w in client.iter_allenamenti()] == ["a", "b"]


def test_client_chiede_il_dettaglio_se_la_pagina_non_lo_porta():
    sommario = {"id": "a", "title": "Day 1", "start_time": "2026-09-24T04:00:38+00:00"}
    finta = FintaApi([{"workouts": [sommario], "page_count": 1}], dettagli={"a": workout("a")})
    client = hevy.HevyClient(api_key="k", transport=finta)
    (w,) = list(client.iter_allenamenti())
    assert "exercises" in w
    assert any("/workouts/a" in url for url, _ in finta.chiamate)


@pytest.mark.parametrize(
    "codice, atteso",
    [
        (401, "rifiutato la chiave"),
        (403, "rifiutato la chiave"),
        (404, "endpoint non trovato"),
        (429, "troppe richieste"),
        (500, "HTTP 500"),
    ],
)
def test_errori_http_spiegati(codice, atteso):
    assert atteso in hevy._spiega_http(codice, "https://api.hevyapp.com/v1/workouts")


def test_risposta_non_json():
    finta = lambda url, headers, timeout: b"<html>errore</html>"
    client = hevy.HevyClient(api_key="k", transport=finta)
    with pytest.raises(hevy.HevyAPIError, match="non JSON"):
        list(client.iter_allenamenti())


# --- sync ------------------------------------------------------------------


def _client(*allenamenti):
    return hevy.HevyClient(
        api_key="k",
        transport=FintaApi([{"workouts": list(allenamenti), "page_count": 1}]),
    )


def test_sync_importa(conn, roma):
    from strength_tracker.ingest import sync_hevy

    rep = sync_hevy(conn, _client(workout()))
    assert len(rep.ingested) == 1 and rep.sets_written == 1
    r = conn.execute("SELECT source, local_date FROM sessions").fetchone()
    assert (r["source"], r["local_date"]) == ("hevy", "2026-09-24")


def test_sync_non_riporta_quelle_gia_presenti(conn, roma):
    from strength_tracker.ingest import sync_hevy

    sync_hevy(conn, _client(workout()))
    rep = sync_hevy(conn, _client(workout()))
    assert rep.ingested == [] and len(rep.already_present) == 1
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 1


def test_sync_non_duplica_quelle_arrivate_dal_csv(conn, roma, tmp_path):
    """Chi ha importato il CSV e poi passa all'API non si ritrova niente doppio."""
    from strength_tracker.ingest import ingest_path, sync_hevy

    cartella = tmp_path / "fit"
    scrivi_csv(
        cartella / "export.csv",
        [riga("Day 1", "Sep 24, 2026 at 6:00 AM", "Sep 24, 2026 at 7:23 AM",
              "Deadlift (Trap bar)", 0, weight_kg="70", reps="5")],
    )
    ingest_path(conn, cartella)
    rep = sync_hevy(conn, _client(workout()))
    assert rep.ingested == [] and len(rep.already_present) == 1
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 1


def test_sync_dry_run_non_scrive(conn, roma):
    from strength_tracker.ingest import sync_hevy

    rep = sync_hevy(conn, _client(workout()), dry_run=True)
    assert len(rep.ingested) == 1  # dice cosa entrerebbe
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 0


def test_sync_force_riscrive(conn, roma):
    from strength_tracker.ingest import sync_hevy

    sync_hevy(conn, _client(workout()))
    rep = sync_hevy(conn, _client(workout()), force=True)
    assert len(rep.updated) == 1
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 1


def test_sync_salta_un_allenamento_rotto_senza_fermarsi(conn, roma):
    from strength_tracker.ingest import sync_hevy

    rotto = workout("rotto", inizio=None)
    rep = sync_hevy(conn, _client(rotto, workout("buono")))
    assert len(rep.ingested) == 1 and len(rep.skipped) == 1


def test_cli_sync_senza_chiave(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("HEVY_API_KEY", raising=False)
    assert main(["--db", str(tmp_path / "x.db"), "sync-hevy"]) == 2
    assert "manca la chiave API" in capsys.readouterr().err


def test_cli_sync_errore_di_rete(tmp_path, capsys, monkeypatch):
    import strength_tracker.hevy as h

    monkeypatch.setenv("HEVY_API_KEY", "k")
    monkeypatch.setattr(
        h, "_transport_urllib",
        lambda url, headers, timeout: (_ for _ in ()).throw(h.HevyAPIError("non riesco a contattare Hevy")),
    )
    assert main(["--db", str(tmp_path / "x.db"), "sync-hevy"]) == 2
    assert "non riesco a contattare Hevy" in capsys.readouterr().err
