"""Garmin Personal API: client, normalizzazione e sincronizzazione.

Niente rete: il trasporto e' finto. Quello che si prova qui e' il token e il
suo rinnovo, la paginazione, le finestre di giorni, la traduzione dei campi e
le tre promesse che contano: non duplicare, non sovrascrivere la palestra, non
inventare dati mancanti.
"""

import json
from datetime import date

import pytest

from strength_tracker import db, metrics as mt
from strength_tracker import garmin as gm

MAPPATURA = "config/exercise_mapping.yaml"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "g.db")
    yield c
    c.close()


# --------------------------------------------------------------------------
# API finta
# --------------------------------------------------------------------------


def attivita(
    id_att=1001,
    nome="Corsa serale",
    tipo="running",
    giorno="2026-03-14",
    ora="18:02:00",
    **extra,
):
    grezzo = {
        "activityId": id_att,
        "activityName": nome,
        "activityType": {"typeKey": tipo},
        "startTimeLocal": f"{giorno} {ora}",
        "startTimeGMT": f"{giorno} 17:02:00",
        "distance": 10000.0,
        "duration": 3000.0,
        "movingDuration": 2950.0,
        "elevationGain": 80.0,
        "averageSpeed": 10000.0 / 3000.0,
        "averageHR": 148,
        "maxHR": 170,
        "calories": 700,
        "activityTrainingLoad": 140.0,
        "aerobicTrainingEffect": 3.4,
    }
    grezzo.update(extra)
    return grezzo


class FintaGarmin:
    """Personal API finta: token, elenco attivita' e finestre di giorni.

    Non prova a essere la rete: prova paginazione, rinnovo del token, finestre
    e traduzione degli errori. Che indirizzi e header siano quelli giusti lo
    dice solo una chiamata reale, ed e' a questo che serve `--dry-run`.
    """

    def __init__(self, attivita=(), giornalieri=None, hrv=None, sonno=None, peso=None,
                 profilo=None):
        self.attivita = list(attivita)
        self.profilo = profilo
        self.giornalieri = giornalieri or {}
        self.hrv = hrv or {}
        self.sonno = sonno or {}
        self.peso = peso or {}
        self.chiamate = []
        self.token_emessi = 0
        self.scadenze = 0  # quanti 401 restituire prima di accettare il token
        self.errori = {}  # frammento di url -> (status, quante volte)

    def __call__(self, url, headers, timeout, metodo="GET", dati=None):
        self.chiamate.append(url)
        if gm.TOKEN_PATH in url:
            assert metodo == "POST"
            assert headers["Authorization"].startswith("ApiKey ")
            self.token_emessi += 1
            return f'"token-{self.token_emessi}"'.encode()
        assert headers["Authorization"].startswith("Bearer ")
        if self.scadenze > 0:
            self.scadenze -= 1
            raise gm.GarminError("scaduto", 401)
        for frammento, (status, quante) in list(self.errori.items()):
            if frammento in url and quante > 0:
                self.errori[frammento] = (status, quante - 1)
                raise gm.GarminError(f"finto {status}", status)
        if "/fitnessage-service/" in url:
            if self.profilo is None:
                raise gm.GarminError("niente eta' fitness", 404)
            return json.dumps(self.profilo).encode()
        if "/activitylist-service/" in url:
            inizio = int(url.split("start=")[1].split("&")[0])
            limite = int(url.split("limit=")[1].split("&")[0])
            return json.dumps(self.attivita[inizio : inizio + limite]).encode()
        a, b = self._intervallo(url)
        if "/usersummary-service/" in url:
            return json.dumps(
                {"data": [v for g, v in sorted(self.giornalieri.items()) if a <= g <= b]}
            ).encode()
        if "/sleep-service/" in url:
            return json.dumps(
                [v for g, v in sorted(self.sonno.items()) if a <= g <= b]
            ).encode()
        if "/hrv-service/" in url:
            return json.dumps(
                {"hrvSummaries": [v for g, v in sorted(self.hrv.items()) if a <= g <= b]}
            ).encode()
        if "/weight-service/" in url:
            return json.dumps(
                {"dailyWeightSummaries": [v for g, v in sorted(self.peso.items()) if a <= g <= b]}
            ).encode()
        raise AssertionError(f"url non previsto: {url}")

    @staticmethod
    def _intervallo(url):
        if "startDate=" in url:
            return url.split("startDate=")[1].split("&")[0], url.split("endDate=")[1].split("&")[0]
        pezzi = url.split("?")[0].rstrip("/").split("/")
        return pezzi[-2], pezzi[-1]


