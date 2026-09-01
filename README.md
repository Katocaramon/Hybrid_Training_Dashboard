# Strength Tracker

Strumento locale per analizzare le sedute di **forza** registrate con un Garmin
Epix Pro Gen 2 ed esportate in `.fit`. Le ripetizioni e i carichi sono inseriti
a mano sull'orologio durante la seduta, quindi i file contengono gia' i dati a
livello di singola serie.

Non e' un'app da bodybuilding: l'obiettivo e' capire se la palestra sta
costruendo forza **senza interferire con il carico di corsa**, con attenzione
particolare a hamstring, adduttori e glutei.

Tutto gira in locale: nessun servizio cloud, nessuna chiamata di rete a
runtime, nessun account Garmin Connect. I file `.fit` sorgente non vengono mai
modificati ne' spostati.

## Stato dei lavori

Il progetto procede per fasi, con una verifica alla fine di ognuna.

| Fase | Contenuto | Stato |
| --- | --- | --- |
| 1 | Scheletro del repo, `pyproject`, CLI funzionante | ✅ fatto |
| 2 | Parser FIT + test + dump JSON di ispezione | ⏳ in attesa di un file `.fit` reale |
| 3 | Schema SQLite e ingestione idempotente | ⬜ |
| 4 | Mappatura esercizi e metriche | ⬜ |
| 5 | Dashboard HTML | ⬜ |

I comandi non ancora implementati escono con codice `1` e lo dicono: non
fingono di aver lavorato.

## Installazione

Con [uv](https://docs.astral.sh/uv/) (consigliato):

```bash
uv sync
uv run strength-tracker --help
```

Fallback senza uv:

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt -e .
strength-tracker --help
```

## Uso

```bash
strength-tracker ingest <path>       # file singolo o cartella, ricorsivo
strength-tracker inspect <file.fit>  # dump JSON dei messaggi grezzi
strength-tracker unmapped            # esercizi non ancora mappati
strength-tracker correct <set_id> --reps N --weight K
strength-tracker report              # genera output/dashboard.html
strength-tracker stats               # riepilogo testuale nel terminale
```

Il flusso normale del dopo-allenamento e' `ingest` seguito da `report`,
concatenati da:

```bash
make session FIT=~/Downloads/palestra
```

### Percorsi di default

Sono relativi alla directory da cui lanci il comando (la root del progetto) e
sovrascrivibili:

| Cosa | Default | Variabile d'ambiente | Flag |
| --- | --- | --- | --- |
| Database | `data/strength.db` | `STRENGTH_TRACKER_DB` | `--db` |
| Mappatura | `config/exercise_mapping.yaml` | `STRENGTH_TRACKER_MAPPING` | `--mapping` |
| Dashboard | `output/dashboard.html` | `STRENGTH_TRACKER_OUTPUT` | `--output` |

## Struttura

```
.
├── config/exercise_mapping.yaml   # mappatura versionata, editabile a mano
├── src/strength_tracker/          # fit_parser, db, ingest, mapping, metrics, dashboard, cli
├── templates/dashboard.html.j2
├── vendor/chart.min.js            # Chart.js 4.4.4 UMD, MIT — inlineato nella dashboard
└── tests/                         # fixtures/ contiene FIT sintetici o anonimizzati
```

## Scelte tecniche

- **Dipendenze minime**: `fitdecode` (parsing FIT), `PyYAML` (mappatura),
  `Jinja2` (template della dashboard). Niente pandas: i volumi di dati sono
  quelli di 2-3 sedute a settimana, SQL e la stdlib bastano e avanzano.
- **SQLite via `sqlite3`**, nessun ORM. Le metriche vivono in viste SQL dove
  possibile, cosi' sono ispezionabili con qualsiasi client.
- **Chart.js vendorizzato** in `vendor/` e inserito inline nell'HTML: la
  dashboard e' un unico file che si apre con doppio click, anche offline.
- **Correzioni non distruttive**: gli override manuali stanno in una tabella
  `corrections` e vengono applicati sopra i dati grezzi in lettura, mai
  sovrascrivendoli.
- **Niente dati inventati**: se un campo manca resta `NULL` e la dashboard lo
  dichiara, invece di mostrare uno zero.
- **Estendibile alla corsa**: lo schema tiene le sedute di forza in tabelle
  proprie e i raccordi temporali (settimana ISO) come chiavi di
  aggregazione, cosi' aggiungere in seguito una tabella di attivita' di corsa
  non richiede di rifare il database.

Le assunzioni di ogni formula (Epley, densita', deriva FC) sono documentate qui
alla Fase 4.

## Privacy

`.gitignore` esclude `data/`, `output/`, `*.fit` e `*.db`. Fanno eccezione le
fixture di test, che sono file sintetici o anonimizzati.
