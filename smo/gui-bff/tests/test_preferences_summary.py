"""The console preferences (`GET`/`PUT /api/me/preferences`, app/preferences.py), the saved KPI layouts (`/api/me/kpi-layouts`, app/kpi_layouts.py) and the summary counts (`GET /api/summary/{page}`, app/summary.py).

R1 Termination and SME are faked by test_main.py's FakeSmo, extended here with list routes that answer a paged envelope whose `total` depends on the
filter, so the BFF's real gateway client runs unmodified. Run: `PYTHONPATH=.:../shared python -m pytest tests/test_preferences_summary.py -q`.
"""

import httpx
import pytest

from app import summary
from app.config import Settings
from app.db import Database
from app.main import create_app, seed_users
from app.smo_client import R1Gateway
from test_main import PASSWORDS, R1, FakeSmo, login


class CountingSmo(FakeSmo):
    """FakeSmo whose list routes answer `{"items": [], "total": N}`: N from `totals[(path, filter)]`, 0 when not listed, and whose statistics routes
    answer `stats[path]`. A module in `down_modules` fails."""

    def __init__(self):
        super().__init__()
        self.totals: dict[tuple[str, str], int] = {("/ran-nf-oam/alarms", "severity=critical"): 1234, ("/ran-nf-oam/alarms", ""): 5000,
                                                   ("/ran-nf-oam/rapp-approvals", "status=PENDING"): 18}
        self.count_calls: list[httpx.Request] = []
        # the two statistics answers that are read by field, not by `total` (RAN NF OAM's /alarms/stats, rApp Management's GET /kill-all)
        self.stats: dict[str, dict] = {"/ran-nf-oam/alarms/stats": {"windowHours": 24, "mttaSeconds": 42.5, "acked": 3, "open": 7},
                                       "/rapp-mgmt/kill-all": {"stopped": 2}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        module = path.split("/")[1]
        if request.url.host == "r1-termination" and request.method == "GET" and path in self.stats:
            if module in self.down_modules:
                raise httpx.ConnectError("down")
            self.count_calls.append(request)
            return httpx.Response(200, json=self.stats[path])
        if request.url.host == "r1-termination" and not path.endswith(("/health", "/ready", "/version", "/bootstrap")) and request.method == "GET" \
                and request.url.params.get("limit") == "1":
            if request.headers.get("authorization", "").removeprefix("Bearer ") not in self.issued:
                return httpx.Response(401, json={"title": "UNAUTHORIZED"})
            if module in self.down_modules:
                raise httpx.ConnectError("down")
            self.count_calls.append(request)
            filt = "&".join(f"{k}={v}" for k, v in request.url.params.multi_items() if k not in ("limit", "since"))
            return httpx.Response(200, json={"items": [], "total": self.totals.get((path, filt), 0), "limit": 1, "offset": 0})
        return super().handler(request)


@pytest.fixture
def smo():
    """A fresh CountingSmo per test."""
    return CountingSmo()


@pytest.fixture
def app(smo):
    """The BFF on an in-memory database with the three seeded users, talking to `smo` through the real gateway client."""
    cfg = Settings(r1_url=R1, jwt_secret="test-secret", cookie_secure=False, admin_password=PASSWORDS["admin"],
                   operator_password=PASSWORDS["operator"], viewer_password=PASSWORDS["viewer"])
    db = Database("sqlite://")
    seed_users(db, cfg)
    return create_app(cfg, db=db, gateway=R1Gateway(R1, db, transport=httpx.MockTransport(smo.handler)))


# ------------------------------------------------------------------ preferences

def test_preferences_default_until_saved_and_need_a_session(app):
    """A user who never saved gets the defaults (dark, m, volt, 50 rows); without a session the route is a 401."""
    from fastapi.testclient import TestClient
    assert TestClient(app).get("/api/me/preferences").status_code == 401
    body = login(app, "viewer").get("/api/me/preferences").json()
    assert body["theme"] == "dark" and body["size"] == "m" and body["accent"] == "volt" and body["rowsPerPage"] == 50 and body["startPage"] == "/"


def test_saved_preferences_come_back_and_belong_to_their_user_only(app):
    """A save is read back on the next GET, by that user only: another user still sees the defaults."""
    viewer = login(app, "viewer")
    saved = viewer.put("/api/me/preferences", json={"theme": "light", "accent": "radisys", "size": "xl", "rowsPerPage": 100, "startPage": "/alarms"})
    assert saved.status_code == 200 and saved.json()["accent"] == "radisys"
    again = viewer.get("/api/me/preferences").json()
    assert (again["theme"], again["accent"], again["size"], again["rowsPerPage"], again["startPage"]) == ("light", "radisys", "xl", 100, "/alarms")
    assert login(app, "operator").get("/api/me/preferences").json()["theme"] == "dark"


@pytest.mark.parametrize("bad", [{"theme": "neon"}, {"accent": "#ff0000"}, {"size": "xxl"}, {"rowsPerPage": 1000}, {"startPage": "https://evil"},
                                 {"colour": "red"}])
# Values outside their closed set, and unknown fields, are refused, because theme, accent and size are written onto <html> as they are.
def test_preferences_outside_their_sets_are_refused(app, bad):
    """Every field is validated against its enum and an unknown field is a 422, so nothing unvalidated reaches the page."""
    assert login(app, "viewer").put("/api/me/preferences", json=bad).status_code == 422


def test_saving_preferences_needs_the_csrf_token(app):
    """The PUT is a state change: without the double-submit token it is refused like every other one."""
    client = login(app, "viewer")
    del client.headers["X-CSRF-Token"]
    assert client.put("/api/me/preferences", json={"theme": "light"}).status_code == 403


def test_deleting_a_user_forgets_their_preferences(app):
    """A deleted user's preferences go with the account, so a new account of the same name starts on the defaults."""
    admin = login(app, "admin")
    assert admin.post("/api/admin/users", json={"username": "kim", "password": "a-long-password", "role": "viewer"}).status_code == 201
    app.state.db.save_preferences("kim", '{"theme": "light"}')
    assert admin.delete("/api/admin/users/kim").status_code == 204
    assert app.state.db.preferences("kim") is None


# ------------------------------------------------------------------ saved KPI layouts (GUI-4.3)

LAYOUT = {"kpis": ["dl_ue_throughput", "handover_success_rate"], "range": "7d", "region": "east"}


def test_a_saved_kpi_layout_comes_back_to_its_user_only(app):
    """A saved layout is listed for its user with its fields and time; saving the same name replaces it; another user sees none."""
    viewer = login(app, "viewer")
    assert viewer.put("/api/me/kpi-layouts/Morning check", json=LAYOUT).status_code == 200
    assert viewer.put("/api/me/kpi-layouts/Morning check", json={**LAYOUT, "range": "1h"}).status_code == 200
    body = viewer.get("/api/me/kpi-layouts").json()
    [layout] = body["items"]
    assert body["max"] == 20 and layout["name"] == "Morning check" and layout["range"] == "1h" and layout["kpis"] == LAYOUT["kpis"]
    assert layout["region"] == "east" and layout["siteCluster"] is None and layout["updatedAt"]
    assert login(app, "operator").get("/api/me/kpi-layouts").json()["items"] == []


@pytest.mark.parametrize("bad", [{"kpis": []}, {"kpis": ["a"] * 9}, {"kpis": ["x", "x"]}, {"kpis": ["../alarms"]}, {"kpis": ["x"], "range": "1y"},
                                 {"kpis": ["x"], "region": ""}, {"kpis": ["x"], "colour": "red"}])
# The page puts each KPI name into a request path, so a name that is not a KPI name's shape is refused like every other bad field.
def test_a_kpi_layout_outside_its_shape_is_refused(app, bad):
    """No KPI, more than eight, a repeat, a name that is not a KPI name, a range outside its set, an empty region or an unknown field: 422."""
    assert login(app, "viewer").put("/api/me/kpi-layouts/mine", json=bad).status_code == 422


def test_a_kpi_layout_name_is_checked_and_the_number_is_bounded(app):
    """A name with a character outside its set or that is too long is 422; the 21st new name is 409 KPI_LAYOUT_LIMIT, while replacing an existing one still works."""
    viewer = login(app, "viewer")
    assert viewer.put("/api/me/kpi-layouts/<script>", json=LAYOUT).json()["title"] == "INVALID_LAYOUT_NAME"
    assert viewer.put(f"/api/me/kpi-layouts/{'x' * 61}", json=LAYOUT).status_code == 422
    for i in range(20):
        assert viewer.put(f"/api/me/kpi-layouts/l{i}", json=LAYOUT).status_code == 200
    full = viewer.put("/api/me/kpi-layouts/one-more", json=LAYOUT)
    assert full.status_code == 409 and full.json()["title"] == "KPI_LAYOUT_LIMIT"
    assert viewer.put("/api/me/kpi-layouts/l0", json=LAYOUT).status_code == 200


def test_a_kpi_layout_is_deleted_needs_the_csrf_token_and_goes_with_the_user(app):
    """DELETE removes a layout (and is 204 again); a change without the CSRF token is 403; deleting the account deletes its layouts."""
    admin = login(app, "admin")
    assert admin.put("/api/me/kpi-layouts/a", json=LAYOUT).status_code == 200
    assert admin.delete("/api/me/kpi-layouts/a").status_code == 204 and admin.delete("/api/me/kpi-layouts/a").status_code == 204
    assert admin.get("/api/me/kpi-layouts").json()["items"] == []
    bare = login(app, "viewer")
    del bare.headers["X-CSRF-Token"]
    assert bare.put("/api/me/kpi-layouts/a", json=LAYOUT).status_code == 403
    assert admin.post("/api/admin/users", json={"username": "kim", "password": "a-long-password", "role": "viewer"}).status_code == 201
    app.state.db.save_kpi_layout("kim", "a", '{"kpis": ["x"]}')
    assert admin.delete("/api/admin/users/kim").status_code == 204
    assert app.state.db.kpi_layouts("kim") == []


# ------------------------------------------------------------------ summary counts

def test_summary_reads_true_totals_not_a_first_page(app, smo):
    """Counts are each module's own `total` for a one-row page with the filter, so 1,234 critical alarms read 1,234, not 100."""
    body = login(app, "viewer").get("/api/summary/alarms").json()
    assert body["counts"]["alarms.critical"] == 1234 and body["counts"]["alarms.total"] == 5000 and body["counts"]["alarms.major"] == 0
    assert body["partial"] == []
    assert all(c.url.params["limit"] == "1" for c in smo.count_calls if not c.url.path.endswith("/stats"))


def test_summary_is_cached_and_shared_between_users(app, smo):
    """A second ask within the cache time, by any user, costs no upstream call."""
    login(app, "viewer").get("/api/summary/nav")
    calls = len(smo.count_calls)
    assert login(app, "operator").get("/api/summary/nav").json()["counts"]["approvals.PENDING"] == 18
    assert len(smo.count_calls) == calls


def test_a_module_that_does_not_answer_makes_the_summary_partial_not_failed(app, smo):
    """A down module's counts are null and the module is named in `partial`; the other counts still come back."""
    smo.down_modules.add("focom")
    body = login(app, "viewer").get("/api/summary/alarms").json()
    assert body["counts"]["ocloudAlarms.total"] is None and body["partial"] == ["focom"] and body["counts"]["alarms.critical"] == 1234


def test_decision_counts_are_bounded_to_the_last_day(app, smo):
    """The 24-hour decision counts send a `since` one day back, so the count never scans the whole decision history."""
    login(app, "viewer").get("/api/summary/decisions")
    sent = [c for c in smo.count_calls if c.url.path == "/ran-nf-oam/decision-records"]
    assert sent and all("since" in c.url.params for c in sent)


def test_an_unknown_summary_page_is_a_404_and_a_session_is_needed(app):
    """Only the pages of `PAGES` exist; the route needs a session like every other."""
    from fastapi.testclient import TestClient
    assert login(app, "viewer").get("/api/summary/nope").status_code == 404
    assert TestClient(app).get("/api/summary/nav").status_code == 401
    assert set(summary.PAGES) >= {"nav", "dashboard", "alarms", "rapps"}


def test_unacknowledged_alarms_and_the_mean_time_to_acknowledge_are_on_the_alarm_and_dashboard_pages(app, smo):
    """GUI-9.8: `alarms.unacked` is the filtered total, `alarms.mtta` the `mttaSeconds` of RAN NF OAM's statistics over 24 h (not a page total)."""
    smo.totals[("/ran-nf-oam/alarms", "ack_state=UNACKNOWLEDGED")] = 321
    viewer = login(app, "viewer")
    for page in ("alarms", "dashboard"):
        counts = viewer.get(f"/api/summary/{page}").json()["counts"]
        assert counts["alarms.unacked"] == 321 and counts["alarms.mtta"] == 42.5
    stats = [c for c in smo.count_calls if c.url.path == "/ran-nf-oam/alarms/stats"]
    assert stats and stats[0].url.params["window_hours"] == "24" and "limit" not in stats[0].url.params


def test_no_acknowledged_alarm_is_a_null_mtta_and_not_a_missing_module(app, smo):
    """A good answer with `mttaSeconds: null` (nothing acknowledged in the window) leaves `partial` empty: the tile shows "—" without blaming a module."""
    smo.stats["/ran-nf-oam/alarms/stats"]["mttaSeconds"] = None
    body = login(app, "viewer").get("/api/summary/alarms").json()
    assert body["counts"]["alarms.mtta"] is None and body["partial"] == []


def test_a_module_without_the_statistics_route_is_named_in_partial(app, smo):
    """An older RAN NF OAM (no `/alarms/stats`, so no `mttaSeconds` in what it answers) makes the value null and the summary partial, not wrong."""
    del smo.stats["/ran-nf-oam/alarms/stats"]
    body = login(app, "viewer").get("/api/summary/alarms").json()
    assert body["counts"]["alarms.mtta"] is None and body["partial"] == ["ran-nf-oam"]


def test_the_number_of_stopped_rapps_is_on_the_rapps_and_dashboard_pages(app, smo):
    """GUI-9.6: `rappsStopped` is the `stopped` count rApp Management's `GET /kill-all` reports."""
    viewer = login(app, "viewer")
    assert viewer.get("/api/summary/rapps").json()["counts"]["rappsStopped"] == 2
    assert viewer.get("/api/summary/dashboard").json()["counts"]["rappsStopped"] == 2


def test_the_model_gates_waiting_are_counted_on_the_nav_approvals_and_aiml_pages(app, smo):
    """GUI-7.3: `modelGates.waiting` is the total of AIMgF's lifecycles with `awaiting_decision=true`, the inbox's model gates and the sidebar's
    Approvals badge with the rApp requests."""
    smo.totals[("/aimgf/model-lifecycles", "awaiting_decision=true")] = 4
    viewer = login(app, "viewer")
    for page in ("nav", "approvals", "aiml"):
        assert viewer.get(f"/api/summary/{page}").json()["counts"]["modelGates.waiting"] == 4
    asked = [c for c in smo.count_calls if c.url.path == "/aimgf/model-lifecycles"]
    assert asked and all(c.url.params.get("awaiting_decision") == "true" for c in asked)


def test_the_change_windows_waiting_are_counted_on_the_nav_and_approvals_pages(app, smo):
    """GUI-7.1: `configJobs.PENDING_APPROVAL` is RAN NF OAM's total of the CM jobs held for a change-window approval, on the inbox and the sidebar's badge."""
    smo.totals[("/ran-nf-oam/config-jobs", "status=PENDING_APPROVAL")] = 3
    viewer = login(app, "viewer")
    for page in ("nav", "approvals", "configuration"):
        assert viewer.get(f"/api/summary/{page}").json()["counts"]["configJobs.PENDING_APPROVAL"] == 3


# ------------------------------------------------------------------ dashboard panels (GUI-9.11)

PANEL_ANSWERS = {
    "/ran-nf-oam/decision-records": {"items": [{"decisionId": "d1"}], "limit": 6, "offset": 0, "hasMore": False},
    "/ran-nf-oam/managed-entities/health": {"groupBy": "region", "groups": [{"key": "eu-west", "elements": 4, "unhealthy": 1, "worstSeverity": "major"}],
                                            "healthScore": 75.0},
    "/ran-nf-oam/managed-entities/worst": [{"managedElementRef": "du-1", "critical": 1, "major": 0, "openAlarms": 1}],
    "/ran-nf-oam/alarms/counts": {"groupBy": "hour", "groups": [{"key": "2026-10-10T08:00:00Z", "count": 2}]},
}


def test_the_dashboard_summary_carries_its_panels_whole(app, smo):
    """The Dashboard's four small lists come back inside its summary, as the modules answered them, so its first load needs no extra calls."""
    smo.stats.update(PANEL_ANSWERS)
    body = login(app, "viewer").get("/api/summary/dashboard").json()
    assert body["panels"]["health"]["healthScore"] == 75.0 and body["panels"]["worst"][0]["managedElementRef"] == "du-1"
    assert body["panels"]["decisions"]["items"][0]["decisionId"] == "d1" and body["panels"]["alarmHours"]["groupBy"] == "hour"
    assert "panels" not in login(app, "viewer").get("/api/summary/alarms").json()


def test_dashboard_panels_follow_the_scope(app, smo):
    """A scoped Dashboard asks every panel with the region and site cluster, like its counts."""
    smo.stats.update(PANEL_ANSWERS)
    login(app, "viewer").get("/api/summary/dashboard", params={"region": "eu-west", "site_cluster": "metro-a"})
    asked = [c for c in smo.count_calls if c.url.path in PANEL_ANSWERS]
    assert {c.url.path for c in asked} == set(PANEL_ANSWERS)
    assert all(c.url.params.get("region") == "eu-west" and c.url.params.get("site_cluster") == "metro-a" for c in asked)


def test_a_panel_whose_module_fails_is_null_and_named_partial(app, smo):
    """A panel the module could not answer is null and its module is listed in `partial`, as for a count."""
    smo.stats.update(PANEL_ANSWERS)
    smo.down_modules.add("ran-nf-oam")
    body = login(app, "viewer").get("/api/summary/dashboard").json()
    assert body["panels"]["health"] is None and "ran-nf-oam" in body["partial"]