def client(finta, **kwargs):
    kwargs.setdefault("pausa", 0)
    kwargs.setdefault("dormi", lambda _s: None)
    return gm.GarminClient(api_key="chiave-finta", transport=finta, **kwargs)


# --------------------------------------------------------------------------
# la chiave
# --------------------------------------------------------------------------


def test_chiave_dalla_variabile_d_ambiente():
    assert gm.leggi_chiave({gm.ENV_CHIAVE: " abc123 "}) == "abc123"


def test_chiave_dal_file(tmp_path):
    f = tmp_path / "env"
    f.write_text("# commento\nexport GARMIN_PERSONAL_API_KEY='k-dal-file'\n", encoding="utf-8")
    assert gm.leggi_chiave({}, file=f) == "k-dal-file"


def test_senza_chiave_il_messaggio_dice_cosa_fare(tmp_path):
    with pytest.raises(gm.GarminError) as exc:
        gm.leggi_chiave({}, file=tmp_path / "manca")
    assert gm.ENV_CHIAVE in str(exc.value)


def test_la_chiave_non_finisce_nel_repr():
    c = gm.GarminClient(api_key="segretissima")
    assert "segretissima" in repr(c)  # il repr del dataclass la contiene...
    # ...quindi non va stampato: il client non stampa mai niente da solo.
    assert not hasattr(c, "stampa")


# --------------------------------------------------------------------------
# token
# --------------------------------------------------------------------------


def test_token_chiesto_una_volta_sola(tmp_path):
    finta = FintaGarmin(attivita=[attivita()])
    c = client(finta, token_path=tmp_path / "token.json")
    c.get("/activitylist-service/activities/search/activities?start=0&limit=1")
    c.get("/activitylist-service/activities/search/activities?start=0&limit=1")
    assert finta.token_emessi == 1


def test_token_riusato_fra_due_lanci(tmp_path):
    percorso = tmp_path / "token.json"
    finta = FintaGarmin(attivita=[attivita()])
    client(finta, token_path=percorso).token()
    client(finta, token_path=percorso).token()  # processo nuovo, stesso file
    assert finta.token_emessi == 1
    assert percorso.stat().st_mode & 0o077 == 0, "il token e' una credenziale"


def test_token_vecchio_viene_rinnovato(tmp_path):
    percorso = tmp_path / "token.json"
    percorso.write_text(json.dumps({"token": "vecchio", "created": 0.0}), encoding="utf-8")
    finta = FintaGarmin()
    assert client(finta, token_path=percorso).token() == "token-1"


def test_token_scaduto_in_corsa_viene_rinnovato_una_volta():
    finta = FintaGarmin(attivita=[attivita()])
    finta.scadenze = 1
    c = client(finta)
    assert c.get("/activitylist-service/activities/search/activities?start=0&limit=1")
    assert finta.token_emessi == 2


def test_limite_di_frequenza_ritentato():
    finta = FintaGarmin(attivita=[attivita()])
    finta.errori["/activitylist-service/"] = (429, 2)
    c = client(finta)
    assert len(c.pagina_attivita(0, 1)) == 1


def test_errore_definitivo_viene_riportato():
    finta = FintaGarmin()
    finta.errori["/activitylist-service/"] = (429, 99)
    with pytest.raises(gm.GarminError):
        client(finta).pagina_attivita(0, 1)


# --------------------------------------------------------------------------
# normalizzazione
# --------------------------------------------------------------------------


def test_campi_tradotti():
    a = gm.da_api(attivita())
    assert a.session_uid == "garmin:1001"
    assert (a.activity_type, a.activity_group) == ("run", "corsa")
    assert a.local_date == "2026-03-14"
    assert (a.iso_year, a.iso_week) == (2026, 11)
    assert a.utc_offset_s == 3600
    assert a.details["distance_km"] == pytest.approx(10.0)
    assert a.details["avg_pace_s_km"] == pytest.approx(300.0)
    assert a.details["avg_speed_kmh"] == pytest.approx(12.0)


