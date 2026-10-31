# Strength Tracker

*[English](README.md) · **Italiano***

Strumento locale per analizzare le sedute di **forza** e confrontarle con il
carico di corsa. I dati arrivano da tre sorgenti:

- i file `.fit` di un Garmin Epix Pro Gen 2 — serie, carichi, frequenza
  cardiaca a 1 Hz e tempi di ogni serie e pausa;
- **Hevy** — serie e carichi affidabili, perche' li scrivi tu nell'app, ma
  senza FC e senza tempi per serie. Da export CSV o direttamente dall'API;
- la **Personal API di Garmin** — corse, bici, HRV, sonno, FC a riposo e peso:
  tutto cio' che un `.fit` di palestra non contiene.

Un database solo e **due dashboard**: palestra e corsa stanno nella stessa
riga settimana per settimana, ma ogni pagina risponde alle sue domande. Un esercizio mappato su piu' sorgenti mantiene **una storia sola**
anche quando cambi strumento di tracciamento.

Non e' un'app da bodybuilding: l'obiettivo e' capire se la palestra sta
costruendo forza **senza interferire con il carico di corsa**, con attenzione
particolare a hamstring, adduttori e glutei.

I dati restano sul tuo disco: nessun servizio cloud, nessun database remoto,
nessuna dashboard online. La rete si usa **solo** quando glielo chiedi tu, con
`sync-hevy` e `sync-garmin`; tutto il resto — ingestione, metriche, dashboard —
gira offline. I file `.fit` sorgente non vengono mai modificati ne' spostati.

## Stato dei lavori

Il progetto procede per fasi, con una verifica alla fine di ognuna.
Tutte e cinque sono complete: `ingest` + `report` è il flusso del dopo-allenamento.

| Fase | Contenuto | Stato |
| --- | --- | --- |
| 1 | Scheletro del repo, `pyproject`, CLI funzionante | ✅ fatto |
| 2 | Parser FIT + test + dump JSON di ispezione | ✅ fatto |
| 3 | Schema SQLite, ingestione idempotente, mappatura, correzioni | ✅ fatto |
| 4 | Metriche (volume, e1RM, densità, deriva FC) | ✅ fatto |
| 5 | Dashboard HTML | ✅ fatto |
| + | Import da Hevy (CSV, API, `sync-hevy`) e dashboard servita in locale | ✅ fatto |
| + | Corsa e recupero dalla Personal API di Garmin, nello stesso database | ✅ fatto |
| + | Fusione col progetto `garmin-dashboard`: due pagine, un repo, un database | ✅ fatto |

## Come si usa, dall'inizio

Gira **in locale sul tuo Mac**. Non c'è niente da mettere online: il database,
i `.fit` e la dashboard restano sul tuo disco.

### 1. Una volta sola: installare

```bash
git clone https://github.com/Katocaramon/Hybrid_Training_Dashboard.git
cd Hybrid_Training_Dashboard
uv sync
```

Se `uv` non ce l'hai: `curl -LsSf https://astral.sh/uv/install.sh | sh`
(oppure `brew install uv`). In alternativa il fallback con venv è più sotto.

Il repo ha un solo branch, `claude/strength-tracker-garmin-fit-hmc1px`, ed è
già quello di default: `git clone` ti dà direttamente questo.

### 2. Dopo ogni seduta: prendere i dati

**Da Hevy, senza toccare niente.** Se hai una chiave API, `sync-hevy` scarica
gli allenamenti da solo:

```bash
export HEVY_API_KEY='la-tua-chiave'   # mettila nel tuo ~/.zshrc
make sync                              # scarica e rigenera la dashboard
```

Oppure `make serve`, che apre la dashboard su `http://127.0.0.1:8765` con un
pulsante **Sincronizza** che fa tutto da solo: scarica, rigenera e ricarica la
pagina. Il server gira solo su questo computer, vive finché lo tieni aperto e
si ferma con Ctrl+C — il file statico resta comunque generato.

Oppure **doppio click su `aggiorna.command`** dal Finder: fa le stesse due cose
e riapre la dashboard, senza passare dal Terminale. Puoi trascinarlo nel Dock.
Lanciato dal Finder non legge il tuo `~/.zshrc`, quindi se la chiave sta lì
mettila anche in `~/.strength-tracker.env`:

```
HEVY_API_KEY=la-tua-chiave
```

La chiave si genera nelle impostazioni del profilo Hevy, sezione sviluppatore.
Va in una variabile d'ambiente, **mai nel repository**. La prima volta conviene
provare a vuoto, che non scrive niente nel database:

