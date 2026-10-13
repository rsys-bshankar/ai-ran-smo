"""PR-RAPP-3: the rApp conformance pack (conformance/rapp).

Offline: the sample packages pass, and crafted bad packages fail the way Onboarding fails them (the verdict is compared with the real Onboarding app in the mesh).
Runtime: the kit takes a sample package through onboard, register, heartbeat, R1 usage and terminate against the in-process mesh, and fakes that each break one
thing show the checks read the platform back instead of counting 2xx answers.
"""

import dataclasses
import io
import json
import re
import sys
import zipfile
from pathlib import Path

import httpx
import pytest

SMO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SMO_ROOT))
sys.path.insert(0, str(SMO_ROOT / "shared"))

from conformance.rapp import package_checks, runtime_checks  # noqa: E402,F401
from conformance.rapp.__main__ import main  # noqa: E402
from conformance.rapp.kit import FAIL, PASS, REGISTRY, SKIP, WARN, PackageContext, RuntimeContext, run, summary  # noqa: E402
from smo_shared import csar_signing as cs  # noqa: E402

SAMPLES = SMO_ROOT / "samples"
DEMO_TRUST = SAMPLES / "demo-signing"
ES = SAMPLES / "energy-saving-rapp.csar"
URL = "http://example/energy-saving-rapp.csar"


def outcome(results) -> dict[str, str]:
    return {r.id: r.status for r in results}


def failures(results) -> dict[str, str]:
    return {r.id: r.detail for r in results if r.status == FAIL}


def entries(path: Path = ES) -> dict[str, bytes]:
    """The files of a package as {name: bytes}."""
    with zipfile.ZipFile(path) as z:
        return {i.filename: z.read(i.filename) for i in z.infolist()}