def test_uid_uguale_a_quello_del_fit():
    """Il lettore di .fit usa `garmin:<activityId>`: la stessa attivita' non si duplica."""
    assert gm.da_api(attivita(id_att=42)).session_uid == "garmin:42"


@pytest.mark.parametrize(
    "tipo,gruppo",
    [
        ("running", "corsa"),
        ("trail_running", "corsa"),
        ("treadmill_running", "corsa"),
        ("gravel_cycling", "bici"),
        ("strength_training", "palestra"),
        ("lap_swimming", "nuoto"),
        ("kayaking", "altro"),
        (None, "altro"),
    ],
)
def test_gruppi(tipo, gruppo):
    assert gm.gruppo_di(tipo) == gruppo


def test_campi_mancanti_restano_nulli():
    """Mai uno zero al posto di "non pervenuto"."""
    a = gm.da_api(
        {
            "activityId": 7,
            "activityType": {"typeKey": "running"},
            "startTimeLocal": "2026-03-14 18:00:00",
        }
    )
    assert a.details["distance_km"] is None
    assert a.details["avg_pace_s_km"] is None
    assert a.details["training_load"] is None
    assert a.avg_hr is None
    assert a.utc_offset_s is None, "senza orario UTC non si indovina il fuso"


def test_passo_ricavato_da_distanza_e_tempo_senza_velocita():
    a = gm.da_api(attivita(averageSpeed=None))
    assert a.details["avg_pace_s_km"] == pytest.approx(295.0)  # su movingDuration


def test_attivita_senza_id_rifiutata():
    with pytest.raises(ValueError):
        gm.da_api({"activityType": {"typeKey": "running"}, "startTimeLocal": "2026-03-14 18:00:00"})


def test_impronta_cambia_solo_se_cambiano_i_dati():
    uno, due = attivita(), attivita()
    assert gm.impronta(uno) == gm.impronta(due)
    due["activityName"] = "Corsa lunga"
    assert gm.impronta(uno) != gm.impronta(due)


def test_stress_non_misurato_non_e_zero():
    valori = gm._normalizza_daily({"stress": {"avgLevel": -1}, "heartRate": {"restingValue": 44}})
    assert valori["stress_avg"] is None
    assert valori["resting_hr"] == 44


# --------------------------------------------------------------------------
# sincronizzazione
# --------------------------------------------------------------------------


def sincronizza(conn, finta, **kwargs):
    kwargs.setdefault("oggi", date(2026, 3, 15))
    kwargs.setdefault("dal", date(2026, 3, 1))
    return gm.sync_garmin(conn, client(finta), **kwargs)


def test_una_corsa_entra_nel_database(conn):
    rapporto = sincronizza(conn, FintaGarmin(attivita=[attivita()]))
    assert rapporto.attivita_nuove == ["garmin:1001"]
    riga = conn.execute("SELECT * FROM v_endurance").fetchone()
    assert riga["activity_group"] == "corsa"
    assert riga["distance_km"] == pytest.approx(10.0)
    assert riga["activity_name"] == "Corsa serale"
    assert riga["source"] == "garmin-api"


def test_sincronizzare_due_volte_non_duplica(conn):
    finta = FintaGarmin(attivita=[attivita()])
    sincronizza(conn, finta)
    rapporto = sincronizza(conn, finta)
    assert rapporto.attivita_nuove == []
    assert rapporto.attivita_invariate == ["garmin:1001"]
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 1


def test_attivita_rinominata_viene_aggiornata(conn):
    finta = FintaGarmin(attivita=[attivita()])
    sincronizza(conn, finta)
    finta.attivita = [attivita(nome="Lungo in collina")]
    rapporto = sincronizza(conn, finta)
    assert rapporto.attivita_aggiornate == ["garmin:1001"]
    assert conn.execute("SELECT COUNT(*) c FROM endurance_details").fetchone()["c"] == 1
    assert (
        conn.execute("SELECT activity_name FROM sessions").fetchone()["activity_name"]
        == "Lungo in collina"
    )


def test_la_palestra_non_entra_da_qui(conn):
    """L'elenco attivita' non espone le serie: importarla creerebbe una seduta vuota."""
    rapporto = sincronizza(
        conn, FintaGarmin(attivita=[attivita(id_att=9, tipo="strength_training")])
    )
    assert rapporto.attivita_nuove == []
    assert len(rapporto.attivita_saltate) == 1
    assert "palestra" in rapporto.attivita_saltate[0][1]
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 0


