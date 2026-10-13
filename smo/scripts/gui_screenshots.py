#!/usr/bin/env python3
"""Takes every screenshot of the operator GUI's README (`gui/docs/screenshots/`) from a running stack, by driving the console as an operator would.

What it is: the reproducible walk-through behind `gui/README.md` → "Screenshots". It signs in through the sign-in form, opens every page and tab,
and walks each module's lifecycle through the GUI's own forms, dialogs and buttons (onboard a package, deploy it, report a fault, recover,
upgrade, roll back, terminate; train → certify → deploy a model; raise, acknowledge and clear an alarm; …), saving a PNG at each state the README
names. Where a step needs another party the GUI does not stand in for, it acts as that party the way `DEMO_RUNBOOK.md` does: a new package version
is built from `samples/energy-saving-rapp` (`samples/build_csar.py`) and copied into the `r1-termination` container's CSAR server with
`docker compose cp`, and a training run that fails is reported through the BFF with the admin session (`POST /api/smo/aimgf/training-jobs/{id}/complete`).

How to run it (from `smo/`, against a stack started fresh and seeded with the four sample-rApp demos, as `gui/README.md` → "Screenshots" says):

    for d in energy-saving mobility-optimization coverage-optimization traffic-steering; do
      docker compose cp samples/$d-rapp/demo.py r1-termination:/tmp/$d-demo.py
      docker compose exec -T r1-termination python3 /tmp/$d-demo.py all
    done
    GUI_E2E_PASSWORD=$(docker compose exec -T gui-bff cat /data/initial-admin-password) \\
    GUI_E2E_OPERATOR_PASSWORD=... GUI_E2E_VIEWER_PASSWORD=... \\
    python scripts/gui_screenshots.py [--base-url http://localhost:3000] [--out gui/docs/screenshots] [--only lcm-rapps] \\
        [--chromium /path/to/chrome] [--compose "sudo docker compose"]

What it needs: Playwright for Python with a Chromium; the admin password in `GUI_E2E_PASSWORD` and the operator's and viewer's in
`GUI_E2E_OPERATOR_PASSWORD` / `GUI_E2E_VIEWER_PASSWORD` (the BFF's `GUI_OPERATOR_PASSWORD` / `GUI_VIEWER_PASSWORD`); `docker compose` able to
reach the stack (for the package copies; `--compose` changes the command); the CSAR server of `DEMO_RUNBOOK.md` step 1 on `r1-termination:8899`
(the demos start it). Each run makes its own names (a run id in package versions, model types, alarm ids, labels), so it can run again on the
same stack; the screens then show the earlier runs' rows too. A full run takes 15 to 20 minutes.

Groups (`--only`): `lcm` (every lifecycle group below, in order) or one of `lcm-rapps`, `lcm-aiml`, `lcm-alarms`, `lcm-kpis`, `lcm-intents`,
`lcm-infra`, `lcm-data`; then `pages`, `roles`, `flows` and `generic`. A full run does the lifecycle groups first, so the page, role and flow
screens show the state they leave. What it deliberately does not do: check anything (that is `gui_e2e.py`), or change the stack beyond what the
screens need. Before editing: the selectors are the pages' visible labels, roles and `data-section` ids (`gui/src/pages/*/sections`); when a
page's markup changes, this script and the README captions change with it.
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

SMO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SMO / "samples"))  # build_csar, the package builder the demos' CSARs come from
VIEWPORT = {"width": 1440, "height": 900}
MAX_HEIGHT = 7000  # a screenshot taller than this is cut; no page or drawer of the console comes near it
CSAR_DIR = "/srv/scratch"  # where DEMO_RUNBOOK.md step 1 serves packages from, inside r1-termination
CSAR_BASE = "http://r1-termination:8899"
DEMO_CSAR = f"{CSAR_BASE}/energy-saving-rapp.csar"  # the demo's own package: onboarding it again is a duplicate, so it fails validation
DEMO_ME = "gnb-du-demo-01"  # the managed element the energy-saving demo registers
LCM_GROUPS = ["lcm-rapps", "lcm-aiml", "lcm-alarms", "lcm-infra", "lcm-kpis", "lcm-intents", "lcm-data"]  # infra before kpis: its order is what a monitor watches
GROUPS = LCM_GROUPS + ["pages", "roles", "flows", "generic"]

# (file, user, path, theme): one full-page screenshot each, taken by `capture_pages`
PAGES = [
    ("pages/dashboard", "admin", "/", "dark"), ("pages/dashboard-light", "admin", "/", "light"),
    ("pages/flows", "admin", "/flows", "dark"),
    ("pages/rapp-directory", "admin", "/rapps#directory", "dark"), ("pages/rapps-instances", "admin", "/rapps#instances", "dark"),
    ("pages/rapps-packages", "admin", "/rapps#packages", "dark"), ("pages/rapps-rollouts", "admin", "/rapps#rollouts", "dark"),
    ("pages/approvals", "admin", "/approvals", "dark"), ("pages/decisions", "admin", "/decisions", "dark"),
    ("pages/safeguards", "admin", "/safeguards", "dark"),
    ("pages/aiml-models", "admin", "/aiml#models", "dark"), ("pages/aiml-training", "admin", "/aiml#training", "dark"),
    ("pages/aiml-inference", "admin", "/aiml#inference", "dark"), ("pages/aiml-features", "admin", "/aiml#features", "dark"),
    ("pages/aiml-groups", "admin", "/aiml#groups", "dark"), ("pages/aiml-mlmf", "admin", "/aiml#mlmf", "dark"),
    ("pages/aiml-registry", "admin", "/aiml#registry", "dark"),
    ("pages/policy-intents", "admin", "/policy#intents", "dark"), ("pages/policy-handlers", "admin", "/policy#handlers", "dark"),
    ("pages/policy-autonomy", "admin", "/policy#autonomy", "dark"), ("pages/policy-formulas", "admin", "/policy#formulas", "dark"),
    ("pages/alarms-ran", "admin", "/alarms#ran", "dark"), ("pages/alarms-ran-light", "admin", "/alarms#ran", "light"),
    ("pages/alarms-ocloud", "admin", "/alarms#ocloud", "dark"),
    ("pages/kpis-overview", "admin", "/kpis#overview", "dark"), ("pages/kpis-pm", "admin", "/kpis#pm", "dark"),
    ("pages/kpis-rapp", "admin", "/kpis#rapp", "dark"), ("pages/kpis-analytics", "admin", "/kpis#analytics", "dark"),
    ("pages/kpis-assurance", "admin", "/kpis#assurance", "dark"), ("pages/kpis-ocloud", "admin", "/kpis#ocloud", "dark"),
    ("pages/kpis-mlmf", "admin", "/kpis#mlmf", "dark"),
    ("pages/topology", "admin", "/topology", "dark"), ("pages/configuration", "admin", "/configuration", "dark"),
    ("pages/software", "admin", "/software", "dark"), ("pages/software-new", "admin", "/software#new", "dark"),
    ("pages/configuration-onboarding", "admin", "/configuration#onboarding", "dark"),
    ("pages/infra-topology", "admin", "/infrastructure#topology", "dark"), ("pages/infra-nfo", "admin", "/infrastructure#nfo", "dark"),
    ("pages/infra-ocloud", "admin", "/infrastructure#ocloud", "dark"), ("pages/infra-o1", "admin", "/infrastructure#o1", "dark"),
    ("pages/infra-orders", "admin", "/infrastructure#orders", "dark"),
    ("pages/data-dme", "admin", "/data#dme", "dark"), ("pages/data-offers", "admin", "/data#offers", "dark"),
    ("pages/data-sme", "admin", "/data#sme", "dark"),
    ("pages/preferences", "admin", "/preferences", "dark"), ("pages/preferences-light", "admin", "/preferences", "light"),
    ("pages/security", "admin", "/security", "dark"),
    ("pages/admin-users", "admin", "/admin#users", "dark"), ("generic/admin-audit-log", "admin", "/admin#audit", "dark"),
    ("pages/admin-msac", "admin", "/admin#msac", "dark"),
    ("pages/exports", "admin", "/exports", "dark"), ("pages/dashboard-scoped", "admin", "/?region=eu-west", "dark"),
    ("pages/alarms-scoped", "admin", "/alarms?region=eu-west&cluster=metro-a#ran", "dark"),
    ("pages/element", "admin", f"/elements/{DEMO_ME}", "dark"), ("pages/element-mos", "admin", f"/elements/{DEMO_ME}#mos", "dark"),
]
ROLE_PAGES = [
    ("roles/viewer-dashboard", "viewer", "/"), ("roles/viewer-alarm-list", "viewer", "/alarms"),
    ("roles/viewer-flows-06", "viewer", "/flows/06"), ("roles/viewer-rapp-packages", "viewer", "/rapps#packages"),
    ("roles/viewer-sme", "viewer", "/data#sme"),
    ("roles/operator-dashboard", "operator", "/"), ("roles/operator-rapp-instances", "operator", "/rapps#instances"),
    ("roles/operator-dme", "operator", "/data#dme"), ("roles/operator-infrastructure", "operator", "/infrastructure#nfo"),
]
# a target the framework's one intent handler (SA SMOS's O1 CM handler) declares it can fulfil: put a cell into energy saving
ENERGY_TARGET = '[{"targetName": "CESManagementFunction.energySavingControl", "targetCondition": "IS_EQUAL_TO", "targetValueRange": "TO_BE_ENERGY_SAVING"}]'
FLOWS = ["01", "02", "03", "04", "06", "07", "08", "09", "10", "15", "16", "19"]


class Walk:
    """One walk-through: the browser, a signed-in page per (user, theme), the output folder and the run id that keeps this run's names unique.

        `page(user)` signs in on first use (through the form, like a person) and reuses the session after; `shot` writes one PNG. Holds no state
        of the stack beyond the ids a group passes to a later one in `ctx`.
    """

    def __init__(self, browser, base: str, out: Path, passwords: dict[str, str], compose: list[str]):
        """Keeps the arguments; `compose` is the command that runs `docker compose` against the stack (split into words)."""
        self.browser, self.base, self.out, self.passwords, self.compose = browser, base, out, passwords, compose
        self.run = str(int(time.time()) % 1000000)  # seconds since the epoch, cut to six digits: unique per run, short enough for a version
        self.sessions: dict[tuple[str, str], object] = {}
        self.ctx: dict[str, str] = {}
        self.saved: list[str] = []
        self.failed: list[str] = []

    # ------------------------------------------------------------ sessions, navigation, screenshots

    def page(self, user: str = "admin", theme: str = "dark"):
        """The signed-in page of `user` in `theme`, signing in through the form the first time; the theme is put in the console's saved preferences key before load."""
        key = (user, theme)
        if key not in self.sessions:
            context = self.browser.new_context(viewport=VIEWPORT)
            # the console reads its theme from this key before the BFF's preferences arrive, so the first paint is already in the wanted theme
            context.add_init_script(f"try{{localStorage.setItem('smo.prefs', JSON.stringify({{theme:'{theme}'}}))}}catch(e){{}}")
            page = context.new_page()
            page.set_default_timeout(30000)
            page.on("dialog", lambda d: d.accept())  # the console's window.confirm() before destructive calls: always yes
            page.goto(self.base + "/login")
            page.get_by_label("Username").fill(user)
            page.get_by_label("Password").fill(self.passwords[user])
            page.get_by_role("button", name="Sign in").click()
            page.get_by_role("navigation", name="Main").wait_for()
            self.sessions[key] = page
        return self.sessions[key]

    def goto(self, page, path: str, settle: int = 2500):
        """Opens `path` in the console and waits for its heading and for the data to arrive (`settle` ms, then until no skeleton shows)."""
        if page.url == self.base + path:
            page.reload()  # the same address with a hash is not a navigation: reload, so filters and open drawers start fresh
        else:
            page.goto(self.base + path)
        page.get_by_role("heading").first.wait_for()
        self.settle(page, settle)

    @staticmethod
    def settle(page, ms: int = 1500):
        """Waits `ms`, then up to 10 s more while a loading skeleton is still on screen."""
        page.wait_for_timeout(ms)
        try:
            page.wait_for_function("() => !document.querySelector('.skeleton, .skel')", timeout=10000)
        except PlaywrightTimeout:
            pass  # a section that never loads is visible in the screenshot, which is the point

    def shot(self, page, name: str, *, theme: str = "dark"):
        """Saves `<out>/<name>.png`: the whole page, and the whole of an open drawer or dialog.

            The viewport is grown to the content's height first (the sidebar and a drawer are as tall as the viewport, so a plain full-page
            shot would cut a drawer and the sidebar), then put back. `theme` "light" also sets it on <html>, as the ThemeProvider does.
        """
        if theme == "light":
            page.evaluate("() => { document.documentElement.dataset.theme = 'light'; }")
        # inner scrollers (a JSON block in a drawer, a textarea) are let out to their full height, so the screenshot shows what they hold
        page.evaluate("""() => {
            for (const el of document.querySelectorAll('.drawer-body pre, .modal-body pre, textarea')) {
                el.style.maxHeight = 'none';
                el.style.flexShrink = '0';  // a drawer body is a flex column: without this the block is squeezed back
                if (el.tagName === 'TEXTAREA' && el.scrollHeight > el.clientHeight) el.style.height = (el.scrollHeight + 4) + 'px';
            }
            window.scrollTo(0, 0);
        }""")
        height = VIEWPORT["height"]
        for _ in range(5):  # growing the viewport can grow the content (min-height: 100vh), so measure again until it fits
            need = page.evaluate("""() => {
                let h = document.documentElement.scrollHeight;
                for (const el of document.querySelectorAll('.sidebar, .drawer-body, .modal-body, .modal')) {
                    const extra = el.scrollHeight - el.clientHeight;
                    if (extra > 1) h = Math.max(h, window.innerHeight + extra + 24);
                }
                return Math.ceil(h);
            }""")
            need = min(need, MAX_HEIGHT)
            if need <= height:
                break
            height = need
            page.set_viewport_size({"width": VIEWPORT["width"], "height": height})
            page.wait_for_timeout(400)
        path = self.out / f"{name}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path))
        page.set_viewport_size(VIEWPORT)
        self.saved.append(name)
        print("saved", name, flush=True)

    def step(self, name: str, fn, *args):
        """Runs one screenshot step `fn(*args)` and records a failure instead of stopping the walk-through; returns what `fn` returned, or None."""
        try:
            return fn(*args)
        except (PlaywrightError, PlaywrightTimeout, RuntimeError, KeyError, IndexError, StopIteration) as exc:
            self.failed.append(f"{name}: {str(exc).splitlines()[0]}")
            print("FAILED", name, str(exc).splitlines()[0], file=sys.stderr, flush=True)
            return None

    # ------------------------------------------------------------ the BFF and the stack, for what the GUI does not stand in for

    def api(self, method: str, path: str, *, json=None, params=None, user: str = "admin"):
        """Calls the BFF's module proxy (`/api/smo<path>`) with `user`'s session and CSRF token; returns the JSON body (None when empty).

            Raises RuntimeError on a 4xx/5xx answer, with the status and the start of the body.
        """
        page = self.page(user)
        csrf = next((c["value"] for c in page.context.cookies() if c["name"] == "smo_csrf"), "")
        url = self.base + "/api/smo" + path
        resp = page.request.fetch(url, method=method, data=json, params=params, headers={"X-CSRF-Token": csrf})
        if resp.status >= 400:
            raise RuntimeError(f"{method} {path} → {resp.status}: {resp.text()[:300]}")
        body = resp.body()
        return resp.json() if body else None

    def items(self, path: str, **params) -> list[dict]:
        """The rows of a module list route: its `items` when it is paged, the list itself otherwise (up to 500 rows)."""
        data = self.api("GET", path, params={"limit": 500, **params})
        return data.get("items", []) if isinstance(data, dict) else (data or [])

    def wait_for(self, what: str, fn, timeout: float = 60.0):
        """Polls `fn()` every second until it returns something truthy and returns that; raises RuntimeError naming `what` after `timeout` s."""
        end = time.time() + timeout
        while time.time() < end:
            value = fn()
            if value:
                return value
            time.sleep(1)
        raise RuntimeError(f"timed out waiting for {what}")

    def serve_csar(self, sample: str, version: str) -> str:
        """Builds `samples/<sample>` as version `version` and copies it into r1-termination's CSAR server; returns its URL there.

            The version is written into `manifest.yaml` and the ASD's `application_version`, so the package's content hash (Onboarding's duplicate
            check) differs from every other build. Runs `<compose> cp` in `smo/`; raises RuntimeError if the copy fails.
        """
        import build_csar  # samples/build_csar.py: signs with the demo publisher key, like the committed CSARs

        name = f"{sample}-{version}"
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / sample
            for rel, content in build_csar.source_files(sample).items():
                target = src / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if rel in ("manifest.yaml", "Definitions/asd.yaml"):
                    text = content.decode()
                    text = re.sub(r"(?m)^version: .*$", f"version: {version}", text)
                    text = re.sub(r'application_version: "[^"]*"', f'application_version: "{version}"', text)
                    content = text.encode()
                target.write_bytes(content)
            csar = Path(tmp) / f"{name}.csar"
            csar.write_bytes(build_csar.build_bytes(sample, source=src))
            done = subprocess.run([*self.compose, "cp", str(csar), f"r1-termination:{CSAR_DIR}/{name}.csar"], cwd=SMO, capture_output=True, text=True)
            if done.returncode:
                raise RuntimeError(f"copying {name}.csar into r1-termination failed: {done.stderr.strip()}")
        return f"{CSAR_BASE}/{name}.csar"

    # ------------------------------------------------------------ GUI helpers

    @staticmethod
    def section(page, section_id: str):
        """The box with `data-section=<section_id>`."""
        return page.locator(f'[data-section="{section_id}"]')

    @staticmethod
    def click(page, locator, settle: int = 1200, dispatch: bool = False):
        """Clicks `locator` (a button that calls the BFF) and waits for that call's answer, then `settle` ms for the tables to refetch.

            `dispatch` sends the click event instead of a pointer click. Raises RuntimeError when the call answered 4xx/5xx.
        """
        with page.expect_response(lambda r: "/api/" in r.url and r.request.method != "GET", timeout=60000) as answer:
            if dispatch:  # a button the drawer's own layout overlaps (the usage table's Stop): send the click event to it directly
                locator.dispatch_event("click")
            else:
                locator.click()
        resp = answer.value
        if resp.status >= 400:  # the console shows it as a toast; the walk-through must not go on as if it had worked
            raise RuntimeError(f"{resp.request.method} {resp.url} → {resp.status}: {resp.text()[:300]}")
        page.wait_for_timeout(settle)

    @staticmethod
    def row(scope, text: str):
        """The first table row in `scope` that contains `text`."""
        return scope.locator("tbody tr", has_text=text).first

    @staticmethod
    def open_row(scope, text: str, cell: int = 2):
        """Clicks cell `cell` of the first row containing `text` (a plain cell: an id cell copies itself, a button cell acts)."""
        Walk.row(scope, text).locator("td").nth(cell).click()

    def drawer(self, page):
        """The open drawer or dialog."""
        return page.get_by_role("dialog").last