def zipped(files: dict[str, bytes], compression=zipfile.ZIP_STORED) -> bytes:
    """Builds package bytes (a zip) from {name: bytes}; stored, or deflated when a test needs a small file that expands."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def unsigned(change=None, path: Path = ES) -> bytes:
    """The energy-saving sample as a package without its signing files, after `change` (if given) has edited the file map."""
    files = {k: v for k, v in entries(path).items() if k not in cs.SIGNING_FILES}
    if change:
        change(files)
    return zipped(files)


def offline(data: bytes, name="pkg.csar", **kwargs):
    """Runs the offline package checks on package bytes; `trust` becomes the trust store and the other keyword arguments the package context."""
    trust = kwargs.pop("trust", None)
    return run(PackageContext(name, data, trust, **kwargs))


# ------------------------------------------------------------------------------------------------------------------------------- offline

def test_each_sample_package_passes_every_offline_check_that_applies_with_its_signature_verified():
    """Each committed sample package passes every offline check with its signature verified against the demo trust store, with no warning, and the
    only check skipped is the one for SME providers, which no sample declares.
    """
    trust = cs.load_trust_store(DEMO_TRUST)
    for csar in sorted(SAMPLES.glob("*.csar")):
        results = offline(csar.read_bytes(), csar.name, trust=trust, require_signed=True)
        assert failures(results) == {}, csar.name
        assert [r.id for r in results if r.status == WARN] == [], csar.name
        assert outcome(results)["PK-S"] == PASS and outcome(results)["PK-V"] == PASS
        assert {r.id for r in results if r.status == SKIP} <= {"PK-7"}                # no sample declares SME providers


def test_without_a_trust_store_the_signature_is_skipped_and_with_require_signed_it_fails():
    """Without a trust store the signature check is skipped, and with `require_signed` it fails and says there is no trust store."""
    assert outcome(offline(ES.read_bytes()))["PK-S"] == SKIP
    failed = failures(offline(ES.read_bytes(), require_signed=True))
    assert "no trust store" in failed["PK-S"] and "no trusted publisher keys" in failed["PK-V"]


BAD_PACKAGES = {
    "not-a-zip": (lambda: b"this is not a zip", "pkg.csar", {"PK-1", "PK-V"}),
    "wrong-extension": (lambda: unsigned(), "pkg.zip", {"PK-1", "PK-V"}),
    "no-tosca-meta": (lambda: unsigned(lambda f: f.pop("TOSCA-Metadata/TOSCA.meta")), "pkg.csar", {"PK-2", "PK-V"}),
    "no-entry-line": (lambda: unsigned(lambda f: f.__setitem__("TOSCA-Metadata/TOSCA.meta", b"CSAR-Version: 1.1\n")), "pkg.csar", {"PK-2", "PK-V"}),
    "entry-file-missing": (lambda: unsigned(lambda f: f.pop("Definitions/asd.yaml")), "pkg.csar", {"PK-2", "PK-V"}),
    "manifest-not-yaml": (lambda: unsigned(lambda f: f.__setitem__("manifest.yaml", b"a: [unclosed\n")), "pkg.csar", {"PK-4", "PK-V"}),
    "unknown-execution-mode": (lambda: unsigned(lambda f: f.__setitem__("manifest.yaml", b"rappManifest: {manifestVersion: '1.0'}\nruntimeProfiles:\n  BOGUS: {cpu: 1}\n")),
                               "pkg.csar", {"PK-4", "PK-V"}),
    "negative-cpu": (lambda: unsigned(lambda f: f.__setitem__("manifest.yaml", b"rappManifest: {manifestVersion: '1.0'}\nruntimeProfiles:\n  TRAINING: {cpu: -1}\n")),
                     "pkg.csar", {"PK-4", "PK-V"}),
    "memory-not-a-quantity": (lambda: unsigned(lambda f: f.__setitem__("manifest.yaml", b"rappManifest: {manifestVersion: '1.0'}\nruntimeProfiles:\n  TRAINING: {cpu: 1, memory: 16 GB}\n")),
                              "pkg.csar", {"PK-4", "PK-V"}),
    "bad-operator-ui": (lambda: unsigned(lambda f: f.__setitem__("manifest.yaml", b"rappManifest: {manifestVersion: '1.0'}\noperatorUi: {version: 9, panels: []}\n")),
                        "pkg.csar", {"PK-4", "PK-V"}),
    "capabilities-not-a-mapping": (lambda: unsigned(lambda f: f.__setitem__("capabilities.yaml", b"- just\n- a list\n")), "pkg.csar", {"PK-6", "PK-V"}),
    "sme-json-broken": (lambda: unsigned(lambda f: f.__setitem__("Files/Sme/providers/p.json", b"{broken")), "pkg.csar", {"PK-7", "PK-V"}),
    # the header is not written whole: the repository holds no private key but the demo one (test_rapp_signing.py)
    "private-key-inside": (lambda: unsigned(lambda f: f.__setitem__("notes.txt", b"-----BEGIN " + b"PRIVATE KEY-----\nAAAA\n-----END " + b"PRIVATE KEY-----\n")),
                           "pkg.csar", {"PK-9"}),
}


# One row per bad package: how to make it, the file name and the set of checks that must fail.
@pytest.mark.parametrize("name", sorted(BAD_PACKAGES))
def test_a_crafted_bad_package_fails_the_check_that_names_the_problem(name):
    """Each crafted bad package fails exactly the checks that name its problem, each with a detail."""
    make, filename, expected = BAD_PACKAGES[name]
    results = offline(make(), filename)
    assert set(failures(results)) == expected, failures(results)
    assert all(detail for detail in failures(results).values())


def test_a_package_that_is_not_a_zip_does_not_crash_the_checks_that_need_one():
    """Bytes that are not a zip do not crash the checks: those that need the archive are skipped."""
    results = offline(b"nope")
    assert {r.id for r in results if r.status == SKIP} >= {"PK-2", "PK-3", "PK-4", "PK-8", "PK-9"}


def zip_limited(monkeypatch, **changes):
    """Lowers Onboarding's zip limits for the test (the kit reads them from `onboarding().ZIP_LIMITS`, so one change moves both)."""
    module = package_checks.onboarding()
    monkeypatch.setattr(module, "ZIP_LIMITS", dataclasses.replace(module.ZIP_LIMITS, **changes))