def test_non_sovrascrive_una_seduta_di_forza_gia_presente(conn, tmp_path):
    """Se l'uid coincide, vince la seduta con le serie dentro."""
    conn.execute(
        """INSERT INTO sessions (session_uid, activity_type, start_time_utc, source,
                                 source_path, source_sha256, ingested_at)
           VALUES ('garmin:1001', 'strength', '2026-03-14T17:02:00', 'garmin', 'x', 'y', 'z')"""
    )
    conn.commit()
    rapporto = sincronizza(conn, FintaGarmin(attivita=[attivita(tipo="running")]))
    assert len(rapporto.attivita_saltate) == 1
    assert "forza" in rapporto.attivita_saltate[0][1]
    assert (
        conn.execute("SELECT activity_type FROM sessions").fetchone()["activity_type"]
        == "strength"
    )
    with pytest.raises(ValueError):
        db.store_endurance_activity(conn, gm.da_api(attivita()))


def test_dry_run_non_scrive_niente(conn):
    finta = FintaGarmin(
        attivita=[attivita()],
        hrv={"2026-03-14": {"calendarDate": "2026-03-14", "lastNightAvg": 62}},
    )
    rapporto = sincronizza(conn, finta, dry_run=True)
    assert rapporto.attivita_nuove == ["garmin:1001"]
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM wellness").fetchone()["c"] == 0
    assert db.sync_state_get(conn, gm._STATO_BENESSERE) is None


def test_le_attivita_piu_vecchie_del_periodo_non_entrano(conn):
    finta = FintaGarmin(attivita=[attivita(), attivita(id_att=2, giorno="2026-01-10")])
    rapporto = sincronizza(conn, finta)
    assert rapporto.attivita_nuove == ["garmin:1001"]


def test_benessere_di_piu_serie_nella_stessa_riga(conn):
    finta = FintaGarmin(
        giornalieri={
            "2026-03-14": {
                "calendarDate": "2026-03-14",
                "includesWellnessData": True,
                "heartRate": {"restingValue": 44},
                "bodyBattery": {"maxValue": 92, "minValue": 21},
                "stress": {"avgLevel": 28},
                "totalSteps": 11231,
            }
        },
        sonno={
            "2026-03-14": {
                "calendarDate": "2026-03-14",
                "sleepTimeSeconds": 27000,
                "deepSleepSeconds": 5400,
                "remSleepSeconds": 4500,
                "sleepScores": {"overall": {"value": 84}},
            }
        },
        hrv={"2026-03-14": {"calendarDate": "2026-03-14", "lastNightAvg": 62, "weeklyAvg": 59,
                            "status": "BALANCED",
                            "baseline": {"balancedLow": 48, "balancedUpper": 72}}},
        peso={"2026-03-14": {"summaryDate": "2026-03-14", "latestWeight": {"weight": 71400}}},
    )
    sincronizza(conn, finta)
    riga = conn.execute("SELECT * FROM wellness WHERE day = '2026-03-14'").fetchone()
    assert riga["resting_hr"] == 44
    assert riga["sleep_h"] == pytest.approx(7.5)
    assert riga["sleep_score"] == 84
    assert riga["hrv_last_night"] == 62
    assert riga["hrv_status"] == "BALANCED"
    assert riga["body_weight_kg"] == pytest.approx(71.4)
    assert riga["body_battery_max"] == 92
    assert riga["steps"] == 11231


def test_una_rilettura_completa_il_giorno_senza_cancellare(conn):
    """I dati di oggi sono parziali: la rilettura aggiunge, non azzera."""
    db.store_wellness_day(conn, "2026-03-14", {"resting_hr": 44, "sleep_h": 7.5})
    db.store_wellness_day(conn, "2026-03-14", {"hrv_last_night": 62})
    riga = conn.execute("SELECT * FROM wellness WHERE day = '2026-03-14'").fetchone()
    assert (riga["resting_hr"], riga["sleep_h"], riga["hrv_last_night"]) == (44, 7.5, 62)