# ============================================================ lifecycle groups


def lcm_rapps(w: Walk):
    """rApps: a package that fails validation, onboarding a new version, deploy, prime, fault → recover, upgrade, roll back, terminate, then the package
    released, deprimed, deprecated and deleted (README "rApps: packages and instances"). Leaves a second version PRIMED with an active usage
    registration, for flow 06's "guard blocking" screen.
    """
    p = w.page()
    v1, v2 = f"1.1.{w.run}", f"1.2.{w.run}"
    url1, url2 = w.serve_csar("energy-saving-rapp", v1), w.serve_csar("energy-saving-rapp", v2)

    def package(version):
        # the package row of this run's version, once Onboarding has validated it
        return next((x for x in w.items("/onboarding/packages") if x.get("version") == version and x["state"] != "ONBOARDING"), None)

    def instance(iid):
        return w.api("GET", f"/rapp-mgmt/instances/{iid}")

    def onboard(url):
        form = w.section(p, "rapps.onboard")
        form.get_by_label("CSAR location").fill(url)
        w.click(p, form.get_by_role("button", name="Onboard"))

    # a duplicate of the demo's package: Onboarding rejects the same content hash, so the package ends FAILED
    w.goto(p, "/rapps#packages")
    before = {x["packageId"] for x in w.items("/onboarding/packages", state="FAILED")}
    onboard(DEMO_CSAR)
    failed = w.wait_for("the duplicate package to fail", lambda: [x for x in w.items("/onboarding/packages", state="FAILED") if x["packageId"] not in before])
    w.ctx["failed_package"] = failed[0]["packageId"]
    w.goto(p, "/rapps#packages")
    w.section(p, "rapps.packages").get_by_role("button", name=re.compile("^FAILED")).click()
    w.settle(p)
    w.shot(p, "lcm/rapps-package-failed")

    # onboard version 1.1 through the form
    w.goto(p, "/rapps#packages")
    w.section(p, "rapps.onboard").get_by_label("CSAR location").fill(url1)
    w.shot(p, "lcm/rapps-onboard-form")
    w.click(p, w.section(p, "rapps.onboard").get_by_role("button", name="Onboard"))
    pkg1 = w.wait_for("version 1.1 to be validated", lambda: package(v1))
    w.goto(p, "/rapps#packages")
    w.shot(p, "lcm/rapps-package-onboarded")
    table = w.section(p, "rapps.packages")
    w.open_row(table, v1, 3)
    w.drawer(p).get_by_role("heading", name="Usage registrations", exact=False).wait_for()
    w.settle(p, 800)
    w.shot(p, "lcm/rapps-package-drawer")
    p.keyboard.press("Escape")

    # deploy it as an ASSIST instance (the autonomy group dispatches for it later)
    w.row(table, v1).get_by_role("button", name="Deploy").click()
    dlg = w.drawer(p)
    dlg.get_by_label("Instance configuration").fill('{"managedElementRef": "gnb-du-demo-01", "cells": ["101", "102"]}')
    dlg.get_by_label("Autonomy mode").select_option("ASSIST")
    w.shot(p, "lcm/rapps-deploy-dialog")
    w.click(p, dlg.get_by_role("button", name="Deploy"))
    inst = w.wait_for("the instance", lambda: [x for x in w.items("/rapp-mgmt/instances") if x["packageId"] == pkg1["packageId"]])[0]["instanceId"]
    w.ctx["assist_instance"] = inst

    # prime the package: the ASSIST instance's usage registration blocks deprime (call flow 06a)
    w.goto(p, "/rapps#packages")
    w.open_row(w.section(p, "rapps.packages"), v1, 3)
    d = w.drawer(p)
    w.click(p, d.get_by_role("button", name="Prime"))
    w.wait_for("the package to be PRIMED", lambda: package(v1)["state"] == "PRIMED")
    w.settle(p, 1500)
    w.shot(p, "lcm/rapps-package-primed-deprime-blocked")
    p.keyboard.press("Escape")

    def open_instance(iid):
        # the instance drawer, from the Instances tab
        w.goto(p, "/rapps#instances", 5500)  # past the summary's 5 s cache, so the tiles agree with the drawer
        w.open_row(w.section(p, "rapps.instances"), iid[:8], 2)
        d = w.drawer(p)
        d.get_by_role("heading", name="Performance").wait_for()
        w.settle(p, 1000)
        return d

    # bootstrap, report performance, then a critical fault → FAULTED → Recover → DEPLOYING → bootstrap again
    d = open_instance(inst)
    w.click(p, d.get_by_role("button", name="Mark bootstrapped"))
    d.locator("summary", has_text="inject test reports").click()
    for metrics in ('{"throughputMbps": 118, "latencyMs": 9}', '{"throughputMbps": 126, "latencyMs": 8}', '{"throughputMbps": 131, "latencyMs": 7}'):
        d.get_by_label("Performance metrics (JSON)").fill(metrics)
        w.click(p, d.get_by_role("button", name="Report performance"), 600)
    d.get_by_label("Fault severity").select_option("minor")
    d.get_by_label("Description").fill("PRB report delayed by 2 granularity periods")
    w.click(p, d.get_by_role("button", name="Report fault"))
    w.settle(p, 1500)
    w.shot(p, "lcm/rapps-instance-drawer")
    d.get_by_label("Fault severity").select_option("critical")
    d.get_by_label("Description").fill("rApp lost its DME data job: no PRB input")
    w.click(p, d.get_by_role("button", name="Report fault"))
    w.wait_for("FAULTED", lambda: instance(inst)["state"] == "FAULTED")
    d = open_instance(inst)
    w.shot(p, "lcm/rapps-instance-faulted")
    w.click(p, d.get_by_role("button", name="Recover"))
    w.wait_for("DEPLOYING", lambda: instance(inst)["state"] == "DEPLOYING")
    d = open_instance(inst)
    w.shot(p, "lcm/rapps-instance-recovering")
    w.click(p, d.get_by_role("button", name="Mark bootstrapped"))
    d = open_instance(inst)
    w.shot(p, "lcm/rapps-instance-recovered")

    # upgrade to version 1.2: dialog, UPGRADING, the replacement DEPLOYING, then committed
    w.goto(p, "/rapps#packages")
    onboard(url2)
    pkg2 = w.wait_for("version 1.2 to be validated", lambda: package(v2))
    d = open_instance(inst)
    d.get_by_role("button", name="Upgrade…").click()
    up = p.get_by_role("dialog").filter(has_text="Upgrade instance")
    up.get_by_label("New package").select_option(label=f"EnergySaving_rApp {v2}")
    w.shot(p, "lcm/rapps-upgrade-dialog")
    w.click(p, up.get_by_role("button", name="Start upgrade"))
    w.wait_for("UPGRADING", lambda: instance(inst)["state"] == "UPGRADING")
    d = open_instance(inst)
    w.shot(p, "lcm/rapps-upgrade-in-progress")
    replacement = instance(inst)["pendingUpgradeInstanceId"]
    d = open_instance(replacement)
    w.shot(p, "lcm/rapps-upgrade-replacement-deploying")
    w.click(p, d.get_by_role("button", name="Mark bootstrapped"))
    d = open_instance(inst)
    w.click(p, d.get_by_role("button", name="Upgrade succeeded"))
    upgraded = w.wait_for("the upgraded instance", lambda: [x for x in w.items("/rapp-mgmt/instances", state="RUNNING") if x["packageId"] == pkg2["packageId"]])[0]["instanceId"]
    d = open_instance(upgraded)
    w.shot(p, "lcm/rapps-upgrade-committed")

    # roll back to 1.1: UPGRADING again, then committed with ROLLBACK in the history
    w.click(p, d.get_by_role("button", name="Roll back"))
    w.wait_for("the rollback to start", lambda: instance(upgraded)["state"] == "UPGRADING")
    d = open_instance(upgraded)
    w.shot(p, "lcm/rapps-rollback-in-progress")
    back = instance(upgraded)["pendingUpgradeInstanceId"]
    d = open_instance(back)
    w.click(p, d.get_by_role("button", name="Mark bootstrapped"))
    d = open_instance(upgraded)
    w.click(p, d.get_by_role("button", name="Upgrade succeeded"))
    final = w.wait_for("the rolled-back instance", lambda: [x for x in w.items("/rapp-mgmt/instances", state="RUNNING") if x["packageId"] == pkg1["packageId"]])[0]["instanceId"]
    d = open_instance(final)
    w.shot(p, "lcm/rapps-rollback-committed")
    w.ctx["rollback_instance"] = final

    # terminate it: NFO tears the workload down and the usage registration stops
    d = open_instance(final)
    w.click(p, d.get_by_role("button", name="Terminate"))
    w.wait_for("UNDEPLOYED", lambda: instance(final)["state"] == "UNDEPLOYED")
    d = open_instance(final)
    w.shot(p, "lcm/rapps-instance-terminated")

    # terminating stopped the usage registrations (call flow 06): deprime, deprecate, delete
    w.goto(p, "/rapps#packages")
    w.open_row(w.section(p, "rapps.packages"), v1, 3)
    d = w.drawer(p)
    usage = f"/onboarding/packages/{pkg1['packageId']}/usage"
    w.wait_for("the usage to stop", lambda: not [u for u in w.items(usage) if u["active"]])
    d.get_by_role("button", name="Deprime").and_(d.locator(":enabled")).wait_for()
    w.settle(p, 1000)
    w.shot(p, "lcm/rapps-package-primed-usage-released")
    w.click(p, d.get_by_role("button", name="Deprime"))
    w.wait_for("AVAILABLE again", lambda: package(v1)["state"] == "AVAILABLE")
    w.settle(p, 1000)
    w.shot(p, "lcm/rapps-package-deprimed")
    p.keyboard.press("Escape")
    w.click(p, w.row(w.section(p, "rapps.packages"), v1).get_by_role("button", name="Deprecate"))
    w.open_row(w.section(p, "rapps.packages"), v1, 3)
    w.settle(p, 1000)
    w.shot(p, "lcm/rapps-package-deprecated")
    p.keyboard.press("Escape")
    w.click(p, w.row(w.section(p, "rapps.packages"), v1).get_by_role("button", name="Delete"))
    w.wait_for("DELETING", lambda: package(v1)["state"] == "DELETING")
    w.goto(p, "/rapps#packages")
    w.open_row(w.section(p, "rapps.packages"), v1, 3)
    w.settle(p, 1000)
    w.shot(p, "lcm/rapps-package-delete-requested")
    p.keyboard.press("Escape")

    # an instance that keeps its fault history (upgrade commits delete the instance they replace): flow 07 follows it
    _fault_story(w, pkg2["packageId"])
    # leave 1.2 PRIMED with an instance holding it (an active usage registration): flow 06's guard-blocking screen follows it
    w.api("POST", f"/onboarding/packages/{pkg2['packageId']}/prime")
    w.api("POST", "/rapp-mgmt/instances", json={"packageId": pkg2["packageId"], "config": {}})
    w.ctx["blocked_package"] = pkg2["packageId"]


