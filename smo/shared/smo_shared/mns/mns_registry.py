"""The MnS registry in its TS 28.622 form (plan tasks S1.7, S1.12): what a producer writes to register, how a reader finds a producer, and the
start-up helper every ProvMnS module calls to register itself.

The registry is three classes of the vendored TS 28.623 MnS Registry NRM (`TS28623_MnSRegistryNrm.yaml`, the OpenAPI of TS 28.622), served by SME
over ProvMnS at the one form `/ProvMnS/{version}/{LDN}` (SME's contract, `specs/smo-schema-bundle/openapi/modules/sme.yaml`):

  SubNetwork=SMO,MnsRegistry=1                        REGISTRY_DN: the registry; SME's, read only
  SubNetwork=SMO,MnsRegistry=1,MnsInfo=<module>       one MnS producer: mnsType, mnsVersion, mnsAddress, mnsScope, mnsCapability, mgmtDataInfoRef
  SubNetwork=SMO,MnsRegistry=1,MgmtDataInfo=<module>  what that producer serves (TS 28.537): supportedManagementData

A module registers the pair (`registration`): its `MgmtDataInfo` first, then its `MnsInfo`, whose `mgmtDataInfoRef` names it. Both are createMOI,
which replaces an instance that exists, so registering again (a restart, a second replica) changes nothing and publishes nothing.

What the attributes carry:

  mnsType          PROV_MNS for a ProvMnS producer
  mnsVersion       the ProvMnS version it serves (PROVMNS_VERSION, D5)
  mnsAddress       its ProvMnS root, `http://<module>:8000/ProvMnS/<version>` by default (`SMO_MNS_ADDRESS` replaces it): R1 Termination forwards
                   `/ProvMnS/<version>/<LDN>` to `<mnsAddress>/<LDN>`
  mnsScope         left out (the producer may hold any DN), or one MnsScope: the DNs under which its instances lie
  mnsCapability    the TS 28.623 MnsCapability values (NR_PROVISIONING, ML_MODEL_MANAGEMENT, ...), when the module says
  supportedManagementData
                   a list of TS 28.623 ManagementData values: the classes the producer serves (one list of class names) and, when it has any, its
                   optimisation domains (one list of the TS 28.537 values COVERAGE, CAPACITY, ENERGY_EFFICIENCY, MOBILITY, ACCESSIBILITY). A value
                   is a domain when it is one of those five, else a class name (class names are CamelCase, the domains upper case)

TS 28.622's MnsInfo has no attribute for the classes a producer serves; TS 28.537's MgmtDataInfo describes the management data a producer can supply,
which for a ProvMnS producer is its classes, so that is where they are. TS 28.533's own registry (class → producer) was not built first and
converted: this is the registry from the start (plan task S1.7, done in this form).

Finding a producer (`entries`, `discover`, `producer_for`) reads a flat getMOIAttributes of the whole registry (`REGISTRY_PATH` with `READ_PARAMS` and
`READ_HEADERS`, every page) and joins each MnsInfo with the MgmtDataInfo it names. `discover` filters by class, by api-cat (an mnsType or one of the
mnsCapability values) and by optimisation domain; `producer_for` is R1 Termination's routing choice for a class and DN (S1.8).
"""

import contextlib
import json
import logging
import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .. import mtls
from .dn import parse_dn, path_of
from .service import PROVMNS_VERSION

log = logging.getLogger("smo.mns.registry")

REGISTRY_DN = "SubNetwork=SMO,MnsRegistry=1"
REGISTRY_CLASSES = ("MnsRegistry", "MnsInfo", "MgmtDataInfo")
PROV_MNS = "PROV_MNS"
OPTIMISATION_DOMAINS = ("COVERAGE", "CAPACITY", "ENERGY_EFFICIENCY", "MOBILITY", "ACCESSIBILITY")
REGISTRY_PATH = f"/ProvMnS/{PROVMNS_VERSION}/{path_of(REGISTRY_DN)}"
READ_PARAMS = {"scope": json.dumps({"scopeType": "BASE_ALL"}), "limit": "500"}
READ_HEADERS = {"Accept": "application/vnd.3gpp.object-tree-flat+json"}
ADDRESS_ENV = "SMO_MNS_ADDRESS"
SWITCH_ENV = "SMO_MNS_REGISTRATION"
RETRY_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)          # the waits between attempts; the last repeats until registration succeeds