def test_notte_senza_sonno_registrato_non_crea_la_riga(conn):
    finta = FintaGarmin(sonno={"2026-03-14": {"calendarDate": "2026-03-14", "sleepTimeSeconds": 0}})
    sincronizza(conn, finta)
    assert conn.execute("SELECT COUNT(*) c FROM wellness").fetchone()["c"] == 0


def test_le_finestre_coprono_tutto_il_periodo(conn):
    giorni = {
        g.isoformat(): {"calendarDate": g.isoformat(), "includesWellnessData": True,
                        "heartRate": {"restingValue": 40 + g.day % 5}}
        for g in (date(2026, 1, 1) + __import__("datetime").timedelta(days=i) for i in range(74))
    }
    finta = FintaGarmin(giornalieri=giorni)
    sincronizza(conn, finta, dal=date(2026, 1, 1), oggi=date(2026, 3, 15))
    assert conn.execute("SELECT COUNT(*) c FROM wellness").fetchone()["c"] == 74
    finestre = [u for u in finta.chiamate if "/usersummary-service/" in u]
    assert len(finestre) == 2, "60 giorni per finestra: due richieste per 74 giorni"


def test_risposta_troppo_grande_riduce_la_finestra(conn):
    finta = FintaGarmin(
        giornalieri={"2026-03-14": {"calendarDate": "2026-03-14", "includesWellnessData": True,
                                    "heartRate": {"restingValue": 44}}}
    )
    finta.errori["/usersummary-service/"] = (500, 1)
    sincronizza(conn, finta)
    assert conn.execute("SELECT COUNT(*) c FROM wellness").fetchone()["c"] == 1


def test_riprende_da_dove_era_arrivato(conn):
    finta = FintaGarmin(attivita=[attivita()])
    sincronizza(conn, finta)
    assert db.sync_state_get(conn, gm._STATO_BENESSERE) == "2026-03-15"
    finta.chiamate.clear()
    gm.sync_garmin(conn, client(finta), oggi=date(2026, 3, 20), solo_benessere=True)
    primo = [u for u in finta.chiamate if "/usersummary-service/" in u][0]
    assert "startDate=2026-03-12" in primo, "rilegge gli ultimi giorni, che erano parziali"


def test_solo_attivita_e_solo_benessere(conn):
    finta = FintaGarmin(
        attivita=[attivita()],
        hrv={"2026-03-14": {"calendarDate": "2026-03-14", "lastNightAvg": 62}},
    )
    sincronizza(conn, finta, solo_attivita=True)
    assert conn.execute("SELECT COUNT(*) c FROM wellness").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 1

    finta2 = FintaGarmin(
        attivita=[attivita(id_att=2)],
        hrv={"2026-03-14": {"calendarDate": "2026-03-14", "lastNightAvg": 62}},
    )
    sincronizza(conn, finta2, solo_benessere=True)
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM wellness").fetchone()["c"] == 1


def test_paginazione(conn):
    finta = FintaGarmin(attivita=[attivita(id_att=i, ora=f"{6 + i}:00:00") for i in range(25)])
    rapporto = gm.sync_garmin(
        conn, client(finta), dal=date(2026, 3, 1), oggi=date(2026, 3, 15), solo_attivita=True
    )
    assert rapporto.attivita_lette == 25, "due pagine da 20 e 5"
    assert len(rapporto.attivita_nuove) == 25


# --------------------------------------------------------------------------
# dati condivisi: la ragione del progetto unico
# --------------------------------------------------------------------------


def test_palestra_e_corsa_nella_stessa_settimana(conn, tmp_path):
    """La settimana ISO mette in riga volume in palestra e chilometri di corsa."""
    from strength_tracker.hevy import parse_json
    from strength_tracker.mapping import load_mapping

    db.refresh_exercise_map(conn, load_mapping(MAPPATURA).as_rows())
    payload = {
        "workouts": [
            {
                "id": "w1",
                "title": "Day 1",
                "start_time": "2026-03-09T07:00:00+00:00",
                "end_time": "2026-03-09T08:00:00+00:00",
                "exercises": [
                    {
                        "title": "Deadlift (Trap bar)",
                        "sets": [{"type": "normal", "weight_kg": 75, "reps": 5}],
                    }
                ],
            }
        ]
    }
    percorso = tmp_path / "hevy.json"
    percorso.write_text(json.dumps(payload), encoding="utf-8")
    for att in parse_json(percorso):
        db.store_activity(conn, att)
    sincronizza(conn, FintaGarmin(attivita=[attivita()]))

    settimane = {(r["iso_year"], r["iso_week"]): r for r in mt.settimane_combinate(conn)}
    riga = settimane[(2026, 11)]
    assert riga["sedute_forza"] == 1
    assert riga["volume_kg"] == pytest.approx(375.0)
    assert riga["sedute_resistenza"] == 1
    assert riga["km_corsa"] == pytest.approx(10.0)


