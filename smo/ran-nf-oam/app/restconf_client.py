"""RESTCONF CM write dispatch (OI-1-cm-sync-restconf) — RFC 8040 beside
netconf_client.py's RFC 6241 path, for an ME provisioned with
o1Protocol=RESTCONF (a vendor declaring O1_RESTCONF, Wave 9).

The ME's adaptor_uri is the RESTCONF root (RFC 8040 section 3.1, e.g.
http://mock-o1-adaptor:8000/restconf). A managed object is the data
resource

    {root}/data/managed-element={ref}[/managed-function={functionRef}]

with each key value percent-encoded (section 3.5.3: `NRCellDU=101` carries
a reserved `=`). Bodies are `application/yang-data+json` (RFC 7951): a
list entry is a one-element array keyed by `ref` / `function-ref`.

The edit-config `operation` maps onto RESTCONF methods as RFC 8040
section 4 defines them:
  merge   -> PATCH  (plain patch, merge semantics, section 4.6.1)
  replace -> PUT    (create or replace the target, section 4.5)
  create  -> POST   on the parent (section 4.4.1; 409 data-exists if present)
  delete  -> DELETE (an absent target is an error, data-missing)
  remove  -> DELETE (an absent target is fine, RFC 6241's remove)
and read-after-write is a GET of the same resource.

Each request goes to the ME's adaptor_uri as given: plain HTTP for an
`http://` root, HTTPS with httpx's default server-certificate check for an
`https://` one. No client credentials are sent (no HTTP authentication, no
client certificate), so a RESTCONF server that requires them is not
reachable from here; NETCONF over SSH or TLS is netconf_ssh.py. As in
netconf_client.py, one exchange is bounded at 30 s and the result is an
EditResult whose reason says whether a failure is worth retrying. A RESTCONF error reply
(`ietf-restconf:errors`) is a definite answer, never retried, whatever its
status code; a timeout, 502/503, or a 5xx without an errors body is
transient.
"""

from urllib.parse import quote

import httpx

from .netconf_client import NETCONF_TIMEOUT_SECONDS, EditResult

YANG_JSON = "application/yang-data+json"
RESTCONF_TIMEOUT_SECONDS = NETCONF_TIMEOUT_SECONDS
ERRORS_KEY = "ietf-restconf:errors"


class RestconfResult(EditResult):
    """The outcome of one RESTCONF change. Falsy when it failed, like EditResult; `error_tag` carries the first `error-tag` of an `ietf-restconf:errors` body when there was one. Only a timeout or an unreachable server is retryable.
    """
    RETRYABLE = {"RESTCONF_TIMEOUT", "RESTCONF_UNREACHABLE"}

    def __init__(self, applied: bool, reason: str | None = None, error_tag: str | None = None):
        super().__init__(applied, reason)
        self.error_tag = error_tag


def _key(value: str) -> str:
    return quote(value, safe="")


def _list_name(managed_function_ref: str | None) -> tuple[str, str]:
    return ("managed-function", "function-ref") if managed_function_ref else ("managed-element", "ref")


def resource_url(root: str, target_ref: str, managed_function_ref: str | None = None) -> str:
    """The RFC 8040 data-resource URL of a managed element, or of one of its managed functions, with each key percent-encoded (a ref such as `NRCellDU=101` contains a reserved '=').
    """
    url = f"{root.rstrip('/')}/data/managed-element={_key(target_ref)}"
    if managed_function_ref:
        url += f"/managed-function={_key(managed_function_ref)}"
    return url


def _parent_url(root: str, target_ref: str, managed_function_ref: str | None) -> str:
    if managed_function_ref:
        return resource_url(root, target_ref)
    return f"{root.rstrip('/')}/data"


def build_body(target_ref: str, attribute_changes: dict, managed_function_ref: str | None = None) -> dict:
    """The RFC 7951 representation of one managed object: its list entry,
    key leaf first. Values are sent as strings, as the NETCONF path sends
    element text."""
    name, key = _list_name(managed_function_ref)
    entry = {key: managed_function_ref or target_ref}
    entry.update({attr: str(value) for attr, value in attribute_changes.items()})
    return {name: [entry]}