def mns_info_dn(module: str) -> str:
    """The DN of `module`'s MnsInfo."""
    return f"{REGISTRY_DN},MnsInfo={module}"


def mgmt_data_info_dn(module: str) -> str:
    """The DN of `module`'s MgmtDataInfo."""
    return f"{REGISTRY_DN},MgmtDataInfo={module}"


def default_address(module: str) -> str:
    """Where `module`'s ProvMnS is reached: `SMO_MNS_ADDRESS` when set, else `http://<module>:8000/ProvMnS/<version>` (`https://` with
    `SMO_MTLS=on`), the address of its compose service and of its chart Service."""
    configured = os.environ.get(ADDRESS_ENV, "").strip().rstrip("/")
    return configured or mtls.http_url(f"http://{module}:8000") + f"/ProvMnS/{PROVMNS_VERSION}"


def registration(module: str, classes: Iterable[str], address: str, *, scope: Iterable[str] = (), capabilities: Iterable[str] = (),
                 domains: Iterable[str] = ()) -> list[tuple[str, dict]]:
    """The two createMOI a producer sends to register, in order: (DN, Resource) of its MgmtDataInfo, then of its MnsInfo. `classes` are the classes it
    serves (at least one), `scope` the DNs its instances lie under (none: anywhere), `domains` its optimisation domains (OPTIMISATION_DOMAINS)."""
    served = sorted(set(classes))
    if not served:
        raise ValueError("a producer registers at least one class")
    wanted_domains = sorted(set(domains))
    unknown = sorted(set(wanted_domains) - set(OPTIMISATION_DOMAINS))
    if unknown:
        raise ValueError(f"not optimisation domains: {', '.join(unknown)}")
    data = {"supportedManagementData": [served, wanted_domains] if wanted_domains else [served]}
    info: dict[str, Any] = {"mnsLabel": module, "mnsType": PROV_MNS, "mnsVersion": PROVMNS_VERSION, "mnsAddress": address,
                            "mgmtDataInfoRef": [mgmt_data_info_dn(module)]}
    scoped = sorted(set(scope))
    if scoped:
        info["mnsScope"] = [scoped]
    if capabilities:
        info["mnsCapability"] = sorted(set(capabilities))
    return [(mgmt_data_info_dn(module), _resource("MgmtDataInfo", module, mgmt_data_info_dn(module), data)),
            (mns_info_dn(module), _resource("MnsInfo", module, mns_info_dn(module), info))]


def _resource(class_name: str, moi_id: str, dn: str, attributes: dict) -> dict:
    return {"id": moi_id, "objectClass": class_name, "objectInstance": dn, "attributes": attributes}


# ---------------------------------------------------------------------------------------------- reading it

@dataclass(frozen=True)
class MnsEntry:
    """One registered producer: its MnsInfo, joined with the classes and domains of the MgmtDataInfo it names."""

    dn: str
    label: str | None
    mns_type: str | None
    version: str | None
    address: str | None
    scope: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    classes: frozenset[str] = field(default_factory=frozenset)
    domains: frozenset[str] = field(default_factory=frozenset)


def _strings(value: Any) -> list[str]:
    """The strings of a list, or of a list of lists (one level), in order; anything else is none."""
    out: list[str] = []
    for item in value if isinstance(value, list) else []:
        out.extend(v for v in (item if isinstance(item, list) else [item]) if isinstance(v, str))
    return out