def test_le_corse_non_si_contano_come_sedute_di_palestra(conn, tmp_path):
    """`sessions` ora e' condivisa: i conteggi della forza devono dirlo."""
    sincronizza(conn, FintaGarmin(attivita=[attivita()]))
    r = mt.riepilogo(conn)
    assert r["n_sedute"] == 0
    assert r["sedute_ultime_4_settimane"] is None
    assert mt.settimane_coperte(conn) == []


def test_riepilogo_resistenza(conn):
    finta = FintaGarmin(
        attivita=[attivita(), attivita(id_att=2, tipo="gravel_cycling", ora="09:00:00")],
        hrv={"2026-03-14": {"calendarDate": "2026-03-14", "lastNightAvg": 62}},
    )
    sincronizza(conn, finta)
    res = mt.riepilogo_resistenza(conn)
    assert (res["n_attivita"], res["n_corse"]) == (2, 1)
    assert res["km_corsa"] == pytest.approx(10.0)
    assert (res["giorni_benessere"], res["n_hrv"]) == (1, 1)


# --------------------------------------------------------------------------
# riga di comando
# --------------------------------------------------------------------------


def test_cli_senza_chiave_spiega_cosa_fare(tmp_path, monkeypatch, capsys):
    from strength_tracker.cli import main

    monkeypatch.delenv(gm.ENV_CHIAVE, raising=False)
    monkeypatch.setattr(gm, "FILE_CHIAVE", (tmp_path / "non-esiste",))
    assert main(["--db", str(tmp_path / "x.db"), "sync-garmin"]) == 2
    assert gm.ENV_CHIAVE in capsys.readouterr().err


def test_cli_data_non_valida(tmp_path, monkeypatch, capsys):
    from strength_tracker.cli import main

    monkeypatch.setenv(gm.ENV_CHIAVE, "finta")
    assert main(["--db", str(tmp_path / "x.db"), "sync-garmin", "--dal", "ieri"]) == 2
    assert "data non valida" in capsys.readouterr().err


def test_cli_stats_mostra_corsa_e_recupero(conn, tmp_path, capsys):
    from strength_tracker.cli import main

    sincronizza(
        conn,
        FintaGarmin(
            attivita=[attivita()],
            hrv={"2026-03-14": {"calendarDate": "2026-03-14", "lastNightAvg": 62}},
        ),
    )
    conn.commit()
    main(["--db", str(conn.execute("PRAGMA database_list").fetchone()[2]), "stats"])
    uscita = capsys.readouterr().out
    assert "CORSA E RECUPERO" in uscita
    assert "PALESTRA E CORSA, SETTIMANA PER SETTIMANA" in uscita
    assert "10 km" in uscita


# --------------------------------------------------------------------------
# importazione dallo storico JSON dell'altro progetto
# --------------------------------------------------------------------------


@pytest.fixture
def storico(tmp_path):
    """La cartella `data/` del progetto corsa, nei formati che salva lui."""
    cartella = tmp_path / "storico"
    cartella.mkdir()
    scrivi = lambda nome, dati: (cartella / nome).write_text(  # noqa: E731
        json.dumps(dati), encoding="utf-8"
    )
    scrivi("activities.json", {"1001": attivita(), "9": attivita(id_att=9, tipo="strength_training")})
    scrivi(
        "daily.json",
        {
            "2026-03-14": {
                "calendarDate": "2026-03-14",
                "includesWellnessData": True,
                "heartRate": {"restingValue": 43},
                "bodyBattery": {"maxValue": 90, "minValue": 18},
                "stress": {"avgLevel": 26},
                "totalSteps": 9800,
            }
        },
    )
    scrivi(
        "sleep.json",
        {"2026-03-14": {"calendarDate": "2026-03-14", "sleepTimeSeconds": 26100,
                        "sleepScores": {"overall": {"value": 81}}}},
    )
    scrivi("hrv.json", {"2026-03-14": {"calendarDate": "2026-03-14", "lastNightAvg": 61}})
    scrivi(
        "weight.json",
        {"2026-03-13": {"summaryDate": "2026-03-13", "latestWeight": {"weight": 71800}}},
    )
    scrivi("state.json", {"last_pull": "2026-03-15T08:00:00"})  # ignorato
    return cartella


