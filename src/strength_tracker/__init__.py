"""Strength Tracker — analisi locale delle sedute di forza da file FIT Garmin.

Tutti i percorsi di default sono relativi alla directory di lavoro corrente
(cioe' la root del progetto), sovrascrivibili da variabili d'ambiente o da CLI.
"""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.1.0"

# Root del pacchetto installato (serve per trovare templates/ e vendor/ anche
# quando il pacchetto e' installato in un venv fuori dal repo).
PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent.parent


def _env_path(var: str, default: str) -> Path:
    return Path(os.environ.get(var, default)).expanduser()


def default_db_path() -> Path:
    """Database SQLite locale (ignorato da git)."""
    return _env_path("STRENGTH_TRACKER_DB", "data/strength.db")


def default_mapping_path() -> Path:
    """Mappatura esercizi versionata nel repo, editabile a mano."""
    return _env_path("STRENGTH_TRACKER_MAPPING", "config/exercise_mapping.yaml")


def default_output_path() -> Path:
    """Dashboard HTML generata."""
    return _env_path("STRENGTH_TRACKER_OUTPUT", "output/dashboard.html")


__all__ = [
    "__version__",
    "PACKAGE_DIR",
    "REPO_ROOT",
    "default_db_path",
    "default_mapping_path",
    "default_output_path",
]
