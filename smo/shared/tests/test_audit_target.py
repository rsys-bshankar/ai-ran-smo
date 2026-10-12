"""smo_shared.audit.clean_target (SEC-15.12): a row's target is stored without a query string or a fragment, and rows written before this still verify.

The cases are exact on purpose (they are held by the mutation gate, `scripts/mutation_pilot.sh`): each input has a known output, including inputs with two of each separator, so a
change of which separator is cut at, or of which part is kept, shows. Run with: cd smo/shared && PYTHONPATH=. python -m pytest tests/test_audit_target.py -q
"""

import datetime

import pytest
from sqlalchemy.orm import Session

from smo_shared import audit
from smo_shared.db import Base
from smo_shared.testing import make_test_engine

T0 = datetime.datetime(2026, 10, 10, 12, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture
def db():
    """A session on an in-memory SQLite engine with every table created."""
    engine = make_test_engine()
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


# Table: (target as given, target stored). A plain path is kept; a query, a fragment, both in either order, a decoded `%3F` and an empty query are cut; the first `?` or `#` wins.
CASES = [
    ("/dme/data-jobs", "/dme/data-jobs"),
    ("/dme/data-jobs?token=SECRET", "/dme/data-jobs"),
    ("/dme/data-jobs?a=1?b=2", "/dme/data-jobs"),
    ("/dme/data-jobs#frag", "/dme/data-jobs"),
    ("/dme/data-jobs#a#b", "/dme/data-jobs"),
    ("/dme/data-jobs?a=1#frag", "/dme/data-jobs"),
    ("/dme/data-jobs#frag?a=1", "/dme/data-jobs"),
    ("/dme/data-jobs?", "/dme/data-jobs"),
    ("/dme/data-jobs?a=1?b=2#c#d", "/dme/data-jobs"),
    ("/ran-nf-oam/x/y/z", "/ran-nf-oam/x/y/z"),
    ("?only=query", ""),
    ("#only", ""),
    ("", ""),
]


@pytest.mark.parametrize("given, stored", CASES)
def test_clean_target_cuts_at_the_first_query_or_fragment_mark(given, stored):
    """`clean_target` returns the part before the first `?` or `#`, and a target with neither unchanged."""
    assert audit.clean_target(given) == stored


@pytest.mark.parametrize("given, stored", CASES)
def test_a_recorded_row_holds_the_cleaned_target_and_its_hash_covers_it(db, given, stored):
    """`record` stores the cleaned target, the hash is computed over what is stored, and the chain verifies."""
    entry = audit.record(db, actor="inv-1", role="rapp", action="post", target=given, result="201", now=T0)
    db.commit()
    assert entry.target == stored
    assert entry.hash == audit.compute_hash(entry)
    assert audit.verify(db) is None


def test_the_secret_in_a_query_is_in_no_field_of_the_row(db):
    """A token in a query string is nowhere in the stored row or its export line, only its path is."""
    entry = audit.record(db, actor="inv-1", role="rapp", action="POST", target="/dme/data-jobs?token=SECRET-VALUE&x=1", result="201", now=T0)
    db.commit()
    assert "SECRET-VALUE" not in repr(audit.as_dict(entry))


def test_a_row_written_before_the_change_still_verifies(db):
    """A row that was stored with a query in its target (as the old code wrote it) is not rewritten, and the chain of old and new rows verifies: the hash reads what is stored."""
    old = audit.record(db, actor="inv-1", role="rapp", action="POST", target="/dme/old", result="201", now=T0)
    old.target = "/dme/old?token=legacy"                                  # what the old code would have stored, set before the hash is made
    old.hash = audit.compute_hash(old)
    head = db.get(audit.AuditHead, 1)
    head.last_hash = old.hash
    db.commit()
    audit.record(db, actor="inv-1", role="rapp", action="POST", target="/dme/new?token=x", result="201", now=T0 + datetime.timedelta(seconds=1))
    db.commit()
    assert db.get(audit.AuditEntry, 1).target == "/dme/old?token=legacy"
    assert db.get(audit.AuditEntry, 2).target == "/dme/new"
    assert audit.verify(db) is None
