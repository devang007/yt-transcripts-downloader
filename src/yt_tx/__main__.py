"""``python -m yt_tx`` entry point.

The web API spawns workers as ``[sys.executable, "-m", "yt_tx", ...]`` rather
than by name, so a run started from the UI uses the same interpreter and
virtualenv as the API itself instead of whatever happens to be first on PATH.
"""

from __future__ import annotations

from .cli import app

if __name__ == "__main__":
    app()
