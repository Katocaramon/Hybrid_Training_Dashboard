"""Rende importabile `fitgen` dai test senza installarlo come pacchetto."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
