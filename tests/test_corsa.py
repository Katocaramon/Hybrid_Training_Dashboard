"""Dashboard della corsa: payload dal database condiviso, pagina offline.

La pagina e' quella del progetto garmin-dashboard; a cambiare e' da dove
prende i numeri. Questi test coprono la traduzione database -> payload, che
e' il pezzo nuovo, piu' il fatto che la pagina resti un file che si apre con
doppio click senza chiedere niente alla rete.
"""

import json
import pathlib
from datetime import date, timedelta

import pytest

from strength_tracker import corsa, db
from test_garmin import FintaGarmin, attivita, client
from strength_tracker import garmin as gm

MAPPATURA = "config/exercise_mapping.yaml"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "c.db")
    yield c
    c.close()


def sincronizza(conn, finta, **kwargs):
    kwargs.setdefault("oggi", date(2026, 3, 15))
    kwargs.setdefault("dal", date(2026, 1, 1))
    return gm.sync_garmin(conn, client(finta), **kwargs)


def seduta_palestra(conn, giorno, titolo="Day 1", durata_s=3600.0):
    conn.execute(
        """INSERT INTO sessions (session_uid, activity_type, workout_name, start_time_utc,
                                 start_time_local, local_date, iso_year, iso_week,
                                 total_timer_s, source, source_path, source_sha256, ingested_at)
           VALUES (?, 'strength', ?, ?, ?, ?, ?, ?, ?, 'hevy', 'x', 'y', 'z')""",
        (
            f"hevy:{giorno}",
            titolo,
            f"{giorno}T07:00:00+00:00",
            f"{giorno}T08:00:00",
            giorno,
            *date.fromisoformat(giorno).isocalendar()[:2],
            durata_s,
        ),
    )
    conn.commit()


# --------------------------------------------------------------------------
# configurazione della gara
# --------------------------------------------------------------------------


def test_config_da_yaml(tmp_path):
    f = tmp_path / "corsa.yaml"
    f.write_text(
        "race_name: Mezza di Roma\nrace_date: 2027-03-14\nplan_start: 2026-11-02\n",
        encoding="utf-8",
    )
    cfg = corsa.carica_config(f)
    assert cfg == {"name": "Mezza di Roma", "date": "2027-03-14", "planStart": "2026-11-02"}


def test_config_dal_settings_json_del_vecchio_progetto(tmp_path):
    """Il file dell'altro progetto si puo' puntare com'e', senza riscriverlo."""
    f = tmp_path / "settings.json"
    f.write_text(
        json.dumps({"race_name": "Maratona", "race_date": "2027-04-11",
                    "plan_start": "2026-12-07"}),
        encoding="utf-8",
    )
    assert corsa.carica_config(f)["name"] == "Maratona"


def test_senza_file_nessuna_gara_inventata(tmp_path):
    assert corsa.carica_config(tmp_path / "non-esiste.yaml") is None
    assert corsa.carica_config(None) is None


def test_config_incompleta_o_storta(tmp_path):
    f = tmp_path / "corsa.yaml"
    f.write_text("race_name: Mezza\n", encoding="utf-8")
    with pytest.raises(ValueError, match="race_date"):
        corsa.carica_config(f)
    f.write_text(
        "race_name: Mezza\nrace_date: 14/03/2027\nplan_start: 2026-11-02\n", encoding="utf-8"
    )
    with pytest.raises(ValueError):
        corsa.carica_config(f)


# --------------------------------------------------------------------------
# attivita'
# --------------------------------------------------------------------------


def test_corsa_bici_e_palestra_nella_stessa_lista(conn):
    sincronizza(
        conn,
        FintaGarmin(
            attivita=[
                attivita(giorno="2026-03-10"),
                attivita(id_att=2, tipo="gravel_cycling", giorno="2026-03-11"),
                attivita(id_att=3, tipo="lap_swimming", giorno="2026-03-12"),
            ]
        ),
    )
    seduta_palestra(conn, "2026-03-09")
    gruppi = {a["g"] for a in corsa.attivita(conn)}
    assert gruppi == {"Corsa", "Bici", "Altro", "Palestra"}


def test_la_palestra_viene_dalle_sedute_vere_non_da_garmin(conn):
    """Garmin non espone le serie: le ore di palestra arrivano da Hevy."""
    sincronizza(conn, FintaGarmin(attivita=[attivita(id_att=9, tipo="strength_training")]))
    seduta_palestra(conn, "2026-03-14", titolo="Lower Body", durata_s=4500.0)
    palestra = [a for a in corsa.attivita(conn) if a["g"] == "Palestra"]
    assert len(palestra) == 1
    assert palestra[0]["name"] == "Lower Body"
    assert palestra[0]["sec"] == 4500.0
    assert palestra[0]["km"] == 0.0


