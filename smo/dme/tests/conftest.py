"""Pytest configuration of the dme unit tests: switches on the in-memory SQLite fallback of `smo_shared.db` before the application is imported.

Without a database URL `smo_shared.db` refuses to start unless `SMO_ALLOW_SQLITE_FALLBACK` is set; this file sets it for the whole suite. It defines no fixtures.
Run the suite from `smo/dme` with `PYTHONPATH=.:../shared python -m pytest tests -q`.
"""

import os

# The in-memory SQLite fallback of smo_shared.db is an explicit opt-in (SMO_ALLOW_SQLITE_FALLBACK), never inferred from pytest being loaded; set here,
# before any application module is imported, because smo_shared.db builds its engine at import.
os.environ.setdefault("SMO_ALLOW_SQLITE_FALLBACK", "1")
# Call flow 11c: the producer health cache is off for the suite, so a test that changes a producer's health sees it at once; test_producer_lifecycle.py
# switches it on where it is the subject.
os.environ.setdefault("DME_PRODUCER_HEALTH_CACHE_SECONDS", "0")
