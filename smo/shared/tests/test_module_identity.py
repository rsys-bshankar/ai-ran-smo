"""One SME invoker per module, shared by its replicas (PR-ST-4: smo_shared/module_identity.py and
`R1Client`'s onboarding). Store tests run on file SQLite and, with `SMO_TEST_POSTGRES_URL`, on real Postgres.

Run with: cd smo/shared && PYTHONPATH=. python -m pytest tests/test_module_identity.py -q
"""

import os
import threading

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from smo_shared import r1_client
from smo_shared.db import Base
from smo_shared.module_identity import DbIdentityStore, ModuleIdentityRow
from smo_shared.r1_client import R1Client, _ModuleIdentity

R1 = "http://r1-termination:8000"
SME = "http://sme:8000"


@pytest.fixture(params=["sqlite", "postgres"])
def engine(request, tmp_path):
    """A database with only the module_identity table, on file SQLite and, when SMO_TEST_POSTGRES_URL is set, on Postgres too (skipped otherwise);
    dropped afterwards.
    """
    if request.param == "postgres":
        if not os.environ.get("SMO_TEST_POSTGRES_URL"):
            pytest.skip("SMO_TEST_POSTGRES_URL not set")
        engine = create_engine(os.environ["SMO_TEST_POSTGRES_URL"], future=True)
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'identity.db'}", future=True)
    Base.metadata.drop_all(engine, tables=[ModuleIdentityRow.__table__])
    Base.metadata.create_all(engine, tables=[ModuleIdentityRow.__table__])
    yield engine
    Base.metadata.drop_all(engine, tables=[ModuleIdentityRow.__table__])
    engine.dispose()


@pytest.fixture
def store(engine):
    return DbIdentityStore(sessionmaker(bind=engine))


# ---------------------------------------------------------------- the store

def test_the_first_insert_wins_and_a_second_is_refused(store):
    """insert stores the identity only for the first caller of a module; the second gets False and the first identity stays."""
    assert store.load("aimgf") is None
    assert store.insert("aimgf", "inv-1", "s-1") is True
    assert store.insert("aimgf", "inv-2", "s-2") is False
    assert store.load("aimgf") == ("inv-1", "s-1")
    assert store.insert("nfo", "inv-3", "s-3") is True and store.load("nfo") == ("inv-3", "s-3")


def test_replace_is_a_compare_and_swap_on_the_stale_invoker(store):
    """replace succeeds only while the stored invoker is still the stale one the caller saw, and does nothing for an unknown module."""
    store.insert("aimgf", "inv-1", "s-1")
    assert store.replace("aimgf", "inv-1", "inv-2", "s-2") is True
    assert store.replace("aimgf", "inv-1", "inv-3", "s-3") is False        # inv-1 is no longer what is stored
    assert store.load("aimgf") == ("inv-2", "s-2")
    assert store.replace("unknown-module", "inv-1", "inv-9", "s-9") is False


