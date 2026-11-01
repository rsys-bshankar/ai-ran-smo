"""Pytest configuration of the mlmr unit tests: switches on the in-memory SQLite fallback of `smo_shared.db` before the application is imported.

Without a database URL `smo_shared.db` refuses to start unless `SMO_ALLOW_SQLITE_FALLBACK` is set; this file sets it for the whole suite. It defines no fixtures.
Run the suite from `smo/mlmr` with `PYTHONPATH=.:../shared python -m pytest tests -q`.
"""

import os

# The in-memory SQLite fallback of smo_shared.db is an explicit opt-in (SMO_ALLOW_SQLITE_FALLBACK), never inferred from pytest being loaded; set here,
# before any application module is imported, because smo_shared.db builds its engine at import.
os.environ.setdefault("SMO_ALLOW_SQLITE_FALLBACK", "1")