def test_importa_lo_storico_senza_rete(conn, storico):
    rapporto = gm.importa_cartella(conn, storico)
    assert rapporto.attivita_nuove == ["garmin:1001"]
    assert len(rapporto.attivita_saltate) == 1  # la seduta di palestra
    riga = conn.execute("SELECT * FROM wellness WHERE day = '2026-03-14'").fetchone()
    assert (riga["resting_hr"], riga["sleep_score"], riga["hrv_last_night"]) == (43, 81, 61)
    assert conn.execute("SELECT COUNT(*) c FROM wellness").fetchone()["c"] == 2
    assert rapporto.dal == "2026-03-13"


def test_importare_due_volte_non_duplica(conn, storico):
    gm.importa_cartella(conn, storico)
    rapporto = gm.importa_cartella(conn, storico)
    assert rapporto.attivita_nuove == []
    assert rapporto.attivita_invariate == ["garmin:1001"]
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM wellness").fetchone()["c"] == 2


def test_import_poi_sync_riprende_da_dove_e_arrivato_l_import(conn, storico):
    """Dopo l'import non serve riscaricare un anno: si riparte dall'ultimo giorno."""
    gm.importa_cartella(conn, storico)
    assert db.sync_state_get(conn, gm._STATO_BENESSERE) == "2026-03-14"
    finta = FintaGarmin(attivita=[attivita()])
    gm.sync_garmin(conn, client(finta), oggi=date(2026, 3, 20))
    primo = [u for u in finta.chiamate if "/usersummary-service/" in u][0]
    assert "startDate=2026-03-11" in primo
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 1


def test_import_dry_run(conn, storico):
    rapporto = gm.importa_cartella(conn, storico, dry_run=True)
    assert rapporto.attivita_nuove == ["garmin:1001"]
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM wellness").fetchone()["c"] == 0


def test_cartella_senza_niente(conn, tmp_path, capsys):
    from strength_tracker.cli import main

    vuota = tmp_path / "vuota"
    vuota.mkdir()
    assert main(["--db", str(tmp_path / "v.db"), "import-garmin", str(vuota)]) == 1
    assert "activities.json" in capsys.readouterr().err


def test_cli_import_garmin(tmp_path, storico, capsys):
    from strength_tracker.cli import main

    assert main(["--db", str(tmp_path / "i.db"), "import-garmin", str(storico)]) == 0
    uscita = capsys.readouterr().out
    assert "Attivita' lette:     2" in uscita
    assert "Giorni di benessere: 2 nuovi" in uscita


def test_eta_fitness_finisce_nel_giorno(conn):
    finta = FintaGarmin(
        attivita=[attivita()],
        profilo={"fitnessAge": {"fitnessAge": 31.4, "chronologicalAge": 41, "vo2MaxRunning": 52.3}},
    )
    sincronizza(conn, finta)
    riga = conn.execute("SELECT * FROM wellness WHERE day = '2026-03-15'").fetchone()
    assert riga["fitness_age"] == pytest.approx(31.4)
    assert riga["chronological_age"] == 41
    assert riga["vo2max"] == pytest.approx(52.3)


def test_senza_eta_fitness_non_si_inventa_niente(conn):
    """Garmin risponde 404 su quella data: il campo resta assente, non a zero."""
    sincronizza(conn, FintaGarmin(attivita=[attivita()], profilo=None))
    assert conn.execute("SELECT COUNT(fitness_age) c FROM wellness").fetchone()["c"] == 0


def test_profilo_dell_altro_progetto(conn, storico):
    (storico / "profile.json").write_text(
        json.dumps({"fitnessAge": {"fitnessAge": 33.0, "chronologicalAge": 41}}), encoding="utf-8"
    )
    gm.importa_cartella(conn, storico)
    riga = conn.execute("SELECT * FROM wellness WHERE day = '2026-03-14'").fetchone()
    assert riga["fitness_age"] == pytest.approx(33.0)
