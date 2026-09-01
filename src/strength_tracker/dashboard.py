"""Generazione della dashboard HTML statica e self-contained.

Un solo file in `output/dashboard.html`: Chart.js viene inserito inline da
`vendor/chart.min.js` e i dati sono un blocco JSON dentro la pagina. Nessuna
richiesta di rete a runtime, nessun server: si apre con doppio click, anche
offline.

I dati arrivano da `metrics`, quindi correzioni e mappatura sono gia'
applicate. Dove una metrica non e' calcolabile la pagina lo scrive: non
mostra zero.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import PACKAGE_DIR, REPO_ROOT
from . import metrics as mt
from .mapping import Mapping

#: Palette categorica validata (slot in ordine fisso: l'ordine e' il
#: meccanismo di sicurezza per i daltonismi, non una scelta estetica).
SERIE_CHIARO = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SERIE_SCURO = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
MAX_GRUPPI = 8


def _cerca_asset(nome: str) -> Path:
    """Trova `templates/` e `vendor/` sia da repo sia da pacchetto installato."""
    for base in (REPO_ROOT, Path.cwd(), PACKAGE_DIR):
        candidato = base / nome
        if candidato.exists():
            return candidato
    raise FileNotFoundError(f"non trovo {nome}/ (cercato in {REPO_ROOT}, {Path.cwd()}, {PACKAGE_DIR})")


def _ordina_gruppi(gruppi: list[str], focus: list[str]) -> list[str]:
    """Ordine stabile dei gruppi muscolari.

    Prima quelli sotto osservazione, poi gli altri in ordine alfabetico. Non
    dipende dai dati: filtrare o aggiungere una settimana non ricolora nulla.
    """
    resto = sorted(g for g in gruppi if g not in focus)
    return [g for g in focus if g in gruppi] + resto


def _accorpa_code(
    gruppi: list[str], peso: dict[str, int], focus: list[str]
) -> tuple[list[str], dict[str, str]]:
    """Oltre gli 8 slot i gruppi piu' piccoli confluiscono in 'altri'.

    Generare un nono colore lo renderebbe indistinguibile dagli altri sotto
    daltonismo. A finire in 'altri' sono i gruppi con meno serie, mai quelli
    sotto osservazione; l'ordine dei colori resta pero' quello stabile di
    `_ordina_gruppi`, cosi' non dipende dai volumi di questa settimana.
    """
    if len(gruppi) <= MAX_GRUPPI:
        return gruppi, {g: g for g in gruppi}
    per_dimensione = sorted(gruppi, key=lambda g: (g not in focus, -peso.get(g, 0), g))
    tenuti = set(per_dimensione[: MAX_GRUPPI - 1])
    ordinati = [g for g in gruppi if g in tenuti]
    mappa = {g: (g if g in tenuti else "altri") for g in gruppi}
    return ordinati + ["altri"], mappa


def costruisci_dati(conn: sqlite3.Connection, mapping: Mapping) -> dict[str, Any]:
    """Tutto quello che serve alla pagina, gia' pronto da serializzare."""
    focus = list(mapping.focus_groups)
    riepilogo = mt.riepilogo(conn)
    settimane = mt.volume_settimanale(conn)
    per_gruppo = mt.serie_per_gruppo(conn, focus=focus)

    gruppi_ordinati = _ordina_gruppi(per_gruppo["gruppi"], focus)
    totali = {g: sum(per_gruppo["serie"][g]) for g in gruppi_ordinati}
    gruppi_finali, accorpamento = _accorpa_code(gruppi_ordinati, totali, focus)
    n_settimane = len(per_gruppo["settimane"])

    serie_per_gruppo: dict[str, list[int]] = {g: [0] * n_settimane for g in gruppi_finali}
    volume_per_gruppo: dict[str, list[float | None]] = {g: [None] * n_settimane for g in gruppi_finali}
    for originale, destinazione in accorpamento.items():
        for i, n in enumerate(per_gruppo["serie"][originale]):
            serie_per_gruppo[destinazione][i] += n
        for i, v in enumerate(per_gruppo["volume_kg"][originale]):
            if v is not None:
                corrente = volume_per_gruppo[destinazione][i]
                volume_per_gruppo[destinazione][i] = (corrente or 0.0) + v

    esercizi = mt.esercizi_disponibili(conn)
    progressioni = {e["nome"]: mt.progressione(conn, e["nome"]) for e in esercizi}

    sedute = mt.sedute(conn)
    for s in sedute:
        s["serie"] = mt.dettaglio_seduta(conn, s["session_id"])

    return {
        "generato_il": datetime.now().isoformat(timespec="seconds"),
        "riepilogo": riepilogo,
        "settimane": settimane,
        "gruppi": {
            "settimane": per_gruppo["settimane"],
            "nomi": gruppi_finali,
            "altri_contiene": sorted(
                g for g, dest in accorpamento.items() if dest == "altri"
            ),
            "serie": serie_per_gruppo,
            "volume_kg": volume_per_gruppo,
            "focus": [g for g in focus if g in gruppi_finali],
        },
        # I gruppi sotto osservazione hanno una card loro: ci restano anche
        # quando non compaiono nei dati, dichiarando che non sono stati
        # allenati invece di sparire in silenzio.
        "focus": [
            {
                "nome": g,
                "presente": g in per_gruppo["gruppi"],
                "slot": gruppi_finali.index(g) if g in gruppi_finali else None,
                "serie": serie_per_gruppo.get(g, [0] * n_settimane),
            }
            for g in focus
        ],
        "esercizi": esercizi,
        "progressioni": progressioni,
        "sedute": sedute,
        "anomalie": mt.anomalie(conn),
        "palette": {"chiaro": SERIE_CHIARO, "scuro": SERIE_SCURO},
        "epley_max_reps": mt.EPLEY_MAX_REPS,
        "finestra_media_mobile": mt.FINESTRA_MEDIA_MOBILE,
    }


def genera(
    conn: sqlite3.Connection,
    mapping: Mapping,
    destinazione: Path,
) -> Path:
    """Scrive la dashboard. Ritorna il percorso del file generato."""
    templates = _cerca_asset("templates")
    chart_js = _cerca_asset("vendor") / "chart.min.js"
    if not chart_js.is_file():
        raise FileNotFoundError(f"manca {chart_js}: Chart.js deve essere vendorizzato nel repo")

    env = Environment(
        loader=FileSystemLoader(str(templates)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    dati = costruisci_dati(conn, mapping)
    # `</script>` dentro il JSON chiuderebbe il tag: va neutralizzato.
    dati_json = json.dumps(dati, ensure_ascii=False, allow_nan=False).replace("</", "<\\/")

    html = env.get_template("dashboard.html.j2").render(
        dati_json=dati_json,
        chart_js=chart_js.read_text(encoding="utf-8"),
        riepilogo=dati["riepilogo"],
    )
    destinazione = Path(destinazione)
    destinazione.parent.mkdir(parents=True, exist_ok=True)
    destinazione.write_text(html, encoding="utf-8")
    return destinazione