```bash
uv run strength-tracker sync-hevy --dry-run
```

**Da Hevy, con l'export.** Senza chiave API: dalle impostazioni del profilo
esporta i dati e ottieni un CSV con **tutto lo storico**. Non serve ritagliarlo:
reimportare l'export completo non duplica niente, entrano solo gli allenamenti
nuovi. Lo stesso vale se salvi su file una risposta JSON dell'API.

Le due strade importano gli stessi dati ma **non si riconoscono fra loro**: se
passi dal CSV a `sync-hevy`, svuota prima il database (`rm data/strength.db`) e
risincronizza, altrimenti ti ritrovi ogni seduta due volte. La dashboard te lo
segnalerebbe comunque, fra le anomalie.

**Dal Garmin**, se vuoi anche la frequenza cardiaca. Due strade, stesso file.

**Da Garmin Connect (web).** Apri l'attività → ingranaggio in alto a destra →
**"Esporta originale"**. Scarichi uno zip con dentro `<id>_ACTIVITY.fit`.

> Deve essere **"Esporta originale"**. TCX, GPX e CSV non contengono i messaggi
> `set`, quindi niente serie, niente carichi, niente FC per serie.

**Dall'orologio via USB.** Collega l'Epix al Mac: compare come disco, i file
stanno in `GARMIN/Activity/` e si chiamano tipo `2026-09-01-06-50-09.fit`.
Sono gli stessi file originali. Il nome non conta: se manca l'id di Connect
l'identità della seduta viene dal seriale dell'orologio più l'ora di creazione,
quindi l'ingestione resta idempotente lo stesso.

### 2b. Corsa e recupero: `sync-garmin`

Serve la tua **Personal API Key** di Garmin, che si genera dal tuo account
Garmin Connect ed e' legata solo a te:

```bash
export GARMIN_PERSONAL_API_KEY='la-tua-chiave'   # o in ~/.strength-tracker.env
uv run strength-tracker sync-garmin --dry-run    # prima prova a vuoto
uv run strength-tracker sync-garmin              # poi davvero
```

Porta nel database condiviso:

- **attivita' di resistenza** — corsa, bici, nuoto: distanza, tempo in
  movimento, dislivello, passo, FC media e massima, carico di allenamento,
  training effect, VO2max e migliori frazioni (1 km, miglio, 5, 10, mezza);
- **stato di recupero**, un giorno per riga — HRV notturna con stato e
  baseline, sonno (totale, profondo, REM, punteggio), FC a riposo, Body
  Battery, stress, passi e peso.

Le **sedute di palestra vengono saltate di proposito**: l'elenco attivita'
dell'API dice che esistono ma non espone le serie, quindi importarle creerebbe
sedute vuote in conflitto con quelle vere di Hevy. Il rapporto finale lo dice,
con il numero di attivita' saltate e il motivo.

Al primo giro scarica un anno di storico (`--giorni N` per cambiarlo, `--dal
GG/MM/AAAA` per partire da una data). Dopo riprende da dove era arrivato,
rileggendo gli ultimi tre giorni, che erano ancora parziali. Si puo'
interrompere: ogni finestra di giorni viene salvata appena letta.

```bash
make sync-all      # Hevy + Garmin + dashboard, in un comando
```

**Hai gia' lo storico altrove?** Se un altro progetto ha gia' scaricato i dati
Garmin come JSON (`activities.json`, `daily.json`, `sleep.json`, `hrv.json`,
`weight.json`), entrano da qui senza toccare la rete:

```bash
uv run strength-tracker import-garmin ~/garmin-dashboard/data
```

Passa dagli stessi normalizzatori della sincronizzazione, quindi stessi campi e
stessa identita': un `sync-garmin` successivo non duplica niente e riprende
dall'ultimo giorno importato, invece di riscaricare un anno.

### 3. Ogni volta: due comandi

Copia i file — `.fit`, `.csv` di Hevy, mischiati, anche in sottocartelle — in
`data/fit/` e lancia:

```bash
make session
```

Che è `ingest` seguito da `report`. Poi apri `output/dashboard.html` con doppio
click — oppure `open output/dashboard.html`.

Se i file li tieni altrove: `make session FIT=~/Downloads/garmin`. La cartella
`data/` è già in `.gitignore`, i tuoi allenamenti non finiscono mai su GitHub.

