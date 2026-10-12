"""smo_shared.errors' validation-error handler: a 422 for a refused request never echoes the caller's input (HISTORY.md QA-9.1, the DAST false positive
`SQL Injection [40018]` on `total=true AND 1=1 --`).

Run with: cd smo/shared && PYTHONPATH=. python -m pytest tests/test_validation_error_handler.py -q
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from smo_shared.errors import install_validation_error_handler, scrub_validation_errors
from smo_shared.openapi_security import apply_r1_gateway_security


def validation_client():
    """Helper: a client for an app with the validation handler and a route taking a bool query parameter and one taking an int body field."""

    class Body(BaseModel):
        count: int

    app = FastAPI()
    install_validation_error_handler(app)

    @app.get("/flag")
    def flag(total: bool = True):
        """A route with one bool query parameter."""
        return {"total": total}

    @app.post("/body")
    def body(item: Body):
        """A route with one int body field."""
        return {"count": item.count}

    return TestClient(app)


def test_a_refused_parameter_answers_422_without_echoing_the_input():
    """A bool parameter given `true AND 1=1 --` is 422 with type, loc and msg only: the caller's text is nowhere in the body."""
    response = validation_client().get("/flag", params={"total": "true AND 1=1 -- "})
    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert set(error) == {"type", "loc", "msg"}
    assert error["type"] == "bool_parsing" and error["loc"] == ["query", "total"]
    assert "1=1" not in response.text


def test_two_refused_payloads_that_differ_only_in_the_echo_answer_identical_bytes():
    """The ZAP 1=1 and 1=2 payloads get byte-identical 422 bodies, so no boolean-based injection can be read into the echo."""
    client = validation_client()
    true_body = client.get("/flag", params={"total": "true AND 1=1 -- "}).content
    false_body = client.get("/flag", params={"total": "true AND 1=2 -- "}).content
    assert true_body == false_body


def test_a_body_error_loses_input_but_keeps_what_was_wrong():
    """A malformed JSON body field: the input value is dropped; type, loc and msg stay."""
    response = validation_client().post("/body", json={"count": "secret-marker"})
    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert set(error) == {"type", "loc", "msg"} and error["loc"] == ["body", "count"]
    assert "secret-marker" not in response.text


def test_scrub_validation_errors_removes_exactly_input_ctx_and_url():
    """The scrub drops the three reflecting fields and keeps every other one, without changing its argument."""
    original = [{"type": "t", "loc": ["q"], "msg": "m", "input": "x", "ctx": {"error": "x"}, "url": "u", "extra": 1}]
    assert scrub_validation_errors(original) == [{"type": "t", "loc": ["q"], "msg": "m", "extra": 1}]
    assert "input" in original[0]


def test_apply_r1_gateway_security_installs_the_validation_handler():
    """Every module calls apply_r1_gateway_security, so installing there is what gives every module the scrubbed 422."""
    app = FastAPI()

    @app.get("/flag")
    def flag(total: bool = True):
        """A route with one bool query parameter."""
        return {"total": total}

    apply_r1_gateway_security(app)
    body = TestClient(app).get("/flag", params={"total": "nope"}).json()
    assert "input" not in body["detail"][0]
