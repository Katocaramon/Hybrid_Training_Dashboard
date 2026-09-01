"""FIT -> dataclass.

Fase 2. Legge i messaggi `set`, `session` e `record` da un file .fit di
Strength Training e li restituisce come dataclass, senza toccare il file
sorgente. File corrotti o attivita' non-strength vengono saltati con warning.
"""

from __future__ import annotations
