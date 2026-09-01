"""Orchestrazione dell'ingestione (file singolo o cartella ricorsiva).

Fase 3. Idempotente: rilanciare l'ingestione sugli stessi file non duplica
nulla e salta i file gia' processati, salvo `--force`.
"""

from __future__ import annotations