def test_racing_inserts_and_racing_replaces_have_one_winner_each(store):
    """Eight threads racing to insert, and then to replace, produce exactly one winner each, which is what stops replicas registering duplicates."""
    workers = 8
    barrier = threading.Barrier(workers)
    inserts, replaces, lock = [], [], threading.Lock()

    def race_insert(n):
        barrier.wait()
        won = store.insert("aimgf", f"inv-{n}", f"s-{n}")
        with lock:
            inserts.append(won)

    threads = [threading.Thread(target=race_insert, args=(n,)) for n in range(workers)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert sorted(inserts) == [False] * (workers - 1) + [True]

    stale = store.load("aimgf")[0]
    barrier2 = threading.Barrier(workers)

    def race_replace(n):
        barrier2.wait()
        won = store.replace("aimgf", stale, f"new-{n}", f"ns-{n}")
        with lock:
            replaces.append(won)

    threads = [threading.Thread(target=race_replace, args=(n,)) for n in range(workers)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert sorted(replaces) == [False] * (workers - 1) + [True]


# ---------------------------------------------------------------- R1Client onboarding with the store

class FakeSme:
    """R1 /bootstrap plus SME's invoker registry and token endpoint (an unknown invoker or wrong secret is a 400)."""

    def __init__(self):
        self.invokers: dict[str, str] = {}
        self.registrations = 0
        self.deleted: list[str] = []
        self.tokens = 0

    def _resp(self, status, payload, method, url):
        return httpx.Response(status, json=payload, request=httpx.Request(method, url))

    def get(self, url, **kw):
        assert url == f"{R1}/bootstrap"
        return self._resp(200, {"apiEndpoints": [{"tokenEndPoint": {"uri": f"{SME}/oauth2/token"}}]}, "GET", url)

    def post(self, url, json=None, **kw):
        if url == f"{SME}/invoker-registrations":
            self.registrations += 1
            invoker_id, secret = f"api-invoker-{self.registrations}", f"secret-{self.registrations}"
            self.invokers[invoker_id] = secret
            return self._resp(201, {"apiInvokerId": invoker_id, "onboardingSecret": secret}, "POST", url)
        assert url == f"{SME}/oauth2/token"
        if self.invokers.get(json["client_id"]) != json["client_secret"]:
            return self._resp(400, {"error": "invalid_client"}, "POST", url)
        self.tokens += 1
        return self._resp(200, {"access_token": f"tok-{self.tokens}", "expires_in": 3600}, "POST", url)

    def delete(self, url, **kw):
        invoker_id = url.rsplit("/", 1)[1]
        self.deleted.append(invoker_id)
        self.invokers.pop(invoker_id, None)
        return self._resp(204, None, "DELETE", url)


@pytest.fixture
def sme(monkeypatch):
    """A stand-in for R1's /bootstrap and SME's registry and token endpoint, patched into the R1 client, with MODULE=aimgf and no pinned invoker."""
    fake = FakeSme()
    monkeypatch.setattr(r1_client.httpx, "get", fake.get)
    monkeypatch.setattr(r1_client.httpx, "post", fake.post)
    monkeypatch.setattr(r1_client.httpx, "delete", fake.delete)
    monkeypatch.setenv("MODULE", "aimgf")
    monkeypatch.delenv("SMO_INVOKER_ID", raising=False)
    monkeypatch.delenv("SMO_INVOKER_SECRET", raising=False)
    return fake


def replica(store):
    """A new process of the module: its own token cache, the shared identity store."""
    return _ModuleIdentity(store=store)


def test_replicas_and_restarts_of_one_module_share_a_single_invoker(sme, store):
    """Several replicas and a restart of one module register a single invoker at SME and all use it."""
    first, second = replica(store), replica(store)
    assert first.token_for(R1) and second.token_for(R1)
    restarted = replica(store)
    assert restarted.token_for(R1)
    assert sme.registrations == 1
    assert first.invoker_id == second.invoker_id == restarted.invoker_id == "api-invoker-1"


def test_each_module_has_its_own_invoker(sme, store, monkeypatch):
    """Different modules do not share an identity: each registers its own."""
    assert replica(store).token_for(R1)
    monkeypatch.setenv("MODULE", "nfo")
    other = replica(store)
    assert other.token_for(R1)
    assert sme.registrations == 2 and other.invoker_id == "api-invoker-2"


def test_losing_the_race_to_store_discards_our_registration_and_adopts_the_winners(sme, store):
    """A replica that loses the race to store its identity deletes its own registration at SME and uses the winner's."""
    class RacedStore(DbIdentityStore):
        def insert(self, module, invoker_id, secret):
            super().insert(module, "api-invoker-WINNER", "secret-WINNER")  # a replica stores its identity first
            sme.invokers["api-invoker-WINNER"] = "secret-WINNER"
            return super().insert(module, invoker_id, secret)

    loser = _ModuleIdentity(store=RacedStore(store._factory))
    assert loser.token_for(R1)
    assert loser.invoker_id == "api-invoker-WINNER"
    assert sme.deleted == ["api-invoker-1"]                 # its own, now redundant, registration is removed
    assert list(sme.invokers) == ["api-invoker-WINNER"]


def test_an_invoker_sme_forgot_is_replaced_once_and_the_other_replicas_adopt_it(sme, store):
    """When SME forgets the invoker, one replica registers a replacement and the others adopt it instead of registering again."""
    first, second = replica(store), replica(store)
    assert first.token_for(R1) and second.token_for(R1)
    sme.invokers.clear()                                     # SME lost its registry (or purged the invoker)

    assert first.token_for(R1, refresh=True)                # grant is refused -> one fresh registration, stored
    assert second.token_for(R1, refresh=True)               # refused with the old id -> adopts the replacement
    assert sme.registrations == 2
    assert first.invoker_id == second.invoker_id == "api-invoker-2"
    assert store.load("aimgf")[0] == "api-invoker-2"


def test_two_replicas_that_both_see_the_stale_invoker_register_one_replacement(sme, store):
    """A replica holding the old id sees the replacement already stored and adopts it without registering another."""
    first, second = replica(store), replica(store)
    assert first.token_for(R1) and second.token_for(R1)
    sme.invokers.clear()

    # the first replica replaces the stale identity; the second still holds the old id when it is refused
    first_won = first._identity_store().replace("aimgf", "api-invoker-1", "api-invoker-X", "secret-X")
    sme.invokers["api-invoker-X"] = "secret-X"
    assert first_won is True
    assert second.token_for(R1, refresh=True)               # second still holds api-invoker-1, sees X stored, adopts it
    assert second.invoker_id == "api-invoker-X" and sme.registrations == 1


def test_a_broken_store_falls_back_to_a_per_process_identity(sme):
    """If the shared store fails, each process registers its own invoker (the pre-sharing behaviour) rather than failing."""
    class Broken:
        def load(self, module):
            raise RuntimeError("database down")

        insert = replace = load

    one, two = _ModuleIdentity(store=Broken()), _ModuleIdentity(store=Broken())
    assert one.token_for(R1) and two.token_for(R1)
    assert sme.registrations == 2                            # as before PR-ST-4: never an error, just no sharing


def test_without_a_module_name_or_with_the_store_off_each_process_registers_its_own(sme, monkeypatch):
    """No MODULE, or SMO_MODULE_IDENTITY_STORE=off, means no shared store: each process registers its own invoker."""
    monkeypatch.delenv("MODULE")
    assert _ModuleIdentity().token_for(R1) and _ModuleIdentity().token_for(R1)
    assert sme.registrations == 2

    monkeypatch.setenv("MODULE", "aimgf")
    monkeypatch.setenv("SMO_MODULE_IDENTITY_STORE", "off")
    assert _ModuleIdentity()._identity_store() is None


def test_a_module_with_no_database_registers_its_own_instead_of_failing(sme, monkeypatch):
    """DOC-CF-2.4: a module that has no database (MODULE set, no SMO_DATABASE_URL, as the MLLF service was) cannot open the store; it gets a token by registering on its own,
    as with the store off, where the first call used to raise (a 503 on /ready, a 500 on the first request)."""
    import smo_shared.module_identity as module_identity

    def no_database(*a, **kw):
        raise RuntimeError("SMO_DATABASE_URL is not set")
    monkeypatch.setenv("MODULE", "no-database-module")
    monkeypatch.delenv("SMO_MODULE_IDENTITY_STORE", raising=False)
    monkeypatch.setattr(module_identity, "DbIdentityStore", no_database)
    client = _ModuleIdentity()
    assert client.token_for(R1) and client._identity_store() is None and sme.registrations == 1


def test_an_identity_from_the_environment_needs_no_store_and_no_registration(sme, store, monkeypatch):
    """An invoker pinned by SMO_INVOKER_ID / SMO_INVOKER_SECRET is used as is: nothing is registered or stored."""
    sme.invokers["provisioned"] = "from-a-secret-store"
    monkeypatch.setenv("SMO_INVOKER_ID", "provisioned")
    monkeypatch.setenv("SMO_INVOKER_SECRET", "from-a-secret-store")
    assert replica(store).token_for(R1)
    assert sme.registrations == 0 and store.load("aimgf") is None


def test_deleting_the_stored_invoker_makes_the_next_start_register_a_new_one(sme, store, engine):
    """PR-SEC-4.8, the rotation runbook for a module's SME invoker secret: delete its row, restart, and the module registers a fresh invoker that
    every replica then shares."""
    first = replica(store)
    assert first.token_for(R1) and first.invoker_id == "api-invoker-1"
    with sessionmaker(bind=engine)() as db:
        db.query(ModuleIdentityRow).delete()
        db.commit()
    restarted = replica(store)
    assert restarted.token_for(R1) and restarted.invoker_id == "api-invoker-2"
    other = replica(store)
    assert other.token_for(R1) and other.invoker_id == "api-invoker-2"
    assert sme.registrations == 2


def test_put_stores_or_replaces_whatever_is_there(store):
    """`put` (an rApp instance's pushed credentials, call flow 01c) writes a new row, then replaces it with the next push."""
    store.put("rapp-instance:i-1", "inv-1", "s1")
    assert store.load("rapp-instance:i-1") == ("inv-1", "s1")
    store.put("rapp-instance:i-1", "inv-1", "s2")
    assert store.load("rapp-instance:i-1") == ("inv-1", "s2")