def over_limit(results, check_id="PK-1"):
    """True when the package failed `check_id` for the zip limits and every check that opens the zip was skipped, not crashed."""
    by_id = {r.id: r for r in results}
    skipped = {"PK-2", "PK-3", "PK-4", "PK-6", "PK-7", "PK-8", "PK-9"}
    return by_id[check_id].status == FAIL and {i for i in skipped if by_id[i].status == SKIP} == skipped


def test_the_conformance_kit_applies_the_zip_limits_onboarding_applies():
    """The kit has no limits of its own: it reads Onboarding's `ZIP_LIMITS`, which are the ones `csar_signing.verify_zip` uses (SEC-15.17b)."""
    assert package_checks.onboarding().ZIP_LIMITS is cs.DEFAULT_LIMITS


def test_a_zip_bomb_fails_pk_1_and_pk_v_and_is_not_decompressed_by_the_other_checks():
    """60 MiB of zeros in a 60 kB zip: above the member limit and the ratio, so PK-1 fails and PK-V agrees, and the checks that open the zip skip."""
    files = {k: v for k, v in entries().items() if k not in cs.SIGNING_FILES}
    files["Artifacts/bomb.bin"] = bytes(60 * 1024 * 1024)
    data = zipped(files, zipfile.ZIP_DEFLATED)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert any(i.file_size > 50 * 1024 * 1024 for i in z.infolist())
    assert len(data) < 200_000
    results = offline(data)
    assert over_limit(results)
    assert "too large" in failures(results)["PK-1"]
    assert "package too large to read" in failures(results)["PK-V"]


def test_a_package_with_too_many_entries_fails_pk_1():
    """More entries than `max_entries` (2000 by default) fails PK-1 before any is read."""
    data = unsigned(lambda f: f.update({f"Artifacts/f{i}.txt": b"x" for i in range(2001)}))
    results = offline(data)
    assert over_limit(results)
    assert "2000 allowed" in failures(results)["PK-1"]


def test_an_oversize_member_fails_pk_1_and_a_total_over_the_limit_does_too(monkeypatch):
    """A file above `max_member_bytes`, and files that together pass `max_total_bytes`, each fail PK-1 (limits lowered so the test stays small)."""
    zip_limited(monkeypatch, max_member_bytes=10_000)
    data = unsigned(lambda f: f.__setitem__("Artifacts/big.bin", b"\x01\x02" * 6_000))
    results = offline(data)
    assert over_limit(results) and "more than the 10000 allowed" in failures(results)["PK-1"]
    zip_limited(monkeypatch, max_member_bytes=1_000_000, max_total_bytes=50_000)
    results = offline(unsigned(lambda f: f.update({"Artifacts/a.bin": bytes(range(256)) * 100, "Artifacts/b.bin": bytes(range(256)) * 100})))
    assert over_limit(results) and "bytes uncompressed" in failures(results)["PK-1"]


def test_a_member_over_the_ratio_fails_pk_1(monkeypatch):
    """A file above the ratio floor that compresses more than `max_ratio` to 1 fails PK-1 (a zip bomb below the size limits)."""
    zip_limited(monkeypatch, ratio_floor_bytes=1000, max_ratio=20)
    files = {k: v for k, v in entries().items() if k not in cs.SIGNING_FILES}
    files["Artifacts/zeros.bin"] = bytes(200_000)
    results = offline(zipped(files, zipfile.ZIP_DEFLATED))
    assert over_limit(results) and "zip bomb" in failures(results)["PK-1"]


