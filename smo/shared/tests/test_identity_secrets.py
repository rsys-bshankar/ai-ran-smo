"""SEC-15.18: the secrets of the shared `module_identity` table are encrypted at rest with a key from the environment.

Covers the envelope (round trip, nonce, the row bound to its module), what an upgrade must keep working (plaintext rows with and without a key), the
refusals (ciphertext without a key, a wrong or unknown key, a changed value), rotation with a previous key, the `encrypt-existing` command, the
start-up check of the key variables, that nothing prints a key or a secret, that `R1Client` works on top of it, and that no other code reads the
column. Run with: cd smo/shared && PYTHONPATH=. python -m pytest tests/test_identity_secrets.py -q
"""

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from prometheus_client import REGISTRY
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from smo_shared import module_identity, r1_client, worker
from smo_shared.db import Base
from smo_shared.health import install_health
from smo_shared.identity_secrets import (PREFIX, IdentityKeyError, check_configuration, is_sealed, key_of, load_keys, seal, unseal)
from smo_shared.module_identity import DbIdentityStore, ModuleIdentityRow
from smo_shared.r1_client import _ModuleIdentity

K1 = "first-identity-key-0123456789abcdefghij"
K2 = "second-identity-key-0123456789abcdefghi"
SECRET = "onboarding-secret-Zq81xT"
R1 = "http://r1-termination:8000"
SME = "http://sme:8000"