def test_ordinate_per_data(conn):
    sincronizza(
        conn,
        FintaGarmin(
            attivita=[
                attivita(id_att=1, giorno="2026-03-12"),
                attivita(id_att=2, giorno="2026-03-05"),
            ]
        ),
    )
    assert [a["d"] for a in corsa.attivita(conn)] == ["2026-03-05", "2026-03-12"]


# --------------------------------------------------------------------------
# settimane e confronto
# --------------------------------------------------------------------------


def test_sedici_settimane_anche_quelle_vuote(conn):
    sincronizza(conn, FintaGarmin(attivita=[attivita(giorno="2026-03-10")]))
    dati = corsa.costruisci_dati(conn, None, oggi=date(2026, 3, 15))
    assert len(dati["weeks"]) == 16
    assert dati["weeks"][-1]["w"] == "2026-03-09"  # il lunedi' di questa settimana
    assert dati["weeks"][-1]["Corsa"]["km"] == pytest.approx(10.0)
    assert dati["weeks"][0]["Corsa"]["n"] == 0, "una settimana senza corse e' zero, non un buco"


def test_confronto_con_la_media_delle_quattro_precedenti(conn):
    # una corsa a settimana per cinque settimane: le 4 precedenti fanno media 10 km
    for i in range(5):
        giorno = (date(2026, 3, 9) - timedelta(weeks=4 - i)).isoformat()
        sincronizza(
            conn,
            FintaGarmin(attivita=[attivita(id_att=100 + i, giorno=giorno)]),
            dal=date(2026, 1, 1),
        )
    dati = corsa.costruisci_dati(conn, None, oggi=date(2026, 3, 15))
    corsa_ = next(c for c in dati["compare"] if c["g"] == "Corsa")
    assert corsa_["cur"]["km"] == pytest.approx(10.0)
    assert corsa_["prev"]["km"] == pytest.approx(10.0)
    assert corsa_["prev"]["n"] == pytest.approx(1.0)


def test_senza_attivita_nessun_confronto(conn):
    dati = corsa.costruisci_dati(conn, None, oggi=date(2026, 3, 15))
    assert dati["compare"] == []
    assert dati["rows"] == []
    assert dati["totals"]["acts"] == 0


# --------------------------------------------------------------------------
# serie giornaliere
# --------------------------------------------------------------------------


def giorni_benessere(conn, quanti=10, primo=date(2026, 3, 1)):
    for i in range(quanti):
        giorno = (primo + timedelta(days=i)).isoformat()
        db.store_wellness_day(
            conn,
            giorno,
            {
                "resting_hr": 42 + i % 3,
                "hrv_last_night": 58 + i % 4,
                "hrv_weekly_avg": 60,
                "hrv_status": "BALANCED",
                "hrv_baseline_low": 48,
                "hrv_baseline_high": 72,
                "sleep_h": 7.2,
                "sleep_score": 80,
                "deep_sleep_h": 1.4,
                "rem_sleep_h": 1.2,
                "body_weight_kg": 71.5,
            },
        )


def test_una_riga_al_giorno_coi_campi_del_benessere(conn):
    giorni_benessere(conn, 10)
    sincronizza(conn, FintaGarmin(attivita=[attivita(giorno="2026-03-05")]))
    dati = corsa.costruisci_dati(conn, None, oggi=date(2026, 3, 10))
    riga = next(r for r in dati["rows"] if r["d"] == "2026-03-05")
    assert riga["rhr"] == 43
    assert riga["hrv"] == 58
    assert (riga["lo"], riga["hi"]) == (48, 72)
    assert riga["sl"] == pytest.approx(7.2)
    assert riga["kg"] == pytest.approx(71.5)
    assert riga["st"] == "BALANCED"


def test_la_media_mobile_aspetta_tre_misure(conn):
    db.store_wellness_day(conn, "2026-03-01", {"resting_hr": 44})
    db.store_wellness_day(conn, "2026-03-02", {"resting_hr": 46})
    dati = corsa.costruisci_dati(conn, None, oggi=date(2026, 3, 3))
    assert "rhr7" not in dati["rows"][0]
    assert "rhr7" not in dati["rows"][1], "due misure non sono una tendenza"
    db.store_wellness_day(conn, "2026-03-03", {"resting_hr": 45})
    dati = corsa.costruisci_dati(conn, None, oggi=date(2026, 3, 3))
    assert dati["rows"][2]["rhr7"] == pytest.approx(45.0)


