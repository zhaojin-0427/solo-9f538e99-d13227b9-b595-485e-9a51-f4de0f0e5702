"""Runtime configuration.

The SQLite database path is taken from the ``WAVEFORM_DB_PATH`` environment
variable so tests can point the whole application at an isolated file.
"""
from __future__ import annotations

import os
from pathlib import Path

DB_PATH = Path(os.environ.get("WAVEFORM_DB_PATH", "/data/waveform.db"))

SAMPLES_PER_MINUTE = 60
