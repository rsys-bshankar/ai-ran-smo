"""`SEC-15.6b`: an rApp that names an MSAC Identity with a credential can send it, in the `X-MSAC-Credential` header of `execute_action` and `mediate_action`.

Run with `cd sdk && PYTHONPATH=.:../shared python -m pytest tests/test_msac_credential.py -q`. Uses the recording `r1` fake of `conftest.py` and the `Scripted` client of
`test_retry_on_conflict.py` (for the header after the retry wrapper); no network. The value is a made-up string.
"""

import logging
import pathlib

import pytest
from conftest import FakeResponse
from test_retry_on_conflict import Scripted

from smo_sdk._common import MSAC_CREDENTIAL_HEADER, SdkError, _RetryOnConflict
from smo_sdk.data import DataClient
from smo_sdk.platform import PlatformClient

SECRET = "s3cret-identity-credential-0042"
CHANGES = [{"managedElementRef": "me-1", "attributeChanges": {"x": 1}}]


def _both(r1, **kwargs):
    """The two methods that POST an action, called the same way."""
    PlatformClient(r1).execute_action("es-rapp", CHANGES, **kwargs)
    DataClient(r1).mediate_action("es-rapp", CHANGES, **kwargs)


def test_the_header_name_is_the_one_ran_nf_oam_reads():
    """The SDK's constant and RAN NF OAM's `CREDENTIAL_HEADER` are the same string (the module is read as text: RAN NF OAM's app is not importable from the SDK's tests)."""
    source = (pathlib.Path(__file__).resolve().parents[2] / "ran-nf-oam" / "app" / "msac.py").read_text()
    assert f'CREDENTIAL_HEADER = "{MSAC_CREDENTIAL_HEADER}"' in source


def test_the_credential_is_sent_in_the_header_of_both_action_methods(r1):
    """With `msac_credential` the call carries `X-MSAC-Credential` and the value is not in the body."""
    _both(r1, msac_credential=SECRET)
    assert len(r1.calls) == 2
    for call in r1.calls:
        assert call["path"] == "/dme/actions"
        assert call["headers"] == {"X-MSAC-Credential": SECRET}
        assert SECRET not in repr(call["json"])


def test_no_header_is_sent_without_a_credential(r1):
    """Left out (or None), the call has no headers at all, as before."""
    _both(r1)
    _both(r1, msac_credential=None)
    assert len(r1.calls) == 4
    assert all("headers" not in call for call in r1.calls)


def test_the_credential_is_keyword_only(r1):
    """It cannot be passed by position, so it can never be mistaken for `decision` or another argument."""
    with pytest.raises(TypeError):
        PlatformClient(r1).execute_action("es-rapp", CHANGES, None, None, "single-ME", None, None, SECRET)
    with pytest.raises(TypeError):
        DataClient(r1).mediate_action("es-rapp", CHANGES, "single-ME", None, None, None, SECRET)


def test_the_header_survives_the_retry_wrapper_with_the_idempotency_key():
    """The wrapper adds `Idempotency-Key` next to the credential and the repeat after a 409 sends both again."""
    conflict = FakeResponse(409, {"detail": {"title": "CONCURRENT_MODIFICATION", "status": 409, "detail": "repeat"}})
    fake = Scripted(conflict, FakeResponse(202, {"actionId": "a"}))
    PlatformClient(_RetryOnConflict(fake)).execute_action("es-rapp", CHANGES, msac_credential=SECRET)
    assert len(fake.headers) == 2
    for headers in fake.headers:
        assert headers[MSAC_CREDENTIAL_HEADER] == SECRET and headers["Idempotency-Key"]
    assert fake.headers[0] == fake.headers[1]


def test_the_credential_is_not_in_a_repr_a_log_or_an_error(r1, caplog):
    """A 403 `MSAC_ACCESS_DENIED` becomes an `SdkError` that does not carry the value; the clients' `repr`, the return value and the log do not either."""
    r1.script(403, {"detail": {"title": "MSAC_ACCESS_DENIED", "status": 403}})
    caplog.set_level(logging.DEBUG)
    platform = PlatformClient(r1)
    with pytest.raises(SdkError) as raised:
        platform.execute_action("es-rapp", CHANGES, msac_credential=SECRET)
    assert SECRET not in str(raised.value) and SECRET not in repr(raised.value) and SECRET not in repr(raised.value.body)
    assert SECRET not in repr(platform) and SECRET not in repr(DataClient(r1))
    r1.script(202, {"actionId": "a", "status": "FORWARDED"})
    assert SECRET not in repr(platform.execute_action("es-rapp", CHANGES, msac_credential=SECRET))
    assert SECRET not in caplog.text


def test_the_sdk_reads_no_environment_variable_for_it(r1, monkeypatch):
    """No environment fallback: a variable of any plausible name is ignored."""
    for name in ("MSAC_CREDENTIAL", "X_MSAC_CREDENTIAL", "SMO_MSAC_CREDENTIAL", "SDK_MSAC_CREDENTIAL"):
        monkeypatch.setenv(name, SECRET)
    _both(r1)
    assert all("headers" not in call for call in r1.calls)