def test_giorno_senza_misura_non_diventa_zero(conn):
    db.store_wellness_day(conn, "2026-03-01", {"resting_hr": 44})
    dati = corsa.costruisci_dati(conn, None, oggi=date(2026, 3, 3))
    assert "rhr" not in dati["rows"][1]
    assert "rhr" not in dati["rows"][2]


def test_carico_acuto_e_cronico(conn):
    """7 giorni contro la media di 4 settimane, dal primo giorno con attivita'."""
    db.store_wellness_day(conn, "2026-02-25", {"resting_hr": 44})  # prima di tutto
    sincronizza(
        conn,
        FintaGarmin(
            attivita=[
                attivita(id_att=1, giorno="2026-03-02"),
                attivita(id_att=2, giorno="2026-03-04"),
            ]
        ),
    )
    dati = corsa.costruisci_dati(conn, None, oggi=date(2026, 3, 5))
    per_giorno = {r["d"]: r for r in dati["rows"]}
    assert per_giorno["2026-03-05"]["ac"] == 280  # due attivita' da 140
    assert per_giorno["2026-03-05"]["ch"] == 70  # 280 / 4 settimane
    assert per_giorno["2026-03-02"]["ac"] == 140  # il giorno della prima attivita'
    assert "ac" not in per_giorno["2026-02-25"], (
        "prima della prima attivita' il carico e' ignoto, non zero"
    )


# --------------------------------------------------------------------------
# record
# --------------------------------------------------------------------------


def test_record_di_sempre_e_degli_ultimi_dodici_mesi(conn):
    sincronizza(
        conn,
        FintaGarmin(
            attivita=[
                attivita(id_att=1, giorno="2024-06-01", fastestSplit_5000=1300.0),
                attivita(id_att=2, giorno="2026-03-01", fastestSplit_5000=1400.0),
            ]
        ),
        dal=date(2024, 1, 1),
    )
    dati = corsa.costruisci_dati(conn, None, oggi=date(2026, 3, 15))
    cinque = next(b for b in dati["best"] if b["label"] == "5 km")
    assert cinque["sec"] == 1300.0 and cinque["d"] == "2024-06-01"
    assert cinque["year"] == 1400.0 and cinque["yearD"] == "2026-03-01"


def test_distanza_mai_corsa_non_compare(conn):
    sincronizza(conn, FintaGarmin(attivita=[attivita()]))
    assert corsa.costruisci_dati(conn, None, oggi=date(2026, 3, 15))["best"] == []


def test_totali_settimane_e_mesi_record(conn):
    sincronizza(
        conn,
        FintaGarmin(
            attivita=[
                attivita(id_att=1, giorno="2026-03-02"),
                attivita(id_att=2, giorno="2026-03-04"),
                attivita(id_att=3, giorno="2026-02-10"),
            ]
        ),
    )
    dati = corsa.costruisci_dati(conn, None, oggi=date(2026, 3, 15))
    assert dati["totals"] == {"acts": 3, "runs": 3, "runKm": 30, "since": "2026-02-10"}
    assert dati["runWeeks"][0]["km"] == pytest.approx(20.0)
    assert dati["runMonths"][0] == {"m": "2026-03", "km": 20.0}


def test_eta_fitness_dall_ultimo_giorno_che_ce_l_ha(conn):
    db.store_wellness_day(conn, "2026-03-01", {"fitness_age": 34.0, "chronological_age": 41})
    db.store_wellness_day(conn, "2026-03-10", {"fitness_age": 32.5, "chronological_age": 41})
    db.store_wellness_day(conn, "2026-03-12", {"resting_hr": 44})
    dati = corsa.costruisci_dati(conn, None, oggi=date(2026, 3, 15))
    assert dati["fitnessAge"] == pytest.approx(32.5)
    assert dati["age"] == 41


# --------------------------------------------------------------------------
# la pagina
# --------------------------------------------------------------------------


def test_pagina_offline_e_autosufficiente(conn, tmp_path):
    import re

    giorni_benessere(conn, 10)
    sincronizza(conn, FintaGarmin(attivita=[attivita(giorno="2026-03-05")]))
    seduta_palestra(conn, "2026-03-04")
    percorso = corsa.genera(conn, tmp_path / "corsa.html", None, oggi=date(2026, 3, 10))
    html = percorso.read_text(encoding="utf-8")
    assert "/*__DATA__*/null" not in html, "i dati devono essere stati iniettati"
    esterni = [
        u
        for u in re.findall(r'(?:src|href)\s*=\s*["\']([^"\']+)', html)
        if u.startswith(("http://", "https://", "//"))
    ]
    assert esterni == [], f"la pagina deve aprirsi offline, trovati: {esterni}"
    assert "Corsa e recupero" in html