@pytest.mark.parametrize("path", ["../escape.txt", "Artifacts/../../escape.txt", "/etc/cron.d/x", "Artifacts\\..\\x.txt"])
def test_an_entry_path_that_climbs_out_or_is_absolute_fails_pk_1(path):
    """A path with `..`, a leading `/` or a backslash fails PK-1, as `csar_signing.verify_zip` refuses it in a signed package."""
    results = offline(unsigned(lambda f: f.__setitem__(path, b"x")))
    assert "unsafe path" in failures(results)["PK-1"]


def test_the_normal_packages_still_pass_pk_1_and_the_zip_limits_do_not_touch_them():
    """The committed sample passes PK-1 with the real limits, unsigned and signed."""
    assert outcome(offline(unsigned()))["PK-1"] == PASS
    assert outcome(offline(ES.read_bytes()))["PK-1"] == PASS


def test_a_member_that_is_larger_than_the_scan_limit_is_not_read_past_it_by_pk_9():
    """PK-9 reads at most `MAX_SCANNED_BYTES` of a file whatever its directory entry says, and a key beyond that is not found (as documented)."""
    big = bytes(package_checks.MAX_SCANNED_BYTES + 10)
    with zipfile.ZipFile(io.BytesIO(zipped({"x.bin": big}))) as z:
        assert package_checks._scanned(z, "x.bin") == b""


def test_advice_is_a_warning_not_a_failure():
    """A package with advice-level problems (a missing application version, a mode with no runtime profile, an unknown data namespace and others)
    only warns and fails nothing.
    """
    def advise(files):
        files["tests/test_x.py"] = b"def test_x(): pass\n"
        files["capabilities.yaml"] = b"capabilities:\n  consumes: [{namespace: quantum, description: x}]\n"
        files["manifest.yaml"] = b"rappManifest: {manifestVersion: '1.0'}\nexecutionModes: [TRAINING, INFERENCE]\nruntimeProfiles:\n  TRAINING: {cpu: 1, memory: 1Gi}\n"
        files["Definitions/asd.yaml"] = b"application_name: Only_A_Name\n"
    results = offline(unsigned(advise))
    assert failures(results) == {}
    warned = {r.id: r.detail for r in results if r.status == WARN}
    assert set(warned) == {"PK-3", "PK-5", "PK-6", "PK-8", "PK-S"} - {"PK-S"}, warned
    assert "application_version" in warned["PK-3"] and "INFERENCE is an execution mode with no runtime profile" in warned["PK-5"] and "quantum" in warned["PK-6"]


def test_an_unsigned_package_is_a_warning_with_a_trust_store_and_a_failure_when_signing_is_required():
    """An unsigned package is a warning when a trust store is given and a failure, with the signature validity check, when signing is required."""
    trust = cs.load_trust_store(DEMO_TRUST)
    assert outcome(offline(unsigned(), trust=trust))["PK-S"] == WARN
    results = offline(unsigned(), trust=trust, require_signed=True)
    assert "not signed" in failures(results)["PK-S"] and "PK-V" in failures(results)


def test_a_package_changed_after_signing_fails_the_signature_check_and_onboardings_verdict():
    """A package edited after signing fails the signature check with the file named, and the validity check (Onboarding's own verdict) fails with
    it.
    """
    trust = cs.load_trust_store(DEMO_TRUST)
    files = entries()
    files["manifest.yaml"] += b"# one more line\n"
    failed = failures(offline(zipped(files), trust=trust))
    assert "manifest.yaml does not match its signed digest" in failed["PK-S"] and "package signature" in failed["PK-V"]


def test_a_package_signed_by_a_publisher_the_trust_store_does_not_hold_is_an_unknown_publisher(tmp_path):
    """A package signed by a publisher whose key is not in the trust store is reported as an unknown publisher by both checks."""
    (tmp_path / "other.pub").write_bytes(cs.generate_keypair()[1])
    failed = failures(offline(ES.read_bytes(), trust=cs.load_trust_store(tmp_path)))
    assert "unknown publisher" in failed["PK-S"] and "unknown publisher" in failed["PK-V"]


