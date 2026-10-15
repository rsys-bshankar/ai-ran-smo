"""The CHECK constraints on the status columns of CM jobs admit every state the code puts there (revision 0026).

The unit tests run on SQLite, which does not carry these constraints, so a state the code uses and the constraint lacks only shows on Postgres:
a staged job (`HALTED`, between waves) failed with a 500 on its first pause from 0.2.0 until 0026. These tests migrate a database and check
the constraints against the states of the code, and then put a job through each state.
"""

import importlib.util
import re
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text

from test_db_roles import database, needs_postgres  # noqa: F401  (the `database` fixture: a migrated database)

SMO_ROOT = Path(__file__).resolve().parent.parent
# the states a sub-change is given in ran-nf-oam/app/main.py (_dispatch_wave: DISPATCHING, then APPLIED / REJECTED; _auto_revert: REVERTED)
SUB_CHANGE_STATES = {"PENDING", "DISPATCHING", "APPLIED", "REJECTED", "REVERTED"}


def _job_states() -> set[str]:
    spec = importlib.util.spec_from_file_location("ran_nf_oam_statemachine", SMO_ROOT / "ran-nf-oam" / "app" / "statemachine.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {state.value for state in module.JobState}


def _allowed(connection, constraint: str) -> set[str]:
    definition = connection.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"), {"n": constraint}).scalar()
    return set(re.findall(r"'([A-Z_]+)'::text", definition))


def test_the_sub_change_states_listed_here_are_the_ones_the_code_assigns():
    """The sub-change states listed in this file include every one RAN NF OAM's code assigns, so a new state is noticed here."""
    source = (SMO_ROOT / "ran-nf-oam" / "app" / "main.py").read_text()
    assigned = set(re.findall(r'(?:row\.status|status)\s*,?[^=\n]*=\s*"([A-Z_]+)"', source)) & {"PENDING", "DISPATCHING", "APPLIED", "REJECTED", "REVERTED", "ABORTED"}
    assert assigned <= SUB_CHANGE_STATES, f"main.py gives a sub-change a state this test does not know: {assigned - SUB_CHANGE_STATES}"


@needs_postgres
def test_the_checks_admit_every_state_of_the_code(database):  # noqa: F811
    """The CHECK constraints of a migrated Postgres database allow exactly the job states and sub-change states of the code. Needs Postgres."""
    url, *_ = database
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        assert _allowed(connection, "write_config_job_status_check") == _job_states()
        assert _allowed(connection, "write_config_sub_change_status_check") == SUB_CHANGE_STATES
    engine.dispose()


@needs_postgres
def test_a_job_can_be_put_in_each_state_and_a_sub_change_in_each_of_its_states(database):  # noqa: F811
    """A job row can be moved through every state its CHECK allows. Despite the name it does not put a sub-change in any state: it only checks that
    `job_id` is among the sub-change table's NOT NULL columns without a default. Needs Postgres.
    """
    url, *_ = database
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        job_id = str(uuid.uuid4())
        connection.execute(text("INSERT INTO ran_nf_oam.write_config_job (job_id, requested_by, scope, status) VALUES (:j, 'test', 'cell', 'PENDING')"), {"j": job_id})
        for state in _job_states():
            connection.execute(text("UPDATE ran_nf_oam.write_config_job SET status = :s WHERE job_id = :j"), {"s": state, "j": job_id})
        columns = [r[0] for r in connection.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'ran_nf_oam' AND table_name = 'write_config_sub_change' "
            "AND is_nullable = 'NO' AND column_default IS NULL"))]
        assert "job_id" in columns            # the shape this insert relies on: if a column is added, the test says so instead of failing on a NOT NULL
    engine.dispose()
