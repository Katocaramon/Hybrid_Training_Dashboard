# Dopo ogni seduta, con la chiave API di Hevy:   make sync
# Palestra + corsa e recupero da Garmin:         make sync-all
# Partendo da file (.fit, CSV o JSON):           make session FIT=~/Downloads/palestra
FIT ?= data/fit

.PHONY: install serve sync sync-all sync-hevy sync-garmin session ingest report \
        stats unmapped test clean

install:
	uv sync

# Dashboard su localhost, col pulsante che sincronizza davvero. Ctrl+C per uscire.
serve:
	uv run strength-tracker serve

# Scarica da Hevy e rigenera la dashboard: nessun export da fare a mano.
sync: sync-hevy report

sync-hevy:
	uv run strength-tracker sync-hevy

# Tutto: palestra da Hevy, corsa e recupero da Garmin, poi la dashboard.
sync-all: sync-hevy sync-garmin report

# Corse, bici, HRV, sonno, FC a riposo e peso. Serve GARMIN_PERSONAL_API_KEY.
sync-garmin:
	uv run strength-tracker sync-garmin

session: ingest report

ingest:
	uv run strength-tracker ingest $(FIT)

report:
	uv run strength-tracker report

stats:
	uv run strength-tracker stats

unmapped:
	uv run strength-tracker unmapped

test:
	uv run pytest

clean:
	rm -rf output/ .pytest_cache/ **/__pycache__/
