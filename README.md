# Strength Tracker

***English** · [Italiano](README.it.md)*

A local tool for analysing **strength** sessions recorded on a Garmin Epix Pro
Gen 2 and exported as `.fit`. Reps and loads are entered by hand on the watch
during the session, so the files already carry set-level data.

This is not a bodybuilding app. The point is to tell whether the gym work is
actually building strength **without interfering with running load**, with
particular attention to hamstrings, adductors and glutes.

Everything runs locally: no cloud service, no network calls at runtime, no
Garmin Connect account. The source `.fit` files are never modified or moved.

> The dashboard and the CLI output are in **Italian** by design — this is a
> personal tool for an Italian-speaking athlete. The code, the schema and this
> document are in English.

## Status

The project was built in five phases, each verified before moving on. All five
are complete: `ingest` + `report` is the post-workout flow.

| Phase | Contents | Status |
| --- | --- | --- |
| 1 | Repo skeleton, `pyproject`, working CLI | ✅ done |
| 2 | FIT parser + tests + JSON inspection dump | ✅ done |
| 3 | SQLite schema, idempotent ingestion, mapping, corrections | ✅ done |
| 4 | Metrics (volume, e1RM, density, HR drift) | ✅ done |
| 5 | HTML dashboard | ✅ done |

## Getting started

It runs **locally on your Mac**. There is nothing to put online: the database,
the `.fit` files and the dashboard all stay on your disk.

### 1. Once: install

```bash
git clone https://github.com/Katocaramon/Hybrid_Training_Dashboard.git
cd Hybrid_Training_Dashboard
uv sync
```

If you don't have `uv`: `brew install uv`, or
`curl -LsSf https://astral.sh/uv/install.sh | sh`. A plain-venv fallback is
documented further down.

The repo has a single branch, `claude/strength-tracker-garmin-fit-hmc1px`, and
it is already the default: `git clone` gives you exactly that.

### 2. After each session: get the `.fit` file

Two routes, same file.

**From Garmin Connect (web).** Open the activity → gear icon, top right →
**"Export Original"**. You get a zip containing `<id>_ACTIVITY.fit`.

> It has to be **Export Original**. TCX, GPX and CSV do not contain `set`
> messages, so no sets, no loads, no per-set heart rate.

**From the watch over USB.** Plug the Epix into the Mac: it mounts as a disk,
and the files live in `GARMIN/Activity/`, named like
`2026-09-01-06-50-09.fit`. These are the same original files. The filename
doesn't matter: without a Connect id, session identity falls back to the
device serial plus creation time, so ingestion stays idempotent either way.

### 3. Every time: two commands

Drop the `.fit` files (any number, subdirectories included) into `data/fit/`
and run:

```bash
make session
```

That is `ingest` followed by `report`. Then open `output/dashboard.html` —
double-click it, or `open output/dashboard.html`.

If you keep the files elsewhere: `make session FIT=~/Downloads/garmin`. The
`data/` directory is already in `.gitignore`, so your training never reaches
GitHub.

**Re-running is safe.** Files already read are skipped, the same session never
lands twice even if you rename the file, and manual corrections survive. Keep
your whole history in there and run `make session` as often as you like.

### 4. Early on: finish the exercise mapping

```bash
strength-tracker unmapped          # what isn't recognised yet
strength-tracker unmapped --yaml   # entries ready to paste
```

Paste them into `config/exercise_mapping.yaml` and run `make report`:
**no re-import needed**, the mapping is applied at read time.

### 5. When a load is missing or wrong

```bash
strength-tracker stats                              # find the set
strength-tracker correct 42 --reps 8 --weight 62.5  # fix it
```

Raw data is never touched: the correction sits on top of it, and the dashboard
marks which values came from the file and which from you.

### Where things live

| What | Where | In the repo? |
| --- | --- | --- |
| Your `.fit` files | `data/fit/` | no, ignored |
| Database | `data/strength.db` | no, ignored |
| Dashboard | `output/dashboard.html` | no, ignored |
| Exercise mapping | `config/exercise_mapping.yaml` | yes, versioned |