def entries(resources: Iterable[dict]) -> list[MnsEntry]:
    """The producers of a flat registry read (`resources`, TS 28.532 Resources of any of the three classes), in DN order: each MnsInfo with the
    classes and domains of every MgmtDataInfo its `mgmtDataInfoRef` names (a reference to nothing adds nothing)."""
    items = [r for r in resources if isinstance(r, dict)]
    data = {r.get("objectInstance"): _strings((r.get("attributes") or {}).get("supportedManagementData"))
            for r in items if r.get("objectClass") == "MgmtDataInfo"}
    out = []
    for r in items:
        if r.get("objectClass") != "MnsInfo":
            continue
        attributes = r.get("attributes") or {}
        values = [v for ref in _strings(attributes.get("mgmtDataInfoRef")) for v in data.get(ref, [])]
        out.append(MnsEntry(dn=str(r.get("objectInstance")), label=attributes.get("mnsLabel"), mns_type=attributes.get("mnsType"),
                            version=attributes.get("mnsVersion"), address=attributes.get("mnsAddress"),
                            scope=tuple(_strings(attributes.get("mnsScope"))), capabilities=tuple(_strings(attributes.get("mnsCapability"))),
                            classes=frozenset(v for v in values if v not in OPTIMISATION_DOMAINS),
                            domains=frozenset(v for v in values if v in OPTIMISATION_DOMAINS)))
    return sorted(out, key=lambda e: e.dn)


def discover(found: Iterable[MnsEntry], *, class_name: str | None = None, api_cat: str | None = None, domain: str | None = None) -> list[MnsEntry]:
    """The producers of `found` that serve `class_name`, whose mnsType or one of whose mnsCapability values is `api_cat`, and that name the
    optimisation domain `domain`; a criterion left as None is not applied."""
    return [e for e in found
            if (class_name is None or class_name in e.classes)
            and (api_cat is None or api_cat == e.mns_type or api_cat in e.capabilities)
            and (domain is None or domain in e.domains)]


def _under(dn: str, base: str) -> bool:
    """Whether `dn` is `base` or lies below it (RDN by RDN, so `SubNetwork=A1` is not under `SubNetwork=A`)."""
    rdns, prefix = parse_dn(dn), parse_dn(base)
    return rdns[:len(prefix)] == prefix


def producer_for(found: Iterable[MnsEntry], class_name: str, dn: str, version: str) -> MnsEntry | None:
    """The ProvMnS producer of `version` that serves `class_name` and whose scope holds `dn`, with an address (None when there is none). Among
    several, the one whose scope names `dn` most closely (the longest matching scope DN) wins, a producer with no scope coming last; then the first
    by DN. `dn` must be a valid DN (MnsError otherwise)."""
    best: tuple[int, str] | None = None
    chosen = None
    for e in discover(found, class_name=class_name, api_cat=PROV_MNS):
        if e.version != version or not e.address or e.mns_type != PROV_MNS:
            continue
        matching = [len(parse_dn(base)) for base in e.scope if _under(dn, base)]
        if e.scope and not matching:
            continue
        rank = (-(max(matching) if matching else 0), e.dn)
        if best is None or rank < best:
            best, chosen = rank, e
    return chosen


def next_page(link: str | None) -> str | None:
    """The URI of the next page in a getMOIAttributes `Link` header (`<uri>; rel="next"`), None when there is none."""
    if not link:
        return None
    for part in link.split(","):
        target, _, params = part.partition(";")
        if 'rel="next"' in params.replace(" ", ""):
            return target.strip().removeprefix("<").removesuffix(">")
    return None


def read(get: Callable[..., Any]) -> list[dict]:
    """Every resource of the registry, through `get(path, params=..., headers=...)` returning an httpx-like response (an R1Client's `get`, a
    TestClient's): the flat read of REGISTRY_PATH, page after page. Raises RuntimeError when a page is not a 200."""
    found: list[dict] = []
    path: str | None = REGISTRY_PATH
    params: dict | None = READ_PARAMS
    while path is not None:
        response = get(path, params=params, headers=READ_HEADERS)
        if response.status_code != 200:
            raise RuntimeError(f"the MnS registry answered {response.status_code} to {path}")
        found.extend(response.json())
        path, params = next_page(response.headers.get("link")), None
    return found