def _fault_story(w: Walk, package_id: str) -> str:
    """Deploys an instance of `package_id` and takes it through performance reports, a minor and a critical fault, recover and bootstrap, through
    the BFF as the rApp itself would report (the same calls the instance drawer's admin tools make); returns its id. Not pictured itself: flow 07's
    board follows it, so the board shows every step of call flow 07 up to Terminate."""
    iid = w.api("POST", "/rapp-mgmt/instances", json={"packageId": package_id, "config": {"managedElementRef": DEMO_ME}, "autonomyMode": "SHADOW"})["instanceId"]
    base = f"/rapp-mgmt/instances/{iid}"
    w.api("POST", f"{base}/bootstrap-complete")
    for metrics in ({"throughputMbps": 121, "latencyMs": 8}, {"throughputMbps": 117, "latencyMs": 9}):
        w.api("POST", f"{base}/performance", json=metrics)
    w.api("POST", f"{base}/fault", params={"severity": "minor", "description": "PRB report delayed"})
    w.api("POST", f"{base}/fault", params={"severity": "critical", "description": "rApp lost its DME data job"})
    w.api("POST", f"{base}/recover")
    w.api("POST", f"{base}/bootstrap-complete")
    return iid


def _lifecycle(w: Walk, model_id: str) -> dict:
    """AIMgF's lifecycle row of `model_id`, or a REGISTERED stand-in when AIMgF has none yet."""
    return next((x for x in w.items("/aimgf/model-lifecycles") if x["modelId"] == model_id),
                {"modelLifecycleState": "REGISTERED", "runtimeLifecycleState": "NOT_DEPLOYED", "clearedNodeGroups": [], "trainingJobId": None})


