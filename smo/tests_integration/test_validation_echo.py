"""A 422 for a refused query parameter does not echo the caller's input (HISTORY.md QA-9.1, the DAST false positive `SQL Injection [40018]`).

Run with: cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_validation_echo.py -q
"""


def test_ran_nf_oam_managed_entities_total_payloads_get_identical_422_bodies(mesh):
    """The exact ZAP payloads `total=true AND 1=1 --` and `... AND 1=2 --` on GET /managed-entities answer byte-identical 422 bodies with no `input`."""
    true_resp = mesh["ran-nf-oam"].get("/managed-entities", params={"total": "true AND 1=1 -- "})
    false_resp = mesh["ran-nf-oam"].get("/managed-entities", params={"total": "true AND 1=2 -- "})
    assert true_resp.status_code == false_resp.status_code == 422
    assert true_resp.content == false_resp.content
    assert "AND 1=" not in true_resp.text
    assert all(set(error) == {"type", "loc", "msg"} for error in true_resp.json()["detail"])
