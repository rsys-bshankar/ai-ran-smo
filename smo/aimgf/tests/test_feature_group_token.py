"""SEC-15.2: a feature group's data-lake token is never returned, and is given by reference (`tokenRef`).

Fixtures and helpers come from `test_main.py` (`client`, `mlmr`, `db_session_factory`, `_feature_group_body`), imported so pytest treats them as its own. SQLite, no network.
Run with `cd smo/aimgf && PYTHONPATH=.:../shared python -m pytest tests/test_feature_group_token.py -q`.
"""

import pytest

from test_main import _feature_group_body, client, db_session_factory, mlmr  # noqa: F401  (pytest fixtures)

SECRET = "s3cr3t-lake-token-VALUE"


def _ref_body(**extra):
    """A valid body that names its token by reference instead of carrying it."""
    body = _feature_group_body(**extra)
    del body["token"]
    return {**body, "tokenRef": body.get("tokenRef", "lake-token")}


def test_create_with_a_reference_shows_token_set_and_the_name(client):
    """A group registered with `tokenRef` is 201 with `tokenSet: true` and the reference name, no `token` key and no deprecation notice."""
    resp = client.post("/feature-groups", json=_ref_body())
    assert resp.status_code == 201
    body = resp.json()
    assert body["tokenSet"] is True and body["tokenRef"] == "lake-token"
    assert "token" not in body and "warnings" not in body
    assert "Deprecation" not in resp.headers


def test_a_clear_text_token_is_stored_but_never_returned(client, db_session_factory):
    """The deprecated `token` field still registers a group, but no route answers it: not the create, not the get, not the list (the leak this finding was about)."""
    created = client.post("/feature-groups", json=_feature_group_body(token=SECRET))
    assert created.status_code == 201
    fetched = client.get("/feature-groups/cellCounters")
    listed = client.get("/feature-groups")
    for resp in (created, fetched, listed):
        assert SECRET not in resp.text
        assert "token" not in (resp.json() if resp is not listed else resp.json()["items"][0])
    assert fetched.json()["tokenSet"] is True and fetched.json()["tokenRef"] is None
    from app.models import FeatureGroup
    with db_session_factory() as db:
        assert db.query(FeatureGroup).one().token == SECRET  # stored as before, so the migration is expand only


def test_the_clear_text_token_answers_a_deprecation_warning(client):
    """A call that sends `token` gets the `Deprecation: true` header and a `warnings` entry, as docs/RELEASES.md asks of a deprecated input."""
    resp = client.post("/feature-groups", json=_feature_group_body())
    assert resp.headers["Deprecation"] == "true"
    assert "tokenRef" in resp.json()["warnings"][0]


def test_the_token_field_is_deprecated_in_the_openapi_document(client):
    """The OpenAPI schema marks `token` of the request as deprecated and no longer requires it."""
    schema = client.get("/openapi.json").json()["components"]["schemas"]["CreateFeatureGroupRequest"]
    assert schema["properties"]["token"]["deprecated"] is True
    assert "token" not in schema["required"] and "tokenRef" in schema["properties"]


def test_both_or_neither_credential_is_422_and_never_echoes_the_secret(client):
    """Sending both `token` and `tokenRef`, or neither, is 422 `SCHEMA_VALIDATION_FAILED`, and the error does not repeat the pasted token."""
    both = client.post("/feature-groups", json=_feature_group_body(token=SECRET, tokenRef="lake-token"))
    neither = client.post("/feature-groups", json={k: v for k, v in _feature_group_body().items() if k != "token"})
    for resp in (both, neither):
        assert resp.status_code == 422
        assert SECRET not in resp.text
    assert client.get("/feature-groups").json()["total"] == 0


@pytest.mark.parametrize("ref", [SECRET, "Upper", "1starts-with-digit", "has space", "a" * 64, "../etc/passwd", ""])
def test_a_reference_that_is_not_a_secret_name_is_refused_without_echo(client, ref):
    """A `tokenRef` that is not a lower-case name (a pasted secret, a path, too long) is 422 and the value is not repeated in the answer."""
    resp = client.post("/feature-groups", json=_ref_body(tokenRef=ref))
    assert resp.status_code == 422
    assert ref not in resp.text or ref == ""
    assert client.get("/feature-groups").json()["total"] == 0


def test_the_token_is_resolved_from_the_environment_or_a_file(client, db_session_factory, tmp_path, monkeypatch):
    """`feature_group_token` resolves a reference from `AIMGF_FEATURE_GROUP_TOKEN_<REF>` or its `_FILE`, falls back to nothing for an unset one, and returns a legacy group's stored token."""
    from app.main import feature_group_token
    from app.models import FeatureGroup
    client.post("/feature-groups", json=_ref_body())
    client.post("/feature-groups", json=_feature_group_body("legacyGroup", token=SECRET))
    with db_session_factory() as db:
        by_ref = db.query(FeatureGroup).filter_by(feature_group_name="cellCounters").one()
        legacy = db.query(FeatureGroup).filter_by(feature_group_name="legacyGroup").one()
        assert feature_group_token(by_ref) is None  # nothing configured: no fall-back to another secret
        monkeypatch.setenv("AIMGF_FEATURE_GROUP_TOKEN_LAKE_TOKEN", "from-env")
        assert feature_group_token(by_ref) == "from-env"
        monkeypatch.delenv("AIMGF_FEATURE_GROUP_TOKEN_LAKE_TOKEN")
        secret_file = tmp_path / "lake"
        secret_file.write_text("from-file\n")
        monkeypatch.setenv("AIMGF_FEATURE_GROUP_TOKEN_LAKE_TOKEN_FILE", str(secret_file))
        assert feature_group_token(by_ref) == "from-file"
        assert feature_group_token(legacy) == SECRET