def keys(current=None, previous=None):
    """The keys of an environment with these values."""
    env = {}
    if current:
        env["SMO_IDENTITY_KEY"] = current
    if previous:
        env["SMO_IDENTITY_PREVIOUS_KEY"] = previous
    return load_keys(env)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """No key variable of the host reaches a test."""
    for name in ("SMO_IDENTITY_KEY", "SMO_IDENTITY_KEY_FILE", "SMO_IDENTITY_PREVIOUS_KEY", "SMO_IDENTITY_PREVIOUS_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def engine(tmp_path):
    """A SQLite database holding only the module_identity table."""
    engine = create_engine(f"sqlite:///{tmp_path / 'identity.db'}", future=True)
    Base.metadata.create_all(engine, tables=[ModuleIdentityRow.__table__])
    yield engine
    engine.dispose()


@pytest.fixture
def factory(engine):
    return sessionmaker(bind=engine)


def raw(factory, module):
    """The column as stored, read without the store."""
    with factory() as db:
        return db.execute(select(ModuleIdentityRow.invoker_secret).where(ModuleIdentityRow.module == module)).scalar_one()


def plant(factory, module, secret):
    """A row exactly as a release without the key wrote it."""
    with factory() as db:
        db.add(ModuleIdentityRow(module=module, invoker_id=f"inv-{module}", invoker_secret=secret))
        db.commit()


def unencrypted_reads(kind):
    return REGISTRY.get_sample_value("smo_identity_unencrypted_reads_total", {"kind": kind}) or 0.0


# ---------------------------------------------------------------- the envelope

def test_a_secret_round_trips_and_the_stored_value_is_a_versioned_envelope_not_the_secret(factory):
    """With a key the column holds `enc:v1:<key id>:...`, never the secret, and the store gives the secret back for load, insert, put and replace."""
    store = DbIdentityStore(factory, keys(K1))
    assert store.insert("aimgf", "inv-1", SECRET) is True
    stored = raw(factory, "aimgf")
    assert stored.startswith(PREFIX) and SECRET not in stored and key_of(stored) == keys(K1).current.kid and len(key_of(stored)) == 8
    assert store.load("aimgf") == ("inv-1", SECRET)
    store.put("rapp-instance:i-1", "inv-2", SECRET + "2")
    assert raw(factory, "rapp-instance:i-1").startswith(PREFIX) and store.load("rapp-instance:i-1") == ("inv-2", SECRET + "2")
    store.put("rapp-instance:i-1", "inv-2", SECRET + "3")                               # a second put replaces the first
    assert store.load("rapp-instance:i-1") == ("inv-2", SECRET + "3")
    assert store.replace("aimgf", "inv-1", "inv-9", SECRET + "9") is True
    assert raw(factory, "aimgf").startswith(PREFIX) and SECRET not in raw(factory, "aimgf") and store.load("aimgf") == ("inv-9", SECRET + "9")


def test_without_a_key_everything_is_stored_and_read_as_before(factory):
    """No key configured: the secret is stored as issued and read back, so a deployment that does nothing behaves exactly as before."""
    store = DbIdentityStore(factory, keys())
    store.insert("aimgf", "inv-1", SECRET)
    assert raw(factory, "aimgf") == SECRET and store.load("aimgf") == ("inv-1", SECRET)


def test_every_write_uses_a_new_nonce():
    """Sealing the same secret twice gives two different values, both of which open."""
    k = keys(K1)
    first, second = seal(k, "aimgf", SECRET), seal(k, "aimgf", SECRET)
    assert first != second and unseal(k, "aimgf", first) == unseal(k, "aimgf", second) == SECRET


def test_a_sealed_value_copied_into_another_row_does_not_open():
    """The module name is authenticated: the identity of one module copied to another module's row is refused."""
    k = keys(K1)
    with pytest.raises(IdentityKeyError, match="cannot be decrypted with SMO_IDENTITY_KEY"):
        unseal(k, "nfo", seal(k, "aimgf", SECRET))


def test_is_sealed_and_key_of_tell_the_formats_apart():
    """Only `enc:v1:` marks ciphertext; a malformed envelope names no key."""
    assert is_sealed(seal(keys(K1), "m", SECRET)) and not is_sealed(SECRET)
    assert key_of(SECRET) is None and key_of("enc:v1:") is None and key_of("enc:v1:abcd1234") is None and key_of("enc:v1::x") is None


# ---------------------------------------------------------------- an upgrade locks nothing out

def test_a_plaintext_row_is_read_with_a_key_configured_and_counted_and_warned_about(factory, caplog):
    """Upgrade safety: with a key set, a row written before the key still reads (never fails closed); a warning and the counter say it is not encrypted."""
    plant(factory, "aimgf", SECRET)
    before = unencrypted_reads("plaintext")
    with caplog.at_level(logging.WARNING, logger="smo.identity_secrets"):
        assert DbIdentityStore(factory, keys(K1)).load("aimgf") == ("inv-aimgf", SECRET)
    assert unencrypted_reads("plaintext") == before + 1
    assert "aimgf" in caplog.text and "encrypt-existing" in caplog.text and SECRET not in caplog.text


def test_a_plaintext_row_with_no_key_is_silent(factory, caplog):
    """No key: a plaintext row is the normal case, so there is no warning and no count."""
    plant(factory, "aimgf", SECRET)
    before = unencrypted_reads("plaintext")
    with caplog.at_level(logging.WARNING):
        assert DbIdentityStore(factory, keys()).load("aimgf") == ("inv-aimgf", SECRET)
    assert unencrypted_reads("plaintext") == before and caplog.text == ""


# ---------------------------------------------------------------- what is refused

def test_an_encrypted_row_with_no_key_fails_naming_the_variable_and_never_returns_the_ciphertext(factory):
    """Ciphertext is never taken for a secret: with no key the load raises an error that names SMO_IDENTITY_KEY and carries neither value."""
    DbIdentityStore(factory, keys(K1)).insert("aimgf", "inv-1", SECRET)
    stored = raw(factory, "aimgf")
    with pytest.raises(IdentityKeyError) as caught:
        DbIdentityStore(factory, keys()).load("aimgf")
    message = str(caught.value)
    assert "SMO_IDENTITY_KEY" in message and SECRET not in message and stored not in message and K1 not in message


def test_a_wrong_key_an_unknown_key_and_a_changed_value_are_refused_without_leaking(factory):
    """A key this process does not have, a key id it knows but a value that does not verify, and a malformed body are all IdentityKeyError."""
    store = DbIdentityStore(factory, keys(K1))
    store.insert("aimgf", "inv-1", SECRET)
    stored = raw(factory, "aimgf")
    with pytest.raises(IdentityKeyError, match="does not have: set SMO_IDENTITY_KEY") as unknown:
        DbIdentityStore(factory, keys(K2)).load("aimgf")
    flipped = stored[:-4] + ("AAAA" if not stored.endswith("AAAA") else "BBBB")
    cut = stored.rsplit(":", 1)[0] + ":" + "AAAA"
    for bad in (flipped, cut, stored.rsplit(":", 1)[0] + ":!!!!"):
        with pytest.raises(IdentityKeyError, match="cannot be decrypted") as caught:
            unseal(keys(K1), "aimgf", bad)
        assert SECRET not in str(caught.value) and K1 not in str(caught.value)
    assert K1 not in str(unknown.value) and K2 not in str(unknown.value) and stored not in str(unknown.value)


# ---------------------------------------------------------------- rotation

def test_rotation_reads_the_old_key_writes_the_new_and_the_command_moves_the_rest(factory):
    """With the new key and the old one as previous, old rows still read (counted as previous-key) and new writes use the new key; `encrypt-existing`
    re-seals the old rows, after which the previous key can be removed."""
    DbIdentityStore(factory, keys(K1)).insert("aimgf", "inv-1", SECRET)
    store = DbIdentityStore(factory, keys(K2, K1))
    before = unencrypted_reads("previous-key")
    assert store.load("aimgf") == ("inv-1", SECRET) and unencrypted_reads("previous-key") == before + 1
    store.insert("nfo", "inv-2", SECRET)
    assert key_of(raw(factory, "nfo")) == keys(K2).current.kid != key_of(raw(factory, "aimgf"))
    assert store.encrypt_existing() == {"encrypted": 0, "resealed": 1, "already": 1, "changed": 0, "unreadable": 0}
    done = DbIdentityStore(factory, keys(K2))                                          # the previous key is gone
    assert done.load("aimgf") == ("inv-1", SECRET) and done.load("nfo") == ("inv-2", SECRET)
    with pytest.raises(IdentityKeyError):
        DbIdentityStore(factory, keys(K1)).load("aimgf")                              # and the old key alone no longer opens anything


# ---------------------------------------------------------------- the key variables

def test_the_key_comes_from_the_environment_or_a_file_and_a_trailing_newline_is_dropped(tmp_path):
    """`SMO_IDENTITY_KEY_FILE` (a mounted secret) gives the same key as the variable, and so does the previous-key file."""
    current, previous = tmp_path / "key", tmp_path / "previous"
    current.write_text(K1 + "\n")
    previous.write_text(K2)
    from_files = load_keys({"SMO_IDENTITY_KEY_FILE": str(current), "SMO_IDENTITY_PREVIOUS_KEY_FILE": str(previous)})
    assert from_files.current.kid == keys(K1).current.kid and from_files.previous.kid == keys(K2).current.kid
    assert load_keys({}).current is None and load_keys({"SMO_IDENTITY_KEY": ""}).current is None


@pytest.mark.parametrize(("env", "named"), [
    ({"SMO_IDENTITY_KEY": K1, "SMO_IDENTITY_KEY_FILE": "/run/secrets/x"}, "SMO_IDENTITY_KEY"),
    ({"SMO_IDENTITY_KEY_FILE": "/nonexistent/identity-key"}, "SMO_IDENTITY_KEY_FILE"),
    ({"SMO_IDENTITY_KEY": "too-short"}, "SMO_IDENTITY_KEY"),
    ({"SMO_IDENTITY_KEY": K1, "SMO_IDENTITY_PREVIOUS_KEY": "too-short"}, "SMO_IDENTITY_PREVIOUS_KEY"),
    ({"SMO_IDENTITY_PREVIOUS_KEY": K1}, "SMO_IDENTITY_PREVIOUS_KEY"),
    ({"SMO_IDENTITY_KEY": K1, "SMO_IDENTITY_PREVIOUS_KEY": K1}, "SMO_IDENTITY_PREVIOUS_KEY"),
])
def test_a_wrongly_set_key_is_refused_naming_the_variable_and_never_the_value(env, named):
    """Both forms set, an unreadable file, a short key, a previous key alone and a previous key equal to the current one all raise at once."""
    with pytest.raises(IdentityKeyError, match=named) as caught:
        check_configuration(env)
    assert K1 not in str(caught.value) and "too-short" not in str(caught.value)


def test_a_service_and_a_worker_refuse_to_start_with_a_wrong_key_and_start_without_one(monkeypatch):
    """`install_health` (every module's app) and the worker raise at start for a wrongly set key variable, and not at all when none is set."""
    install_health(FastAPI())
    check_configuration()
    monkeypatch.setenv("SMO_IDENTITY_KEY", "too-short")
    with pytest.raises(IdentityKeyError, match="SMO_IDENTITY_KEY"):
        install_health(FastAPI())
    with pytest.raises(IdentityKeyError, match="SMO_IDENTITY_KEY"):
        worker.main([], module="m")


def test_a_store_made_from_a_wrong_environment_raises_at_construction(monkeypatch):
    """The default store reads the environment when it is made, so a wrong key never turns into a silent plaintext write."""
    monkeypatch.setenv("SMO_IDENTITY_KEY", "too-short")
    with pytest.raises(IdentityKeyError):
        DbIdentityStore()
    monkeypatch.setenv("SMO_IDENTITY_KEY", K1)
    assert DbIdentityStore()._keys.current is not None


def test_keys_do_not_print_in_repr():
    """The repr of the keys shows key ids only, and the secret is not in the repr of an error."""
    k = keys(K1, K2)
    assert K1 not in repr(k) and K2 not in repr(k) and k.current.kid in repr(k)
    assert repr(keys()) == "IdentityKeys(current=None, previous=None)"


# ---------------------------------------------------------------- the command

def test_encrypt_existing_seals_plaintext_rows_reports_counts_and_is_idempotent(factory, caplog):
    """The command seals the rows that predate the key, counts them, leaves a sealed row alone, and a second run changes nothing; it does not warn per row."""
    plant(factory, "aimgf", SECRET)
    plant(factory, "nfo", SECRET + "-n")
    store = DbIdentityStore(factory, keys(K1))
    store.insert("sme", "inv-s", SECRET + "-s")
    before = unencrypted_reads("plaintext")
    with caplog.at_level(logging.WARNING):
        assert store.encrypt_existing() == {"encrypted": 2, "resealed": 0, "already": 1, "changed": 0, "unreadable": 0}
    assert caplog.text == "" and unencrypted_reads("plaintext") == before
    assert all(raw(factory, m).startswith(PREFIX) and SECRET not in raw(factory, m) for m in ("aimgf", "nfo", "sme"))
    assert store.load("aimgf") == ("inv-aimgf", SECRET) and store.load("nfo") == ("inv-nfo", SECRET + "-n")
    assert store.encrypt_existing() == {"encrypted": 0, "resealed": 0, "already": 3, "changed": 0, "unreadable": 0}


def test_encrypt_existing_leaves_an_unreadable_row_alone_and_counts_it(factory):
    """A row sealed with a key that is not given is counted as unreadable and left as it is; the others are still done."""
    DbIdentityStore(factory, keys(K2)).insert("aimgf", "inv-1", SECRET)
    stranger = raw(factory, "aimgf")
    plant(factory, "nfo", SECRET)
    assert DbIdentityStore(factory, keys(K1)).encrypt_existing() == {"encrypted": 1, "resealed": 0, "already": 0, "changed": 0, "unreadable": 1}
    assert raw(factory, "aimgf") == stranger


def test_encrypt_existing_does_not_overwrite_a_row_a_replica_rewrote_meanwhile(factory, monkeypatch):
    """The update is a compare-and-swap on the value read: a replica that put new credentials in between wins, and the row is counted as changed."""
    plant(factory, "aimgf", SECRET)
    store = DbIdentityStore(factory, keys(K1))
    real = module_identity.unseal

    def rewrite_then_unseal(*args, **kwargs):
        DbIdentityStore(factory, keys(K1)).put("aimgf", "inv-new", "newer-secret")
        return real(*args, **kwargs)
    monkeypatch.setattr(module_identity, "unseal", rewrite_then_unseal)
    assert store.encrypt_existing() == {"encrypted": 0, "resealed": 0, "already": 0, "changed": 1, "unreadable": 0}
    monkeypatch.setattr(module_identity, "unseal", real)
    assert store.load("aimgf") == ("inv-new", "newer-secret")


def test_encrypt_existing_without_a_key_is_an_error(factory):
    """There is nothing to encrypt with: the error names the variable."""
    with pytest.raises(IdentityKeyError, match="SMO_IDENTITY_KEY is not set"):
        DbIdentityStore(factory, keys()).encrypt_existing()


def test_the_command_line_prints_counts_only_and_returns_the_exit_codes(factory, monkeypatch, capsys):
    """`main` prints `name=count` pairs (exit 0), exit 1 when a row cannot be read, exit 2 for a missing key (message on stderr); never a secret or key."""
    plant(factory, "aimgf", SECRET)
    monkeypatch.setattr(module_identity, "DbIdentityStore", lambda: DbIdentityStore(factory, keys(K1)))
    assert module_identity.main(["encrypt-existing"]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "encrypted=1 resealed=0 already=0 changed=0 unreadable=0" and out.err == ""
    assert module_identity.main(["encrypt-existing"]) == 0 and "already=1" in capsys.readouterr().out
    plant(factory, "nfo", "enc:v1:deadbeef:AAAA")
    assert module_identity.main(["encrypt-existing"]) == 1 and "unreadable=1" in capsys.readouterr().out
    monkeypatch.setattr(module_identity, "DbIdentityStore", lambda: DbIdentityStore(factory, keys()))
    assert module_identity.main(["encrypt-existing"]) == 2
    err = capsys.readouterr().err
    assert "SMO_IDENTITY_KEY" in err and SECRET not in err and K1 not in err


def test_the_module_runs_as_a_command_against_a_real_database_file(tmp_path):
    """`python -m smo_shared.module_identity encrypt-existing` as an operator runs it: against a database file, with the key in the environment, it seals the
    row and prints the counts; a second run says `already`; with no key it exits 2 and the message names the variable."""
    url = f"sqlite:///{tmp_path / 'cli.db'}"
    engine = create_engine(url, future=True)
    Base.metadata.create_all(engine, tables=[ModuleIdentityRow.__table__])
    plant(sessionmaker(bind=engine), "aimgf", SECRET)
    engine.dispose()
    base = {k: v for k, v in os.environ.items() if not k.startswith("SMO_IDENTITY")}
    env = {**base, "SMO_DATABASE_URL": url, "PYTHONPATH": str(SMO / "shared")}

    def run(extra):
        return subprocess.run([sys.executable, "-m", "smo_shared.module_identity", "encrypt-existing"], capture_output=True, text=True, env={**env, **extra}, timeout=120)
    first, second, keyless = run({"SMO_IDENTITY_KEY": K1}), run({"SMO_IDENTITY_KEY": K1}), run({})
    assert (first.returncode, first.stdout.strip()) == (0, "encrypted=1 resealed=0 already=0 changed=0 unreadable=0")
    assert (second.returncode, second.stdout.strip()) == (0, "encrypted=0 resealed=0 already=1 changed=0 unreadable=0")
    assert keyless.returncode == 2 and "SMO_IDENTITY_KEY" in keyless.stderr and SECRET not in first.stdout + first.stderr + keyless.stderr + keyless.stdout


# ---------------------------------------------------------------- R1Client on top of it

class FakeSme:
    """R1 /bootstrap plus SME's invoker registry and token endpoint."""

    def __init__(self):
        self.invokers, self.registrations = {}, 0

    def _resp(self, status, payload, method, url):
        return httpx.Response(status, json=payload, request=httpx.Request(method, url))

    def get(self, url, **kw):
        return self._resp(200, {"apiEndpoints": [{"tokenEndPoint": {"uri": f"{SME}/oauth2/token"}}]}, "GET", url)

    def post(self, url, json=None, **kw):
        if url == f"{SME}/invoker-registrations":
            self.registrations += 1
            invoker_id, secret = f"api-invoker-{self.registrations}", f"secret-{self.registrations}-Hk3"
            self.invokers[invoker_id] = secret
            return self._resp(201, {"apiInvokerId": invoker_id, "onboardingSecret": secret}, "POST", url)
        if self.invokers.get(json["client_id"]) != json["client_secret"]:
            return self._resp(400, {"error": "invalid_client"}, "POST", url)
        return self._resp(200, {"access_token": "tok", "expires_in": 3600}, "POST", url)


@pytest.fixture
def sme(monkeypatch):
    """A stand-in for R1's /bootstrap and SME, patched into the R1 client, with MODULE=aimgf and no pinned invoker."""
    fake = FakeSme()
    monkeypatch.setattr(r1_client.httpx, "get", fake.get)
    monkeypatch.setattr(r1_client.httpx, "post", fake.post)
    monkeypatch.setenv("MODULE", "aimgf")
    monkeypatch.delenv("SMO_INVOKER_ID", raising=False)
    monkeypatch.delenv("SMO_INVOKER_SECRET", raising=False)
    return fake


def test_replicas_share_one_encrypted_identity_and_a_replica_without_the_key_registers_nothing(sme, factory, caplog):
    """A module's identity is stored encrypted and shared by replicas with the key; a replica without it gets no token (the log names the variable)
    and, unlike a store outage, does not register another invoker at SME."""
    first, second = _ModuleIdentity(store=DbIdentityStore(factory, keys(K1))), _ModuleIdentity(store=DbIdentityStore(factory, keys(K1)))
    assert first.token_for(R1) and second.token_for(R1) and sme.registrations == 1
    assert raw(factory, "aimgf").startswith(PREFIX) and sme.invokers["api-invoker-1"] not in raw(factory, "aimgf")
    keyless = _ModuleIdentity(store=DbIdentityStore(factory, keys()))
    with caplog.at_level(logging.WARNING, logger="smo.r1_client"):
        assert keyless.token_for(R1) is None
    assert sme.registrations == 1 and "SMO_IDENTITY_KEY" in caplog.text and sme.invokers["api-invoker-1"] not in caplog.text


def test_a_replica_that_loses_the_race_and_cannot_read_the_winners_identity_gets_no_token(sme, factory):
    """The loser of the race to store its identity offboards its duplicate and reads the winner's row; if that row is encrypted and it has no key, the
    error is raised (no token), not swallowed into a per-process identity."""
    class RacedStore(DbIdentityStore):
        def insert(self, module, invoker_id, secret):
            DbIdentityStore(factory, keys(K1)).insert(module, "api-invoker-WINNER", "secret-WINNER")
            return super().insert(module, invoker_id, secret)

    loser = _ModuleIdentity(store=RacedStore(factory, keys()))
    assert loser.token_for(R1) is None and sme.registrations == 1


def test_an_instances_credentials_are_stored_encrypted_and_read_back(factory, monkeypatch):
    """`bind_instance_identity` (rApp Management's push to an rApp, call flow 01c) writes through the store, so the credentials are sealed at rest."""
    monkeypatch.setenv("MODULE", "energy-saving-rapp")
    store = DbIdentityStore(factory, keys(K1))
    monkeypatch.setattr(r1_client, "_store", lambda: store)
    monkeypatch.setattr(r1_client, "_instance_identities", {})
    r1_client.bind_instance_identity("abc", "inv-abc", SECRET)
    assert raw(factory, "rapp-instance:abc").startswith(PREFIX) and SECRET not in raw(factory, "rapp-instance:abc")
    assert r1_client._load_stored("abc") == ("inv-abc", SECRET)


# ---------------------------------------------------------------- the one door

SMO = Path(__file__).resolve().parents[2]


def test_nothing_but_the_store_reads_or_writes_the_secret_column():
    """Every reader of `module_identity` goes through `DbIdentityStore`: no other source file names the model, the table in SQL or the column of the
    row, so no raw read can return ciphertext or skip the sealing (a test or the migration scripts may name the table to set up a schema)."""
    allowed = {"shared/smo_shared/module_identity.py", "scripts/check_migration_matches_models.py"}
    pattern = re.compile(r"ModuleIdentityRow|FROM\s+module_identity|INTO\s+module_identity|UPDATE\s+module_identity|\.invoker_secret\b.*Row|row\.invoker_secret", re.IGNORECASE)
    offenders = []
    for path in SMO.rglob("*.py"):
        relative = path.relative_to(SMO).as_posix()
        if "node_modules" in relative or "/tests/" in f"/{relative}" or relative.startswith("tests_integration/") or relative in allowed:
            continue
        if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
            offenders.append(relative)
    assert offenders == []
