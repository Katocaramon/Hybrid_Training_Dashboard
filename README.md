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
| 2 | Parser FIT + test + dump JSON di ispezione | ✅ fatto |
| 3 | Schema SQLite e ingestione idempotente | ⬜ |
| 4 | Mappatura esercizi e metriche | ⬜ |
| 5 | Dashboard HTML | ⬜ |

I comandi non ancora implementati escono con codice `1` e lo dicono: non
fingono di aver lavorato.

## Cosa contiene davvero un file FIT di forza

Verificato su un file reale (Epix Pro Gen 2, export Garmin Connect
`<activity_id>_ACTIVITY.fit`), non assunto dalla specifica. `strength-tracker
inspect <file.fit> --raw` rifa' questa ispezione su qualsiasi file, campi
`unknown_*` compresi: e' il primo comando da lanciare se un firmware nuovo
cambia le carte in tavola.

- **`set` (msg 225)** — una riga per serie *e* per pausa (`set_type` =
  `active` / `rest`). Campi utili: `message_index`, `start_time`, `duration`,
  `repetitions`, `weight` (kg, scala 16), `weight_display_unit`, `category`,
  `category_subtype`, `wkt_step_index`.
- `set.timestamp` **non** e' l'orario della serie: e' costante e pari
  all'inizio della sessione. L'orario vero e' `start_time`.
- `category` e `category_subtype` sono **array** (3 slot, spesso ripetuti o
  nulli): un set puo' dichiarare piu' categorie. Si legge a coppie e si usa la
  prima valida, conservando comunque l'array completo.
- Gli esercizi sono **indici numerici** di un catalogo chiuso
  (`category=21, subtype=42`). Il profilo FIT incluso in `fitdecode` contiene
  gli enum completi (53 categorie, 51 cataloghi di nomi), quindi la coppia
  diventa uno slug stabile: `pull_up/band_assisted_pull_up`. Se categoria o
  indice non sono nel profilo si tiene il numero grezzo (`pull_up/42`,
  `250/7`): niente crash, niente nomi inventati, e la serie finisce fra i non
  mappati.
- **`exercise_title` (msg 264)** — l'orologio scrive nel file stesso una
  tabella `(categoria, nome) -> etichetta` ("Band-assisted Pull-up"): la
  usiamo come etichetta leggibile quando c'e'.
- **`workout_step` (msg 27)** — se la seduta segue un allenamento
  strutturato, `set.wkt_step_index` punta qui e da' ripetizioni
  (`duration_reps`) e peso (`exercise_weight`, scala **100**, non 16)
  **pianificati**. Sono valori pianificati, non eseguiti: restano in colonne
  separate e non entrano mai nel volume.
- **`record` (msg 20)** — frequenza cardiaca a 1 Hz per tutta la seduta
  (~4300 campioni per 70 minuti).
- **`session` (msg 18)** — `total_timer_time` e' il tempo attivo (non esiste
  un campo dedicato), piu' FC media/max, calorie, `sport_profile_name`.
- **Non esiste un campo `activity_id`** dentro il FIT: l'id di Garmin Connect
  sta solo nel nome del file esportato.
- L'offset dal fuso locale si ricava da `activity.local_timestamp -
  activity.timestamp`. Serve per datare correttamente le sedute serali.

### Identita' di una seduta

`session_uid` in ordine di preferenza:

1. `garmin:<activity_id>` dal nome del file esportato;
2. `device:<serial>:<time_created>` — stabile anche se riesporti lo stesso
   allenamento e i byte cambiano;
3. `sha256:<hash del contenuto>`.

E' la chiave su cui poggia l'idempotenza dell'ingestione (Fase 3).

### Tolleranza agli errori

File troncati, file non-FIT e attivita' senza messaggi `set` (una corsa, per
dire) vengono **saltati con un motivo esplicito**, senza far cadere il resto
del batch. `parse_paths()` restituisce `(path, attivita', motivo_dello_skip)`.

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
└── tests/
    ├── fitgen.py                  # encoder FIT minimale: genera le fixture binarie
    └── fixtures/*.fit             # FIT sintetici versionati (nessun dato personale)
```

I test girano su file `.fit` binari veri prodotti da `tests/fitgen.py` e letti
dallo stesso `fitdecode` usato in produzione: nessun mock. Per rigenerarli:

```bash
uv run python tests/fitgen.py
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