@pytest.mark.parametrize("name", sorted(BAD_PACKAGES))
def test_the_offline_verdict_is_the_one_the_real_onboarding_gives(name, mesh, loaded_apps, monkeypatch):
    """PK-V fails exactly when POST /packages ends FAILED, for each crafted package (the private-key one is hygiene advice Onboarding does not give)."""
    make, filename, _ = BAD_PACKAGES[name]
    data = make()
    verdict = outcome(offline(data, filename))["PK-V"]

    class Resp:
        content = data

        def raise_for_status(self):
            pass
    real_get = httpx.get
    location = f"http://example/{filename}"
    monkeypatch.setattr(loaded_apps["onboarding"].httpx, "get", lambda loc, timeout=None, **kw: Resp() if loc == location else real_get(loc, timeout=timeout, **kw))
    answer = mesh["onboarding"].post("/packages", json={"location": location}).json()
    state = mesh["onboarding"].get(f"/packages/{answer['packageId']}/onboarding-status").json()["state"]
    assert (verdict == FAIL) == (state == "FAILED"), (name, verdict, state)


def test_the_good_packages_are_accepted_by_both(mesh, loaded_apps, monkeypatch):
    """The sample package is accepted by the real Onboarding app in the mesh (state AVAILABLE) and passes the kit's validity check, so the kit's
    offline verdict matches Onboarding's.
    """
    data = ES.read_bytes()

    class Resp:
        content = data

        def raise_for_status(self):
            pass
    monkeypatch.setattr(loaded_apps["onboarding"].httpx, "get", lambda loc, timeout=None, **kw: Resp())
    answer = mesh["onboarding"].post("/packages", json={"location": URL}).json()
    assert mesh["onboarding"].get(f"/packages/{answer['packageId']}/onboarding-status").json()["state"] == "AVAILABLE"
    assert outcome(offline(data))["PK-V"] == PASS


# ---------------------------------------------------------------------------------------------------------------------------- the CLI

def test_the_package_command_writes_the_report_files_and_exits_0_on_the_samples(tmp_path, capsys):
    """The `package` command on the sample directory with the demo trust store exits 0, writes the JSON and Markdown reports with no failure or
    warning, covers the four packages and prints the same Markdown.
    """
    out = tmp_path / "report"
    assert main(["package", str(SAMPLES), "--trust", str(DEMO_TRUST), "--require-signed", "--out", str(out)]) == 0
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["kit"] == "conformance.rapp" and report["kind"] == "package" and report["summary"]["fail"] == 0 and report["summary"]["warn"] == 0
    assert len(report["packages"]) == 4 and len(report["results"]) == 4 * len([c for c in REGISTRY if c.kind == "package"])
    markdown = (tmp_path / "report.md").read_text()
    assert "0 failed, 0 warnings" in markdown and "| PK-S |" in markdown and "energy-saving-rapp.csar" in markdown
    assert capsys.readouterr().out.rstrip("\n") == markdown.rstrip("\n")


def test_the_package_command_exits_1_on_a_bad_package_and_2_on_a_usage_problem(tmp_path, capsys):
    """The `package` command exits 1 with the failing check's message for a bad package, and 2 for usage problems: a missing path,
    `--require-signed` without a trust store, a missing trust store, a directory with no package, and no arguments.
    """
    bad = tmp_path / "bad.csar"
    bad.write_bytes(BAD_PACKAGES["negative-cpu"][0]())
    assert main(["package", str(bad)]) == 1
    assert "runtimeProfiles.TRAINING.cpu must be a non-negative number" in capsys.readouterr().out
    assert main(["package", str(tmp_path / "missing.csar")]) == 2
    assert main(["package", str(ES), "--require-signed"]) == 2
    assert main(["package", str(ES), "--trust", str(tmp_path / "no-store")]) == 2
    (tmp_path / "empty").mkdir()
    assert main(["package", str(tmp_path / "empty")]) == 2                         # a directory with no .csar in it
    assert main([]) == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err and "--require-signed needs --trust" in err