## What a strength FIT file actually contains

Verified against real files (Epix Pro Gen 2, Garmin Connect
`<activity_id>_ACTIVITY.fit` export), not assumed from the spec.
`strength-tracker inspect <file.fit> --raw` redoes this inspection on any file,
`unknown_*` fields included — it's the first command to run if a firmware
update changes the rules.

- **`set` (msg 225)** — one row per set *and* per rest (`set_type` =
  `active` / `rest`). Useful fields: `message_index`, `start_time`,
  `duration`, `repetitions`, `weight` (kg, scale 16), `weight_display_unit`,
  `category`, `category_subtype`, `wkt_step_index`.
- `set.timestamp` is **not** the set's time: it is constant and equal to the
  session start. The real time is `start_time`.
- `category` and `category_subtype` are **arrays** (3 slots, often repeated or
  null): a set can declare more than one category. They are read pairwise, the
  first valid pair is used, and the full arrays are kept.
- Exercises are **numeric indices** into a closed catalogue (`category=21,
  subtype=42`). The FIT profile bundled with `fitdecode` carries the complete
  enums (53 categories, 51 name catalogues), so the pair becomes a stable slug:
  `pull_up/band_assisted_pull_up`. If the category or the index isn't in the
  profile, the raw number is kept (`pull_up/42`, `250/7`): no crash, no
  invented names, and the set shows up as unmapped.
- **`exercise_title` (msg 264)** — the watch writes a `(category, name) ->
  label` table into the file itself ("Band-assisted Pull-up"). It's used as the
  human-readable label when present.
- **`workout_step` (msg 27)** — if the session follows a structured workout,
  `set.wkt_step_index` points here and yields **planned** reps
  (`duration_reps`) and weight (`exercise_weight`, scale **100**, not 16).
  These are planned, not performed: they live in separate columns and never
  enter volume.
- **`workout_step.notes` carries the real name of off-catalogue exercises.**
  The Copenhagen plank doesn't exist in the Garmin catalogue: it is recorded as
  `plank/side_plank`, and the only thing separating it from an actual side
  plank is the step note, "Copenhagen plank". The same raw key can therefore
  mean two different exercises in two different sessions, and the mapping can
  qualify on that (see below).
- `exercise_title` labels are **in the watch's language** ("Stacco con
  Trap-bar"). The catalogue slug stays stable and English: the slug is the
  mapping key, the label is only for reading.
- **`record` (msg 20)** — heart rate at 1 Hz for the whole session (~4,300
  samples over 70 minutes).
- **`session` (msg 18)** — `total_timer_time` is the active time (there is no
  dedicated field), plus average/max HR, calories, `sport_profile_name`.
- **There is no `activity_id` field** inside the FIT: the Garmin Connect id
  exists only in the exported filename.
- The local UTC offset is derived from `activity.local_timestamp -
  activity.timestamp`. It is what dates evening sessions correctly.

### Session identity

`session_uid`, in order of preference:

1. `garmin:<activity_id>` from the exported filename;
2. `device:<serial>:<time_created>` — stable even if you re-export the same
   workout and the bytes change;
3. `sha256:<content hash>`.

This is the key idempotent ingestion rests on.

### Error tolerance

Truncated files, non-FIT files and activities without `set` messages (a run,
say) are **skipped with an explicit reason**, without bringing down the rest of
the batch. `parse_paths()` returns `(path, activity, skip_reason)`.

## The database

Four data tables plus two housekeeping ones, no ORM:

| Table | Contents |
| --- | --- |
| `sessions` | one row per activity: unique `session_uid`, local date, ISO week, duration, active time, avg/max HR, calories, device, source file, ingestion time |
| `sets` | one row per set **and** per rest (`set_type`), raw file data only |
| `hr_samples` | heart rate at 1 Hz, keyed by `(session_id, ts_utc)` |
| `corrections` | manual overrides, append-only |
| `exercise_map` | projection of the mapping YAML, rewritten on every command |
| `ingested_files` | log of files already read, with the reason for any skip |

