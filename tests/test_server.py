"""Test del server locale.

Girano contro un server vero, avviato su una porta libera: niente mock del
protocollo HTTP. La sincronizzazione e' finta solo nella parte che toccherebbe
la rete di Hevy.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

import fitgen
from strength_tracker import db
from strength_tracker import server as srv
from strength_tracker.ingest import ingest_path


@pytest.fixture
def ambiente(tmp_path):
    """Database con una seduta vera e una dashboard gia' generata."""
    cartella = tmp_path / "fit"
    fitgen.build_strength_fit(
        cartella / "a.fit", start=datetime(2026, 9, 3, 17, tzinfo=timezone.utc)
    )
    dbfile = tmp_path / "s.db"
    conn = db.connect(dbfile)
    ingest_path(conn, cartella)
    conn.close()
    return dbfile, tmp_path / "out" / "dashboard.html"


def avvia(opzioni):
    server = srv.crea_server(opzioni)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def chiedi(url, metodo="GET", headers=None):
    richiesta = urllib.request.Request(url, method=metodo, headers=headers or {})
    try:
        with urllib.request.urlopen(richiesta, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture
def opzioni(ambiente):
    from strength_tracker import dashboard
    from strength_tracker.mapping import load_mapping

    dbfile, uscita = ambiente
    mappatura = load_mapping("config/exercise_mapping.yaml")
    chiamate = []

    def sincronizza(conn: sqlite3.Connection):
        chiamate.append("sync")
        return ["Allenamenti letti:   1", "Sedute nuove:        1"], None

    def rigenera(conn: sqlite3.Connection):
        return dashboard.genera(conn, mappatura, uscita)

    o = srv.Opzioni(
        db_path=dbfile,
        output_path=uscita,
        sincronizza=sincronizza,
        rigenera=rigenera,
        porta=0,
    )
    o.chiamate = chiamate  # type: ignore[attr-defined]
    return o


# --- servizio della pagina -------------------------------------------------


def test_serve_la_dashboard(opzioni):
    server = avvia(opzioni)
    try:
        stato, corpo = chiedi(opzioni.url)
        assert stato == 200
        assert b"Strength Tracker" in corpo
        assert b"istruzioni-servita" in corpo
    finally:
        server.shutdown()


def test_genera_la_dashboard_se_manca(opzioni):
    assert not Path(opzioni.output_path).exists()
    server = avvia(opzioni)
    try:
        stato, corpo = chiedi(opzioni.url)
        assert stato == 200 and len(corpo) > 100_000  # Chart.js e' dentro
        assert Path(opzioni.output_path).is_file()
    finally:
        server.shutdown()


def test_la_pagina_non_va_in_cache(opzioni):
    """Contiene i dati: una copia in cache mostrerebbe numeri vecchi."""
    server = avvia(opzioni)
    try:
        richiesta = urllib.request.Request(opzioni.url)
        with urllib.request.urlopen(richiesta, timeout=5) as r:
            assert r.headers.get("Cache-Control") == "no-store"
    finally:
        server.shutdown()


def test_percorso_sconosciuto(opzioni):
    server = avvia(opzioni)
    try:
        stato, _ = chiedi(opzioni.url + "altro")
        assert stato == 404
    finally:
        server.shutdown()


# --- sincronizzazione ------------------------------------------------------


def test_sync_funziona(opzioni):
    server = avvia(opzioni)
    try:
        stato, corpo = chiedi(
            opzioni.url + "sync", "POST", {srv.HEADER_RICHIESTA: "1"}
        )
        assert stato == 200
        dati = json.loads(corpo)
        assert dati["ok"] is True
        assert any("Sedute nuove" in r for r in dati["righe"])
        assert opzioni.chiamate == ["sync"]
        assert Path(opzioni.output_path).is_file()  # dashboard rigenerata
    finally:
        server.shutdown()


def test_sync_riporta_l_errore_invece_di_fingere(opzioni):
    opzioni.sincronizza = lambda conn: ([], "Hevy ha rifiutato la chiave API")
    server = avvia(opzioni)
    try:
        stato, corpo = chiedi(opzioni.url + "sync", "POST", {srv.HEADER_RICHIESTA: "1"})
        assert stato == 502
        assert "rifiutato la chiave" in json.loads(corpo)["errore"]
    finally:
        server.shutdown()


# --- difese ----------------------------------------------------------------


def test_sync_rifiutato_senza_header(opzioni):
    """Una pagina qualunque nel browser non deve poter far partire un sync."""
    server = avvia(opzioni)
    try:
        stato, corpo = chiedi(opzioni.url + "sync", "POST")
        assert stato == 403
        assert srv.HEADER_RICHIESTA in json.loads(corpo)["errore"]
        assert opzioni.chiamate == []
    finally:
        server.shutdown()


def test_sync_rifiutato_da_un_altra_origine(opzioni):
    server = avvia(opzioni)
    try:
        stato, corpo = chiedi(
            opzioni.url + "sync",
            "POST",
            {srv.HEADER_RICHIESTA: "1", "Origin": "https://sito-qualunque.example"},
        )
        assert stato == 403
        assert "origine" in json.loads(corpo)["errore"]
        assert opzioni.chiamate == []
    finally:
        server.shutdown()


def test_sync_accettato_dalla_nostra_origine(opzioni):
    server = avvia(opzioni)
    try:
        stato, _ = chiedi(
            opzioni.url + "sync",
            "POST",
            {srv.HEADER_RICHIESTA: "1", "Origin": opzioni.url.rstrip("/")},
        )
        assert stato == 200
    finally:
        server.shutdown()


def test_ascolta_solo_su_localhost(opzioni):
    server = avvia(opzioni)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.shutdown()