def test_list_names_every_check_with_its_kind(capsys):
    """`--list` prints one line per registered check, with unique ids that include the first signature and the last runtime check."""
    assert main(["--list"]) == 0
    listing = capsys.readouterr().out
    assert len(listing.splitlines()) == len(REGISTRY) and "PK-V" in listing and "RT-8" in listing
    assert len({c.id for c in REGISTRY}) == len(REGISTRY)


def test_only_runs_one_check_or_one_group(tmp_path):
    """`--only` selects a single check by id or a whole group by name, and the report holds just those."""
    out = tmp_path / "r"
    assert main(["package", str(ES), "--only", "PK-9", "--only", "SIGNATURE", "--out", str(out)]) == 0
    assert [r["id"] for r in json.loads((tmp_path / "r.json").read_text())["results"]] == ["PK-9", "PK-S"]


# ------------------------------------------------------------------------------------------------------------------------------ runtime

@pytest.fixture
def serve(loaded_apps, monkeypatch):
    """Onboarding fetches the package over HTTP: serve `data` at URL (and leave every other call to the mesh)."""
    def _serve(data: bytes = ES.read_bytes()):
        class Resp:
            content = data

            def raise_for_status(self):
                pass
        real_get = httpx.get
        monkeypatch.setattr(loaded_apps["onboarding"].httpx, "get", lambda loc, timeout=None, **kw: Resp() if loc == URL else real_get(loc, timeout=timeout, **kw))
    return _serve


def runtime(services, **kwargs):
    """Runs the runtime checks against `services` for the sample package URL and returns (context, results)."""
    ctx = RuntimeContext(services, URL, **kwargs)
    return ctx, run(ctx)


def test_the_platform_passes_every_runtime_check_with_a_sample_package(mesh, serve):
    """A sample package passes all eight runtime checks (onboard, run, usage, reports, configuration, fault, terminate, delete) against the
    in-process platform.
    """
    serve()
    ctx, results = runtime(mesh)
    assert failures(results) == {}
    assert [r.id for r in results] == [f"RT-{n}" for n in range(1, 9)]
    assert summary(results) == {PASS: 8, FAIL: 0, WARN: 0, SKIP: 0}


def test_a_run_removes_what_it_made_so_a_second_run_of_the_same_package_passes_too(mesh, serve):
    """A run removes the instance it made and leaves the package deleting, so the same package can be run a second time."""
    serve()
    for _ in range(2):
        assert failures(runtime(mesh)[1]) == {}
    instances = mesh["rapp-mgmt"].get("/instances").json()["items"]
    assert instances == []
    states = {p["state"] for p in mesh["onboarding"].get("/packages").json()["items"]}
    assert states == {"DELETING"}


def test_keep_leaves_the_package_onboarded_and_the_instance_removed(mesh, serve):
    """With `keep` the package stays onboarded (AVAILABLE) while the instance is still removed."""
    serve()
    assert failures(runtime(mesh, keep=True)[1]) == {}
    assert mesh["rapp-mgmt"].get("/instances").json()["items"] == []
    assert {p["state"] for p in mesh["onboarding"].get("/packages").json()["items"]} == {"AVAILABLE"}


def test_each_signed_sample_runs_through_the_platform(mesh, serve):
    """Each of the signed sample packages passes the runtime checks."""
    for csar in sorted(SAMPLES.glob("*.csar")):
        serve(csar.read_bytes())
        assert failures(runtime(mesh)[1]) == {}, csar.name


def test_the_autonomy_mode_asked_for_is_the_one_the_instance_has(mesh, serve):
    """A run asked for ASSIST mode passes, so the instance has the mode the run asked for."""
    serve()
    assert failures(runtime(mesh, autonomy_mode="ASSIST")[1]) == {}


class Faulty:
    """A platform in front of one module's client that breaks one thing: `rule(verb, path, response)` may return another httpx.Response."""

    def __init__(self, real, rule):
        self.real, self.rule = real, rule

    def __getattr__(self, verb):
        def call(path, **kwargs):
            response = getattr(self.real, verb)(path, **kwargs)
            replaced = self.rule(verb, path, response)
            return replaced if replaced is not None else response
        return call