### Raw data, mapping and corrections stay separate

`sets` holds only what is in the file. Normalisation lives in `exercise_map`
and corrections in `corrections`: the **`v_sets`** view layers them over the
raw data at read time, and the raw data is never rewritten. Three practical
consequences:

- edit `config/exercise_mapping.yaml`, run `report`: **no re-ingestion**, the
  names change immediately;
- every `v_sets` row carries both the effective value (`reps`, `weight_kg`) and
  the file's own (`reps_raw`, `weight_kg_raw`), plus its provenance
  (`data_source` = `file` or `correzione`);
- `volume_kg` is `NULL` when reps **or** weight is missing. Never zero standing
  in for "not measured": that is the difference between a bodyweight plank and
  an absent datum.

Corrections do not point at `sets.id` (which changes under `--force`) but at
the stable `(session_uid, set_index)` pair, so they **survive re-ingestion**.
They are append-only, the latest one wins, and the history is kept.

### Idempotence at three levels

1. files already read (same path, same sha256) are skipped without even
   reopening them, unless `--force`;
2. `sessions.session_uid` is `UNIQUE`: the same workout never lands twice, even
   if you rename or move the file;
3. rewriting a session deletes and re-inserts its sets and HR samples — no
   duplicates, and corrections stay put.

### Why heart rate is kept sample-by-sample

1 Hz means ~4,300 rows per session, i.e. ~700k rows a year at three sessions a
week: SQLite doesn't notice. Keeping the raw samples means per-set HR can be
recomputed even if set boundaries change later (or a correction moves one).
Aggregating up front would save space that isn't a problem, at the cost of data
you can't get back.

### Room for running load

`sessions` is already the generic activity table: it has `activity_type`,
duration, HR, calories and a precomputed ISO week. Adding running sessions
means inserting them here with `activity_type='run'` plus a detail table
(distance, pace, elevation) with an FK on `sessions(id)`. No destructive
migration, and cross-referencing gym against running by week becomes a join on
`(iso_year, iso_week)`.

### Reps and loads: when they're there and when they're not

Garmin Connect only exports `.fit` as **"Export Original"**: the original file
uploaded by the watch. Edits made afterwards in the app (reps, loads,
corrections) stay in Connect's database and **never make it into the exported
FIT**.

Confirmed on two real sessions:

| Session | `repetitions` | `weight` |
| --- | --- | --- |
| 01/09 "Day 1 Upper Body", values entered later on the phone | absent across 35 sets | absent |
| 01/09 "Day 2 Legs", values confirmed on the watch | present on 14 of 15 sets | present wherever there was a load |

So the data does come through, but **only if confirmed on the watch during the
session**. Where it's missing, `volume_kg` stays `NULL`; you can fill it in
afterwards with `strength-tracker correct <set_id> --reps N --weight K`, which
writes to `corrections` without touching the raw data.

The metrics that don't depend on load — sets per muscle group, time under
tension, session density, HR drift — work regardless.

### When the same raw key means two different exercises

A Copenhagen plank arrives as `plank/side_plank`, exactly like a real side
plank. What separates them is the workout step note. So a mapping entry can
qualify a key:

```yaml
  - name: Copenhagen plank
    primary: adduttori
    match:
      - key: plank/side_plank
        note: Copenhagen plank      # beats the generic entry

  - name: Side plank
    primary: core
    match: [plank/side_plank]       # no note: an actual side plank
```

`v_sets` joins the mapping twice and the note-qualified entry wins. The
comparison is case-insensitive and ignores surrounding whitespace.

## The metrics

Everything starts from `v_sets`, so corrections and mapping are already
applied. Every assumption is written down here rather than buried in the code.

