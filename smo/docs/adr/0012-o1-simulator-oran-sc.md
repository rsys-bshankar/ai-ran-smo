# ADR 0012 — O1 simulator: O-RAN-SC sim-o1-ofhmp-interfaces

> **Status: decided and built 1 November 2026 (plan task S0.15, closed in `HISTORY.md` PR-CONV-S0.15).** The simulator runs in the compose profile
> `oran-sim` and as the chart's optional `oranSim` component, and the CI job "O-RAN-SC simulator" takes the three paths of S0.15 against the real RAN NF OAM.
> The mock O1 adaptor (`mock-o1-adaptor/`) stays for the unit and in-process mesh tests; netconf-lab (`netconf-lab/`) stays as the SSH/TLS lab.

Paths are relative to `smo/`. Written against `main` at 1dbc70f. The simulator facts below were read from its source at the commit named in section 2 and
checked by running the image this repository builds (section 3) next to RAN NF OAM. The architecture is [ADR 0008](0008-converged-architecture.md) (R16: the
previous O-RAN-SC simulator, sim-o1-interface, is deprecated in O-RAN-SC's N release); the interfaces are [ADR 0011](0011-standard-interfaces-only.md); the
NETCONF client is [ADR 0002](0002-netconf-over-ssh-client.md).

## 1. Decision

The O1 simulator of the converged SMO is **O-RAN-SC sim-o1-ofhmp-interfaces** ("PyNTS"), the successor of sim-o1-interface, at the head of its
`m-release` branch. Only its **simulated O-DU (O1)** runs: the O-RU open-fronthaul M-plane is out of Phase 1 (decision D20), so its O-RU image is not built
or run. It is built by this repository from the pinned source (section 3), because O-RAN-SC publishes no image of it.

## 2. The release

| | |
|---|---|
| Project | O-RAN-SC `sim/o1-ofhmp-interfaces` (gerrit.o-ran-sc.org; mirrored at github.com/o-ran-sc/sim-o1-ofhmp-interfaces); `INFO.yaml`: lifecycle *Incubation*, PTL Alex Stancu (highstreet technologies) |
| Release | Branch `m-release`, head **`12124176a491a873eb41423200c7f7add9ea99d4`** ("Fix for TLS issues", 3 October 2025). There is **no release tag**: the repository has none, and its newest release is this branch. `master` is the same code (its one newer commit adds a CI workflow) |
| Images upstream | `pynts-base`, `pynts-o-du-o1`, `pynts-o-ru-mplane`, built by its `Makefile` on the developer's machine (`NTS_VERSION=0.9.5` in its `.env`). **None is published**: no container-release definition, no tag to build one from |
| Licence | Apache-2.0 (`LICENSES`; every source file carries the header, copyright highstreet technologies and others) |
| What runs here | `oran-sim-du`: the O-DU image, built from `oran-sim/Dockerfile` (section 3) |

## 3. How the image is made (no published image: built from the pinned source)

The published images cannot be pulled from CI because there are none. As S0.15 allows, the image is **built from the tagged source** instead, in two
places that share one Dockerfile:

- **`oran-sim/Dockerfile`** is upstream's `base/Dockerfile` and `o-du-o1/Dockerfile` in one file, with four changes: (1) the base image is pinned,
  `ubuntu:22.04@sha256:076e7bf8…` (listed in `deploy/mirror/images.txt`, so CI pulls it from the GHCR mirror); (2) every file comes from the named build
  context `pynts`, the upstream repository at the commit above; (3) the data directory upstream bind-mounts at `/data` is copied in, so the image runs on its
  own in compose and in the chart; (4) its NETCONF server configuration is ours (section 5). Upstream's `# syntax=docker/dockerfile:1` line is left out: it
  would pull an unpinned BuildKit frontend.
- **Compose** builds it (`docker compose --profile oran-sim up --build`), the source pinned in `docker-compose.yml`
  (`ORAN_SIM_SOURCE`, default `https://github.com/o-ran-sc/sim-o1-ofhmp-interfaces.git#12124176…`). CI's job builds it the same way.
- **`.github/workflows/oran-sim-image.yml`** (the "mirror job": by hand and on a push to `main` that changes `oran-sim/`; never on a pull request)
  builds the same Dockerfile from the same source and publishes `ghcr.io/<owner>/<repo>/oran-sim-du:m-release-1212417`, the image the chart runs, and writes
  its digest to the job summary.

**What "pinned" means here.** The simulator's own image has no upstream digest to pin, so `images.txt` lists its pinned *base*, and the source is pinned by
commit. The build is not reproducible bit for bit, because upstream's build reads inputs it does not pin, and this file keeps them as upstream has them:
the Ubuntu packages (apt), the Python packages (unversioned `requirements.txt`), the libyang v3.4.2, sysrepo v2.11.7, sysrepo-python v1.7.3, libnetconf2
v3.5.1 and netopeer2 v2.2.31 sources (git tags; libyang-python is reset to commit 012d144) and the 3GPP YANG models (a forge.3gpp.org archive of tag
`Tag_Rel18_SA103`). Accepted for a test simulator: it is never part of a product install, and pinning each of these would be a fork of upstream's build.
Two runs of the workflow can therefore push two digests under one tag; the summary of each run says which.

## 4. What it simulates, and what it does not

**The simulated O-DU (O1)**, one per container (compose: `ORAN_SIM_COUNT`, default 2; chart: `oranSim.count`):

- **NETCONF** (Netopeer2 and sysrepo, as above) with the 3GPP NRM YANG of Rel-18 (`_3gpp-common-managed-element`, `_3gpp-nr-nrm-gnbdufunction`,
  `_3gpp-nr-nrm-nrcelldu`, `_3gpp-nr-nrm-nrsectorcarrier`, `_3gpp-nr-nrm-bwp`, `_3gpp-nr-nrm-rrmpolicy` and the common modules they need) and the O-RAN O-DU
  modules (`o-ran-aggregation-*`, the O-RU aggregation an O-DU exposes). Seed data: `ManagedElement-002` / `GNBDUFunction-001` / `NRCellDU-001`.
- **VES** to the URL it is given (here RAN NF OAM's listener, HTTP Basic): one `pnfRegistration` per NETCONF listen endpoint at start (the SSH one carries the
  address, port, user and password in `additionalFields`; the TLS one the key id), resent every 10 s until answered 2xx; and every PM log period (60 s) a
  **`stndDefined`** event, namespace `3GPP-PerformanceAssurance`, schema `TS28532_FileDataReportingMnS.yaml#components/schemas/NotifyFileReady`, whose
  `fileInfoList[0].fileLocation` is `sftp://netconf:<password>@<address>:22/ftp/A<start>-<end>_1_<hostname>.xml`.
- **PM files**: an XML measurement file per log period in `/ftp` (four counters, `RRC.ConnMean`, `RRC.ConnMax` and their `.226F04` variants), served
  over **SFTP** (OpenSSH, port 22, chrooted). It also configures **FTP** (vsftpd), but listening on IPv6 only, which compose's default network does not have,
  so here SFTP is the one transport; neither FTPES nor HTTPS is offered.
- A REST control API on 8080 (fault and PM status, start and stop).
- The VES `sourceName` of its PM events is the container's hostname; of a `pnfRegistration`, the hostname with `_<port>` appended when there are two
  endpoints (as here).

**What it does not do (m-release), each checked in its source and in a run:**

| Missing | Why | Consequence here |
|---|---|---|
| **No VES alarm** | Fault management is not started at start-up (`core.py` has the start commented out); the O-DU installs neither `ietf-alarms` nor `o-ran-fm`, so it is disabled; when it runs, it sends NETCONF notifications only, and the `stndDefined` `NotifyNewAlarm` class (`ves_alarm.py`) is never called. The O-DU's only other `stndDefined` events are O-RU notifications it relays over call-home | Path b) of S0.15 cannot be fed by the simulator: step B1 is XFAIL. S3.15's done-when ("simulator alarms raised, acknowledged, cleared and rebuilt") needs an alarm source decided in that row |
| No VES heartbeat | Commented out at start-up | RAN NF OAM's endpoint health is not fed by it |
| No NETCONF call-home to the SMO | The O-DU only listens for call-home *from* O-RUs (TLS, 4335, and that listener fails on an IPv4-only network: it opens an IPv6 socket); its call-home endpoint configuration is commented out | Registration is VES `pnfRegistration` only (section 6) |
| No O-CU | It simulates an O-DU and an O-RU, nothing else | S3.13's "ten simulated O-DU/O-CU nodes" gets O-DUs only from it |
| TLS server certificate made at start for `client.smo`, not for the address | `util/crypto.py` | Unusable by a client that checks the host name (section 5) |
| No published image and no release tag | Section 2 | Built here (section 3) |

**What sim-o1-interface (ntsim-ng) could do that this cannot** (from its source and documentation, `ntsimulator/ntsim-ng`, `docs/overview.rst`): a
**manager** that starts and stops simulated network functions at run time over its own NETCONF/YANG model (here the count is fixed when compose or the chart
starts them); **fault generation over NETCONF and over VES** with a configurable fault-delay list and counters; **VES heartbeat** with a period;
**NETCONF call-home** to the controller; `datastore-populate` (data generated from the YANG models) and `invoke-notification` (any YANG notification on
demand) and `invoke-ves-pm-file-ready` RPCs; web cut-through; a topology service image; and versioned releases (1.8.1, H release, with release notes).

## 5. How it is run here

- **Hardening.** Like every other third-party service: `cap_drop: [ALL]` and `no-new-privileges`. Its daemons run as root, so it then gets back only what
  they need: SETUID, SETGID, SYS_CHROOT and AUDIT_WRITE (sshd's privilege separation, the SFTP chroot and its login record), NET_BIND_SERVICE (ports 22 and
  830), CHOWN, DAC_OVERRIDE and FOWNER (the files sysrepo, sshd and the PM writer create), NET_RAW (it sends a DHCPDISCOVER at start, for zero-touch
  options, and stops if it cannot). Upstream runs it `privileged` with NET_ADMIN; neither is given. The root file system is writable (sysrepo's datastores,
  the PM files). No port is published on the host. Verified: with exactly these, NETCONF (830, 6513), SFTP (22) and the REST API start, and the VES
  events go out.
- **An SSH endpoint (change 4 of the Dockerfile).** Upstream's `/data/ietf-netconf-server-running.json` has only the TLS endpoint, and the server certificate
  the simulator makes at start names `client.smo`, not its address. RAN NF OAM's TLS client always checks that the certificate names the host (ADR 0002,
  `ran-nf-oam/app/netconf_tls.py`; there is no switch, on purpose), so it cannot use that endpoint, and making it able to is not this task (no module code is
  changed). `oran-sim/ietf-netconf-server-running.json` therefore adds an SSH endpoint on 830 (user `netconf`, the image's built-in password) beside the same
  TLS endpoint; the simulator then announces both in its `pnfRegistration`.
- **The password** `netconf!` is upstream's built-in, public one (its NETCONF user and its SFTP user); RAN NF OAM gets it as the credential `oransim`
  (`NETCONF_CRED_ORANSIM_PASSWORD`; compose: `ORAN_SIM_NETCONF_PASSWORD`; chart: a Secret it makes). The VES listener's password is the operator's
  (`RAN_NF_OAM_VES_PASSWORD`; chart: `ranNfOam.vesPasswordSecretRef`, which the simulators read too).

## 6. Registration with RAN NF OAM

The simulators register the way a real element does: **VES `pnfRegistration`** to RAN NF OAM's listener (the O-DU has no call-home to the SMO, section 4).
Today RAN NF OAM answers it `IGNORED DOMAIN_NOT_MAPPED`: acting on a `pnfRegistration` (and on call-home) is plan task **S8.2** (zero-touch provisioning).
Until then the job registers each element the way an operator does today, with the product's own route (`POST /o1-adaptor-endpoints`, then the host key
pinned with `PUT .../host-keys`), filled from what the `pnfRegistration` carried (address, port, user); no test-only route is used. The element is named
by the simulator's hostname, the `sourceName` of its PM events.

