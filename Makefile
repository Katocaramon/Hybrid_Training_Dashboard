# Dopo ogni seduta, con la chiave API di Hevy:   make sync
# Partendo da file (.fit, CSV o JSON):           make session FIT=~/Downloads/palestra
FIT ?= data/fit

.PHONY: install sync sync-hevy session ingest report stats unmapped test clean

install:
	uv sync

# Scarica da Hevy e rigenera la dashboard: nessun export da fare a mano.
sync: sync-hevy report

sync-hevy:
	uv run strength-tracker sync-hevy

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