| Metric | Formula | Assumptions and limits |
| --- | --- | --- |
| **Tonnage** | Σ (weight × reps) | Only sets with `weight_mode = carico`. If reps or weight is missing the set doesn't contribute and is counted separately: `NULL`, never zero |
| **e1RM** | Epley: weight × (1 + reps / 30) | A linear estimate calibrated on short sets. Above **12 reps** it is flagged unreliable. At 1 rep the formula would give 1.033× the weight, so that case returns the weight itself |
| **Density** | tonnage / active time | Active time = `session.total_timer_time`, the only one the watch provides |
| **Work/rest** | Σ active set duration / Σ rest duration | From the `set` messages. Unrecorded rests are not estimated |
| **HR drift** | avg HR of the last third of sets − the first third | A crude fatigue proxy: it also rises simply because the session warms you up. Needs ≥3 sets with HR, otherwise `NULL` |
| **Sets per group** | count of active sets per ISO week | More robust than tonnage when exercises change or load isn't measurable |
| **Moving average** | 4-week mean of weekly volume | Computed **only over weeks that have data**: a week without training isn't worth zero, or the average would collapse for no reason |

Per-set HR comes from intersecting the 1 Hz samples with each set's time
window. The comparison runs on epoch numbers rather than ISO strings: with
timezones, textual comparison is unreliable.

### Not every weight is a load

`weight_mode` in the mapping says how to read the recorded weight:

| Mode | Meaning | Tonnage | Effective load |
| --- | --- | --- | --- |
| `carico` (default) | the weight is external load | weight × reps | the weight |
| `assistenza` | the weight is the **assistance** received | `NULL` | bodyweight − assistance |
| `corpo_libero` | no external load | `NULL` | bodyweight |

On an assisted pull-up machine, 40 kg is how much the machine *helps* you.
Multiplying it by reps would produce a tonnage that is not merely wrong but
sign-inverted, since more assistance means an easier set. Here progress is
assistance going **down**, and that is what the progression chart shows
(`assistenza_minima_kg`, with e1RM computed on the effective load).

Bodyweight is read from the file's own `user_profile` message. Where it feeds
an estimate the row carries `carico_stimato = 1` and the tonnage goes to
`volume_stimato_kg`, kept **out** of the headline total: the top-line number
stays real external load.

### Filling in missing loads by hand

When values were entered in Garmin Connect after the session, they are not in
the `.fit`. Transcribe them into a CSV, numbering sets **the way Connect shows
them** (active sets only, from 1):

```csv
data,seduta,serie,reps,peso_kg,nota
01/09/2026,Day 1,7,10,24,Pressa su panca con manubrio
```

```bash
strength-tracker correct --from-csv examples/correzioni_day1_20260901.csv
```

The `seduta` column is only needed when there is more than one workout that
day. `examples/` contains the "Day 1 Upper Body" session of 01/09/2026 already
transcribed. They all land in `corrections`: raw data stays intact and
`data_source` says where each value came from.

## The dashboard

```bash
strength-tracker report                  # writes output/dashboard.html
make session FIT=~/Downloads/palestra    # ingest + report in one go
```

A **single HTML file** at `output/dashboard.html`: Chart.js is inlined from the
repo and the data is a JSON block inside the page. Zero network requests, zero
server — double-click it, works on a plane. No `src` or `href` tag points
outward, and a test enforces that.

What's in it:

- **Summary**: sessions, period covered, total volume (and how many sets back
  it up), time under tension, sessions in the last 4 weeks.
- **Weekly volume** with a 4-week moving average.
- **Load per muscle group** by week, stacked bars, with a sets / tonnage
  toggle: sets stay readable even where load isn't measurable.
- **Groups under watch** (hamstrings, adductors, glutes): all three are always
  present, and if a group wasn't trained the page **says so** instead of
  showing an empty chart.
- **Per-exercise progression** with a selector: best weight and e1RM on the
  same axis (both are kg), total reps in a separate chart — never two scales on
  one plot. For assisted pull-ups it shows assistance instead of weight, and
  says why.
- **Recent sessions**, expandable set by set: numbering identical to Garmin
  Connect's, per-set average HR, and a tag saying whether each value came from
  the file or from a correction.