def lcm_aiml(w: Walk):
    """AI/ML: register a model in the dialog and walk it through every gate to PROMOTED with an ACTIVE runtime and inference jobs; a second model
    whose training fails and is retried; a coordination group of the two; MLMF reports under a floor; a feature group (README "AI/ML")."""
    p = w.page()
    mtype = f"CellLoadForecaster-{w.run}"
    w.goto(p, "/aiml#models")
    p.get_by_role("button", name="Register model").click()
    dlg = p.get_by_role("dialog")
    for label, value in (("Model type", mtype), ("Description", "Per-cell PRB load, 15-minute horizon"), ("Owner", "ran-optimisation"),
                         ("Author", "smo-gui walk-through"), ("Required resource type", "GPU"),
                         ("Input data type", "RAN.PMCounters.PRB_UTILIZATION"), ("Output data type", "PRB_FORECAST")):
        dlg.get_by_label(label).fill(value)
    w.shot(p, "lcm/aiml-register-model-dialog")
    w.click(p, dlg.get_by_role("button", name="Register"))
    mid = w.wait_for("the model", lambda: next((m["modelId"] for m in w.items("/mlmr/models") if m["modelType"] == mtype), None))
    w.ctx["model"] = mid

    def state():
        return _lifecycle(w, mid)["modelLifecycleState"]

    def select(name=mtype):
        # the model's card on the board, so the detail, governance, artifacts and runtime boxes show it
        w.goto(p, "/aiml#models", 3000)
        card = p.get_by_role("button", name=re.compile(re.escape(name)))
        if not card.count():  # a column shows its first five cards: past them, pick the model in the table view
            p.get_by_role("radio", name="Table").click()
            w.section(p, "aiml.table").get_by_label("Filter by model type").fill(name)
            w.settle(p, 1500)
            card = w.row(w.section(p, "aiml.table"), name).locator("td").nth(2)
        card.first.click()
        w.section(p, "aiml.detail").get_by_role("heading").first.wait_for()
        w.settle(p, 2000)

    def act(label, want, box="aiml.detail"):
        # one lifecycle button, then wait until AIMgF reports `want`
        w.click(p, w.section(p, box).get_by_role("button", name=label, exact=True))
        w.wait_for(want, lambda: state() == want if box == "aiml.detail" else True)

    select()
    act("Request training", "TRAINING")
    select()
    w.shot(p, "lcm/aiml-model-training")
    act("Training complete", "TRAINED")
    select()
    w.shot(p, "lcm/aiml-model-trained-approval-gate")
    act("Approve training", "TRAINED")
    select()
    act("Request validation", "VALIDATING")
    select()
    w.shot(p, "lcm/aiml-model-validating")
    act("Validation complete", "VALIDATED")
    select()
    w.shot(p, "lcm/aiml-model-validated-approval-gate")
    act("Approve validation", "VALIDATED")
    select()
    act("Request emulation", "EMULATING")
    select()
    act("Emulation complete", "EMULATED")
    select()
    w.shot(p, "lcm/aiml-model-emulated-submit")
    act("Submit for approval", "PENDING_APPROVAL")
    select()
    w.shot(p, "lcm/aiml-model-pending-approval")
    act("Approve", "APPROVED")
    select()
    act("Certify", "CERTIFIED")
    select()
    w.shot(p, "lcm/aiml-model-certified")
    act("Promote", "PROMOTED")
    select()
    art = w.section(p, "aiml.artifacts")
    art.get_by_label("Deploy to node groups").fill("edge-gpu-a, edge-gpu-b")
    w.shot(p, "lcm/aiml-model-promoted-deploy-node-groups")
    w.click(p, art.get_by_role("button", name="Deploy", exact=True))
    w.wait_for("the cleared node groups", lambda: _lifecycle(w, mid)["clearedNodeGroups"])
    select()
    w.shot(p, "lcm/aiml-model-node-groups-cleared")

    def runtime():
        return _lifecycle(w, mid)["runtimeLifecycleState"]

    rt = w.section(p, "aiml.runtime")
    w.click(p, rt.get_by_role("button", name="Deploy runtime"))
    w.wait_for("the runtime to deploy", lambda: runtime() == "DEPLOYED")
    select()
    w.shot(p, "lcm/aiml-model-runtime-deployed")
    rt = w.section(p, "aiml.runtime")
    w.click(p, rt.get_by_role("button", name="Activate"))
    w.wait_for("the runtime to activate", lambda: runtime() == "ACTIVE")
    select()
    # AIMgF fails an inference job left RUNNING for 5 s (its deadline), so the screen and the resolution follow the request without a reload
    def infer(outcome, shot_name=None):
        rt = w.section(p, "aiml.runtime")
        w.click(p, rt.get_by_role("button", name="Request inference job"), 300)
        button = rt.get_by_role("button", name=outcome, exact=True).first
        button.wait_for(timeout=4000)
        if shot_name:
            w.shot(p, shot_name)
        w.click(p, button, 800)

    infer("Completed", "lcm/aiml-model-runtime-active-inference-requested")
    infer("Failed")
    select()
    w.shot(p, "lcm/aiml-model-promoted-runtime-active")
    rt = w.section(p, "aiml.runtime")
    rt.get_by_role("button", name=re.compile("^(Write back|Update)$")).first.click()
    metrics = p.get_by_role("dialog").filter(has_text="Write back model metrics")
    metrics.get_by_label("Model metrics (JSON)").fill('{\n  "accuracy": 0.94,\n  "mae": 3.1,\n  "loss": 0.11\n}')
    w.shot(p, "lcm/aiml-training-metrics-dialog")
    w.click(p, metrics.get_by_role("button", name="Save"))
    w.goto(p, "/aiml#inference")
    w.shot(p, "lcm/aiml-inference-job-running")

    # a second model whose training run fails (reported as the trainer would), then Retry training
    mtype2 = f"HandoverFailurePredictor-{w.run}"
    mid2 = w.api("POST", "/mlmr/models", json={"modelType": mtype2, "version": "1.0.0", "description": "Too-late handovers per cell pair",
                                                 "owner": "ran-optimisation", "inputDataType": "RAN.PMCounters.HO_PERFORMANCE"})["modelId"]
    select(mtype2)
    w.click(p, w.section(p, "aiml.detail").get_by_role("button", name="Request training"))
    job = w.wait_for("the training job", lambda: _lifecycle(w, mid2)["trainingJobId"])
    w.api("POST", f"/aimgf/training-jobs/{job}/complete", json={"succeeded": False})
    w.wait_for("FAILED", lambda: _lifecycle(w, mid2)["modelLifecycleState"] == "FAILED")
    select(mtype2)
    w.click(p, w.section(p, "aiml.detail").get_by_role("button", name="Retry training"))
    w.wait_for("the retrain", lambda: _lifecycle(w, mid2)["modelLifecycleState"] == "TRAINING")
    select(mtype2)
    w.shot(p, "lcm/aiml-model-retrain-requested")

    # a coordination group of the two models
    w.goto(p, "/aiml#groups")
    form = w.section(p, "aiml.newGroup")
    form.get_by_label("Member models").select_option([mid, mid2])
    form.get_by_label("Use cases").fill("load-forecast, handover-robustness")
    w.shot(p, "lcm/aiml-coordination-group-form")
    w.click(p, form.get_by_role("button", name="Create"))
    w.ctx["group"] = next(g["groupId"] for g in w.items("/mlmr/coordination-groups") if mid in g["memberModelIds"])

    # MLMF: subscribe the promoted model with a floor, then reports above and under it
    prb = next(t["dmeTypeId"] for t in w.items("/dme/dme-types") if t["typeName"] == "RAN.PMCounters.PRB_UTILIZATION")
    w.goto(p, "/aiml#mlmf")
    form = w.section(p, "aiml.mlmfSubscribe")
    form.get_by_label("Model").select_option(mid)
    form.get_by_label("DME type").fill(prb)
    form.get_by_label("Guard KPI floor").fill('{"accuracy": 0.9}')
    w.click(p, form.get_by_role("button", name="Subscribe", exact=True))
    w.goto(p, "/aiml#mlmf")
    w.open_row(w.section(p, "aiml.mlmf"), mtype, 2)
    reports = w.section(p, "aiml.mlmfReports")
    reports.locator("summary").click()
    for value in ("0.95", "0.93", "0.82"):
        reports.get_by_label("Metrics (JSON)").fill(f'{{"accuracy": {value}}}')
        w.click(p, reports.get_by_role("button", name="Report", exact=True))
    w.settle(p, 1500)
    w.shot(p, "lcm/aiml-mlmf-subscription-and-reports")

    # a feature group: datalake settings, the token given by secret reference
    w.goto(p, "/aiml#features")
    form = w.section(p, "aiml.newFeatureGroup")
    for label, value in (("^Name", f"prb_features_{w.run}"), ("^Features", "pdcpBytesDl,pdcpBytesUl,prbUsedDl"), ("^Host", "influxdb.datalake"),
                         ("^Bucket", "ran-pm"), ("^Secret reference", "walk-through-token"), ("^DB org", "radisys"), ("^Measurement", "cell_pm")):
        form.get_by_label(re.compile(label)).fill(value)
    w.shot(p, "lcm/aiml-feature-group-form")
    w.click(p, form.get_by_role("button", name="Create feature group"))


