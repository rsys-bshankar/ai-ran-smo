"""The AI Runtime SDK: the package an rApp author imports (`from smo_sdk import AiRuntimeSdk`).

`AiRuntimeSdk` is six namespace clients (`data`, `analytics`, `models`, `lifecycle`, `intent`, `platform`) over one shared `smo_shared.r1_client.R1Client`, so an rApp calls
`sdk.models.register_model(...)` instead of hand-building an HTTP request against `/aimgf/models`. Golden rule 6 of `docs/ARCHITECTURE.md`: the SDK is a thin client over the same R1
path every other cross-module call uses, not a second one. It adds no business logic and no client-side validation; the platform's answer, when it is an error, is raised as `SdkError`.

Nothing in the SMO modules imports it (they call `R1Client` directly); the sample rApps under `samples/` and rApp authors do. `operator_ui.py` is authoring support and is imported
by name (`from smo_sdk import operator_ui`), not through `AiRuntimeSdk`. Per-method behaviour and design decisions are in `sdk/README.md`; update it with a change here.
"""

from smo_shared.r1_client import R1Client

from ._common import SdkError
from ._retry import RetryPolicy
from .analytics import AnalyticsClient
from .data import DataClient
from .intent import IntentClient
from .lifecycle import LifecycleClient
from .models import ModelsClient
from .platform import PlatformClient

__all__ = ["AiRuntimeSdk", "SdkError", "RetryPolicy", "DataClient", "AnalyticsClient", "ModelsClient", "LifecycleClient", "IntentClient", "PlatformClient"]


class AiRuntimeSdk:
    """The six namespaces from the layered architecture diagram, one client each, sharing one `R1Client` and so one cached SME token (see `smo_shared/r1_client.py` on per-process token caching)."""

    def __init__(self, r1: R1Client | None = None, retry: RetryPolicy | None = None):
        """Build the six clients over `r1`, or over a new `R1Client()` (which onboards its SME invoker on first use) when none is given; tests pass a fake.

        `retry` is the retry policy of every call (SDK-PY-RETRY); None reads it once from the environment (`RetryPolicy.from_env()`:
        `SMO_SDK_RETRY`, `SMO_SDK_RETRY_MAX_ATTEMPTS`, `SMO_SDK_RETRY_BASE_DELAY_MS`, `SMO_SDK_RETRY_MAX_DELAY_MS`), which raises `ValueError`
        for a value it cannot use. `RetryPolicy.off()` sends every call once.
        """
        r1 = r1 or R1Client()
        retry = retry if retry is not None else RetryPolicy.from_env()
        self.data = DataClient(r1, retry)
        self.analytics = AnalyticsClient(r1, retry)
        self.models = ModelsClient(r1, retry)
        self.lifecycle = LifecycleClient(r1, retry)
        self.intent = IntentClient(r1, retry)
        self.platform = PlatformClient(r1, retry)
