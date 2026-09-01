# Flusso normale del dopo-allenamento:  make session FIT=~/Downloads/palestra
FIT ?= data/fit

.PHONY: install session ingest report stats unmapped test clean

install:
	uv sync

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