def lcm_alarms(w: Walk):
    """Alarms: inject a critical and a major alarm with the admin tool (what the element's NotifyNewAlarm carries), filter by severity, then raise →
    acknowledge → clear one, and list it with "show cleared" (README "Alarms")."""
    p = w.page()
    src = f"lab-{w.run}-cpri"
    w.goto(p, "/alarms#ran")
    for sid, severity, cause, problem, kind in ((src, "critical", "LOSS_OF_SIGNAL", "CPRI link to RU-3 down", "EQUIPMENT_ALARM"),
                                                (f"lab-{w.run}-thr", "major", "THRESHOLD_CROSSED", "DL PRB usage above 95 % for 15 min", "QUALITY_OF_SERVICE_ALARM")):
        tool = w.section(p, "alarms.inject")
        if not tool.evaluate("e => e.open"):
            tool.locator("summary").click()
        tool.get_by_label("Managed element").fill(DEMO_ME)
        tool.get_by_label("Source alarm ID").fill(sid)
        tool.get_by_label("Managed function").fill("NRCellDU=101")
        tool.get_by_label("Severity").select_option(severity)
        tool.get_by_label("Probable cause").fill(cause)
        tool.get_by_label("Specific problem").fill(problem)
        tool.get_by_label("Alarm type").select_option(kind)
        w.click(p, tool.get_by_role("button", name="Inject alarm"))
    w.goto(p, "/alarms#ran", 6000)
    p.get_by_title("Show only critical alarms").click()
    w.settle(p, 2000)
    w.shot(p, "lcm/alarms-filter-critical")
    table = w.section(p, "alarms.table")
    w.open_row(table, src, 2)
    w.settle(p, 800)
    w.shot(p, "lcm/alarm-raised-unacknowledged")
    w.click(p, w.section(p, "alarms.detail").get_by_role("button", name="Ack", exact=True), 2500)
    w.shot(p, "lcm/alarm-acknowledged")
    table.get_by_label("Severity").select_option("")  # a cleared alarm's severity is "cleared": drop the critical filter
    table.get_by_label("show cleared").check()  # keeps the row on the page once it is cleared, so the detail follows it
    w.settle(p, 1500)
    w.click(p, w.section(p, "alarms.detail").get_by_role("button", name="Clear", exact=True), 2500)
    w.shot(p, "lcm/alarm-cleared")
    w.goto(p, "/alarms#ran", 6000)
    w.section(p, "alarms.table").get_by_label("show cleared").check()
    w.settle(p, 2000)
    w.shot(p, "lcm/alarms-with-cleared")


def lcm_infra(w: Walk):
    """Infrastructure: a service order composed from step templates and executed, its NF deployment healed, scaled, failed by its deployment manager
    and healed again; an O-Cloud resource and an inventory subscription; O1 endpoints, health discovery, CM writes and a software job (README
    "Infrastructure"). Onboards one package version, whose NF descriptor the order's DEPLOY step can use."""
    p = w.page()
    # the order's DEPLOY step is prefilled with an NF descriptor no deployment uses yet: onboard one more package version so there is one
    url = w.serve_csar("energy-saving-rapp", f"1.3.{w.run}")
    w.api("POST", "/onboarding/packages", json={"location": url, "applicationType": "rApp"})
    w.wait_for("the order's package", lambda: next((x for x in w.items("/onboarding/packages") if x.get("version") == f"1.3.{w.run}" and x["state"] == "AVAILABLE"), None))

    scope = f"cell-cluster-7 rollout {w.run}"
    w.goto(p, "/infrastructure#orders")
    box = w.section(p, "infrastructure.submit-order")
    box.get_by_role("button", name="New service order").click()
    box.get_by_label("Scope", exact=True).fill(scope)
    box.get_by_label("Steps (JSON array)").fill("[]")
    p.wait_for_timeout(1500)  # the prefill lists (models, descriptors, deployments) load once the form opens
    for step in ("INFRA", "TRAINING", "DEPLOY"):
        box.get_by_role("button", name=step, exact=True).click()
    w.shot(p, "lcm/orders-compose")
    w.click(p, box.get_by_role("button", name="Submit order"), 3000)
    order = w.wait_for("the order", lambda: next((o for o in w.items("/so-smos/orders") if o["scope"] == scope), None))
    w.ctx["order"] = order["orderId"]
    w.goto(p, "/infrastructure#orders")
    w.section(p, "infrastructure.orders").locator("article", has_text=scope).get_by_role("button", name="Details").click()
    w.settle(p, 800)
    w.shot(p, "lcm/order-drawer-steps")

    # the order's NF deployment: RUNNING → heal and scale → RUNTIME_FAILURE (ABNORMAL) → heal
    dep_id = next(st["result"]["nfDeploymentId"] for st in order["steps"] if st["stepType"] == "DEPLOY" and st.get("result"))
    dep = w.api("GET", f"/nfo/deployments/{dep_id}")

    def deployment():
        return w.api("GET", f"/nfo/deployments/{dep['nfDeploymentId']}")

    def open_dep():
        w.goto(p, "/infrastructure#nfo", 3000)
        w.open_row(w.section(p, "infrastructure.deployments"), dep["name"], 2)
        d = w.drawer(p)
        d.get_by_role("heading", name="LCM operations").wait_for()
        w.settle(p, 1200)
        return d

    d = open_dep()
    w.shot(p, "lcm/nfo-deployment-running")
    w.click(p, d.get_by_role("button", name="Heal", exact=True))
    w.wait_for("RUNNING after heal", lambda: deployment()["state"] == "RUNNING")
    d = open_dep()
    w.click(p, d.get_by_role("button", name="Scale", exact=True))
    w.wait_for("RUNNING after scale", lambda: deployment()["state"] == "RUNNING")
    d = open_dep()
    w.shot(p, "lcm/nfo-heal-and-scale-operations")
    d.get_by_label("Detail").fill("node gpu-2 lost: kubelet stopped reporting")
    w.click(p, d.get_by_role("button", name="RUNTIME_FAILURE"))
    w.wait_for("ABNORMAL", lambda: deployment()["state"] == "ABNORMAL")
    d = open_dep()
    w.shot(p, "lcm/nfo-runtime-failure-abnormal")
    w.click(p, d.get_by_role("button", name="Heal", exact=True))
    w.wait_for("RUNNING again", lambda: deployment()["state"] == "RUNNING")
    d = open_dep()
    w.shot(p, "lcm/nfo-healed-running")
    p.keyboard.press("Escape")

    # O-Cloud: provision a resource in pool-0, then an inventory-change subscription
    rtype = w.items("/focom/resource-types")[0]["resourceTypeId"]  # FOCOM refuses a type it does not know (unless it auto-registers)
    w.goto(p, "/infrastructure#ocloud")
    inv = w.section(p, "infrastructure.inventory")
    inv.get_by_role("button", name="Resource pools").click()
    w.settle(p, 1500)
    inv.locator("tbody tr").first.locator("td").nth(1).click()
    res = w.section(p, "infrastructure.pool-resources")
    res.locator("summary").click()
    res.get_by_label("Spec (JSON)").fill(f'{{"resourceTypeId": "{rtype}", "description": "GPU node for model training ({w.run})"}}')
    w.click(p, res.get_by_role("button", name="Provision", exact=True), 2500)
    w.shot(p, "lcm/ocloud-resource-provisioned")
    subs = w.section(p, "infrastructure.inventory-subscriptions")
    subs.get_by_label("Callback").fill("http://r1-termination:8899/inventory-events")
    subs.get_by_label("Resource type filter").fill(rtype)
    w.shot(p, "lcm/ocloud-inventory-subscription-form")
    w.click(p, subs.get_by_role("button", name="Subscribe", exact=True), 2000)
    w.shot(p, "lcm/ocloud-inventory-subscription-created")

    # O1: register three endpoints (NETCONF, RESTCONF at its RFC 8040 root, one whose adaptor does not answer), discover, write, update software
    me, rc, gone = f"lab-du-{w.run}", f"lab-rc-{w.run}", f"lab-gone-{w.run}"
    w.goto(p, "/infrastructure#o1")
    endpoints = w.section(p, "infrastructure.o1-endpoints")
    for ref, protocol, uri in ((me, "NETCONF", None), (rc, "RESTCONF", "http://mock-o1-adaptor:8000/restconf"), (gone, "NETCONF", "http://lab-unreachable:8000/edit-config")):
        endpoints.get_by_role("button", name="Register endpoint").click()
        dlg = p.get_by_role("dialog")
        dlg.get_by_label("Managed element ref").fill(ref)
        dlg.get_by_label("O1 protocol").select_option(protocol)
        if uri:
            dlg.get_by_label("Adaptor URI").fill(uri)
        dlg.get_by_label("Vendor").fill("Radisys")
        dlg.get_by_label("Region").fill("eu-west")
        dlg.get_by_label("Tenant").fill("acme")
        if ref == me:
            w.shot(p, "lcm/o1-register-endpoint-dialog")
        w.click(p, dlg.get_by_role("button", name="Register"))
    w.goto(p, "/infrastructure#o1")
    w.click(p, w.section(p, "infrastructure.o1-endpoints").get_by_role("button", name="Run health discovery"), 2500)
    w.shot(p, "lcm/o1-health-discovery")

    def cm_write(elements, shot_name=None):
        # one CM write through the dialog; returns the new job's id
        before = {j["jobId"] for j in w.items("/ran-nf-oam/config-jobs")}
        w.goto(p, "/infrastructure#o1")
        w.section(p, "infrastructure.config-jobs").get_by_role("button", name="New config write").click()
        dlg = p.get_by_role("dialog")
        dlg.get_by_label("Managed elements").select_option(elements)
        if shot_name:
            w.shot(p, shot_name)
        w.click(p, dlg.get_by_role("button", name="Submit"), 2500)
        return w.wait_for("the config job", lambda: next((j["jobId"] for j in w.items("/ran-nf-oam/config-jobs") if j["jobId"] not in before), None))

    def open_job(job):
        w.wait_for("the job to finish", lambda: w.api("GET", f"/ran-nf-oam/config-jobs/{job}")["status"] not in ("PENDING", "PROCESSING"))
        w.goto(p, "/infrastructure#o1", 3000)
        w.open_row(w.section(p, "infrastructure.config-jobs"), job[:8], 1)
        w.drawer(p).locator("tbody tr").first.wait_for()
        w.settle(p, 1000)

    job = cm_write([me, gone], "lcm/o1-cm-write-dialog")
    w.goto(p, "/infrastructure#o1", 3000)
    w.shot(p, "lcm/o1-cm-write-submitted")
    open_job(job)
    w.shot(p, "lcm/o1-cm-job-partial-success")
    w.ctx["cm_job"] = job
    job = cm_write([rc])
    open_job(job)
    w.shot(p, "lcm/o1-cm-write-restconf-applied")
    w.goto(p, "/infrastructure#o1")
    swm = w.section(p, "infrastructure.swm-jobs")
    swm.get_by_role("button", name="Start software update…").click()
    swm.get_by_label("Managed element").select_option(me)
    w.click(p, swm.get_by_role("button", name="Start software update"))
    w.goto(p, "/infrastructure#o1")
    w.click(p, w.row(w.section(p, "infrastructure.swm-jobs"), me).get_by_role("button", name=re.compile(" ok$")), 2000)
    w.shot(p, "lcm/o1-software-job-advanced")