## 7. What passes today (CI job "O-RAN-SC simulator", `scripts/oran_sim_check.py`)

| Step | Today | Until |
|---|---|---|
| R1 `pnfRegistration` reaches the listener, 2xx | passes | |
| R2 RAN NF OAM makes an element of it | expected to fail | PR-CONV-S8.2 |
| R3 operator registration and host-key pin | passes | |
| a) A1 RAN NF OAM's NETCONF client connects (SSH, pinned key) and the simulator serves the 3GPP NRM modules | passes | |
| a) A2 the same client reads the ManagedElement (GNBDUFunction, NRCellDU) with get-config | passes (`ManagedElement-002` / `GNBDUFunction-001` / `NRCellDU-001`, administrativeState LOCKED) | |
| a) A3–A5 getMOIAttributes and modifyMOIAttributes on the NRCellDU through RAN NF OAM's ProvMnS, read back over NETCONF | expected to fail | PR-CONV-S3.3 |
| b) B1 the simulator posts an alarm as `stndDefined` 3GPP-FaultSupervision | expected to fail (the simulator sends none, section 4) | PR-CONV-S3.15 |
| b) B2 a simulator alarm in RAN NF OAM's alarm list | expected to fail | PR-CONV-S3.15 |
| c) C1 `notifyFileReady` as `stndDefined` 3GPP-PerformanceAssurance reaches the listener, 2xx | passes (answered `IGNORED PERF3GPP_FIELDS_INVALID`) | |
| c) C2 the named file is there over SFTP, with counters | passes | |
| c) C3–C4 RAN NF OAM fetches the file and holds the counters | expected to fail | PR-CONV-S3.16 |

An expected failure that starts to pass (XPASS) fails the job, so the PR that makes it pass also removes its marking.

## 8. Consequences

- S3.3, S3.15, S3.16 and S8.2 have a running element to prove themselves against; `OPEN_ITEMS.md` points their rows at the job's expected failures.
- S3.15 must decide where simulator alarms come from: a later simulator release, a contribution upstream (start fault management and send `NotifyNewAlarm`
  over VES), or the NETCONF notifications the simulator already sends.
- S3.13 (ten nodes) scales the O-DU count; the O-CU half needs another source.
- RAN NF OAM's TLS transport cannot use the simulator's TLS endpoint as long as the simulator's certificate does not name its address.