# ---------------------------------------------------------------------------------------------- registering at start-up

def register(module: str, classes: Iterable[str], *, put: Callable[..., Any] | None = None, address: str | None = None,
             scope: Iterable[str] = (), capabilities: Iterable[str] = (), domains: Iterable[str] = ()) -> None:
    """Registers `module` as the producer of `classes`: the two createMOI of `registration`, sent with `put(path, json=...)` (by default an
    R1Client's, so through R1 Termination with the module's own token). Raises RuntimeError when either is not a 200 or 201."""
    if put is None:
        from ..r1_client import R1Client          # imported here: the client reads its settings at import, a test passing its own `put` needs none
        put = R1Client().put
    for dn, body in registration(module, classes, address or default_address(module), scope=scope, capabilities=capabilities, domains=domains):
        response = put(f"/ProvMnS/{PROVMNS_VERSION}/{path_of(dn)}", json=body)
        if response.status_code not in (200, 201):
            raise RuntimeError(f"registering {dn} answered {response.status_code}")


class SelfRegistration:
    """`register` run in a background thread at start-up, repeated (RETRY_SECONDS apart) until it succeeds, since R1 Termination and SME may start
    after the module. `registered` is set once it has; `stop()` ends the attempts (at shutdown)."""

    def __init__(self, module: str, classes: Iterable[str], waits: tuple[float, ...] = RETRY_SECONDS, **options: Any):
        """Keeps what `register` is called with; nothing is sent until `start()`."""
        self.module, self.classes, self.options, self.waits = module, tuple(classes), options, waits
        self.registered = threading.Event()
        self.attempts = 0
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def attempt(self) -> bool:
        """One registration; True when it succeeded (a failure is logged, never raised)."""
        self.attempts += 1
        try:
            register(self.module, self.classes, **self.options)
        except Exception as exc:          # noqa: BLE001 (any failure is retried: a refused call, an unreachable gateway, a missing token)
            log.warning("MnS registry: registering %s failed (attempt %d): %s", self.module, self.attempts, exc)
            return False
        self.registered.set()
        log.info("MnS registry: %s registered as the producer of %s", self.module, ", ".join(sorted(self.classes)))
        return True

    def _run(self) -> None:
        while not self._stopping.is_set() and not self.attempt():
            self._stopping.wait(self.waits[min(self.attempts - 1, len(self.waits) - 1)])

    def start(self) -> None:
        """Starts the attempts in a daemon thread (once)."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name=f"mns-registration-{self.module}", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Ends the attempts; one in progress finishes."""
        self._stopping.set()

    def reset(self) -> None:
        """Forgets every attempt, so the next `start()` registers again (a test that starts the same app twice, on two databases)."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.registered.clear()
        self._stopping.clear()
        self._thread = None
        self.attempts = 0


def enabled() -> bool:
    """Whether a module registers at start-up: `SMO_MNS_REGISTRATION` (default `on`; `off`, `false`, `0` or `no` turn it off), read on every call."""
    return os.environ.get(SWITCH_ENV, "on").strip().lower() not in ("off", "false", "0", "no")


def install_self_registration(app, module: str, classes: Iterable[str], **options: Any) -> SelfRegistration:
    """Registers `module` as the producer of `classes` when `app` starts (in the background, repeated until it succeeds), unless SMO_MNS_REGISTRATION
    is off at that moment; stops trying when `app` shuts down. Wraps the app's lifespan (whatever it already does runs inside). Returns the
    SelfRegistration (a test waits on its `registered`)."""
    registration_ = SelfRegistration(module, classes, **options)
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(started_app):
        if enabled():
            registration_.start()
        try:
            async with inner(started_app) as state:
                yield state
        finally:
            registration_.stop()

    app.router.lifespan_context = lifespan
    return registration_