def lcm_kpis(w: Walk):
    """KPIs & Assurance: a PM subscription, an MDAF producer and report (as a producer rApp would), and SA SMOS monitors: register one on the infra
    group's order, evaluate a breach, RECONNECT, escalate; then one on the AI/ML group's coordination group, whose action retrains it (README "KPIs")."""
    p = w.page()
    w.goto(p, "/kpis#pm")
    form = w.section(p, "kpis.newPm")
    form.get_by_label("Managed element").select_option(DEMO_ME)
    form.get_by_label("Counter type").fill("RRU.PrbUsedDl")
    form.get_by_label("Granularity (s)").fill("300")
    w.shot(p, "lcm/kpis-pm-subscription-form")
    w.click(p, form.get_by_role("button", name="Subscribe", exact=True))

    w.goto(p, "/kpis#analytics")
    tools = w.section(p, "kpis.producerTools")
    tools.get_by_label("Producer ID").fill("energy-saving-rapp")  # SME-enrolled by the energy-saving demo, as DEMO_RUNBOOK §14 uses it
    tools.get_by_label("Analytics type").first.fill("coverage-issue-analysis")
    w.click(p, tools.get_by_role("button", name="Register producer"))
    tools.get_by_label("Analytics type").nth(1).fill("coverage-issue-analysis")
    tools.get_by_label("Output (JSON)").fill(f'{{"coverageHoles": 2, "worstCell": "NRCellDU=103", "confidence": 0.87, "run": "{w.run}"}}')
    w.click(p, tools.get_by_role("button", name="Publish report"))
    w.goto(p, "/kpis#analytics")
    w.open_row(w.section(p, "kpis.analyticsReports"), "coverage-issue-analysis", 1)
    w.settle(p, 600)
    w.shot(p, "lcm/kpis-analytics-report-dialog")

    order = w.ctx.get("order") or next(o["orderId"] for o in reversed(w.items("/so-smos/orders")) if all(s["status"] == "COMPLETED" for s in o["steps"]))
    group = w.ctx.get("group") or w.items("/mlmr/coordination-groups")[-1]["groupId"]

    def register(target, thresholds, shot_name=None):
        # a monitor through the form; returns its id
        before = {m["monitorId"] for m in w.items("/sa-smos/monitors")}
        w.goto(p, "/kpis#assurance")
        form = w.section(p, "kpis.newMonitor")
        form.get_by_label("Scope").select_option(target)
        form.get_by_label("Thresholds").fill(thresholds)
        if shot_name:
            w.shot(p, shot_name)
        w.click(p, form.get_by_role("button", name="Register"))
        return w.wait_for("the monitor", lambda: next((m["monitorId"] for m in w.items("/sa-smos/monitors") if m["monitorId"] not in before), None))

    def open_monitor(mid):
        w.goto(p, "/kpis#assurance", 3000)
        w.open_row(w.section(p, "kpis.monitors"), mid[:8], 2)
        panel = w.section(p, "kpis.monitor")
        panel.wait_for()
        return panel

    monitor = register(f"order:{order}", '{"throughputMbps": 100, "availabilityPct": 99.5}', "lcm/kpis-register-monitor-form")
    w.goto(p, "/kpis#assurance", 3000)
    w.shot(p, "lcm/kpis-monitor-registered")
    panel = open_monitor(monitor)
    panel.get_by_label("Current metrics (JSON)").fill('{"throughputMbps": 72, "availabilityPct": 99.8}')
    w.click(p, panel.get_by_role("button", name="Evaluate"), 800)
    w.shot(p, "lcm/kpis-monitor-evaluate-breach")
    panel.get_by_label("Action type").select_option("RECONNECT")
    w.click(p, panel.get_by_role("button", name="Execute"), 2000)
    open_monitor(monitor)
    w.shot(p, "lcm/kpis-remedial-action-resolved")
    panel = w.section(p, "kpis.monitor")
    panel.get_by_label("Reason").fill("RECONNECT did not restore throughput: needs a site visit")
    w.click(p, panel.get_by_role("button", name="Escalate"), 2000)
    open_monitor(monitor)
    w.shot(p, "lcm/kpis-escalated-to-operator")
    monitor = register(f"group:{group}", '{"accuracy": 0.9}')
    panel = open_monitor(monitor)
    panel.get_by_label("Action type").select_option("CONFIG_CHANGE")
    w.click(p, panel.get_by_role("button", name="Execute"), 2000)
    open_monitor(monitor)
    w.shot(p, "lcm/kpis-group-retrain-remediation")


def lcm_intents(w: Walk):
    """Intents: create one in the form, open it, deactivate it, publish a report as its handler; then an ASSIST rApp asks for an autonomy dispatch,
    which waits for a scope and is resolved (README "Intents"). Deploys and bootstraps one ASSIST instance of the demo's package through the BFF."""
    p = w.page()
    label = f"night-time energy saving {w.run}"
    w.goto(p, "/policy#intents")
    form = w.section(p, "intents.new")
    form.get_by_label("Handler (RMIH)").select_option("sa-smos")
    form.get_by_label("User label").fill(label)
    form.get_by_label("Priority").fill("2")
    form.get_by_label("Expectation targets").fill(ENERGY_TARGET)
    p.wait_for_timeout(800)
    w.shot(p, "lcm/policy-intent-create-form")
    w.click(p, form.get_by_role("button", name="Create intent"))
    intent = w.wait_for("the intent", lambda: next((i["intentId"] for i in w.items("/intent-service/intents") if i.get("userLabel") == label), None))

    def open_intent():
        w.goto(p, "/policy#intents", 3000)
        w.row(w.section(p, "intents.table"), label).get_by_role("button", name="Reports").click()
        d = w.drawer(p)
        d.get_by_role("heading", name="Intent reports").wait_for()
        w.settle(p, 1000)
        return d

    d = open_intent()
    w.shot(p, "lcm/policy-intent-drawer")
    w.click(p, d.get_by_role("button", name="Deactivate"))
    d = open_intent()
    w.shot(p, "lcm/policy-intent-deactivated")
    d.locator("summary", has_text="publish a report").click()
    d.get_by_label("Fulfilment").select_option("DEGRADED")
    w.shot(p, "lcm/policy-intent-report-form")
    w.click(p, d.get_by_role("button", name="Publish report"), 2000)
    w.shot(p, "lcm/policy-intent-report-published")

    # an ASSIST instance (set up through the BFF: the deploy dialog is the rApps group's screen) asks for a dispatch
    demo_pkg = next(x for x in w.items("/onboarding/packages") if x["name"] == "EnergySaving_rApp" and x["version"] == "1.0.0")
    inst = w.api("POST", "/rapp-mgmt/instances", json={"packageId": demo_pkg["packageId"], "config": {"managedElementRef": DEMO_ME}, "autonomyMode": "ASSIST"})
    iid = inst["instanceId"]
    w.api("POST", f"/rapp-mgmt/instances/{iid}/bootstrap-complete")
    w.goto(p, "/policy#autonomy")
    form = w.section(p, "intents.requestDispatch")
    form.get_by_label("rApp instance").select_option(iid)
    form.get_by_label("Handler (RMIH)").select_option("sa-smos")
    form.get_by_label("Expectation targets").fill(ENERGY_TARGET)
    w.click(p, form.get_by_role("button", name="Request dispatch"))
    w.goto(p, "/policy#autonomy", 3000)
    w.shot(p, "lcm/policy-autonomy-awaiting-scope")
    row = w.row(w.section(p, "intents.dispatches"), "AWAITING_SCOPE")
    row.get_by_label("Region scope (JSON)").fill('{"cells": ["101", "102"]}')
    w.click(p, row.get_by_role("button", name="Resolve"), 2000)
    w.goto(p, "/policy#autonomy", 3000)
    w.shot(p, "lcm/policy-autonomy-resolved")