def answer(status: int, body) -> httpx.Response:
    return httpx.Response(status, json=body)


def break_one(mesh, service, rule):
    return {**mesh, service: Faulty(mesh[service], rule)}


def test_a_package_that_onboarding_refuses_fails_rt_1_with_its_reason_and_skips_the_rest(mesh, serve, monkeypatch):
    """A package Onboarding refuses fails RT-1 with its reason and every other runtime check is skipped saying what it needs."""
    serve(unsigned(lambda f: f.__setitem__("manifest.yaml", b"a: [unclosed\n")))
    _, results = runtime(mesh)
    assert outcome(results)["RT-1"] == FAIL and "FAILED" in failures(results)["RT-1"]
    assert {r.status for r in results if r.id != "RT-1"} == {SKIP}
    assert all("needs" in r.detail for r in results if r.id != "RT-1")


def test_the_trust_policy_of_the_platform_shows_in_rt_1(mesh, serve, monkeypatch, tmp_path):
    """When the platform requires signed packages with a trust store, an unsigned package fails RT-1 with the platform's own reason, so the kit
    sees the platform's policy.
    """
    serve(unsigned())
    (tmp_path / "other.pub").write_bytes(cs.generate_keypair()[1])
    monkeypatch.setenv("ONBOARDING_TRUST_STORE", str(tmp_path))
    monkeypatch.setenv("ONBOARDING_REQUIRE_SIGNED_PACKAGES", "true")
    _, results = runtime(mesh)
    assert "package signature: the package is not signed" in failures(results)["RT-1"]


def test_an_instance_that_never_runs_fails_rt_2_and_the_run_still_cleans_up(mesh, serve):
    """An instance that never reaches RUNNING fails RT-2, and the run still terminates and deletes it."""
    serve()

    def rule(verb, path, response):
        if verb == "post" and path.endswith("/bootstrap-complete"):
            return answer(200, {"state": "DEPLOYING"})
    ctx, results = runtime(break_one(mesh, "rapp-mgmt", rule))
    assert "expected RUNNING" in failures(results)["RT-2"]
    assert mesh["rapp-mgmt"].get("/instances").json()["items"] == []                # terminated and deleted by the clean-up


def test_a_usage_registration_that_is_missing_fails_rt_3(mesh, serve):
    """A platform that lists no usage registration for the package fails RT-3 with the count."""
    serve()

    def rule(verb, path, response):
        if verb == "get" and path.endswith("/usage"):
            return answer(200, {"items": []})
    assert "0 usage registrations" in failures(runtime(break_one(mesh, "onboarding", rule))[1])["RT-3"]


def test_reports_that_are_acknowledged_and_not_kept_fail_rt_4(mesh, serve):
    """Reports the platform acknowledges but does not return when read back fail RT-4."""
    serve()

    def rule(verb, path, response):
        if verb == "get" and path.endswith("/performance"):
            return answer(200, {"items": []})
    assert "read back as []" in failures(runtime(break_one(mesh, "rapp-mgmt", rule))[1])["RT-4"]


def test_a_configuration_that_is_not_what_was_written_fails_rt_5(mesh, serve):
    """A configuration that reads back differently from what was written fails RT-5."""
    serve()

    def rule(verb, path, response):
        if verb == "get" and path.endswith("/config"):
            return answer(200, {"stale": True})
    assert "differs from what was written" in failures(runtime(break_one(mesh, "rapp-mgmt", rule))[1])["RT-5"]


def test_a_warning_fault_that_faults_the_instance_fails_rt_6(mesh, serve):
    """A warning-level fault that moves the instance to FAULTED fails RT-6, since a warning must leave it RUNNING."""
    serve()

    def rule(verb, path, response):
        if verb == "post" and path.endswith("/fault"):
            return answer(200, {"status": "recorded", "instanceState": "FAULTED"})
    assert "still RUNNING" in failures(runtime(break_one(mesh, "rapp-mgmt", rule))[1])["RT-6"]


