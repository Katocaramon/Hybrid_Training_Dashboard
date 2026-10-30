"""Server locale, per avere in dashboard un pulsante che sincronizza davvero.

Una pagina aperta con `file://` non puo' eseguire comandi: e' il browser a
impedirlo. Servendo la stessa pagina da `http://127.0.0.1` il vincolo cade,
perche' il pulsante non esegue niente — chiede al processo che hai avviato tu
di farlo.

Cosa e' e cosa non e':

* usa `http.server` della libreria standard, nessuna dipendenza in piu';
* ascolta **solo su 127.0.0.1**: non e' raggiungibile dalla rete;
* vive finche' lo tieni aperto, non e' un servizio installato;
* il file statico resta: `report` continua a produrre un HTML che si apre con
  doppio click, e questo server e' solo un modo in piu' di guardarlo.

Sulle richieste in arrivo si fanno due controlli, perche' una pagina qualunque
aperta nel tuo browser potrebbe altrimenti far partire una sincronizzazione a
tua insaputa:

* `POST /sync` richiede l'header `X-Strength-Tracker`, che una richiesta
  cross-origin non puo' aggiungere senza un preflight (che rifiutiamo);
* se la richiesta porta un `Origin`, deve essere il nostro.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

#: Header che deve accompagnare le richieste che cambiano qualcosa.
HEADER_RICHIESTA = "X-Strength-Tracker"


@dataclass
class Opzioni:
    """Tutto quello che serve al server, iniettato per poterlo provare."""

    db_path: Path
    output_path: Path
    #: Restituisce (righe_di_report, errore) — None se e' andata bene.
    sincronizza: Callable[[sqlite3.Connection], tuple[list[str], str | None]]
    #: Rigenera il file HTML a partire dal database.
    rigenera: Callable[[sqlite3.Connection], Path]
    host: str = "127.0.0.1"
    porta: int = 8765
    #: File da cui la dashboard dipende: se uno e' piu' recente dell'HTML
    #: gia' generato, la pagina va rifatta. Senza questo il server
    #: servirebbe una pagina vecchia dopo un aggiornamento del codice o una
    #: modifica alla mappatura, ed e' difficile accorgersene.
    fonti: tuple[Path, ...] = ()
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.porta}/"


def va_rigenerata(html: Path, fonti: tuple[Path, ...]) -> bool:
    """La pagina e' da rifare se manca o se una delle sue fonti e' piu' recente.

    Le fonti sono il database (dati nuovi) e il template (codice nuovo): senza
    questo controllo il server continuerebbe a servire l'HTML che ha trovato,
    e un aggiornamento del progetto non si vedrebbe mai.
    """
    html = Path(html)
    if not html.is_file():
        return True
    quando = html.stat().st_mtime
    return any(Path(f).exists() and Path(f).stat().st_mtime > quando for f in fonti)


def _crea_gestore(opzioni: Opzioni) -> type[BaseHTTPRequestHandler]:
    class Gestore(BaseHTTPRequestHandler):
        server_version = "strength-tracker"

        def log_message(self, formato: str, *args: Any) -> None:  # pragma: no cover
            log.debug("%s - %s", self.address_string(), formato % args)

        # --- utilita' ---------------------------------------------------
        def _json(self, codice: int, corpo: dict[str, Any]) -> None:
            grezzo = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
            self.send_response(codice)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(grezzo)))
            self.end_headers()
            self.wfile.write(grezzo)

        def _origine_accettabile(self) -> bool:
            origine = self.headers.get("Origin")
            if origine is None:
                return True  # richiesta non cross-origin
            return origine.rstrip("/") == opzioni.url.rstrip("/")

        def _connessione(self) -> sqlite3.Connection:
            from . import db

            return db.connect(opzioni.db_path)

        # --- rotte ------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            percorso = self.path.split("?", 1)[0]
            if percorso in ("/", "/index.html", "/dashboard.html"):
                return self._manda_dashboard()
            self._json(404, {"errore": f"percorso sconosciuto: {percorso}"})

        def _manda_dashboard(self) -> None:
            file = Path(opzioni.output_path)
            if va_rigenerata(file, opzioni.fonti):
                with opzioni._lock:
                    conn = self._connessione()
                    try:
                        file = opzioni.rigenera(conn)
                    finally:
                        conn.close()
            grezzo = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(grezzo)))
            # La pagina contiene i dati: non va messa in cache, altrimenti
            # dopo una sincronizzazione il browser mostrerebbe la vecchia.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(grezzo)

        def do_POST(self) -> None:  # noqa: N802
            percorso = self.path.split("?", 1)[0]
            if percorso != "/sync":
                return self._json(404, {"errore": f"percorso sconosciuto: {percorso}"})
            if self.headers.get(HEADER_RICHIESTA) is None:
                return self._json(
                    403, {"errore": f"richiesta senza header {HEADER_RICHIESTA}"}
                )
            if not self._origine_accettabile():
                return self._json(403, {"errore": "origine non ammessa"})

            with opzioni._lock:
                conn = self._connessione()
                try:
                    righe, errore = opzioni.sincronizza(conn)
                    if errore:
                        return self._json(502, {"errore": errore, "righe": righe})
                    opzioni.rigenera(conn)
                finally:
                    conn.close()
            self._json(200, {"ok": True, "righe": righe})

    return Gestore


def crea_server(opzioni: Opzioni) -> HTTPServer:
    """Server pronto, non ancora avviato. Porta 0 = una libera a caso."""
    server = HTTPServer((opzioni.host, opzioni.porta), _crea_gestore(opzioni))
    opzioni.porta = server.server_address[1]
    return server


def avvia(opzioni: Opzioni, apri_browser: bool = True) -> None:
    """Avvia il server e resta in ascolto finche' non lo fermi con Ctrl+C."""
    server = crea_server(opzioni)
    print(f"Dashboard su {opzioni.url}")
    print("Il pulsante 'Sincronizza' scarica da Hevy e ricarica la pagina.")
    print("Ctrl+C per fermare.")
    if apri_browser:  # pragma: no cover
        import webbrowser

        webbrowser.open(opzioni.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        print("\nServer fermato.")
    finally:
        server.server_close()