def lcm_data(w: Walk):
    """Data & Exposure: register a producer data type and an offer as a producer rApp would, notify data ready, create a data job; onboard an SME
    invoker (its one-time secret), give it a trusted-invoker security context, and subscribe to CAPIF events (README "Data & Exposure")."""
    p = w.page()
    name = f"CoverageIssue{w.run}"
    w.goto(p, "/data#offers")
    types = w.section(p, "data.types")
    types.locator("summary").click()
    for label, value in (("Name", name), ("Producer ID", f"lab-producer-{w.run}"), ("Health callback URL", "http://r1-termination:8899/"),
                         ("Job callback URL", "http://r1-termination:8899/dme-jobs")):
        types.get_by_label(label, exact=True).fill(value)
    w.shot(p, "lcm/dme-register-type-form")
    w.click(p, types.get_by_role("button", name="Register type"))
    w.goto(p, "/data#offers")
    offers = w.section(p, "data.offers")
    offers.locator("summary").click()
    offers.get_by_label(re.compile("^Type")).select_option(label=f"RAN.{name}")
    w.click(p, offers.get_by_role("button", name="Create offer"))
    w.goto(p, "/data#offers", 3000)
    w.click(p, w.row(w.section(p, "data.offers"), f"RAN.{name}").get_by_role("button", name="Notify data ready"))
    w.shot(p, "lcm/dme-offer-notify-data-ready")

    consumer = f"lab-consumer-{w.run}"
    w.goto(p, "/data#dme")
    jobs = w.section(p, "data.jobs")
    jobs.get_by_label(re.compile("^Type")).select_option(label=f"RAN.{name}")
    jobs.get_by_label("Consumer ID").fill(consumer)
    p.wait_for_timeout(1500)  # the committed-method hint arrives with the type's offers
    w.shot(p, "lcm/dme-data-job-form")
    w.click(p, jobs.get_by_role("button", name="Create job"))
    w.goto(p, "/data#dme", 3000)
    w.open_row(w.section(p, "data.jobs"), consumer, 2)
    w.settle(p, 600)
    w.shot(p, "lcm/dme-data-job-dialog")

    w.goto(p, "/data#sme")
    invokers = w.section(p, "data.invokers")
    invokers.get_by_label("Invoker public key").fill(f"lab-invoker-{w.run}-public-key")
    w.click(p, invokers.get_by_role("button", name="Onboard invoker"))
    secret = p.get_by_role("dialog")
    secret.wait_for()
    invoker = secret.locator("code").first.inner_text()
    w.shot(p, "lcm/sme-invoker-secret-dialog")
    p.keyboard.press("Escape")
    w.goto(p, "/data#sme", 3000)
    w.row(w.section(p, "data.invokers"), invoker).get_by_role("button", name="Trust…").click()
    dlg = p.get_by_role("dialog")
    dlg.get_by_label("AEF ID").fill("aef-ran-nf-oam")
    dlg.get_by_label("API ID").fill("mdaf.coverage-issue-analysis")
    w.shot(p, "lcm/sme-trusted-invoker-dialog")
    w.click(p, dlg.get_by_role("button", name="Register"))

    w.goto(p, "/data#sme")
    subs = w.section(p, "data.capif-subscriptions")
    subs.get_by_label("Subscriber ID").fill(f"lab-subscriber-{w.run}")
    subs.get_by_label(re.compile("^Events")).select_option(["SERVICE_API_AVAILABLE", "SERVICE_API_UPDATE"])
    subs.get_by_label("Callback URI").fill("http://r1-termination:8899/capif-events")
    w.shot(p, "lcm/sme-event-subscription-form")
    w.click(p, subs.get_by_role("button", name="Subscribe", exact=True), 2000)
    w.shot(p, "lcm/sme-event-subscription-created")


# ============================================================ pages, roles, flows, generic


# The places the demo elements are given so the scope picker has something to offer: the first two elements in eu-west, each in its own site
# cluster, the rest in eu-central (the demos register their elements with no region).
DEMO_PLACES = [("eu-west", "metro-a"), ("eu-west", "metro-b"), ("eu-central", "rural-1"), ("eu-central", "rural-1")]

# Plays the O-Cloud's collector (DEMO_RUNBOOK.md, FOCOM FCAPS): a CPU and a memory reading for every FOCOM resource and the Phase 1 cluster.
UTILISATION_PRODUCER = """
import httpx
f = 'http://focom:8000'
ids = ['phase1-degenerate-cluster']
for pool in httpx.get(f + '/resource-pools', params={'limit': 500}).json().get('items', []):
    rs = httpx.get(f + '/resource-pools/' + pool['resourcePoolId'] + '/resources', params={'limit': 500}).json()
    ids += [r['resourceId'] for r in (rs.get('items', []) if isinstance(rs, dict) else rs)]
for i, rid in enumerate(ids):
    for name, value in (('CPU_UTILIZATION', 35 + (i * 17) % 60), ('MEMORY_UTILIZATION', 28 + (i * 23) % 55)):
        httpx.post(f + '/performance/ingest', json={'resourceId': rid, 'performanceMeasurementDefinitionId': name, 'measurementValue': value}).raise_for_status()
print(len(ids))
"""


# The rApp's part of a two-person approval (#401), played inside the compose network as DEMO_RUNBOOK's CM-write step does: a write by the
# invoker `two-person-demo` (the role R1 would stamp), which RAN NF OAM parks because an admin's policy asks two people for it. argv[1]: an element.
TWO_PERSON_WRITE = """
import json, sys, httpx
r = httpx.post("http://ran-nf-oam:8000/config-jobs", headers={"X-R1-Invoker-Id": "two-person-demo", "X-R1-Role": "rapp"}, json={
    "requestedBy": "two-person-demo", "accessScope": "cell",
    "decision": {"rationale": "Wake cell 2: load above the energy-saving threshold", "modelVersion": "es-v3"},
    "changes": [{"managedElementRef": sys.argv[1], "managedFunctionRef": "NRCellDU=2", "attributeChanges": {"administrativeState": "UNLOCKED"}}]})
r.raise_for_status()
print(json.dumps(r.json()))
"""


def seed_two_person_approval(w: Walk, element: str) -> str:
    """A request waiting for its second approver (README: Approvals, two-person approval): the admin asks two people for the invoker
    `two-person-demo` (`PUT /rapp-approval-policy/{invoker}`, `requiredApprovals: 2`), the rApp's write is parked (`TWO_PERSON_WRITE`), and the
    operator gives the first approval through the BFF, which records it under the signed-in user. Returns the approval id."""
    w.api("PUT", "/ran-nf-oam/rapp-approval-policy/two-person-demo",
          json={"requestedBy": "smo-gui", "timeoutSeconds": 86400, "onTimeout": "EXPIRE", "requiredApprovals": 2})
    done = subprocess.run([*w.compose, "exec", "-T", "r1-termination", "python3", "-c", TWO_PERSON_WRITE, element], cwd=SMO, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"the parked write failed: {done.stderr[-300:]}")
    approval_id = json.loads(done.stdout.strip().splitlines()[-1])["approvalId"]
    w.api("POST", f"/ran-nf-oam/rapp-approvals/{approval_id}/approve", json={"decidedBy": "smo-gui", "reason": "Load figures checked"}, user="operator")
    return approval_id


def seed_console(w: Walk):
    """Gives the stack what the console's later features read and the demos do not make: a region and a site cluster on each demo element
    (`PUT /managed-entities/{me}/scope` and `/site-cluster`, admin), utilisation readings for the FOCOM resources (`UTILISATION_PRODUCER`,
    run inside r1-termination as the runbook does), one onboarding template for the demo elements' type (`PUT /onboarding-templates/{name}`,
    admin, MGT-14.6) so Configuration → Element onboarding shows one, a request waiting for its second approver (`seed_two_person_approval`),
    and one decisions export job (`POST /api/exports`) so the Exports page has a row."""
    elements = w.items("/ran-nf-oam/managed-entities")
    for entity, (region, cluster) in zip(elements, DEMO_PLACES * (len(elements) // len(DEMO_PLACES) + 1)):
        ref = entity["managedElementRef"]
        w.api("PUT", f"/ran-nf-oam/managed-entities/{ref}/scope", json={"region": region, "tenant": entity.get("tenant")})
        w.api("PUT", f"/ran-nf-oam/managed-entities/{ref}/site-cluster", json={"siteCluster": cluster})
    entity_type = next((e.get("entityType") for e in elements if e.get("entityType")), "O-DU")
    w.api("PUT", "/ran-nf-oam/onboarding-templates/du-basic", json={
        "entityType": entity_type, "vendorName": None, "description": "Unlock the DU function of a new element",
        "changes": [{"managedFunctionRef": "GNBDUFunction=1", "attributeChanges": {"administrativeState": "UNLOCKED"}, "operation": "merge"}],
        "softwareBaseline": None, "requireBaseline": False, "autoApply": False, "enabled": True})
    seed_two_person_approval(w, elements[0]["managedElementRef"])
    done = subprocess.run([*w.compose, "exec", "-T", "r1-termination", "python3", "-c", UTILISATION_PRODUCER], cwd=SMO, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"utilisation producer failed: {done.stderr[-300:]}")
    page = w.page()
    csrf = next((c["value"] for c in page.context.cookies() if c["name"] == "smo_csrf"), "")
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7 * 86400))
    resp = page.request.post(w.base + "/api/exports", data={"kind": "decisions", "since": since}, headers={"X-CSRF-Token": csrf})
    if resp.status >= 400:
        raise RuntimeError(f"POST /api/exports → {resp.status}: {resp.text()[:200]}")
    time.sleep(3)          # let the job finish, so the Exports screenshot shows a DONE row with its size