**Rilanciarlo non fa danni.** I file già letti vengono saltati, la stessa
seduta non entra due volte nemmeno se rinomini il file, e le correzioni manuali
sopravvivono. Puoi tenere lì dentro tutto lo storico e ridare `make session`
ogni volta.

### 4. Le prime volte: completare la mappatura

```bash
strength-tracker unmapped          # cosa non riconosce ancora
strength-tracker unmapped --yaml   # le voci pronte da incollare
```

Incolli in `config/exercise_mapping.yaml`, rilanci `make report`: **non serve
re-importare niente**, la mappatura si applica in lettura.

### 5. Quando un carico manca o è sbagliato

```bash
strength-tracker stats                              # trova la serie
strength-tracker correct 42 --reps 8 --weight 62.5  # la corregge
```

I dati grezzi non vengono toccati: la correzione ci va sopra e la dashboard
segnala quali valori vengono dal file e quali da te.

### Dove finisce cosa

| Cosa | Dove | Nel repo? |
| --- | --- | --- |
| I tuoi `.fit` | `data/fit/` | no, ignorato |
| Database | `data/strength.db` | no, ignorato |
| Dashboard palestra | `output/dashboard.html` | no, ignorato |
| Dashboard corsa | `output/corsa.html` | no, ignorato |
| Mappatura esercizi | `config/exercise_mapping.yaml` | sì, versionata |
| Chiavi API | ambiente o `~/.strength-tracker.env` | no, mai |
| Token Garmin (cache 20h) | `data/garmin_token.json` | no, ignorato |

## Le sorgenti della palestra

Per le sedute di forza ci sono due sorgenti: i `.fit` dell'orologio e Hevy.
`ingest` riconosce il formato dall'estensione e riempie le stesse tabelle. I
dati di Hevy arrivano in due forme — il **CSV** esportato dall'app e il
**JSON** dell'API (`/v1/workouts`) — e passano dallo stesso parser. Entrambe
contengono tutti gli allenamenti, quindi un file solo puo' produrre molte
sedute.

Il payload dell'API dice le stesse cose meglio del CSV: orari in **UTC vero**
invece che in ora locale muta, un `exercise_template_id` stabile accanto al
titolo, e il tipo di ogni serie (`normal` o `warmup`). Gli orari UTC vengono
datati nel fuso del computer — `STRENGTH_TRACKER_TZ` lo sovrascrive — perche'
Hevy non registra in che fuso ti sei allenato.

**Scegli una sorgente sola.** Un allenamento che arriva dall'API è
identificato dall'id che Hevy stesso gli dà (`hevy:<workout_id>`): stabile per
costruzione, indifferente agli arrotondamenti dell'orario e al cambio dell'ora.
Il CSV quell'id non ce l'ha, quindi lì l'identità si ricostruisce da titolo più
ora locale al minuto — ed è una ricostruzione, che sullo stesso allenamento può
non coincidere.

Mescolare export CSV e sync via API sullo stesso periodo crea quindi sedute
doppie. Se succede non resta nascosto: il controllo sulle **sedute
sovrapposte** le segnala fra le anomalie.