def test_la_pagina_non_si_chiude_da_sola(conn, tmp_path):
    """Un `</script>` dentro i dati chiuderebbe il tag: va neutralizzato."""
    seduta_palestra(conn, "2026-03-04", titolo="</script><b>ciao")
    percorso = corsa.genera(conn, tmp_path / "corsa.html", None, oggi=date(2026, 3, 10))
    html = percorso.read_text(encoding="utf-8")
    assert "</script><b>ciao" not in html
    assert "<\\/script>" in html


def test_senza_gara_la_pagina_si_genera_lo_stesso(conn, tmp_path):
    percorso = corsa.genera(conn, tmp_path / "corsa.html", None, oggi=date(2026, 3, 10))
    assert '"race":null' in percorso.read_text(encoding="utf-8")


def test_la_gara_finisce_nella_pagina(conn, tmp_path):
    cfg = corsa.ConfigCorsa(name="Mezza di Roma", date="2027-03-14", planStart="2026-11-02")
    percorso = corsa.genera(conn, tmp_path / "corsa.html", cfg, oggi=date(2026, 3, 10))
    assert "Mezza di Roma" in percorso.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# riga di comando e server
# --------------------------------------------------------------------------


def test_cli_report_corsa(conn, tmp_path, capsys):
    from strength_tracker.cli import main

    sincronizza(conn, FintaGarmin(attivita=[attivita()]))
    conn.commit()
    percorso_db = conn.execute("PRAGMA database_list").fetchone()[2]
    uscita = tmp_path / "out" / "corsa.html"
    assert main(["--db", percorso_db, "report-corsa", "--output", str(uscita)]) == 0
    assert uscita.is_file()
    assert "Dashboard corsa generata" in capsys.readouterr().out


def test_cli_report_corsa_senza_attivita(tmp_path, capsys):
    from strength_tracker.cli import main

    uscita = tmp_path / "corsa.html"
    assert main(["--db", str(tmp_path / "v.db"), "report-corsa", "--output", str(uscita)]) == 1
    assert uscita.is_file(), "la pagina si genera comunque, vuota ma valida"
    assert "sync-garmin" in capsys.readouterr().err


def test_il_server_espone_le_due_pagine(conn, tmp_path):
    import threading
    import urllib.request

    from strength_tracker import server

    sincronizza(conn, FintaGarmin(attivita=[attivita()]))
    conn.commit()
    palestra = tmp_path / "dashboard.html"
    pagina_corsa = tmp_path / "corsa.html"
    palestra.write_text("<html>palestra</html>", encoding="utf-8")

    opzioni = server.Opzioni(
        db_path=conn.execute("PRAGMA database_list").fetchone()[2],
        output_path=palestra,
        sincronizza=lambda c: (["ok"], None),
        rigenera=lambda c: palestra,
        porta=0,
        extra=(
            server.Pagina(
                "/corsa", pagina_corsa, lambda c: corsa.genera(c, pagina_corsa, None)
            ),
        ),
    )
    srv = server.crea_server(opzioni)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{opzioni.porta}"
        assert "palestra" in urllib.request.urlopen(base + "/").read().decode()
        pagina = urllib.request.urlopen(base + "/corsa").read().decode()
        assert "Corsa e recupero" in pagina
        richiesta = urllib.request.Request(
            base + "/sync", method="POST", headers={server.HEADER_RICHIESTA: "1"}
        )
        assert json.loads(urllib.request.urlopen(richiesta).read())["ok"] is True
        assert pagina_corsa.is_file(), "la sincronizzazione rigenera anche la corsa"
    finally:
        srv.shutdown()
        srv.server_close()


def test_il_modello_copiato_e_non_modificato_viene_fermato(tmp_path):
    """Un `cp` dell'esempio senza modifiche mostrerebbe una gara finta credibile."""
    f = tmp_path / "corsa.yaml"
    f.write_text(
        pathlib.Path("config/corsa.example.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="modello"):
        corsa.carica_config(f)


def test_cli_dice_quale_gara_sta_usando(conn, tmp_path, capsys):
    from strength_tracker.cli import main

    conn.commit()
    config = tmp_path / "corsa.yaml"
    config.write_text(
        "race_name: Mezza Maratona Roma-Ostia\nrace_date: 2027-02-28\nplan_start: 2026-10-26\n",
        encoding="utf-8",
    )
    main(
        [
            "--db",
            conn.execute("PRAGMA database_list").fetchone()[2],
            "report-corsa",
            "--output",
            str(tmp_path / "corsa.html"),
            "--settings",
            str(config),
        ]
    )
    uscita = capsys.readouterr().out
    assert "Mezza Maratona Roma-Ostia" in uscita
    assert "28/02/2027" in uscita