def capture_pages(w: Walk):
    """Every page and tab in `PAGES` (admin, dark unless the name says light), the three sample-rApp page screens, the relation drawer and the
    pinned rApp in the sidebar (README: each module's tab rows, "rApp directory and declared pages", the new pages and the light theme)."""
    w.step("seed-console", seed_console, w)
    for name, user, path, theme in PAGES:
        p = w.page(user, theme)

        def one(p=p, name=name, path=path, theme=theme):
            # one page: open, let it load, save
            w.goto(p, path, 3500)
            w.shot(p, name, theme=theme)

        w.step(name, one)
    p = w.page()

    def two_person():
        # the request waiting for its second approver, opened from the queue (the operator gave the first approval)
        w.goto(p, "/approvals", 3500)
        p.get_by_role("button", name="Review…").first.click()
        w.settle(p, 2500)
        w.shot(p, "pages/approvals-two-person")

    w.step("pages/approvals-two-person", two_person)
    # the demos' rApps: those whose own operator API is registered draw their declared page
    rapps = [r for r in p.request.get(w.base + "/api/rapps", params={"limit": 50}).json()["items"] if r.get("hasPage") and r.get("operatorApiRegistered")]
    by_name = {r["name"]: r["instanceId"] for r in rapps}
    energy, mobility = by_name.get("EnergySaving_rApp"), by_name.get("MobilityOptimization_rApp")
    if energy:
        w.step("pages/rapp-page-energy-saving", lambda: (w.goto(p, f"/rapps/{energy}", 4000), w.shot(p, "pages/rapp-page-energy-saving")))
    if mobility:
        viewer = w.page("viewer")
        w.step("pages/rapp-page-mobility", lambda: (w.goto(p, f"/rapps/{mobility}", 4000), w.shot(p, "pages/rapp-page-mobility")))
        w.step("pages/rapp-page-mobility-viewer", lambda: (w.goto(viewer, f"/rapps/{mobility}", 4000), w.shot(viewer, "pages/rapp-page-mobility-viewer")))

        def drawer():
            # the first row of the relations table opens its drawer
            w.goto(p, f"/rapps/{mobility}", 4000)
            p.locator("table tbody tr").first.click()
            w.settle(p, 2500)
            w.shot(p, "pages/rapp-drawer-mobility")

        w.step("pages/rapp-drawer-mobility", drawer)
    if energy:
        def pinned():
            # pin it (when it is not pinned already), then the sidebar shows it under rApps
            w.goto(p, f"/rapps/{energy}", 3000)
            pin = p.get_by_role("button", name=re.compile("^☆ Pin"))
            if pin.count():
                w.click(p, pin.first, 2000)
            w.shot(p, "pages/rapp-sidebar-pinned")

        w.step("pages/rapp-sidebar-pinned", pinned)


def capture_roles(w: Walk):
    """The same live state through the viewer's and the operator's eyes (README "Role views")."""
    for name, user, path in ROLE_PAGES:
        p = w.page(user)
        w.step(name, lambda p=p, name=name, path=path: (w.goto(p, path, 3500), w.shot(p, name)))


def _flow_subjects(w: Walk) -> dict[str, str]:
    """The subject each flow board follows: what the lifecycle groups left at its most telling state, found through the API (so `--only flows`
    works on its own). A flow with no such subject is left out and follows the board's default, the newest subject."""
    found: dict[str, str] = {}
    packages = w.items("/onboarding/packages")
    failed = [x["packageId"] for x in packages if x["state"] == "FAILED"]
    blocked = [x["packageId"] for x in packages if x["state"] == "PRIMED" and any(u["active"] for u in w.items(f"/onboarding/packages/{x['packageId']}/usage"))]
    if failed:
        found["06"] = failed[-1]
    if blocked:
        found["06-blocked"] = blocked[-1]
    lifecycles = {x["modelId"]: x for x in w.items("/aimgf/model-lifecycles")}
    promoted = [m["modelId"] for m in w.items("/mlmr/models") if m["modelType"].startswith("CellLoadForecaster-")
                and lifecycles.get(m["modelId"], {}).get("runtimeLifecycleState") == "ACTIVE"]
    if promoted:
        found["02"] = promoted[-1]
    partial = [j["jobId"] for j in w.items("/ran-nf-oam/config-jobs", status="PARTIAL_SUCCESS")]
    if partial:
        found["03"] = partial[-1]
    for inst in w.items("/rapp-mgmt/instances", state="RUNNING"):  # the instance `_fault_story` made: faulted, recovered, running
        if any(f.get("severity") == "critical" for f in w.items(f"/rapp-mgmt/instances/{inst['instanceId']}/faults")):
            found["07"] = inst["instanceId"]
    mine = [i["intentId"] for i in w.items("/intent-service/intents") if i.get("rmioId") == "smo-gui"]
    if mine:
        found["09"] = mine[-1]
    healed = [d["nfDeploymentId"] for d in w.items("/nfo/deployments") if d["name"].startswith("so-deploy-") and d["state"] == "RUNNING"]
    if healed:
        found["15"] = healed[-1]
    return found


def capture_flows(w: Walk):
    """Every lifecycle-flow board (README "Lifecycle flows"), following the subjects `_flow_subjects` picks; flow 06 twice: a package that failed
    validation, and a PRIMED package whose active usage blocks the delete."""
    p = w.page()
    subjects = _flow_subjects(w)
    for flow in FLOWS + ["06-blocked"]:
        if flow == "06-blocked" and flow not in subjects:
            continue
        query = f"?subject={subjects[flow]}" if flow in subjects else ""
        path = f"/flows/{flow.split('-')[0]}{query}"
        w.step(f"flows/f{flow}", lambda flow=flow, path=path: (w.goto(p, path, 4000), w.shot(p, f"flows/f{flow}")))


def capture_generic(w: Walk):
    """Sign-in (plain, failed, locked), the change-password and add-user dialogs, and the ⌘K search (README "Generic"). The lock-out uses a user
    name that does not exist, so no real account is locked."""
    p = w.page()

    def dialogs():
        # the two dialogs every admin meets, and the search
        w.goto(p, "/")
        p.get_by_role("button", name="Change password").click()
        p.wait_for_timeout(600)
        w.shot(p, "generic/change-password-dialog")
        w.goto(p, "/admin#users")
        p.get_by_role("button", name="Add user").first.click()
        p.wait_for_timeout(600)
        w.shot(p, "generic/admin-add-user-dialog")
        w.goto(p, "/")
        p.keyboard.press("Control+k")
        p.keyboard.type("gnb", delay=60)
        p.wait_for_timeout(1500)
        w.shot(p, "generic/search")

    w.step("generic dialogs", dialogs)

    def sign_in():
        # a fresh context: signed out
        page = w.browser.new_context(viewport=VIEWPORT).new_page()
        page.goto(w.base + "/login")
        page.get_by_label("Username").wait_for()
        page.wait_for_timeout(800)
        w.shot(page, "generic/login")
        page.get_by_label("Username").fill(f"nobody-{w.run}")
        page.get_by_label("Password").fill("wrong-password-1")
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_timeout(1200)
        w.shot(page, "generic/login-failed")
        for _ in range(5):  # the BFF locks a name after 5 failures in a row
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_timeout(600)
        w.shot(page, "generic/login-locked")
        page.context.close()

    w.step("generic sign-in", sign_in)


# ============================================================ runner

GROUP_FUNCS = {"lcm-rapps": lcm_rapps, "lcm-aiml": lcm_aiml, "lcm-alarms": lcm_alarms, "lcm-infra": lcm_infra,
               "lcm-kpis": lcm_kpis, "lcm-intents": lcm_intents, "lcm-data": lcm_data,
               "pages": capture_pages, "roles": capture_roles, "flows": capture_flows, "generic": capture_generic}


def main(argv: list[str] | None = None) -> int:
    """Parses the arguments, runs the chosen groups in order and prints what was saved and what failed; exit 1 when a screenshot failed.

        Reads the three passwords from the environment (GUI_E2E_PASSWORD required; operator and viewer only for the groups that sign them in).
    """
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default="http://localhost:3000", help="the console's address")
    ap.add_argument("--out", default=str(SMO / "gui" / "docs" / "screenshots"), help="the screenshots folder")
    ap.add_argument("--only", action="append", choices=GROUPS + ["lcm"], help="run only this group (repeatable); default: all, lifecycle first")
    ap.add_argument("--chromium", default=os.environ.get("PLAYWRIGHT_CHROMIUM"), help="a Chromium executable instead of Playwright's own")
    ap.add_argument("--compose", default=os.environ.get("SMO_COMPOSE", "docker compose"), help="the command that runs docker compose for the stack")
    args = ap.parse_args(argv)
    if not os.environ.get("GUI_E2E_PASSWORD"):
        ap.error("set GUI_E2E_PASSWORD to the admin password")
    chosen = args.only or GROUPS
    groups = [g for g in GROUPS if g in chosen or (g in LCM_GROUPS and "lcm" in chosen)]
    passwords = {"admin": os.environ["GUI_E2E_PASSWORD"], "operator": os.environ.get("GUI_E2E_OPERATOR_PASSWORD", ""),
                 "viewer": os.environ.get("GUI_E2E_VIEWER_PASSWORD", "")}
    started = time.time()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=args.chromium) if args.chromium else pw.chromium.launch()
        walk = Walk(browser, args.base_url.rstrip("/"), Path(args.out), passwords, shlex.split(args.compose))
        try:
            for group in groups:
                print(f"== {group}", flush=True)
                walk.step(group, GROUP_FUNCS[group], walk)
        finally:
            browser.close()
    print(f"{len(walk.saved)} screenshots in {time.time() - started:.0f} s; {len(walk.failed)} failed")
    for line in walk.failed:
        print("  FAILED", line)
    return 1 if walk.failed else 0


if __name__ == "__main__":
    sys.exit(main())