| | Garmin `.fit` | Hevy `.csv` |
| --- | --- | --- |
| Serie, reps, carichi | solo se confermati sull'orologio | **sempre** |
| Frequenza cardiaca | 1 Hz per tutta la seduta | assente |
| Orario e durata di ogni serie | sì | assente |
| Durata delle pause | sì | assente |
| Tempo attivo | `total_timer_time` | assente (c'è solo inizio–fine) |
| Fuso orario | dal dispositivo | assente: l'ora è locale, senza offset |
| Nome esercizio | indice numerico del catalogo | testo in chiaro (piu' un id stabile, via API) |

Quello che manca **non viene stimato**: per le sedute Hevy la deriva della FC e
il rapporto lavoro/riposo restano `NULL`, e la densità si calcola sul tempo
totale invece che sul tempo attivo, dichiarandolo (`densita_base`).

### Un esercizio, due chiavi

Le chiavi grezze di Hevy sono `hevy:<nome esercizio>`, così non si confondono
con gli slug del catalogo Garmin. Metterle nella **stessa voce** della
mappatura è ciò che tiene insieme la storia di un esercizio attraverso il
cambio di strumento:

```yaml
  - name: Dumbbell bench press
    primary: petto
    match:
      - bench_press/dumbbell_bench_press     # dall'orologio
      - "hevy:Bench Press (Dumbbell)"        # da Hevy
```

Un avvertimento: su Hevy **la nota dello step non esiste**, quindi il trucco
che distingue il Copenhagen plank da un plank laterale non funziona. Su Hevy
conviene scegliere una voce di esercizio distinta e mapparla a parte.

### La stessa seduta registrata due volte

Se logghi la stessa sessione sull'orologio *e* su Hevy, il tonnellaggio viene
contato due volte. Il sistema non sceglie per te: se ne accorge — sedute di
sorgenti diverse che si sovrappongono nello stesso giorno — e lo segnala fra le
anomalie. Sta a te cancellarne una o smettere di importare una delle due.

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
  strutturato, `set.wkt_step_index` punta qui e dà ripetizioni
  (`duration_reps`) e peso (`exercise_weight`, scala **100**, non 16)
  **pianificati**. Sono valori pianificati, non eseguiti: restano in colonne
  separate e non entrano mai nel volume.
- **`workout_step.notes` porta il nome vero degli esercizi fuori catalogo.**
  Il Copenhagen plank nel catalogo Garmin non esiste: viene registrato come
  `plank/side_plank`, e a distinguerlo da un plank laterale vero è solo la
  nota dello step, "Copenhagen plank". La stessa chiave grezza può quindi
  voler dire due esercizi diversi in due sedute diverse, e la mappatura sa
  qualificarci sopra (vedi sotto).
- Le etichette di `exercise_title` sono **nella lingua dell'orologio**
  ("Stacco con Trap-bar"). Lo slug del catalogo resta invece stabile e in
  inglese: è quello la chiave di mappatura, l'etichetta è solo per leggere.
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

## Il database

Quattro tabelle di dati più due di servizio, nessun ORM:

| Tabella | Contenuto |
| --- | --- |
| `sessions` | una riga per attività: `session_uid` univoco, `source` (`garmin` o `hevy`), data locale, settimana ISO, durata, tempo attivo, FC media/max, calorie, dispositivo, file sorgente, ora di ingestione |
| `sets` | una riga per serie **e** per pausa (`set_type`), solo dati grezzi del file; da Hevy arrivano anche `rpe`, `superset_id` e `set_kind` |
| `hr_samples` | frequenza cardiaca a 1 Hz, `(session_id, ts_utc)` come chiave |
| `corrections` | override manuali, append-only |
| `exercise_map` | proiezione del YAML di mappatura, riscritta a ogni comando |
| `ingested_files` | registro dei file già letti, con il motivo degli scarti |

### Dati grezzi, mappatura e correzioni restano separati

`sets` contiene solo quello che c'è nel file. La normalizzazione vive in
`exercise_map` e le correzioni in `corrections`: la vista **`v_sets`** le
sovrappone in lettura ai dati grezzi, che non vengono mai riscritti. Tre
conseguenze pratiche:

- editi `config/exercise_mapping.yaml` e rilanci `report`: **niente
  re-ingestione**, i nomi cambiano subito;
- ogni riga di `v_sets` porta sia il valore effettivo (`reps`, `weight_kg`)
  sia quello del file (`reps_raw`, `weight_kg_raw`) e la sua provenienza
  (`data_source` = `file` o `correzione`);
- `volume_kg` è `NULL` se manca reps **o** peso. Mai zero al posto di "non
  misurato": è la differenza fra un plank a corpo libero e un dato assente.

Le correzioni non puntano a `sets.id` (che cambia con `--force`) ma alla
coppia stabile `(session_uid, set_index)`: **sopravvivono alla
re-ingestione**. Sono append-only, vale l'ultima, e lo storico resta.

### Idempotenza su tre livelli

1. i file già letti (stesso percorso, stesso sha256) vengono saltati senza
   nemmeno riaprirli, salvo `--force`;
2. `sessions.session_uid` è `UNIQUE`: lo stesso allenamento non entra due
   volte nemmeno se rinomini o sposti il file;
3. riscrivere una seduta cancella e reinserisce le sue serie e i suoi
   campioni FC — nessun duplicato, e le correzioni restano.

### Perché la FC resta a campione singolo

1 Hz sono ~4.300 righe per seduta, cioè ~700k righe l'anno a 3 sedute a
settimana: SQLite non se ne accorge. Tenere il grezzo permette di
ricalcolare la FC per serie anche se domani cambiassero i confini delle
serie (o se una correzione ne sposta uno). Aggregare subito farebbe
risparmiare spazio che non è un problema, perdendo dati che non si
recuperano.

### Il carico di corsa, nello stesso database

`sessions` era già la tabella generica delle attività, con `activity_type`,
durata, FC, calorie e la settimana ISO precalcolata: la promessa era che le
sedute di corsa potessero entrare senza rifare nulla. La **migrazione 5** la
mantiene, senza una riga di dati riscritta.

Le attività di resistenza stanno in `sessions` con `activity_type` diverso da
`'strength'` (`run`, `bike`, `swim`, `endurance`) e il loro dettaglio in
`endurance_details` — distanza, passo, dislivello, carico di allenamento,
training effect, VO2max, migliori frazioni — con FK su `sessions(id)`.

Lo stato quotidiano dell'atleta non appartiene a nessuna seduta, quindi ha una
tabella sua: `wellness`, una riga per giorno con HRV (media notturna,
settimanale, stato e baseline), sonno (totale, profondo, REM, punteggio), FC a
riposo, Body Battery, stress, passi e peso.

Sopra ci sono due viste:

| Vista | Cosa dà |
| --- | --- |
| `v_endurance` | ogni attività col suo dettaglio: la controparte di `v_sets` per la corsa |
| `v_settimane` | una riga per settimana ISO: sedute e volume in palestra **accanto** a km, tempo e dislivello di corsa |

`v_settimane` è il motivo per cui i due progetti condividono un database: se il
volume in palestra sale e i chilometri scendono nella stessa settimana,
l'interferenza si vede qui. Niente viene aggregato alla scrittura, così la
definizione di "settimana carica" può cambiare domani senza toccare i dati.

Dato che `sessions` ora è condivisa, ogni conteggio di sedute di forza filtra
esplicitamente su `activity_type = 'strength'`: una corsa non si conta come
seduta di palestra, e la dashboard della forza resta quella di prima.

### Reps e carichi: quando ci sono e quando no

Garmin Connect esporta i `.fit` solo come **"Export Original"**: il file
originale caricato dall'orologio. Le modifiche fatte dopo dall'app (reps,
carichi, correzioni) restano nel database di Connect e **non finiscono mai nel
FIT esportato**.

Confermato su due sedute reali:

| Seduta | `repetitions` | `weight` |
| --- | --- | --- |
| 01/09 "Day 1 Upper Body", valori inseriti dopo sul telefono | assenti su 35 serie | assenti |
| 01/09 "Day 2 Legs", valori confermati sull'orologio | presenti su 14 serie su 15 | presenti dove il carico c'era |

Quindi i dati arrivano, ma **solo se confermati sull'orologio durante la
seduta**. Dove mancano, `volume_kg` resta `NULL`: si inseriscono a posteriori
con `strength-tracker correct <set_id> --reps N --weight K`, che li scrive in
`corrections` senza toccare i dati grezzi.

Le metriche che non dipendono dal carico — serie per gruppo muscolare, tempo
sotto tensione, densità della seduta, deriva della FC — funzionano comunque.

### Quando la stessa chiave grezza vuol dire due esercizi

Il Copenhagen plank arriva come `plank/side_plank`, esattamente come un plank
laterale vero. A distinguerli è la nota dello step dell'allenamento. La
mappatura può quindi qualificare una chiave:

```yaml
  - name: Copenhagen plank
    primary: adduttori
    match:
      - key: plank/side_plank
        note: Copenhagen plank      # vince sulla voce generica

  - name: Side plank
    primary: core
    match: [plank/side_plank]       # senza nota: plank laterale vero
```

`v_sets` fa due join sulla mappatura e la voce qualificata dalla nota ha la
precedenza. Il confronto è case-insensitive e ignora gli spazi ai bordi.

## Le metriche

Tutte partono da `v_sets`, quindi correzioni e mappatura sono già applicate.
Ogni assunzione è qui, non nascosta nel codice.

| Metrica | Formula | Assunzioni e limiti |
| --- | --- | --- |
| **Tonnellaggio** | Σ (peso × reps) | Solo serie con `weight_mode = carico`. Se manca reps o peso la serie non contribuisce ed è contata a parte: `NULL`, mai zero |
| **e1RM** | Epley: peso × (1 + reps / 30) | Stima lineare tarata sulle serie corte. Sopra le **12 reps** è marcata inaffidabile. A 1 rep la formula darebbe 1,033× il peso, quindi quel caso restituisce il peso |
| **Densità** | tonnellaggio / tempo attivo | Tempo attivo = `session.total_timer_time`. Hevy non ce l'ha: lì si usa il tempo totale e la riga lo dichiara in `densita_base` |
| **Lavoro/riposo** | Σ durata serie attive / Σ durata pause | Dai messaggi `set`. Le pause non registrate non vengono stimate, e Hevy non le contiene affatto: per quelle sedute resta `NULL` |
| **Deriva FC** | FC media ultimo terzo − primo terzo delle serie | Proxy grezzo di fatica: sale anche solo perché la seduta scalda. Servono ≥3 serie con FC, altrimenti `NULL` |
| **Serie per gruppo** | conteggio serie attive per settimana ISO | Più robusta del tonnellaggio quando gli esercizi cambiano o il carico non è misurabile. Le serie di riscaldamento di Hevy (`set_kind = warmup`) sono contate come le altre |
| **Misura del lavoro** | ripetizioni totali, oppure tempo sotto tensione | Scelta per esercizio: un plank non ha ripetizioni, uno stacco non ha una durata che voglia dire qualcosa. La dashboard mostra quella che l'esercizio ha davvero |
| **Media mobile** | media 4 settimane sul volume | Calcolata **solo sulle settimane con dati**: una settimana senza allenamento non vale zero, altrimenti la media crollerebbe per finta |

La FC per serie si ottiene incrociando i campioni a 1 Hz con la finestra
temporale di ogni serie. Il confronto è su epoch e non su stringhe ISO: con i
fusi orari il confronto testuale è inaffidabile.

### Non tutti i pesi sono carichi

`weight_mode` nella mappatura dice come leggere il peso registrato:

| Modo | Significato | Tonnellaggio | Carico effettivo |
| --- | --- | --- | --- |
| `carico` (default) | il peso è il carico esterno | peso × reps | il peso |
| `assistenza` | il peso è l'**aiuto** ricevuto | `NULL` | peso corporeo − assistenza |
| `corpo_libero` | nessun carico esterno | `NULL` | peso corporeo |

Alle trazioni assistite 40 kg indicano quanto la macchina ti *aiuta*:
moltiplicarli per le ripetizioni darebbe un tonnellaggio non solo sbagliato ma
rovesciato di segno, perché più assistenza significa serie più facile. Qui il
progresso è l'assistenza che **cala**, ed è quello che la progressione mostra
(`assistenza_minima_kg`, ed e1RM calcolato sul carico effettivo).

Il peso corporeo viene letto dal messaggio `user_profile` del file stesso. Dove
serve per una stima, la riga porta `carico_stimato = 1` e il tonnellaggio
finisce in `volume_stimato_kg`, tenuto **fuori** dal totale: il numero di testa
resta il carico esterno vero.

### Inserire a mano i carichi mancanti

Quando i valori sono stati messi su Garmin Connect dopo la seduta, nel `.fit`
non ci sono. Si trascrivono in un CSV numerando le serie **come le mostra
Connect** (solo le attive, da 1):

```csv
data,seduta,serie,reps,peso_kg,nota
01/09/2026,Day 1,7,10,24,Pressa su panca con manubrio
```

```bash
strength-tracker correct --from-csv examples/correzioni_day1_20260901.csv
```

La colonna `seduta` serve solo quando in quel giorno c'è più di un
allenamento. In `examples/` c'è la seduta "Day 1 Upper Body" del 01/09/2026
già trascritta. Finiscono tutte in `corrections`: i dati grezzi restano
intatti e `data_source` dice da dove viene ogni valore.

## Le due dashboard

Un database, due pagine. Non è un compromesso: le domande sono diverse, e una
pagina sola le avrebbe diluite entrambe.

| Pagina | File | Cosa risponde |
| --- | --- | --- |
| Palestra | `output/dashboard.html` | sto diventando più forte? dove sto caricando? |
| Corsa e recupero | `output/corsa.html` | quanto sto correndo? come sto recuperando? |

```bash
strength-tracker report         # genera output/dashboard.html
strength-tracker report-corsa   # genera output/corsa.html
make report                     # tutte e due
make session FIT=~/Downloads/palestra   # ingest + report in un colpo
```

Con `make serve` stanno nello stesso server, la palestra su `/` e la corsa su
`/corsa`, con un collegamento dall'una all'altra in alto a destra. Il pulsante
**Sincronizza** vale per entrambe: scarica da Hevy e da Garmin e rigenera tutte
e due le pagine.

### La dashboard della corsa

Viene dal progetto `garmin-dashboard`, ed è la stessa pagina: SVG disegnati a
mano, nessuna dipendenza, nemmeno Chart.js. A cambiare è da dove prende i
numeri — il database condiviso invece dei suoi `data/*.json`. Due conseguenze:

- Garmin si interroga **una volta sola**, non una per dashboard.
- il gruppo "Palestra" non viene più dalle attività Garmin, che non espongono
  le serie, ma dalle **sedute di forza vere**: stesse ore, contate da dove ci
  sono anche carichi e ripetizioni.

Mostra conto alla rovescia verso la gara, HRV notturna con la sua fascia
normale, chilometri a settimana, ore per sport, carico acuto contro cronico,
battito a riposo, sonno, record sulle varie distanze e ultime attività.

La gara si configura in `config/corsa.yaml` (copia `config/corsa.example.yaml`):

```yaml
race_name: Mezza di Roma
race_date: 2027-03-14
plan_start: 2026-11-02     # il lunedì della prima settimana del piano
```

Senza quel file la pagina si genera lo stesso e dice che la gara manca, invece
di inventarne una. Si può anche puntare il vecchio `settings.json`:
`strength-tracker report-corsa --settings ~/garmin-dashboard/settings.json`.

### Due modi di aprirla

**Doppio click sul file.** `output/dashboard.html` è autonomo e funziona
offline, ma è una fotografia: per aggiornarlo serve un comando, perché il
browser non permette a un file locale di eseguirne.

**`make serve`.** Le stesse identiche pagine servite da
`http://127.0.0.1:8765` (palestra) e `http://127.0.0.1:8765/corsa`, dove il
pulsante in alto **sincronizza davvero**: chiama il processo che hai avviato
tu, che scarica da Hevy e da Garmin e rigenera entrambe. Server della libreria standard,
nessuna dipendenza in più, in ascolto solo su localhost. `POST /sync` richiede
un header che una richiesta cross-origin non può aggiungere, e rifiuta le
origini estranee: una pagina qualunque aperta nel browser non può farti partire
una sincronizzazione.

Il resto di questa sezione riguarda la dashboard della palestra.

Un **unico file HTML** in `output/dashboard.html`: Chart.js è inserito inline
dal repo e i dati sono un blocco JSON dentro la pagina. Zero richieste di rete,
zero server — si apre con doppio click, anche in aereo. Nessun tag `src` o
`href` verso l'esterno, e c'è un test che lo verifica.

Cosa contiene:

- **Riepilogo**: sedute, periodo, volume totale (con quante serie lo
  sostengono), tempo sotto tensione, sedute nelle ultime 4 settimane.
- **Volume settimanale** con media mobile a 4 settimane.
- **Carico per gruppo muscolare** a settimana, barre impilate, con selettore
  serie / tonnellaggio: le serie restano leggibili anche dove il carico non è
  misurabile.
- **Gruppi sotto osservazione** (hamstring, adduttori, glutei): ci sono sempre
  tutti e tre, e se un gruppo non è stato allenato la pagina lo **scrive**
  invece di mostrare un grafico vuoto.
- **Progressione per esercizio** con selettore: peso migliore ed e1RM sullo
  stesso asse (sono entrambi kg), ripetizioni totali in un grafico a parte —
  mai due scale sullo stesso grafico. Per le trazioni assistite mostra
  l'assistenza al posto del peso, e lo dice.
- **Ultime sedute** espandibili serie per serie: numerazione identica a quella
  di Garmin Connect, FC media della singola serie, e un tag che dice se il
  valore viene dal file o da una correzione.
- **Anomalie**: esercizi non mappati, serie con peso zero, ripetizioni
  sospette, serie di durata anomala, sedute senza ripetizioni. Ognuna con una
  riga che spiega cosa farci.
- **Note di metodo** in fondo: le assunzioni di ogni formula, sulla pagina e
  non solo qui.

Dettagli di resa: tema chiaro e scuro (segue il sistema, con interruttore in
alto a destra), palette categorica a 8 slot in ordine fisso — l'ordine è quello
che la rende leggibile ai daltonismi, non una scelta estetica — e oltre l'ottavo
gruppo i più piccoli confluiscono in "altri", che dichiara cosa contiene. Ogni
grafico ha la sua tabella, e le stime poco affidabili (e1RM oltre le 12
ripetizioni) cambiano **forma** del punto, non solo colore.

## Installazione senza uv

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt -e .
strength-tracker --help
```

Con questa strada i comandi si lanciano come `strength-tracker ...` invece di
`uv run strength-tracker ...` (e il `Makefile` va adattato di conseguenza).

## Tutti i comandi

```bash
strength-tracker sync-hevy              # scarica da Hevy (serve HEVY_API_KEY)
strength-tracker sync-hevy --dry-run    # mostra cosa entrerebbe, senza scrivere
strength-tracker sync-garmin            # corse, bici, HRV, sonno, FC a riposo, peso
strength-tracker sync-garmin --dry-run  # come sopra, senza scrivere niente
strength-tracker sync-garmin --dal 01/09/2026   # da una data precisa
strength-tracker import-garmin <dir>   # storico Garmin gia' in JSON, senza rete
strength-tracker ingest <path>          # file singolo o cartella, ricorsivo
strength-tracker ingest <path> --force  # rilegge anche i file già processati
strength-tracker inspect <file.fit>     # dump JSON dei messaggi grezzi (--raw per gli unknown_*)
strength-tracker unmapped               # esercizi non ancora mappati
strength-tracker unmapped --yaml        # le voci già pronte da incollare nel YAML
strength-tracker correct <set_id> --reps N --weight K [--exercise RAW_KEY] [--note "..."]
strength-tracker correct --from-csv <file.csv>   # correzioni in blocco
strength-tracker report                 # genera output/dashboard.html
strength-tracker report-corsa           # genera output/corsa.html
strength-tracker serve                  # dashboard su localhost, col pulsante Sincronizza
strength-tracker stats                  # riepilogo testuale nel terminale
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
| Dashboard palestra | `output/dashboard.html` | `STRENGTH_TRACKER_OUTPUT` | `--output` |
| Dashboard corsa | `output/corsa.html` | `STRENGTH_TRACKER_OUTPUT_CORSA` | `--output` |
| Gara e piano | `config/corsa.yaml` | `STRENGTH_TRACKER_CORSA_CONFIG` | `--settings` |
| Chiave API Hevy | — | `HEVY_API_KEY` | `--api-key` |
| Chiave Personal API Garmin | — | `GARMIN_PERSONAL_API_KEY` | `--api-key` |
| Token Garmin (cache 20h) | `data/garmin_token.json` | — | — |
| Fuso per gli orari Hevy | quello del computer | `STRENGTH_TRACKER_TZ` | — |

## Struttura

```
.
├── config/exercise_mapping.yaml   # mappatura versionata, editabile a mano
├── config/corsa.example.yaml      # modello per gara e piano (copia in corsa.yaml)
├── src/strength_tracker/          # fit_parser, hevy, garmin, db, ingest, mapping,
│                                  # metrics, dashboard, server, cli
├── templates/dashboard.html.j2    # palestra (Jinja + Chart.js)
├── templates/corsa.html           # corsa (SVG a mano, nessuna dipendenza)
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
- **Palestra e corsa nello stesso database, dashboard separate**: i numeri
  sono confrontabili e interrogabili con una join su `(iso_year, iso_week)`,
  ma ogni dashboard resta quella che era. La corsa e' entrata con una
  migrazione additiva, senza riscrivere un dato.
- **Due pagine, due tecnologie, nessun framework**: la palestra usa Jinja e
  Chart.js vendorizzato, la corsa disegna i suoi SVG a mano e non ha nemmeno
  quello. Uniformarle avrebbe voluto dire riscrivere codice che funziona, per
  guadagnarci niente: sono due file statici, non due applicazioni.
- **La rete solo su richiesta**: l'unico modulo che la tocca e' quello delle
  sincronizzazioni. Le chiavi API si leggono da variabile d'ambiente o da un
  file fuori dal repository, non vengono mai stampate, e il token Garmin
  (valido 24 ore) finisce in `data/garmin_token.json` con permessi `0600`.

Le assunzioni di ogni formula (Epley, densita', deriva FC) sono documentate
nella sezione [Le metriche](#le-metriche).

## Privacy

`.gitignore` esclude `data/`, `output/`, `*.fit`, `*.db`, i file `*.env` e
`garmin_token.json`. Fanno eccezione le fixture di test, che sono file
sintetici o anonimizzati.

Le chiavi API non stanno nel repository. Mettile nell'ambiente, oppure in
`~/.strength-tracker.env` (che `aggiorna.command` legge, perche' un processo
lanciato dal Finder non passa per `~/.zshrc`):

```
HEVY_API_KEY=...
GARMIN_PERSONAL_API_KEY=...
```

La Personal API di Garmin e' legata al tuo account e viene usata in **sola
lettura**: il programma fa solo GET, piu' la POST che chiede il token. Non
scrive niente su Garmin Connect.