def _error_tag(resp: httpx.Response) -> str | None:
    """The `error-tag` of the first error in a RESTCONF errors body, or None when the body is not JSON or carries none."""
    try:
        errors = resp.json().get(ERRORS_KEY, {}).get("error", [])
    except (ValueError, AttributeError):
        return None
    return errors[0].get("error-tag") if errors and isinstance(errors[0], dict) else None


def _send(method: str, url: str, body: dict | None = None) -> tuple[httpx.Response | None, RestconfResult | None]:
    # The per-verb httpx functions, as every other caller in this build uses
    # (and the integration mesh intercepts); GET/DELETE take no body.
    """One HTTP exchange: (response, None) on a 2xx answer, otherwise (None, failure). Timeouts and 408/504 are RESTCONF_TIMEOUT; connection errors, 502/503, and a 5xx without an errors body are RESTCONF_UNREACHABLE (transient); any other failure, including a 5xx that carries an errors body, is RESTCONF_REQUEST_FAILED and is never retried.
    """
    send = getattr(httpx, method.lower())
    kwargs = {"json": body} if body is not None else {}
    try:
        resp = send(url, timeout=RESTCONF_TIMEOUT_SECONDS, headers={"Content-Type": YANG_JSON, "Accept": YANG_JSON},
                    **kwargs)
    except httpx.TimeoutException:
        return None, RestconfResult(False, "RESTCONF_TIMEOUT")
    except httpx.HTTPError:
        return None, RestconfResult(False, "RESTCONF_UNREACHABLE")
    if resp.status_code < 300:
        return resp, None
    tag = _error_tag(resp)
    if resp.status_code in (408, 504):
        return None, RestconfResult(False, "RESTCONF_TIMEOUT", tag)
    if (tag is None and resp.status_code >= 500) or resp.status_code in (502, 503):
        return None, RestconfResult(False, "RESTCONF_UNREACHABLE", tag)
    return None, RestconfResult(False, "RESTCONF_REQUEST_FAILED", tag)


def send_edit(root: str, target_ref: str, attribute_changes: dict, message_id: str, operation: str = "merge",
              managed_function_ref: str | None = None) -> RestconfResult:
    """Applies one change; truthy when the server answered 2xx. `message_id`
    is unused on the wire (RESTCONF has no RPC message-id) and kept for the
    same call shape as netconf_client.send_edit_config."""
    url = resource_url(root, target_ref, managed_function_ref)
    body = build_body(target_ref, attribute_changes, managed_function_ref)
    if operation == "merge":
        _, failure = _send("PATCH", url, body)
    elif operation == "replace":
        _, failure = _send("PUT", url, body)
    elif operation == "create":
        _, failure = _send("POST", _parent_url(root, target_ref, managed_function_ref), body)
    elif operation in ("delete", "remove"):
        _, failure = _send("DELETE", url)
        if failure is not None and operation == "remove" and failure.error_tag == "data-missing":
            failure = None
    else:
        return RestconfResult(False, "RESTCONF_REQUEST_FAILED", "invalid-value")
    # `failure or ...` would be wrong: a failed result is falsy by design
    return failure if failure is not None else RestconfResult(True)


def send_get(root: str, target_ref: str, message_id: str, managed_function_ref: str | None = None) -> dict | None:
    """Read-after-write: the managed object's attributes (key leaf
    excluded), or None if the read failed."""
    resp, _ = _send("GET", resource_url(root, target_ref, managed_function_ref))
    if resp is None:
        return None
    name, key = _list_name(managed_function_ref)
    try:
        entries = resp.json().get(name)
    except (ValueError, AttributeError):
        return None
    if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
        return None
    return {attr: value for attr, value in entries[0].items() if attr != key}