def test_a_terminate_that_leaves_the_workload_or_the_usage_fails_rt_7(mesh, serve):
    """A terminate whose teardown report shows a failed usage stop fails RT-7 and not RT-8."""
    serve()

    def rule(verb, path, response):
        if verb == "post" and path.endswith("/terminate") and response.status_code == 200:
            body = response.json()
            body["lastTeardown"]["usageStop"] = "FAILED: HTTP 503"
            return answer(200, body)
    failed = failures(runtime(break_one(mesh, "rapp-mgmt", rule))[1])
    assert "usageStop" in failed["RT-7"] and "RT-8" not in failed


def test_an_instance_that_survives_its_deletion_fails_rt_8(mesh, serve):
    """A delete that does not answer 204 fails RT-8."""
    serve()

    def rule(verb, path, response):
        if verb == "delete" and "/instances/" in path:
            return httpx.Response(200, json={})
    assert "expected 204" in failures(runtime(break_one(mesh, "rapp-mgmt", rule))[1])["RT-8"]


def test_a_platform_that_does_not_answer_is_a_failure_with_the_reason_not_a_crash(mesh, serve):
    """A platform that cannot be reached is a failed check that names the error, not an exception."""
    serve()

    class Down:
        def __getattr__(self, verb):
            def call(path, **kwargs):
                raise httpx.ConnectError("connection refused")
            return call
    results = runtime({**mesh, "onboarding": Down()})[1]
    assert "no usable answer: ConnectError" in failures(results)["RT-1"]


def test_the_runtime_command_writes_both_report_files(mesh, serve, tmp_path, capsys):
    """The `runtime` command writes the JSON and Markdown reports and exits 0 when all eight checks pass."""
    serve()
    out = tmp_path / "rt"
    assert main(["runtime", "--package-url", URL, "--direct", "--out", str(out)], services=mesh) == 0
    report = json.loads((tmp_path / "rt.json").read_text())
    assert report["kind"] == "runtime" and report["packageUrl"] == URL and report["summary"] == {"pass": 8, "fail": 0, "warn": 0, "skip": 0}
    assert re.search(r"\*\*8 passed, 0 failed, 0 warnings, 0 skipped\.\*\*", (tmp_path / "rt.md").read_text())
    assert [r["id"] for r in report["results"]] == [f"RT-{n}" for n in range(1, 9)]


def test_the_runtime_command_needs_a_way_to_reach_the_platform(capsys):
    """The `runtime` command exits 2 without a way to reach the platform, and when `--direct` and `--r1` are given together, and says to give
    `--r1`.
    """
    assert main(["runtime", "--package-url", URL]) == 2
    assert main(["runtime", "--package-url", URL, "--direct", "--r1", "http://x"]) == 2
    assert "give --r1" in capsys.readouterr().err


def test_the_default_clients_are_built_from_the_gateway_or_the_module_names(monkeypatch):
    """Without injected services the command builds its clients from `--r1` (module paths under the gateway, with the given header) or, with
    `--direct`, from a per-module URL template.
    """
    import conformance.rapp.__main__ as cli
    seen = []
    monkeypatch.setattr(cli, "run", lambda ctx, only=None: seen.append(ctx.services) or [])
    cli.main(["runtime", "--package-url", URL, "--r1", "http://r1:8000/", "--header", "Authorization: Bearer t"])
    cli.main(["runtime", "--package-url", URL, "--direct", "--direct-template", "http://{service}.smo:8000"])
    gateway, direct = seen
    assert str(gateway["onboarding"].base_url) == "http://r1:8000/onboarding/" and gateway["onboarding"].headers["authorization"] == "Bearer t"
    assert str(direct["rapp-mgmt"].base_url) == "http://rapp-mgmt.smo:8000"