- **Anomalies**: unmapped exercises, zero-weight sets, suspicious rep counts,
  abnormally long sets, sessions with no reps at all. Each with a line
  explaining what to do about it.
- **Method notes** at the bottom: every formula's assumptions, on the page
  itself rather than only here.

Rendering details: light and dark themes (following the system, with a toggle
top right); an eight-slot categorical palette in fixed order — the order is
what keeps it readable under colour blindness, not an aesthetic choice — and
past the eighth group the smallest ones fold into "altri", which declares what
it contains. Every chart has a table view, and unreliable estimates (e1RM above
12 reps) change the point's **shape**, not just its colour.

## Installing without uv

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt -e .
strength-tracker --help
```

Going this route, commands are run as `strength-tracker ...` instead of
`uv run strength-tracker ...` (and the `Makefile` needs adjusting to match).

## All commands

```bash
strength-tracker ingest <path>          # single file or directory, recursive
strength-tracker ingest <path> --force  # re-read files already processed
strength-tracker inspect <file.fit>     # JSON dump of raw messages (--raw for unknown_*)
strength-tracker unmapped               # exercises not yet mapped
strength-tracker unmapped --yaml        # entries ready to paste into the YAML
strength-tracker correct <set_id> --reps N --weight K [--exercise RAW_KEY] [--note "..."]
strength-tracker correct --from-csv <file.csv>   # bulk corrections
strength-tracker report                 # writes output/dashboard.html
strength-tracker stats                  # quick textual summary in the terminal
```

The normal post-workout flow is `ingest` then `report`, chained by:

```bash
make session FIT=~/Downloads/palestra
```

### Default paths

Relative to the directory you run the command from (the project root), and
overridable:

| What | Default | Environment variable | Flag |
| --- | --- | --- | --- |
| Database | `data/strength.db` | `STRENGTH_TRACKER_DB` | `--db` |
| Mapping | `config/exercise_mapping.yaml` | `STRENGTH_TRACKER_MAPPING` | `--mapping` |
| Dashboard | `output/dashboard.html` | `STRENGTH_TRACKER_OUTPUT` | `--output` |

## Layout

```
.
├── config/exercise_mapping.yaml   # versioned mapping, hand-editable
├── src/strength_tracker/          # fit_parser, db, ingest, mapping, metrics, dashboard, cli
├── templates/dashboard.html.j2
├── vendor/chart.min.js            # Chart.js 4.4.4 UMD, MIT — inlined into the dashboard
└── tests/
    ├── fitgen.py                  # minimal FIT encoder: generates the binary fixtures
    └── fixtures/*.fit             # versioned synthetic FIT files (no personal data)
```

Tests run against real binary `.fit` files produced by `tests/fitgen.py` and
read by the same `fitdecode` used in production: no mocks. To regenerate them:

```bash
uv run python tests/fitgen.py
```

## Technical choices

- **Minimal dependencies**: `fitdecode` (FIT parsing), `PyYAML` (mapping),
  `Jinja2` (dashboard template). No pandas: the data volumes are those of two
  or three sessions a week, and SQL plus the stdlib are more than enough.
- **SQLite via `sqlite3`**, no ORM. Metrics live in SQL views where possible,
  so they're inspectable from any client.
- **Chart.js vendored** into `vendor/` and inlined into the HTML: the dashboard
  is a single file you can double-click, offline included.
- **Non-destructive corrections**: manual overrides live in a `corrections`
  table and are applied over the raw data at read time, never overwriting it.
- **No invented data**: a missing field stays `NULL` and the dashboard declares
  it, rather than showing a zero.
- **Extensible to running**: the schema keeps strength sessions in a generic
  activity table with ISO-week aggregation keys, so adding running activities
  later won't require rebuilding the database.

Every formula's assumptions (Epley, density, HR drift) are documented under
[The metrics](#the-metrics).

## Privacy

`.gitignore` excludes `data/`, `output/`, `*.fit` and `*.db`. The exception is
the test fixtures, which are synthetic files carrying no personal data.
